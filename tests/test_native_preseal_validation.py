"""Native preparation snapshots retain independent nested mutation validation."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel
from test_native_transport_integration import offline as offline
from test_native_transport_rendering import rendered_case

from judgmentllm.models import Level, Trait
from judgmentllm.output_contracts import RubricOutputContract
from judgmentllm.transport import OutputCommitmentV3, PreparedRequestV3


def corrupt(model: BaseModel, **values: object) -> BaseModel:
    # Deliberately bypass EvidenceModel's validating model_copy. The receiving
    # constructor must independently reject even this manufactured corruption.
    return BaseModel.model_copy(model, update=values)


@pytest.mark.parametrize(
    "level",
    ["output", "selection", "projection", "scope", "contract", "trait", "category"],
)
def test_primitive_snapshot_hash_never_bypasses_nested_validation(
    tmp_path: Path, level: str
) -> None:
    wire = rendered_case(tmp_path, "openai.responses.text.format", "together")
    output = wire.output_commitment
    contract = output.contract
    assert isinstance(contract, RubricOutputContract)
    marker = {"private-injected-key": "private-injected-value"}
    if level == "output":
        changed = corrupt(output, **marker)
    elif level == "selection":
        changed = corrupt(output, selection=corrupt(output.selection, **marker))
    elif level == "projection":
        assert output.projection is not None
        changed = corrupt(output, projection=corrupt(output.projection, **marker))
    elif level == "scope":
        changed = corrupt(output, scope=corrupt(output.scope, **marker))
    elif level == "contract":
        changed = corrupt(output, contract=corrupt(contract, **marker))
    else:
        traits = list(contract.traits)
        trait = traits[0]
        if level == "trait":
            traits[0] = cast(Trait, corrupt(trait, **marker))
        else:
            levels = list(trait.levels)
            levels[0] = cast(Level, corrupt(levels[0], **marker))
            traits[0] = cast(Trait, corrupt(trait, levels=levels))
        changed = corrupt(output, contract=corrupt(contract, traits=tuple(traits)))
    with pytest.raises(ValueError) as error:
        PreparedRequestV3.model_validate({**dict(wire), "output_commitment": changed})
    assert "private-injected" not in str(error.value)
    # The separate public output boundary remains independently strict too.
    with pytest.raises(ValueError):
        OutputCommitmentV3.model_validate(dict(changed))


def test_primitive_snapshot_hash_still_binds_current_category_bytes(
    tmp_path: Path,
) -> None:
    wire = rendered_case(tmp_path, "openai.responses.text.format", "together")
    output = wire.output_commitment
    assert isinstance(output.contract, RubricOutputContract)
    traits = list(output.contract.traits)
    levels = list(traits[0].levels)
    levels[0] = levels[0].model_copy(update={"description": "changed harmless prose"})
    traits[0] = traits[0].model_copy(update={"levels": levels})
    changed = corrupt(output, contract=corrupt(output.contract, traits=tuple(traits)))
    # Even a category-description change that leaves the generated schema valid
    # changes the committed canonical assessment source and cannot reuse a seal.
    with pytest.raises(ValueError):
        PreparedRequestV3.model_validate({**dict(wire), "output_commitment": changed})
