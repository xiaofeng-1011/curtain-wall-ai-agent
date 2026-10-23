"""Unit tests use isolated resources; the real release has separate acceptance."""
import pytest


@pytest.fixture(autouse=True)
def isolated_builtin_default(tmp_path, monkeypatch):
    # Bundle-specific tests explicitly replace this with their tiny fixture.
    # Ordinary UI tests must not deploy the full release or inspect user config.
    monkeypatch.setenv('MW_STANDARD_BUNDLE', str(tmp_path/'no-installed-bundle'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'profile'))
