# Garmin MCP on Railway

This runs the Garmin history tools on a persistent Railway service, independently
of a laptop. It supports a private OpenAI Secure MCP Tunnel, or a directly hosted
HTTPS endpoint protected by single-owner OAuth. Both expose the same read tools.

The direct HTTPS option is prepared for the current deployment because the
Platform key-creation UI does not expose the documented restricted Tunnels scopes.
No broad OpenAI API key or admin key is required for this option. The existing
tunnel can remain unused; it does not need to be deleted.

The cloud entry point explicitly registers 35 read tools. It does not expose
weight deletion, food logging, activity uploads or other Garmin write actions.
It does not schedule Garmin-to-Notion syncing or change a training plan.

## Existing resources

- GitHub: `MayanRafic/garmin_mcp`
- Railway project: `427a9fbb-4b7b-4ff7-919f-3204d7ce1321`
- Production environment: `f01cd560-24e8-42da-9ae5-e62894245f12`
- Service `garmin-mcp`: `cba4b331-9ed8-4f51-80bc-1955effee7fa`
- Persistent volume: mount `/data`, initially 100 MB.

The project's initial display name is `health-agent`; it can be renamed to
`garmin-mcp` in Railway settings without changing the IDs. The separate
`whatsapp-context` project is not involved.

## Direct HTTPS connection (current deployment)

Use `GARMIN_TRANSPORT=https`, `PORT=8080`, the same persistent `/data` volume and
Garmin session already staged in Railway. Set `OWNER_PASSWORD` directly in Railway
to a new password of at least 10 characters. No particular character mix is
required. Use a unique password for this connection. Railway
stores the value, and a separate password manager is optional. This is the
password for approving the ChatGPT connection, not the Garmin password. Never
paste it into chat. `CONTROL_PLANE_API_KEY` is not used in HTTPS mode.

Once the staged service exists live, generate its Railway HTTPS domain targeting
port 8080. The runtime derives its canonical origin from `RAILWAY_PUBLIC_DOMAIN`;
alternatively set `PUBLIC_URL` explicitly to that exact HTTPS origin. A new staged
service cannot receive a domain before its first deployment. If the initial start
stops for a missing domain, generate the domain and redeploy the same pinned source.
The runtime fails closed until the origin and owner password are configured.

In the **adva** ChatGPT workspace, add a custom MCP server named **Garmin**, using
the generated `https://<railway-domain>/mcp` URL, OAuth, and dynamic client
registration. Discovery advertises S256 PKCE and issuer identification (RFC 9207),
with the stable ChatGPT callback `https://chatgpt.com/connector_platform_oauth_redirect`.
Confirm the management page shows that exact callback; if it shows another URI,
put that exact URI in `OAUTH_REDIRECT_URIS` and redeploy before linking. This setting
is a comma-separated allowlist; arbitrary callbacks are rejected. Then sign in
using the new connection password and explicitly allow read access.

The server validates owner consent, CSRF, PKCE, client, resource, expiry and scope.
OAuth clients and grant state persist in `/data/oauth/oauth.sqlite3` with private
file permissions; access, refresh and authorization-code values are stored as
hashes. Refresh tokens rotate; detected reuse revokes the token family. Request
bodies and authorization rates are bounded. Access logging is disabled so callback
parameters and tokens do not enter HTTP access logs. The Garmin session and OAuth
approval state remain separate.

Public discovery and `/healthz` contain no health data. `/mcp` requires a valid
`garmin:read` access token, including for tool discovery. Verify an unauthenticated
request returns 401, then perform a real `count_activities` and small activity-page
read through ChatGPT. A health response or local mocked test is not live Garmin
verification.

## Private tunnel deployment (alternative)

Use `Dockerfile.railway` and `railway.json`, one replica, sleeping disabled.
No public domain or incoming TCP proxy is required. The container makes outbound
HTTPS requests to Garmin and the OpenAI tunnel service.

Non-secret variables:

| Variable | Value |
| --- | --- |
| `GARMINTOKENS` | `/data/garmin` |
| `TZ` | `Asia/Jerusalem` |
| `CONTROL_PLANE_TUNNEL_ID` | The complete tunnel ID from OpenAI Platform |

Protected secret variables:

| Variable | Purpose |
| --- | --- |
| `GARMIN_SESSION_B64` | One-time import of the already-authenticated Garmin session |
| `CONTROL_PLANE_API_KEY` | Restricted OpenAI tunnel runtime key, with Tunnels Read + Use |

Keep these values out of chat, Git, build arguments, screenshots and command-line
arguments. Enter them directly into Railway's protected service variables.

## Transfer the Garmin session from the authenticated Mac

Authentication has already succeeded on the user's Mac. In that Mac's Terminal:

```bash
pbcopy < "$HOME/.garminconnect_base64"
```

This copies the session to the clipboard without displaying it. Paste it directly
into the Railway service variable `GARMIN_SESSION_B64`. Do not paste it into chat.
Clear the clipboard after saving:

```bash
printf '' | pbcopy
```

On the first start, the cloud runtime validates the session structure and imports
it to `/data/garmin` with private file permissions. Later starts prefer the saved
session, so an old seed cannot overwrite refreshed tokens. The seed is removed
from the process environment before the tunnel process starts.

After a successful live read and a restart verification, remove the seed variable
from Railway; the persistent volume then holds the current session. Reauthentication
can still be needed if Garmin revokes or expires a session. Restore a full new
session through a controlled maintenance flow, not by deleting a volume blindly.

## Create the private ChatGPT connection

1. Open [OpenAI Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels).
   Create a tunnel associated with the intended ChatGPT workspace and set its ID
   as `CONTROL_PLANE_TUNNEL_ID` in Railway. Creating a tunnel needs Tunnels Read +
   Manage. If this surface is unavailable, resolve account permissions or use an
   authenticated remote MCP deployment instead; do not expose an unauthenticated
   public server as a workaround.
2. Create a restricted [runtime key](https://platform.openai.com/settings/organization/api-keys)
   with Tunnels Read + Use for that tunnel. Save it directly as the protected
   `CONTROL_PLANE_API_KEY` variable in Railway. Do not use an admin key for the
   long-lived runtime.
3. Review the staged Railway changes and deploy. Follow build and runtime logs
   until the deployment reaches `SUCCESS`; a queued build is not verification.
4. In ChatGPT Plugins, add a custom MCP server named `Garmin`, choose Connection:
   Tunnel, select the same tunnel ID, and install the private plugin. The stdio
   server has no separate OAuth layer; access is through the workspace-scoped
   private tunnel. Only intended users should have tunnel-use permission.
5. Make a read request from the ChatGPT conversation, starting with
   `count_activities` and a small `get_activities` page. Only a successful real
   tool call establishes that the account is connected.

See the [official Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

## Verification and history review

For a trusted operator shell, tunnel health endpoints listen on loopback port
8080: `/healthz` and `/readyz`. They are not exposed as public Railway domains.
Inspect readiness as well as process health. The MCP child authenticates when
started, so a healthy tunnel process alone does not establish Garmin access.

For the baseline, count and paginate through all recorded activities and compare
the observed count and date coverage. Review recent 4-, 8- and 12-week activity
trends, then compare older running periods. Review sleep, resting HR, HRV,
readiness and recorded weight where the account and watch provide them. Explicitly
report missing data and unworn-watch periods. Garmin estimates are not medical
clearance to return after a leg injury.

The first deployment does not automatically write to Notion. A scheduled,
deduplicated sync with separate Notion credentials is a subsequent step.

## Build and test

```bash
uv sync --locked
uv run pytest tests/unit tests/integration -q
docker build -f Dockerfile.railway -t garmin-mcp:cloud .
```

The optional `proxy_ca` build secret supports builds behind a managed HTTPS
proxy without baking that environment's CA into the image. Normal Railway builds
do not need this secret. The tunnel-client installer verifies the release archive
against its published SHA256 and keeps the binary version pinned.
