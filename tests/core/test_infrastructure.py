from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mpilot.core.env import env_first, float_env_any, int_env_any, read_env_file_value, telegram_bot_token
from mpilot.core.hermes import hermes_send_command, send_hermes_message
from mpilot.core.json_store import LockedJsonStore
from mpilot.core.targets import looks_like_hermes_target, parse_telegram_target, resolve_notification_target
from mpilot.core.telegram import (
    send_or_edit_telegram_status_message,
    telegram_edit_error_allows_replacement,
)


def test_env_first_returns_first_present_value():
    env = {"SECOND": "two", "THIRD": "three"}

    assert env_first("FIRST", "SECOND", "THIRD", default="fallback", env=env) == "two"


def test_numeric_env_helpers_fall_back_on_missing_or_invalid_values():
    env = {"BAD_FLOAT": "nope", "BAD_INT": "1.2", "GOOD_INT": "7"}

    assert float_env_any("MISSING", "BAD_FLOAT", default=2.5, env=env) == 2.5
    assert int_env_any("BAD_INT", "GOOD_INT", default=3, env=env) == 3


def test_read_env_file_value_handles_export_quotes_and_comments(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# ignored",
                "export TELEGRAM_BOT_TOKEN='profile-token'",
                "OTHER=value",
            ]
        ),
        encoding="utf-8",
    )

    assert read_env_file_value(env_file, "TELEGRAM_BOT_TOKEN") == "profile-token"


def test_telegram_bot_token_prefers_explicit_env_before_profile_file(tmp_path: Path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / ".env").write_text("TELEGRAM_BOT_TOKEN=from-file\n", encoding="utf-8")
    env = {"APP_TOKEN": "from-env", "HERMES_HOME": str(profile)}

    assert (
        telegram_bot_token(
            "APP_TOKEN",
            "TELEGRAM_BOT_TOKEN",
            hermes_env_path_names=("APP_HERMES_ENV_PATH",),
            env=env,
            home=tmp_path,
        )
        == "from-env"
    )


def test_hermes_send_command_includes_profile_when_configured():
    assert hermes_send_command(
        target="telegram:1",
        message="done",
        bin_name="/bin/hermes",
        profile="bot",
    ) == ["/bin/hermes", "--profile", "bot", "send", "--to", "telegram:1", "--quiet", "done"]


def test_send_hermes_message_reports_timeout(monkeypatch):
    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["hermes"], 4)

    monkeypatch.setattr("mpilot.core.hermes.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="hermes send timed out after 4"):
        send_hermes_message(
            "telegram:1",
            "hello",
            bin_env_names=("HERMES_BIN",),
            profile_env_names=("HERMES_PROFILE",),
            timeout_env_names=("HERMES_TIMEOUT",),
            env={"HERMES_TIMEOUT": "4"},
        )


def test_target_helpers_parse_telegram_and_resolve_requester_target():
    assert parse_telegram_target("telegram:123:456") == ("123", "456")
    assert looks_like_hermes_target("telegram:123") is True
    assert resolve_notification_target(None, "telegram:123") == "telegram:123"


def test_telegram_edit_error_policy_can_include_message_id_invalid():
    assert telegram_edit_error_allows_replacement("Bad Request: MESSAGE_ID_INVALID") is True
    assert telegram_edit_error_allows_replacement(
        "Bad Request: MESSAGE_ID_INVALID",
        include_message_id_invalid=False,
    ) is False


def test_sync_telegram_status_edit_replaces_missing_message():
    calls: list[tuple[str, dict]] = []

    def fake_post(_token: str, method: str, payload: dict):
        calls.append((method, payload))
        if method == "editMessageText":
            raise RuntimeError("Bad Request: message to edit not found")
        return {"ok": True, "result": {"message_id": 789}}

    assert (
        send_or_edit_telegram_status_message(
            token="token",
            chat_id="123",
            thread_id=None,
            message="working",
            message_id="456",
            api_post=fake_post,
        )
        == "789"
    )
    assert [method for method, _payload in calls] == ["editMessageText", "sendMessage"]


def test_locked_json_store_quarantines_corrupt_files(tmp_path: Path):
    path = tmp_path / "watches.json"
    path.write_text("{not-json", encoding="utf-8")
    store = LockedJsonStore(path)

    assert store._read() == {"watches": []}
    assert not path.exists()
    assert path.with_suffix(".json.corrupt").exists()


def test_locked_json_store_writes_atomically_under_reentrant_lock(tmp_path: Path):
    path = tmp_path / "watches.json"
    store = LockedJsonStore(path)

    with store._store_lock():
        with store._store_lock():
            store._write({"watches": [{"id": "one"}]})

    assert store._read() == {"watches": [{"id": "one"}]}
