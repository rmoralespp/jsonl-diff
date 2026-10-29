import json
from decimal import Decimal, InvalidOperation

import pytest


@pytest.fixture
def decimal_supports_extreme_exponents():
    """Detect exponent support without assuming a particular Decimal backend."""
    try:
        return Decimal("1e9999999999999999999").is_finite()
    except InvalidOperation:
        return False


@pytest.fixture
def write_jsonl(tmp_path):
    """Create a JSONL file while allowing raw lines for strict-input tests."""

    def write(name, records):
        path = tmp_path / name
        lines = [
            record if isinstance(record, str) else json.dumps(record, separators=(",", ":"))
            for record in records
        ]
        content = "\n".join(lines)
        if lines:
            content += "\n"
        path.write_text(content, encoding="utf-8")
        return path

    return write
