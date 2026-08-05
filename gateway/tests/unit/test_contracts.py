"""Conformance with the shared contract files."""

from __future__ import annotations

import json
from pathlib import Path

from gateway.api.schemas import GenerationRequest
from gateway.core.models import ErrorCode, contract_codes

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def test_error_codes_match_the_contract_exactly() -> None:
    assert {code.value for code in ErrorCode} == contract_codes()


def test_request_schema_matches_the_contract_fields() -> None:
    schema = json.loads((CONTRACTS / "generation-request.schema.json").read_text())
    declared = set(schema["properties"]) - {"correlation_id"}

    assert set(GenerationRequest.model_fields) == declared


def test_bounds_match_the_contract() -> None:
    schema = json.loads((CONTRACTS / "generation-request.schema.json").read_text())
    props = schema["properties"]

    assert props["prompt"]["maxLength"] == 2000
    assert props["num_inference_steps"]["maximum"] == 50
    assert props["width"]["minimum"] == 256
