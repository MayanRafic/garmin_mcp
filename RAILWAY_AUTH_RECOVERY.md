# Recover the Railway Garmin connection

The production service is `garmin-mcp` in Railway project `health-agent`, on
branch `setup/railway-garmin-mcp`. The original runtime uses garminconnect 0.2.38
and garth 0.5.21. The failure observed October 31, 2026 is a 429 while refreshing
access at `/oauth-service/oauth/exchange/user/2.0`, before the activity API call.

Garth is deprecated after Garmin changed its authentication flow:
https://github.com/matin/garth/releases/tag/v0.8.0
The maintained Garmin SDK uses native DI authentication and explicitly rejects
old garth token stores. Old OAuth1/OAuth2 tokens cannot be converted.

This change uses garminconnect 0.3.20, native session storage in
`/data/garmin/garmin_tokens.json`, and DI refresh-token rotation. Existing OAuth
consent grants in `/data/oauth`, server URL, read-only tools, and old token files
are preserved. Python 3.12 or newer is required (the Railway image already uses
3.12). A rate-limited API or DI refresh pauses Garmin calls for 15 minutes in the
running process; it does not delete tokens or repeatedly call the exchange.

## One-time sign-in, on your own computer

After the fix branch is available, run:

```sh
uvx --python 3.12 --from git+https://github.com/MayanRafic/garmin_mcp@fix/garmin-auth-rate-limit garmin-mcp-auth --force-reauth
```

The CLI prompts for your Garmin email, password and MFA if required. It saves a
native session locally and an owner-readable base64 export at
`~/.garminconnect_base64` (unless `GARMINTOKENS_BASE64` overrides it). The export
is an account credential: enter its contents only in the existing Railway
`GARMIN_SESSION_B64` variable, not in a chat, issue, PR, or repository file.

Coordinate the secret update with deployment of this fix. The old runtime will
continue using its existing saved tokens, ignoring a new seed on restart; the
updated runtime imports the new seed when the native token file is first absent.
Do not delete the `/data` volume or alter `OWNER_PASSWORD`, `PUBLIC_URL`, or
`OAUTH_REDIRECT_URIS`. Deployment before the new session is supplied will fail
authentication; code changes alone cannot restore access.

## Verify after the session update and deployment

1. Confirm Railway reports the new deployment as `SUCCESS`.
2. Call `count_activities` once and verify it returns an activity total.
3. Read one day's sleep or HRV and confirm real data access.
4. If Garmin still returns 429, allow the cooldown to expire before retrying.

Automated tests use synthetic sessions and mocked network responses. They verify
native refresh and persistence, refusal of old seed formats without secret
disclosure, preservation of old token files, cooldown, read-only tool discovery,
and the existing HTTP OAuth consent flow. Live account access requires the
owner's one-time sign-in and is not established by passing these tests.
