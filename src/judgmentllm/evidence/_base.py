"""Shared strict records and content-free validation for detached evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    GetCoreSchemaHandler,
    ModelWrapValidatorHandler,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import CoreSchema, PydanticCustomError, core_schema

from judgmentllm.models import ID, StrictModel


class EvidenceModel(StrictModel):
    """Frozen evidence v1; validation never echoes supplied values or keys.

    Trusted identity/configuration fields must be supplied by the caller, never
    copied from arbitrary provider text. Extraction sanitizes untrusted output.
    """

    model_config = ConfigDict(
        frozen=True, hide_input_in_errors=True, revalidate_instances="always"
    )
    schema_version: Literal[1] = 1

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: type[BaseModel], handler: GetCoreSchemaHandler, /
    ) -> CoreSchema:
        # Inherited model validators can sit inside a subclass's after validator.
        # Guard the complete schema so cross-field failures are sanitized too,
        # including direct constructors and TypeAdapter validation.
        def safe(value: Any, validate: core_schema.ValidatorFunctionWrapHandler) -> Any:
            try:
                return validate(value)
            except Exception:
                pass
            raise _invalid_evidence() from None

        return core_schema.no_info_wrap_validator_function(safe, handler(source))

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported evidence version")
        return value

    @model_validator(mode="wrap")
    @classmethod
    def safe_validation(
        cls,
        value: object,
        handler: ModelWrapValidatorHandler[Self],
        info: ValidationInfo,
    ) -> Self:
        try:
            # A wrap handler sees decoded Python containers even in JSON mode.
            # Let Pydantic restore field types (tuple/date/Decimal), then perform
            # the usual strict record validation. Never coerce Python inputs.
            if info.mode == "json" and isinstance(value, dict):
                supplied = cast(dict[str, object], value)
                value = {
                    key: TypeAdapter(
                        cls.model_fields[key].annotation or Any
                    ).validate_json(json.dumps(item), strict=True)
                    if key in cls.model_fields
                    and key not in {"schema_version", "semantics_version"}
                    else item
                    for key, item in supplied.items()
                }
            return handler(value)
        except Exception:
            pass
        raise _invalid_evidence() from None

    @classmethod
    def model_validate_json(
        cls, json_data: str | bytes | bytearray, **kwargs: Any
    ) -> Self:
        try:
            return super().model_validate_json(json_data, **kwargs)
        except Exception:
            pass
        raise _invalid_evidence() from None

    @classmethod
    def model_validate_strings(cls, obj: Any, **kwargs: Any) -> Self:
        try:
            return super().model_validate_strings(obj, **kwargs)
        except Exception:
            pass
        raise _invalid_evidence() from None

    @classmethod
    def model_construct(
        cls, _fields_set: set[str] | None = None, **values: Any
    ) -> Self:
        return cls.model_validate(values)

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        values = self.model_dump()
        if update is not None:
            values.update(update)
        return type(self).model_validate(values)

    @classmethod
    def parse_raw(cls, *args: Any, **kwargs: Any) -> Self:
        raise ValueError("Use model_validate_json() for evidence records.")

    @classmethod
    def parse_file(cls, *args: Any, **kwargs: Any) -> Self:
        raise ValueError("File parsing is unsupported for evidence records.")

    def copy(self, *args: Any, **kwargs: Any) -> Self:
        raise ValueError("Use model_copy() for evidence records.")

    def __setattr__(self, name: str, value: Any) -> None:
        raise _invalid_evidence() from None


def _invalid_evidence() -> ValidationError:
    return ValidationError.from_exception_data(
        "EvidenceValidation",
        [
            {
                "type": PydanticCustomError(
                    "evidence_invalid", "Invalid evidence record."
                ),
                "loc": (),
                "input": None,
            }
        ],
        hide_input=True,
    )


class EvidenceIdentity(EvidenceModel):
    """Existing execution IDs, supplied from trusted submission/observation records."""

    run_id: ID
    request_uid: ID
    attempt_id: ID
    submission_id: ID
    observation_id: ID | None = None
