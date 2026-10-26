"""Optional real-browser regression through the external OAuth callback."""

import base64
import hashlib
import shutil
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

import httpx
import pytest

from garmin_mcp.http_runtime import OwnerOAuth, STABLE_CALLBACK, build_http_app


@pytest.mark.asyncio
async def test_chromium_can_submit_owner_consent(tmp_path):
    playwright = pytest.importorskip("playwright.async_api")
    chromium = shutil.which("chromium")
    if not chromium:
        pytest.skip("System Chromium is required for this optional browser check")
    origin = "https://garmin.test"
    password = "test-pass1"
    verifier = "browser-test-" + "a" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    provider = OwnerOAuth(origin, tmp_path, password)
    app = build_http_app(Mock(), provider)
    transport = httpx.ASGITransport(app=app)
    observed = {}
    console_errors = []

    # All browser traffic is fulfilled locally through ASGI, with no external network.
    # Use a fresh HTTP client per request so only Chromium manages the cookies.
    async def handle(route, request):
        if request.url.startswith(STABLE_CALLBACK + "?"):
            assert request.method == "GET" and not request.post_data
            callback = parse_qs(urlsplit(request.url).query)
            assert callback["state"] == ["browser-test-state"]
            assert callback["iss"] == [origin + "/"]
            # Simulate the relying party exchanging the code only after the
            # browser actually reaches its callback, then discover protected tools.
            async with httpx.AsyncClient(transport=transport, base_url=origin) as client:
                tokens = await client.post("/token", data={
                    "grant_type": "authorization_code", "client_id": registration.json()["client_id"],
                    "code": callback["code"][0], "redirect_uri": STABLE_CALLBACK,
                    "resource": origin + "/mcp", "code_verifier": verifier,
                })
                assert tokens.status_code == 200, tokens.text
                tools = await client.post("/mcp", headers={
                    "Authorization": "Bearer " + tokens.json()["access_token"],
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-06-18",
                }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
                assert tools.status_code == 200, tools.text
                assert tools.json()["result"]["tools"]
            observed["callback_reached"] = True
            await route.fulfill(status=200, content_type="text/html", body="Connection approved")
            return
        if not request.url.startswith(origin + "/"):
            await route.abort()
            return
        headers = await request.all_headers()
        if request.method == "POST" and request.url == origin + "/consent":
            observed["origin"] = headers.get("origin")
            observed["has_cookie"] = "__Host-garmin-consent=" in headers.get("cookie", "")
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.request(request.method, request.url, headers=headers,
                                            content=request.post_data_buffer, follow_redirects=False)
        if request.method == "POST" and request.url == origin + "/consent":
            observed["status"] = response.status_code
            if response.status_code == 303:
                observed["authorized"] = response.headers["location"].startswith(STABLE_CALLBACK + "?")
        await route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

    async with httpx.AsyncClient(transport=transport, base_url=origin) as client:
        registration = await client.post("/register", json={
            "redirect_uris": [STABLE_CALLBACK], "client_name": "Browser regression",
            "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "scope": "garmin:read",
        })
    assert registration.status_code == 201
    params = {"response_type": "code", "client_id": registration.json()["client_id"],
              "redirect_uri": STABLE_CALLBACK, "scope": "garmin:read", "state": "browser-test-state",
              "resource": origin + "/mcp", "code_challenge_method": "S256", "code_challenge": challenge}
    async with httpx.AsyncClient(transport=transport, base_url=origin) as client:
        authorization = await client.get("/authorize", params=params, follow_redirects=False)
    consent_url = authorization.headers["location"]
    try:
        async with app.router.lifespan_context(app), playwright.async_playwright() as api:
            browser = await api.chromium.launch(executable_path=chromium, headless=True,
                args=["--no-sandbox", "--proxy-server=http://127.0.0.1:9"])
            context = await browser.new_context()
            await context.route("**/*", handle)
            page = await context.new_page()
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            await page.goto(consent_url)
            await page.locator("input[name=password]").fill(password)
            await page.get_by_role("button", name="Allow read access").click()
            for _ in range(30):
                if observed.get("callback_reached") or console_errors:
                    break
                await page.wait_for_timeout(100)
            await browser.close()
    finally:
        provider.store.db.close()
    assert observed == {"origin": origin, "has_cookie": True, "status": 303,
                        "authorized": True, "callback_reached": True}, console_errors
