"""Offline regression for the production annotation-only normalization."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("normalize_sdk_types", ROOT / "scripts/normalize_sdk_types.py")
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
normalize = module.normalize


def test_preserves_defaults_comments_and_imports(tmp_path):
    source = """from __future__ import annotations
from pydantic import BaseModel, Field, constr
# retain generated provenance
class Body(BaseModel):
    text: constr(min_length=1) = Field("x", alias="value")
"""
    result = normalize(source)
    assert "# retain generated provenance" in result
    assert 'Field("x", alias="value")' in result
    assert "BaseModel, Field, constr" in result
    assert normalize(result) == result
    candidate = tmp_path / "candidate.py"
    candidate.write_text(result)
    candidate_spec = importlib.util.spec_from_file_location("candidate", candidate)
    candidate_module = importlib.util.module_from_spec(candidate_spec)
    candidate_spec.loader.exec_module(candidate_module)
    body = candidate_module.Body
    body.model_rebuild(_types_namespace=vars(candidate_module))
    assert body(value="ok").model_dump(by_alias=True) == {"value": "ok"}
    with pytest.raises(ValueError):
        body(value="")


@pytest.mark.parametrize("expression", ["constr(1)", "conint(unknown=True)", "confloat(**options)"])
def test_unknown_constraint_forms_fail_closed(expression):
    with pytest.raises(ValueError, match="Unsupported constraint"):
        normalize(f"from __future__ import annotations\nx: {expression}\n")
