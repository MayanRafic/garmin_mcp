"""Cloud authentication persistence and MCP access-boundary tests (no live account)."""

import base64
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from garmin_mcp.cloud import (
    PersistentGarmin, READ_TOOLS, build_app, seed_tokens, tunnel_command,
)


@pytest.fixture
def session():
    return base64.b64encode(json.dumps([
        {"oauth_token": "test-token-1", "oauth_token_secret": "test-secret", "domain": "garmin.com"},
        {"scope": "test", "jti": "test", "token_type": "bearer",
         "access_token": "test-token-2", "refresh_token": "test-refresh",
         "expires_in": 3600, "expires_at": 9999999999,
         "refresh_token_expires_in": 3600, "refresh_token_expires_at": 9999999999},
    ]).encode()).decode()


def test_seed_is_private_and_not_reimported(tmp_path, monkeypatch, session):
    directory = tmp_path / "garmin"
    monkeypatch.setenv("GARMIN_SESSION_B64", session)
    seed_tokens(directory)
    token = directory / "oauth2_token.json"
    assert json.loads(token.read_text())["access_token"] == "test-token-2"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert token.stat().st_mode & 0o777 == 0o600
    assert "GARMIN_SESSION_B64" not in os.environ
    newer = json.loads(token.read_text()) | {"access_token": "refreshed"}
    token.write_text(json.dumps(newer))
    monkeypatch.setenv("GARMIN_SESSION_B64", session)
    seed_tokens(directory)
    assert json.loads(token.read_text())["access_token"] == "refreshed"


def test_bad_secret_is_not_echoed(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMIN_SESSION_B64", "private-invalid-secret")
    with pytest.raises(ValueError) as caught:
        seed_tokens(tmp_path / "garmin")
    assert "private-invalid-secret" not in str(caught.value)


def test_partial_session_is_not_silently_overwritten(tmp_path, monkeypatch, session):
    directory = tmp_path / "garmin"
    directory.mkdir()
    (directory / "oauth1_token.json").write_text("existing")
    monkeypatch.setenv("GARMIN_SESSION_B64", session)
    with pytest.raises(ValueError, match="Incomplete saved"):
        seed_tokens(directory)
    assert (directory / "oauth1_token.json").read_text() == "existing"


@pytest.mark.parametrize("fail", [False, True])
def test_refreshed_session_saved_after_sdk_call(tmp_path, fail):
    client = Mock()
    def dump(path):
        for name in ("oauth1_token.json", "oauth2_token.json"):
            (Path(path) / name).write_text('{"new": true}')
    client.garth.dump.side_effect = dump
    client.get_stats.side_effect = RuntimeError("API failed") if fail else None
    wrapped = PersistentGarmin(client, tmp_path / "garmin")
    if fail:
        with pytest.raises(RuntimeError, match="API failed"):
            wrapped.get_stats("2026-10-25")
    else:
        wrapped.get_stats("2026-10-25")
    assert json.loads((tmp_path / "garmin/oauth2_token.json").read_text()) == {"new": True}


@pytest.mark.asyncio
async def test_cloud_discovery_excludes_writes_and_read_call_works(mock_garmin_client):
    app = build_app(mock_garmin_client)
    tools = await app.list_tools()
    assert {tool.name for tool in tools} == READ_TOOLS
    assert all(tool.annotations.readOnlyHint and not tool.annotations.destructiveHint for tool in tools)
    for name in ("delete_weigh_ins", "add_weigh_in", "log_food", "request_reload", "upload_workout"):
        with pytest.raises(Exception, match="Unknown tool"):
            await app.call_tool(name, {})
    mock_garmin_client.get_stats.return_value = {"calendarDate": "2026-10-25", "totalSteps": 321}
    result = await app.call_tool("get_stats", {"date": "2026-10-25"})
    assert '321' in str(result)
    mock_garmin_client.get_stats.assert_called_once_with("2026-10-25")


def test_tunnel_credentials_never_go_in_command_arguments(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TUNNEL_ID", "tunnel_" + "a" * 32)
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "private-runtime-key")
    command = tunnel_command()
    assert "private-runtime-key" not in " ".join(command)
    assert "--health.listen-addr=127.0.0.1:8080" in command


def test_no_tunnel_key_stops_startup(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TUNNEL_ID", "tunnel_" + "a" * 32)
    monkeypatch.delenv("CONTROL_PLANE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="CONTROL_PLANE_API_KEY"):
        tunnel_command()
