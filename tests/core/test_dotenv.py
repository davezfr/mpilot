from __future__ import annotations

import os

from mpilot.core.dotenv import load_project_dotenv


def test_load_project_dotenv_preserves_explicit_environment(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("EXPLICIT=value-from-file\nFROM_FILE=loaded\n", encoding="utf-8")
    monkeypatch.setenv("EXPLICIT", "value-from-process")
    monkeypatch.delenv("FROM_FILE", raising=False)
    monkeypatch.delenv("MPILOT_NO_DOTENV", raising=False)
    monkeypatch.delenv("BABELARR_NO_DOTENV", raising=False)
    monkeypatch.delenv("MST_NO_DOTENV", raising=False)

    loaded_path = load_project_dotenv(tmp_path)

    assert loaded_path == tmp_path / ".env"
    assert os.environ["EXPLICIT"] == "value-from-process"
    assert os.environ["FROM_FILE"] == "loaded"


def test_load_project_dotenv_honors_mpilot_disable_flag(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("SHOULD_NOT_LOAD=yes\n", encoding="utf-8")
    monkeypatch.setenv("MPILOT_NO_DOTENV", "1")
    monkeypatch.delenv("SHOULD_NOT_LOAD", raising=False)

    assert load_project_dotenv(tmp_path) is None
    assert "SHOULD_NOT_LOAD" not in os.environ
