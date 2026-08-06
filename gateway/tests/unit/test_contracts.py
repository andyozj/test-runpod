"""Conformance with the shared contract files.

Package isolation means the worker and the gateway each define the request
schema and the error codes. These assertions are the only thing preventing the
two from drifting into a state where the gateway accepts a request the worker
rejects — a failure that appears only in production.

The bound assertions compare the Pydantic model's own constraints against the
contract, not the contract against literals: reading the same file twice proves
nothing about the code that has to honour it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from annotated_types import Ge, Le, MaxLen, MinLen

from gateway.api.schemas import GenerationRequest
from gateway.contracts import contract_path
from gateway.core.models import ErrorCode, contract_codes

SCHEMA = json.loads(contract_path("generation-request.schema.json").read_text())
PROPERTIES: dict[str, dict[str, Any]] = SCHEMA["properties"]

# The gateway assigns the correlation id itself; a caller-supplied one would let
# two callers share a trace. The worker accepts it because the gateway sends it.
GATEWAY_OMITS = {"correlation_id"}


def _constraints(field: str) -> dict[str, Any]:
    """Return the model field's declared bounds, keyed by JSON Schema name."""
    found: dict[str, Any] = {}
    for item in GenerationRequest.model_fields[field].metadata:
        if isinstance(item, Ge):
            found["minimum"] = item.ge
        elif isinstance(item, Le):
            found["maximum"] = item.le
        elif isinstance(item, MinLen):
            found["minLength"] = item.min_length
        elif isinstance(item, MaxLen):
            found["maxLength"] = item.max_length
    return found


def test_error_codes_match_the_contract_exactly() -> None:
    assert {code.value for code in ErrorCode} == contract_codes()


def test_request_schema_matches_the_contract_fields() -> None:
    assert set(GenerationRequest.model_fields) == set(PROPERTIES) - GATEWAY_OMITS


@pytest.mark.parametrize("field", sorted(set(PROPERTIES) - GATEWAY_OMITS))
def test_model_bounds_match_the_contract(field: str) -> None:
    declared = {
        key: value
        for key, value in PROPERTIES[field].items()
        if key in {"minimum", "maximum", "minLength", "maxLength"}
    }

    assert _constraints(field) == declared


@pytest.mark.parametrize(
    "field", sorted(f for f in set(PROPERTIES) - GATEWAY_OMITS if f != "prompt")
)
def test_model_defaults_match_the_contract(field: str) -> None:
    assert GenerationRequest.model_fields[field].default == PROPERTIES[field]["default"]


def test_output_format_accepts_exactly_the_contract_enum() -> None:
    annotation = GenerationRequest.model_fields["output_format"].annotation

    assert set(annotation.__args__) == set(PROPERTIES["output_format"]["enum"])  # type: ignore[union-attr]


def test_prompt_is_the_only_required_field() -> None:
    required = {
        name
        for name, field in GenerationRequest.model_fields.items()
        if field.is_required()
    }

    assert required == set(SCHEMA["required"])


def test_the_model_forbids_fields_the_contract_does_not_declare() -> None:
    assert SCHEMA["additionalProperties"] is False
    assert GenerationRequest.model_config["extra"] == "forbid"
