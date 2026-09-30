from __future__ import annotations

from pathlib import Path

import pytest
import yaml

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config" / "example.yaml"


@pytest.fixture
def example_config() -> Path:
    return EXAMPLE_CONFIG


@pytest.fixture
def example_raw() -> dict:
    return yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
