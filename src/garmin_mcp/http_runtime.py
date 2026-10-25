"""Single-owner OAuth for the optional, directly hosted HTTPS MCP transport.

The MCP SDK implements PKCE, client authentication, discovery and bearer checks.
This provider persists consent grants and hashed tokens on the private volume.
It never accepts a Garmin password and never grants Garmin write access.
"""

from collections import defaultdict, deque
from contextlib import contextmanager
import hashlib
import hmac
from html import escape
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizeError, RefreshToken,
    RegistrationError, TokenError,
)
from mcp.server.auth.routes import build_metadata
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from garmin_mcp.cloud import authenticated_client, build_app


SCOPES = ["garmin:read", "offline_access"]
STABLE_CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
COOKIE = "__Host-garmin-consent"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def canonical_origin(value):
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.path not in ("", "/") or parts.query or parts.fragment or parts.port not in (None, 443)):
        raise ValueError("PUBLIC_URL must be the service's HTTPS origin")
    return f"https://{parts.netloc}"


class Store:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        path = directory / "oauth.sqlite3"
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        path.chmod(0o600)
        self.db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT, expires REAL)")
        self.lock = threading.RLock()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def get(self, key):
        row = self.db.execute("SELECT value FROM state WHERE key=? AND (expires IS NULL OR expires>?)",
                              (key, time.time())).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value, expires=None):
        self.db.execute("DELETE FROM state WHERE expires IS NOT NULL AND expires<=?", (time.time(),))
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?,?)", (key, json.dumps(value), expires))

    def delete(self, key):
        self.db.execute("DELETE FROM state WHERE key=?", (key,))

    def count(self, prefix):
        return self.db.execute("SELECT COUNT(*) FROM state WHERE key LIKE ? AND (expires IS NULL OR expires>?)",
                               (prefix + "%", time.time())).fetchone()[0]


class OwnerOAuth:
    def __init__(self, origin, directory, password, redirects=None):
        self.origin = canonical_origin(origin)
        self.issuer = self.origin + "/"
        self.resource = self.origin + "/mcp"
        if len(password) < 20:
            raise ValueError("Set OWNER_PASSWORD to a new password of at least 20 characters")
        self.salt = secrets.token_bytes(16)
        self.password_hash = self.hash_password(password)
        self.redirects = set(redirects if redirects is not None else [STABLE_CALLBACK])
        self.store = Store(directory)

    def hash_password(self, password):
        return hashlib.scrypt(password.encode(), salt=self.salt, n=16384, r=8, p=1, dklen=32)

    async def get_client(self, client_id):
        value = self.store.get("client:" + client_id)
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def register_client(self, client_info):
        if (not client_info.redirect_uris
                or any(str(uri) not in self.redirects for uri in client_info.redirect_uris)):
            raise RegistrationError("invalid_redirect_uri", "Callback is not allowed by the owner")
        if client_info.token_endpoint_auth_method not in ("none", "client_secret_post", "client_secret_basic"):
            raise RegistrationError("invalid_client_metadata", "Unsupported client authentication")
        with self.store.transaction():
            if self.store.count("client:") >= 64:
                raise RegistrationError("invalid_client_metadata", "Client limit reached")
            self.store.put("client:" + client_info.client_id, client_info.model_dump(mode="json"))

    async def authorize(self, client, params):
        scopes = params.scopes or ["garmin:read"]
        if "garmin:read" not in scopes or set(scopes) - set(SCOPES):
            raise AuthorizeError("invalid_scope", "Only Garmin read access is supported")
        if params.resource != self.resource:
            raise AuthorizeError("invalid_request", "Unexpected resource")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError("invalid_request", "S256 PKCE is required")
        ticket = secrets.token_urlsafe(32)
        with self.store.transaction():
            if self.store.count("pending:") >= 256:
                raise AuthorizeError("temporarily_unavailable", "Please retry later")
            self.store.put("pending:" + digest(ticket), {
                "client_id": client.client_id, "name": client.client_name or "ChatGPT",
                "params": params.model_dump(mode="json"), "scopes": scopes,
                "expires": time.time() + 600,
            }, time.time() + 600)
        return self.origin + "/consent?" + urlencode({"ticket": ticket})

    def issue(self, client_id, scopes, family=None):
        family = family or secrets.token_urlsafe(32)
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        base = {"client_id": client_id, "scopes": scopes, "family": family, "resource": self.resource}
        self.store.put("access:" + digest(access), base | {"expires_at": int(time.time()) + 3600}, time.time() + 3600)
        self.store.put("refresh:" + digest(refresh), base | {"expires_at": int(time.time()) + 30 * 86400}, time.time() + 30 * 86400)
        return OAuthToken(access_token=access, refresh_token=refresh, token_type="Bearer",
                          expires_in=3600, scope=" ".join(scopes))

    async def load_authorization_code(self, client, authorization_code):
        grant = self.store.get("code:" + digest(authorization_code))
        if not grant or grant["client_id"] != client.client_id:
            return None
        return AuthorizationCode(code=authorization_code, **grant)

    async def exchange_authorization_code(self, client, authorization_code):
        key = "code:" + digest(authorization_code.code)
        with self.store.transaction():
            grant = self.store.get(key)
            if not grant or grant["client_id"] != client.client_id or grant["resource"] != self.resource:
                raise TokenError("invalid_grant", "Authorization expired or already used")
            self.store.delete(key)
            return self.issue(client.client_id, grant["scopes"])

    def revoked(self, grant):
        return self.store.get("revoked:" + grant["family"]) is not None

    async def load_access_token(self, token):
        grant = self.store.get("access:" + digest(token))
        if not grant or grant["resource"] != self.resource or self.revoked(grant):
            return None
        return AccessToken(token=token, **grant)

    async def load_refresh_token(self, client, refresh_token):
        grant = self.store.get("refresh:" + digest(refresh_token))
        if (not grant or grant["client_id"] != client.client_id
                or grant["resource"] != self.resource or self.revoked(grant)):
            return None
        return RefreshToken(token=refresh_token, **grant)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        key = "refresh:" + digest(refresh_token.token)
        with self.store.transaction():
            grant = self.store.get(key)
            if (not grant or grant["client_id"] != client.client_id
                    or grant["resource"] != self.resource or self.revoked(grant)):
                raise TokenError("invalid_grant", "Refresh token unavailable")
            if set(scopes) - set(grant["scopes"]):
                raise TokenError("invalid_scope", "Scope expansion is not allowed")
            if grant.get("used"):
                self.store.put("revoked:" + grant["family"], True, time.time() + 31 * 86400)
                tokens = None
            else:
                self.store.put(key, grant | {"used": True}, grant["expires_at"])
                tokens = self.issue(client.client_id, scopes, grant["family"])
        if tokens is None:
            raise TokenError("invalid_grant", "Refresh token reuse detected; reconnect")
        return tokens

    async def revoke_token(self, token):
        prefix = "access:" if isinstance(token, AccessToken) else "refresh:"
        grant = self.store.get(prefix + digest(token.token))
        if grant:
            self.store.put("revoked:" + grant["family"], True, time.time() + 31 * 86400)

    async def consent(self, request):
        form = await request.form() if request.method == "POST" else request.query_params
        ticket = form.get("ticket", "")
        if not isinstance(ticket, str):
            return PlainTextResponse("Invalid connection request", 400)
        key = "pending:" + digest(ticket)
        with self.store.transaction():
            grant = self.store.get(key)
            if not grant:
                return PlainTextResponse("Connection request expired. Start again in ChatGPT.", 400)
            if request.method == "GET":
                csrf = secrets.token_urlsafe(32)
                self.store.put(key, grant | {"csrf": digest(csrf)}, grant["expires"])
                response = HTMLResponse(
                    "<!doctype html><html lang='en'><meta charset='utf-8'><title>Connect Garmin Health</title>"
                    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                    "<h1>Connect Garmin Health</h1><p>Allow " + escape(grant["name"]) +
                    " to read your Garmin activity, sleep, wellness, weight and nutrition history.</p>"
                    "<p>This connection cannot change your Garmin records.</p>"
                    "<form method='post' action='/consent'>"
                    "<input type='hidden' name='ticket' value='" + escape(ticket, quote=True) + "'>"
                    "<input type='hidden' name='csrf' value='" + csrf + "'>"
                    "<label>Connection password <input name='password' type='password' autocomplete='current-password' required></label>"
                    "<p>Use the connection password you saved during setup.</p><button>Allow read access</button></form></html>"
                )
                response.set_cookie(COOKIE, csrf, secure=True, httponly=True, samesite="lax", max_age=600, path="/")
                return response
            csrf = form.get("csrf", "")
            password = form.get("password", "")
            if (request.headers.get("origin") != self.origin
                    or not isinstance(csrf, str) or not isinstance(password, str)
                    or not hmac.compare_digest(csrf, request.cookies.get(COOKIE, ""))
                    or not hmac.compare_digest(digest(csrf), grant.get("csrf", ""))
                    or not hmac.compare_digest(self.hash_password(password), self.password_hash)):
                return PlainTextResponse("Unable to authorize. Return and check your connection password.", 403)
            code = secrets.token_urlsafe(32)
            params = grant["params"]
            self.store.put("code:" + digest(code), {
                "client_id": grant["client_id"], "scopes": grant["scopes"], "expires_at": time.time() + 120,
                "redirect_uri": params["redirect_uri"], "code_challenge": params["code_challenge"],
                "redirect_uri_provided_explicitly": params["redirect_uri_provided_explicitly"], "resource": self.resource,
            }, time.time() + 120)
            self.store.delete(key)
            target = urlsplit(params["redirect_uri"])
            query = parse_qsl(target.query) + [("code", code), ("iss", self.issuer)]
            if params["state"] is not None:
                query.append(("state", params["state"]))
            response = RedirectResponse(urlunsplit(target._replace(query=urlencode(query))), status_code=303)
            response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
            return response


class GuardMiddleware:
    """Bound request bodies/rates, bind token requests to this resource, add safe headers."""

    def __init__(self, app, provider):
        self.app, self.provider = app, provider
        self.requests = defaultdict(deque)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path in ("/register", "/authorize", "/token", "/consent"):
            window, maximum = (900, 10) if path == "/consent" and scope["method"] == "POST" else (60, 120)
            recent = self.requests[(path, scope["method"])]
            now = time.monotonic()
            while recent and recent[0] < now - window:
                recent.popleft()
            if len(recent) >= maximum:
                return await PlainTextResponse("Please retry later", 429, headers={"Retry-After": str(window)})(scope, receive, send)
            recent.append(now)
        original_receive = receive
        if scope["method"] == "POST":
            body = bytearray()
            while True:
                message = await original_receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > 65536:
                    return await PlainTextResponse("Request too large", 413)(scope, original_receive, send)
                if not message.get("more_body", False):
                    break
            if path == "/token" and parse_qs(body.decode("utf-8", errors="replace")).get("resource") != [self.provider.resource]:
                return await JSONResponse({"error": "invalid_target"}, 400)(scope, original_receive, send)
            sent_body = False

            async def receive():
                nonlocal sent_body
                if not sent_body:
                    sent_body = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await original_receive()

        async def safe_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if path == "/authorize":
                    # RFC 9207 covers SDK-generated error redirects as well as consent success.
                    for i, (name, value) in enumerate(headers):
                        if name.lower() == b"location":
                            target = urlsplit(value.decode("latin-1"))
                            query = [(k, v) for k, v in parse_qsl(target.query) if k != "iss"]
                            query.append(("iss", self.provider.issuer))
                            headers[i] = (name, urlunsplit(target._replace(query=urlencode(query))).encode("latin-1"))
                headers.extend([(b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer"),
                                (b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
                                (b"content-security-policy", b"default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")])
                message = dict(message, headers=headers)
            await send(message)
        await self.app(scope, receive, safe_send)


def build_http_app(client, provider):
    registration = ClientRegistrationOptions(enabled=True, valid_scopes=SCOPES, default_scopes=["garmin:read"])
    revocation = RevocationOptions(enabled=True)
    issuer = AnyHttpUrl(provider.issuer)
    mcp = build_app(client, auth_server_provider=provider, stateless_http=True, json_response=True,
                    auth=AuthSettings(issuer_url=issuer, resource_server_url=AnyHttpUrl(provider.resource),
                                      required_scopes=["garmin:read"], client_registration_options=registration,
                                      revocation_options=revocation),
                    transport_security=TransportSecuritySettings(allowed_hosts=[urlsplit(provider.origin).netloc],
                                                                 allowed_origins=[provider.origin, "https://chatgpt.com"]))
    app = mcp.streamable_http_app()

    async def metadata(request):
        data = build_metadata(issuer, None, registration, revocation).model_dump(mode="json", exclude_none=True)
        data.update(authorization_response_iss_parameter_supported=True,
                    token_endpoint_auth_methods_supported=["none", "client_secret_post", "client_secret_basic"])
        return JSONResponse(data)

    async def health(request):
        return JSONResponse({"status": "ok"})

    app.routes.insert(0, Route("/.well-known/oauth-authorization-server", metadata))
    app.routes.extend([Route("/consent", provider.consent, methods=["GET", "POST"]), Route("/healthz", health)])
    app.add_middleware(GuardMiddleware, provider=provider)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(provider.origin).hostname])
    return app


def main():
    import uvicorn
    os.umask(0o077)
    origin = os.getenv("PUBLIC_URL") or "https://" + os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    password = os.environ.pop("OWNER_PASSWORD", "")
    redirects = [v.strip() for v in os.getenv("OAUTH_REDIRECT_URIS", STABLE_CALLBACK).split(",") if v.strip()]
    try:
        provider = OwnerOAuth(origin, os.getenv("OAUTH_DATA_DIR", "/data/oauth"), password, redirects)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    del password
    app = build_http_app(authenticated_client(), provider)
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")), access_log=False, log_level="warning")
