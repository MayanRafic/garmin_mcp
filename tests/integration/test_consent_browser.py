"""Optional real-browser regression for the owner consent form's Origin header."""

import base64
import hashlib
import shutil
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

    # All browser traffic is fulfilled locally through ASGI, with no external network.
    # Use a fresh HTTP client per request so only Chromium manages the cookies.
    async def handle(route, request):
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
                # The browser contract under test ends at form submission; don't
                # navigate to an external callback with the test authorization code.
                await route.fulfill(status=200, content_type="text/html", body="Connection approved")
                return
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
        async with playwright.async_playwright() as api:
            browser = await api.chromium.launch(executable_path=chromium, headless=True, args=["--no-sandbox"])
            context = await browser.new_context()
            await context.route("**/*", handle)
            page = await context.new_page()
            await page.goto(consent_url)
            await page.locator("input[name=password]").fill(password)
            async with page.expect_navigation():
                await page.get_by_role("button", name="Allow read access").click()
            await browser.close()
    finally:
        provider.store.db.close()
    assert observed == {"origin": origin, "has_cookie": True, "status": 303, "authorized": True}
