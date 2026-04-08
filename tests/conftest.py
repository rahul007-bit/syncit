"""Shared pytest fixtures for syncit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def bundle_yaml(fixture_dir: Path) -> Path:
    return fixture_dir / "bundle.yaml"


@pytest.fixture
def requirements_txt(fixture_dir: Path) -> Path:
    return fixture_dir / "requirements.txt"


@pytest.fixture
def tmp_bundle_dir(tmp_path: Path) -> Path:
    """A temporary directory acting as a bundle root."""
    d = tmp_path / "bundle-test"
    d.mkdir()
    return d


@pytest.fixture
def tmp_state_file(tmp_path: Path) -> Path:
    return tmp_path / "state.json"
