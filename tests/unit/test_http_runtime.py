"""Exercise the real HTTP OAuth/PKCE flow and access boundary without Garmin calls."""

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient

from garmin_mcp.cloud import READ_TOOLS
from garmin_mcp.http_runtime import OwnerOAuth, STABLE_CALLBACK, build_http_app


ORIGIN = "https://garmin.test"
# Exercise the full consent/token flow with the shortest accepted password.
PASSWORD = "test-pass1"
VERIFIER = "test-pkce-verifier-" + "a" * 43
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


@pytest.fixture
def setup(tmp_path):
    provider = OwnerOAuth(ORIGIN, tmp_path, PASSWORD)
    garmin = Mock()
    garmin.get_stats.return_value = {"totalSteps": 321}
    with TestClient(build_http_app(garmin, provider), base_url=ORIGIN, follow_redirects=False) as http:
        yield http, provider, garmin
    provider.store.db.close()


def register(http, callback=STABLE_CALLBACK):
    return http.post("/register", json={"redirect_uris": [callback], "client_name": "ChatGPT",
        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "scope": "garmin:read offline_access"})


def consent_request(http, client_id, **overrides):
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": STABLE_CALLBACK,
              "scope": "garmin:read offline_access", "state": "keep-this-state", "resource": ORIGIN + "/mcp",
              "code_challenge_method": "S256", "code_challenge": CHALLENGE} | overrides
    authorization = http.get("/authorize", params=params)
    location = authorization.headers.get("location")
    assert location, authorization.text
    return authorization, location


def approve(http, location):
    consent = http.get(location)
    assert consent.status_code == 200
    import re
    csrf = re.search("name='csrf' value='([^']+)'", consent.text).group(1)
    ticket = parse_qs(urlsplit(location).query)["ticket"][0]
    return http.post("/consent", headers={"Origin": ORIGIN},
                     data={"ticket": ticket, "csrf": csrf, "password": PASSWORD})


def get_tokens(http):
    client = register(http).json()
    authorization, location = consent_request(http, client["client_id"])
    assert authorization.status_code in (302, 303, 307)
    approved = approve(http, location)
    assert approved.status_code == 303, approved.text
    callback = parse_qs(urlsplit(approved.headers["location"]).query)
    assert callback["state"] == ["keep-this-state"]
    assert callback["iss"] == [ORIGIN + "/"]
    request = {"grant_type": "authorization_code", "client_id": client["client_id"],
               "code": callback["code"][0], "redirect_uri": STABLE_CALLBACK,
               "resource": ORIGIN + "/mcp", "code_verifier": VERIFIER}
    wrong_pkce = http.post("/token", data=request | {"code_verifier": "wrong"})
    assert wrong_pkce.status_code == 400
    tokens = http.post("/token", data=request)
    assert tokens.status_code == 200, tokens.text
    assert http.post("/token", data=request).status_code == 400  # One-use authorization code.
    return client, tokens.json()


def rpc(http, method, token=None, params=None):
    headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
    if token:
        headers["Authorization"] = "Bearer " + token
    return http.post("/mcp", headers=headers,
                     json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def test_discovery_and_private_data_are_separated(setup):
    http, provider, garmin = setup
    metadata = http.get("/.well-known/oauth-authorization-server").json()
    resource = http.get("/.well-known/oauth-protected-resource/mcp").json()
    assert metadata["issuer"] == resource["authorization_servers"][0] == ORIGIN + "/"
    assert metadata["authorization_response_iss_parameter_supported"] is True
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert resource["resource"] == ORIGIN + "/mcp"
    assert rpc(http, "tools/list").status_code == 401
    assert rpc(http, "tools/list", "invalid").status_code == 401
    assert "resource_metadata" in rpc(http, "tools/list").headers["www-authenticate"]
    assert not garmin.mock_calls
    assert register(http, "https://attacker.example/callback").status_code == 400
    assert http.get("/healthz", headers={"Host": "attacker.example"}).status_code == 400


def test_full_link_read_and_refresh_rotation(setup):
    http, provider, garmin = setup
    client, tokens = get_tokens(http)
    result = rpc(http, "tools/list", tokens["access_token"])
    assert result.status_code == 200, result.text
    tools = result.json()["result"]["tools"]
    assert {t["name"] for t in tools} == READ_TOOLS
    assert all(t["annotations"]["readOnlyHint"] for t in tools)
    result = rpc(http, "tools/call", tokens["access_token"], {"name": "get_stats", "arguments": {"date": "2026-10-25"}})
    assert "321" in result.text
    garmin.get_stats.assert_called_once_with("2026-10-25")
    refresh = {"grant_type": "refresh_token", "client_id": client["client_id"],
               "refresh_token": tokens["refresh_token"], "resource": ORIGIN + "/mcp"}
    assert http.post("/token", data=refresh | {"resource": "https://other.test/mcp"}).status_code == 400
    assert http.post("/token", data=refresh | {"scope": "garmin:write"}).status_code == 400
    rotated = http.post("/token", data=refresh)
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["refresh_token"] != tokens["refresh_token"]
    assert rpc(http, "tools/list", rotated.json()["access_token"]).status_code == 200
    assert http.post("/token", data=refresh).status_code == 400
    assert rpc(http, "tools/list", rotated.json()["access_token"]).status_code == 401
    stored = " ".join(row[0] for row in provider.store.db.execute("SELECT value FROM state"))
    assert tokens["access_token"] not in stored and tokens["refresh_token"] not in stored


def test_consent_needs_password_cookie_and_origin(setup):
    http, provider, _ = setup
    client = register(http).json()
    _, location = consent_request(http, client["client_id"])
    http.get(location)
    ticket = parse_qs(urlsplit(location).query)["ticket"][0]
    csrf = http.cookies.get("__Host-garmin-consent")
    form = {"ticket": ticket, "csrf": csrf, "password": PASSWORD}
    assert http.post("/consent", data=form, headers={"Origin": "https://attacker.test"}).status_code == 403
    assert http.post("/consent", data=form | {"password": "wrong"}, headers={"Origin": ORIGIN}).status_code == 403
    assert http.post("/consent", data=form | {"csrf": "wrong"}, headers={"Origin": ORIGIN}).status_code == 403
    assert not provider.store.count("code:")
    assert http.post("/consent", data=form, headers={"Origin": ORIGIN}).status_code == 303
    assert http.post("/consent", data=form, headers={"Origin": ORIGIN}).status_code == 400


def test_error_redirect_identifies_issuer_and_bad_targets_fail(setup):
    http, _, _ = setup
    client = register(http).json()
    response, location = consent_request(http, client["client_id"], scope="garmin:write")
    params = parse_qs(urlsplit(location).query)
    assert params["error"] == ["invalid_scope"]
    assert params["iss"] == [ORIGIN + "/"]
    assert http.post("/token", data={"resource": "https://other.test"}).status_code == 400
    assert http.post("/register", content=b"x" * 65537).status_code == 413


def test_tokens_and_clients_survive_restart_but_not_audience_change(tmp_path):
    first = OwnerOAuth(ORIGIN, tmp_path, PASSWORD)
    with TestClient(build_http_app(Mock(), first), base_url=ORIGIN, follow_redirects=False) as http:
        client, tokens = get_tokens(http)
    first.store.db.close()
    second = OwnerOAuth(ORIGIN, tmp_path, PASSWORD)
    with TestClient(build_http_app(Mock(), second), base_url=ORIGIN, follow_redirects=False) as http:
        assert rpc(http, "tools/list", tokens["access_token"]).status_code == 200
        result = http.post("/token", data={"grant_type": "refresh_token", "client_id": client["client_id"],
                           "refresh_token": tokens["refresh_token"], "resource": ORIGIN + "/mcp"})
        assert result.status_code == 200
    second.store.db.close()
    third = OwnerOAuth("https://new.test", tmp_path, PASSWORD)
    with TestClient(build_http_app(Mock(), third), base_url="https://new.test", follow_redirects=False) as http:
        assert rpc(http, "tools/list", tokens["access_token"]).status_code == 401
    third.store.db.close()


def test_connection_configuration_fails_closed(tmp_path):
    for password in ("", "test-pass"):
        with pytest.raises(ValueError, match="OWNER_PASSWORD"):
            OwnerOAuth(ORIGIN, tmp_path, password)
    for origin in ("http://garmin.test", "https://garmin.test/other", "https://user:pass@garmin.test", "https://"):
        with pytest.raises(ValueError, match="PUBLIC_URL"):
            OwnerOAuth(origin, tmp_path, PASSWORD)
