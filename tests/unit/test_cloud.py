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
    return base64.b64encode(json.dumps({
        "di_token": "test-token-2", "di_refresh_token": "test-refresh",
        "di_client_id": "test-client",
    }).encode()).decode()


def test_seed_is_private_and_not_reimported(tmp_path, monkeypatch, session):
    directory = tmp_path / "garmin"
    monkeypatch.setenv("GARMIN_SESSION_B64", session)
    seed_tokens(directory)
    token = directory / "garmin_tokens.json"
    assert json.loads(token.read_text())["di_token"] == "test-token-2"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert token.stat().st_mode & 0o777 == 0o600
    assert "GARMIN_SESSION_B64" not in os.environ
    newer = json.loads(token.read_text()) | {"di_token": "refreshed"}
    token.write_text(json.dumps(newer))
    monkeypatch.setenv("GARMIN_SESSION_B64", session)
    seed_tokens(directory)
    assert json.loads(token.read_text())["di_token"] == "refreshed"


def test_bad_secret_is_not_echoed(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMIN_SESSION_B64", "private-invalid-secret")
    with pytest.raises(ValueError) as caught:
        seed_tokens(tmp_path / "garmin")
    assert "private-invalid-secret" not in str(caught.value)


def test_partial_session_is_not_silently_overwritten(tmp_path, monkeypatch, session):
    directory = tmp_path / "garmin"
    directory.mkdir()
    (directory / "garmin_tokens.json").write_text("")
    monkeypatch.setenv("GARMIN_SESSION_B64", session)
    with pytest.raises(ValueError, match="Incomplete saved"):
        seed_tokens(directory)
    assert (directory / "garmin_tokens.json").read_text() == ""


@pytest.mark.parametrize("fail", [False, True])
def test_refreshed_session_saved_after_sdk_call(tmp_path, fail):
    client = Mock()
    def dump(path):
        for name in ("garmin_tokens.json",):
            (Path(path) / name).write_text('{"new": true}')
    client.garth.dump.side_effect = dump
    client.get_stats.side_effect = RuntimeError("API failed") if fail else None
    wrapped = PersistentGarmin(client, tmp_path / "garmin")
    if fail:
        with pytest.raises(RuntimeError, match="API failed"):
            wrapped.get_stats("2026-10-25")
    else:
        wrapped.get_stats("2026-10-25")
    assert json.loads((tmp_path / "garmin/garmin_tokens.json").read_text()) == {"new": True}


def test_native_seed_preserves_legacy_tokens_for_recovery(tmp_path, monkeypatch, session):
    directory = tmp_path / "garmin"
    directory.mkdir()
    for name in ("oauth1_token.json", "oauth2_token.json"):
        (directory / name).write_text("legacy-session")
    monkeypatch.setenv("GARMIN_SESSION_B64", session)
    seed_tokens(directory)
    assert (directory / "garmin_tokens.json").is_file()
    assert (directory / "oauth1_token.json").read_text() == "legacy-session"
    assert (directory / "oauth2_token.json").read_text() == "legacy-session"


def test_old_garth_seed_requires_fresh_login_without_leaking_tokens(tmp_path, monkeypatch):
    legacy = base64.b64encode(json.dumps([{"oauth_token": "private-old-token"}, {}]).encode()).decode()
    monkeypatch.setenv("GARMIN_SESSION_B64", legacy)
    with pytest.raises(ValueError, match="cannot be converted") as caught:
        seed_tokens(tmp_path / "garmin")
    assert "private-old-token" not in str(caught.value)
    assert not (tmp_path / "garmin/garmin_tokens.json").exists()


def test_rate_limit_cooldown_blocks_other_tools_and_then_recovers(tmp_path, monkeypatch):
    from garminconnect import GarminConnectTooManyRequestsError
    from unittest.mock import patch
    client = Mock()
    client.get_stats.side_effect = GarminConnectTooManyRequestsError("sensitive detail")
    wrapped = PersistentGarmin(client, tmp_path / "garmin")
    clock = [1000.0]
    monkeypatch.setattr("garmin_mcp.cloud.time.monotonic", lambda: clock[0])
    with patch("garmin_mcp.cloud.save_tokens"):
        with pytest.raises(RuntimeError, match="retry after 900"):
            wrapped.get_stats("2026-10-31")
        with pytest.raises(RuntimeError, match="retry after 900"):
            wrapped.count_activities()
        client.count_activities.assert_not_called()
        clock[0] += 901
        client.count_activities.return_value = 540
        assert wrapped.count_activities() == 540


def test_native_expired_session_refreshes_once_and_persists_rotation(tmp_path, monkeypatch):
    from garmin_mcp.sdk import Garmin
    def jwt(exp):
        header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode().rstrip("=")
        body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
        return f"{header}.{body}.test-signature"
    api = Garmin()
    assert api.garth is api.client
    api.client.di_token = jwt(1)
    api.client.di_refresh_token = "old-refresh"
    api.client.di_client_id = "test-client"
    new_token = jwt(9999999999)
    response = Mock(ok=True)
    response.json.return_value = {"access_token": new_token, "refresh_token": "rotated-refresh"}
    post = Mock(return_value=response)
    monkeypatch.setattr(api.client, "_di_token_post", post)
    data_response = Mock(status_code=200)
    data_response.json.return_value = {"totalCount": 540}
    request = Mock(return_value=data_response)
    monkeypatch.setattr(api.client._api_session, "request", request)
    wrapped = PersistentGarmin(api, tmp_path / "garmin")
    assert wrapped.count_activities() == 540
    post.assert_called_once_with("test-client", "application/json", {
        "grant_type": "refresh_token", "client_id": "test-client", "refresh_token": "old-refresh"})
    request.assert_called_once()
    assert request.call_args.kwargs["headers"]["Authorization"] == "Bearer " + new_token
    saved = json.loads((tmp_path / "garmin/garmin_tokens.json").read_text())
    assert saved["di_refresh_token"] == "rotated-refresh"
    assert saved["di_token"] == new_token


def test_rate_limited_native_refresh_skips_api_and_preserves_session(tmp_path, monkeypatch):
    from garmin_mcp.sdk import Garmin
    from garminconnect import GarminConnectAuthenticationError
    api = Garmin()
    api.client.di_token = "existing-token"
    api.client.di_refresh_token = "existing-refresh"
    api.client.di_client_id = "test-client"
    monkeypatch.setattr(api.client, "_token_expires_soon", lambda: True)
    refresh = Mock(side_effect=GarminConnectAuthenticationError("DI token refresh failed: 429"))
    monkeypatch.setattr(api.client, "_refresh_di_token", refresh)
    data_request = Mock()
    monkeypatch.setattr(api.client._api_session, "request", data_request)
    wrapped = PersistentGarmin(api, tmp_path / "garmin")
    for _ in range(2):
        with pytest.raises(RuntimeError, match="rate limited"):
            wrapped.count_activities()
    refresh.assert_called_once()
    data_request.assert_not_called()
    saved = json.loads((tmp_path / "garmin/garmin_tokens.json").read_text())
    assert saved["di_refresh_token"] == "existing-refresh"


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
