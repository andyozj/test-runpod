"""Conformance with the shared contract files.

Package isolation means the worker and the gateway each define the request
schema and the error codes. These assertions are the only thing preventing the
two from drifting into a state where the gateway accepts a request the worker
rejects — a failure that appears only in production.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from worker.errors import ErrorCode, contract_codes
from worker.schemas import GenerationRequest

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def test_error_codes_match_the_contract_exactly() -> None:
    assert {code.value for code in ErrorCode} == contract_codes()


def test_request_schema_accepts_every_contract_field() -> None:
    schema = json.loads((CONTRACTS / "generation-request.schema.json").read_text())
    declared = set(schema["properties"])

    assert set(GenerationRequest.model_fields) == declared


@pytest.mark.parametrize(
    ("field", "key", "expected"),
    [
        ("width", "default", 1024),
        ("height", "default", 1024),
        ("num_inference_steps", "default", 28),
        ("num_inference_steps", "maximum", 50),
        ("prompt", "maxLength", 2000),
    ],
)
def test_bounds_match_the_contract(field: str, key: str, expected: int) -> None:
    schema = json.loads((CONTRACTS / "generation-request.schema.json").read_text())

    assert schema["properties"][field][key] == expected


def test_model_revision_is_a_well_formed_sha() -> None:
    revision = (CONTRACTS / "model-revision.txt").read_text().strip()

    assert re.fullmatch(r"[0-9a-f]{40}", revision)
