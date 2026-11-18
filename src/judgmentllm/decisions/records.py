"""Strict source scheduling, named questions and nonsecret commitments."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from judgmentllm.decisions._base import DecisionsModel
from judgmentllm.io import _revalidate_record  # pyright: ignore[reportPrivateUsage]
from judgmentllm.models import ID, PairTask, PairwiseCriterion, ResolvedLevel, Trait
from judgmentllm.persistence.models import SHA256
from judgmentllm.persistence.sources import PairStructure
from judgmentllm.planning import stable_id


class PresentationMemberV1(DecisionsModel):
    pair_uid: ID
    trait_id: ID


class PresentationScheduleV1(DecisionsModel):
    repetitions: int = Field(default=1, gt=0)
    include_reversals: bool = False
    seed: int | None = None


class PairPresentationInputV1(DecisionsModel):
    """One unscheduled source occurrence; member-level schedules are forbidden."""

    pair_presentation_version: Literal[1] = 1
    source_presentation_id: ID
    pair_id: ID
    left_item_id: ID
    right_item_id: ID
    members: tuple[PresentationMemberV1, ...]
    schedule: PresentationScheduleV1 = Field(default_factory=PresentationScheduleV1)

    @field_validator("pair_presentation_version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("pair_presentation_version must be integer 1")
        return value

    @model_validator(mode="after")
    def unique(self) -> PairPresentationInputV1:
        if self.left_item_id == self.right_item_id:
            raise ValueError("presentation needs distinct original items")
        if (
            not self.members
            or len({m.pair_uid for m in self.members}) != len(self.members)
            or len({m.trait_id for m in self.members}) != len(self.members)
        ):
            raise ValueError(
                "presentation needs unique ordered source members and criteria"
            )
        return self


class PreparedPairPresentationV1(DecisionsModel):
    """One common orientation projected into distinct criterion occurrences."""

    presentation_membership_version: Literal[1] = 1
    source_presentation_id: ID
    presentation_id: ID
    schedule: PresentationScheduleV1
    members: tuple[PairStructure, ...]

    @model_validator(mode="after")
    def common_presentation(self) -> PreparedPairPresentationV1:
        if not self.members:
            raise ValueError("presentation membership is empty")
        first = self.members[0]
        original = [first.left_item_id, first.right_item_id]
        orientation = stable_id(
            "presentation-orientation-v1",
            self.schedule.seed,
            self.source_presentation_id,
            first.pair_id,
            first.repetition_index,
        )
        base = (
            original
            if self.schedule.seed is None or int(orientation[-1], 16) % 2 == 0
            else original[::-1]
        )
        expected_order = base[::-1] if first.is_reversal else base
        if (
            first.repetition_index >= self.schedule.repetitions
            or (first.is_reversal and not self.schedule.include_reversals)
            or first.presented_order != expected_order
            or self.presentation_id
            != stable_id(
                "presentation-v1",
                self.source_presentation_id,
                first.pair_id,
                *original,
                first.repetition_index,
                first.is_reversal,
            )
        ):
            raise ValueError("prepared presentation differs from its source schedule")
        if len({p.trait_id for p in self.members}) != len(self.members) or len(
            {p.source_pair_uid for p in self.members}
        ) != len(self.members):
            raise ValueError(
                "prepared presentation repeats a source member or criterion"
            )
        for member in self.members:
            if (
                member.source_pair_uid is None
                or member.pair_uid
                != stable_id(
                    "pair",
                    member.source_pair_uid,
                    member.pair_id,
                    member.left_item_id,
                    member.right_item_id,
                    member.trait_id,
                    member.repetition_index,
                    member.is_reversal,
                )
                or member.reversal_group
                != stable_id(
                    "presentation-reversal-v1",
                    self.source_presentation_id,
                    member.pair_id,
                    member.repetition_index,
                )
            ):
                raise ValueError("prepared member differs from its source occurrence")
        if any(
            (
                p.pair_id,
                p.left_item_id,
                p.right_item_id,
                p.presented_order,
                p.repetition_index,
                p.is_reversal,
                p.reversal_group,
            )
            != (
                first.pair_id,
                first.left_item_id,
                first.right_item_id,
                first.presented_order,
                first.repetition_index,
                first.is_reversal,
                first.reversal_group,
            )
            for p in self.members
        ):
            raise ValueError(
                "presentation members have conflicting scheduling evidence"
            )
        return self


def project_presentation(
    source: PairPresentationInputV1,
) -> Iterator[PreparedPairPresentationV1]:
    """Schedule once, before criterion projection; never call prepare_pairs."""
    source = _revalidate_record(source)
    for repetition in range(source.schedule.repetitions):
        original = [source.left_item_id, source.right_item_id]
        orientation = stable_id(
            "presentation-orientation-v1",
            source.schedule.seed,
            source.source_presentation_id,
            source.pair_id,
            repetition,
        )
        order = (
            original
            if source.schedule.seed is None or int(orientation[-1], 16) % 2 == 0
            else original[::-1]
        )
        for reversal in (
            (False, True) if source.schedule.include_reversals else (False,)
        ):
            yield PreparedPairPresentationV1(
                source_presentation_id=source.source_presentation_id,
                presentation_id=stable_id(
                    "presentation-v1",
                    source.source_presentation_id,
                    source.pair_id,
                    *original,
                    repetition,
                    reversal,
                ),
                schedule=source.schedule,
                members=tuple(
                    PairStructure(
                        pair_id=source.pair_id,
                        pair_uid=stable_id(
                            "pair",
                            m.pair_uid,
                            source.pair_id,
                            *original,
                            m.trait_id,
                            repetition,
                            reversal,
                        ),
                        left_item_id=source.left_item_id,
                        right_item_id=source.right_item_id,
                        trait_id=m.trait_id,
                        original_order=original,
                        presented_order=order[::-1] if reversal else order,
                        source_pair_uid=m.pair_uid,
                        repetition_index=repetition,
                        is_reversal=reversal,
                        reversal_group=stable_id(
                            "presentation-reversal-v1",
                            source.source_presentation_id,
                            source.pair_id,
                            repetition,
                        ),
                    )
                    for m in source.members
                ),
            )


def prepare_pair_presentations(
    sources: Iterable[PairPresentationInputV1],
) -> Iterator[PreparedPairPresentationV1]:
    """Stream one schedule at a time; a temporary unique index bounds memory."""
    with tempfile.TemporaryDirectory(prefix="judgmentllm-presentations-") as temporary:
        with closing(
            sqlite3.connect(Path(temporary) / "identities.sqlite3")
        ) as connection:
            connection.execute(
                "CREATE TABLE identities(kind TEXT, id TEXT, PRIMARY KEY(kind,id))"
            )
            for source in sources:
                source = _revalidate_record(source)
                try:
                    connection.execute(
                        "INSERT INTO identities VALUES('source',?)",
                        (source.source_presentation_id,),
                    )
                    connection.executemany(
                        "INSERT INTO identities VALUES('member',?)",
                        ((m.pair_uid,) for m in source.members),
                    )
                except sqlite3.IntegrityError:
                    raise ValueError(
                        "duplicate presentation source or source pair UID"
                    ) from None
                yield from project_presentation(source)


def question_key(task_id: str) -> str:
    return "q_" + hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:60]


class DecisionQuestionV1(DecisionsModel):
    """Complete in-memory semantics, never persisted as safe minimal metadata."""

    task_id: ID
    key: str = Field(pattern=r"^q_[0-9a-f]{60}$")
    trait_id: ID
    kind: Literal["score", "choice"]
    instructions: str = Field(repr=False)
    trait: Trait | None = Field(default=None, repr=False)
    criterion: PairwiseCriterion | None = Field(default=None, repr=False)
    pair: PairTask | None = None
    exemplar_ids: tuple[ID, ...] = ()

    @model_validator(mode="after")
    def semantic_kind(self) -> DecisionQuestionV1:
        if self.key != question_key(self.task_id):
            raise ValueError("question key differs from task identity")
        if (self.kind == "score") != (self.trait is not None) or (
            self.kind == "choice"
        ) != (self.pair is not None and self.criterion is not None):
            raise ValueError("question kind differs from its assessment")
        if self.kind == "score" and (
            self.criterion is not None
            or self.trait is None
            or self.trait.trait_id != self.trait_id
        ):
            raise ValueError("score question carries conflicting criterion identity")
        if self.kind == "choice" and (
            self.criterion is None
            or self.pair is None
            or self.criterion.trait_id != self.trait_id
            or self.pair.trait_id != self.trait_id
            or self.task_id
            != stable_id("task", "pairwise", self.pair.pair_uid, self.trait_id)
        ):
            raise ValueError("choice question carries conflicting source identity")
        return self


class QuestionCommitmentV1(DecisionsModel):
    task_id: ID
    trait_id: ID
    key: str = Field(pattern=r"^q_[0-9a-f]{60}$")
    ordinal: int = Field(ge=0)
    kind: Literal["score", "choice"]
    question_sha256: SHA256
    question_byte_length: int = Field(ge=0)
    semantics_sha256: SHA256
    exemplar_ids: tuple[ID, ...]
    scale_mapping: tuple[ResolvedLevel, ...] = ()
    pair: PairStructure | None = None

    @model_validator(mode="after")
    def exact_question(self) -> QuestionCommitmentV1:
        if (
            self.key != question_key(self.task_id)
            or (self.kind == "choice") != (self.pair is not None)
            or (self.kind == "score") != bool(self.scale_mapping)
        ):
            raise ValueError("question commitment mapping or identity differs")
        return self


class DecisionsLimitsV1(DecisionsModel):
    """Exact-scope synthetic limits; None never means an infinite vendor limit."""

    limit_version: Literal[1] = 1
    max_questions: int | None = Field(default=None, gt=0)
    max_body_bytes: int | None = Field(default=None, gt=0)
    max_context_tokens: int | None = Field(default=None, gt=0)
    counter: Literal["synthetic_utf8_v1"] | None = None
    source: Literal["synthetic.offline.fixture"] = "synthetic.offline.fixture"
    checked_on: Literal["2026-11-15"] = "2026-11-15"
    local_body_bytes: int = Field(default=8_388_608, gt=0)


class DecisionsLimitsV2(DecisionsLimitsV1):
    """Documented Decisions bounds; unknown ceilings are deliberately absent."""

    limit_version: Literal[2] = 2  # pyright: ignore[reportIncompatibleVariableOverride]
    max_questions: Literal[400] = 400  # pyright: ignore[reportIncompatibleVariableOverride]
    max_body_bytes: None = None  # pyright: ignore[reportIncompatibleVariableOverride]
    max_context_tokens: None = None  # pyright: ignore[reportIncompatibleVariableOverride]
    counter: None = None  # pyright: ignore[reportIncompatibleVariableOverride]
    local_body_bytes: int = Field(default=8_388_608, gt=0, le=8_388_608)
    source: Literal["openai.decisions.documentation"] = "openai.decisions.documentation"  # pyright: ignore[reportIncompatibleVariableOverride]
    checked_on: Literal["2026-11-17"] = "2026-11-17"  # pyright: ignore[reportIncompatibleVariableOverride]


class OpenAIDecisionsContextV1(DecisionsModel):
    protocol: Literal["openai-decisions-production-v1"] = (
        "openai-decisions-production-v1"
    )
    provider: Literal["openai"] = "openai"
    endpoint: Literal["decisions"] = "decisions"
    service_identity: Literal["openai.decisions.v1"] = "openai.decisions.v1"
    sdk_version: Literal["3.42.1"] = "3.42.1"
    api_version: Literal["v1"] = "v1"
    credential_source: Literal["OPENAI_API_KEY"] = "OPENAI_API_KEY"
    account_scope: ID | None = None
    limits: DecisionsLimitsV2 = Field(default_factory=DecisionsLimitsV2)


TYPESAFE_MODEL = "jev-1.14.0"
TYPESAFE_ROUTE = ("typesafe", "systemone")
TYPESAFE_ORIGIN = "https://api.typesafe.ai"
TYPESAFE_PROTOCOL = "typesafe-systemone-production-v1"
TYPESAFE_SDK = "0.7.5"
TYPESAFE_CONTEXT_WARNING = (
    "TypeSafe Jev context fit is unverified for both published limits. "
    "TypeSafe may reject the request after forwarding."
)


class TypeSafeDecisionsLimitsV3(DecisionsLimitsV1):
    """Documented context ceilings and the approved non-strict admission policy."""

    limit_version: Literal[3] = 3  # pyright: ignore[reportIncompatibleVariableOverride]
    max_questions: None = None  # pyright: ignore[reportIncompatibleVariableOverride]
    max_body_bytes: Literal[33_554_432] = 33_554_432  # pyright: ignore[reportIncompatibleVariableOverride]
    max_context_tokens: None = None  # pyright: ignore[reportIncompatibleVariableOverride]
    counter: None = None  # pyright: ignore[reportIncompatibleVariableOverride]
    source: Literal["https://docs.typesafe.ai/models#context-length"] = (  # pyright: ignore[reportIncompatibleVariableOverride]
        "https://docs.typesafe.ai/models#context-length"
    )
    checked_on: Literal["2026-11-17"] = "2026-11-17"  # pyright: ignore[reportIncompatibleVariableOverride]
    body_source: Literal["https://docs.typesafe.ai/api/errors"] = (
        "https://docs.typesafe.ai/api/errors"
    )
    local_body_bytes: int = Field(default=8_388_608, gt=0, le=8_388_608)
    request_context_limit: Literal["64k tokens"] = "64k tokens"
    question_context_limit: Literal["32k tokens"] = "32k tokens"
    request_context_fit: Literal["unverified"] = "unverified"
    question_context_fit: Literal["unverified"] = "unverified"
    admission_policy: Literal["typesafe-non-strict-server-validation-v1"] = (
        "typesafe-non-strict-server-validation-v1"
    )


class TypeSafeDecisionsContextV1(DecisionsModel):
    protocol: Literal["typesafe-systemone-production-v1"] = TYPESAFE_PROTOCOL
    provider: Literal["typesafe"] = "typesafe"
    endpoint: Literal["systemone"] = "systemone"
    request_url: Literal["https://api.typesafe.ai/v1/systemone"] = (
        "https://api.typesafe.ai/v1/systemone"
    )
    model: Literal["jev-1.14.0"] = TYPESAFE_MODEL
    service_identity: Literal["typesafe.systemone.v1"] = "typesafe.systemone.v1"
    sdk_version: Literal["0.7.5"] = TYPESAFE_SDK
    api_version: Literal["v1"] = "v1"
    credential_source: Literal["TYPESAFE_API_KEY"] = "TYPESAFE_API_KEY"
    account_scope: ID | None = None
    limits: TypeSafeDecisionsLimitsV3 = Field(default_factory=TypeSafeDecisionsLimitsV3)
