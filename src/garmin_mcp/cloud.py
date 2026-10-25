"""Private, read-only Garmin MCP runtime with persistent authentication.

The default transport uses OpenAI Secure MCP Tunnel without a public MCP port.
GARMIN_TRANSPORT=https selects the alternative single-owner OAuth HTTP server.
GARMIN_SESSION_B64 is a one-time seed supplied through the host's secret store.
"""

import functools
import os
from pathlib import Path
import re
import sys
import tempfile
import threading

from garminconnect import Garmin
from garth.http import Client
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from garmin_mcp import activity_management, health_wellness, nutrition, training, weight_management


READ_TOOLS = frozenset({
    "count_activities", "get_activities", "get_activities_by_date", "get_activity",
    "get_activity_splits", "get_activity_hr_in_timezones", "get_activity_types",
    "get_stats", "get_user_summary", "get_daily_steps", "get_body_composition",
    "get_training_readiness", "get_morning_training_readiness", "get_body_battery",
    "get_rhr_day", "get_heart_rates_summary", "get_hydration_data",
    "get_sleep_data", "get_sleep_summary", "get_stress_summary",
    "get_respiration_summary", "get_weekly_steps", "get_weekly_stress",
    "get_weekly_intensity_minutes", "get_hrv_data", "get_training_status",
    "get_training_effect", "get_lactate_threshold", "get_endurance_score",
    "get_progress_summary_between_dates", "get_weigh_ins", "get_daily_weigh_ins",
    "get_nutrition_daily_food_log", "get_nutrition_daily_meals",
    "get_nutrition_daily_settings",
})
TOKEN_FILES = ("oauth1_token.json", "oauth2_token.json")


def save_tokens(client, directory: Path) -> None:
    """Save refreshed tokens with private permissions and atomic file replacement."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix=".garmin-tokens-", dir=directory.parent) as temp:
        client.dump(temp)
        for name in TOKEN_FILES:
            source = Path(temp) / name
            if not source.is_file() or not source.stat().st_size:
                raise ValueError("Incomplete Garmin authentication state")
            source.chmod(0o600)
        for name in TOKEN_FILES:
            os.replace(Path(temp) / name, directory / name)


def seed_tokens(directory: Path) -> None:
    """Import a secret once, preserving newer persisted tokens on later restarts."""
    seed = os.environ.pop("GARMIN_SESSION_B64", "").strip()
    present = [(directory / name).is_file() and (directory / name).stat().st_size > 0
               for name in TOKEN_FILES]
    if all(present):
        return
    if any(present):
        raise ValueError("Incomplete saved Garmin session; restore both token files")
    if not seed:
        raise ValueError("Set GARMIN_SESSION_B64 securely before first deployment")
    try:
        client = Client()
        client.loads(seed)
        save_tokens(client, directory)
    except Exception:
        # Token parser errors can contain secret values. Never echo them.
        raise ValueError("GARMIN_SESSION_B64 is not a valid Garmin session") from None


class PersistentGarmin:
    """Serialize SDK calls and preserve any refreshed session after each call."""

    def __init__(self, client, directory: Path):
        self.client = client
        self.directory = directory
        self.lock = threading.RLock()

    def __getattr__(self, name):
        method = getattr(self.client, name)
        if not callable(method):
            return method

        @functools.wraps(method)
        def call(*args, **kwargs):
            with self.lock:
                try:
                    return method(*args, **kwargs)
                finally:
                    save_tokens(self.client.garth, self.directory)
        return call


class ReadOnlyRegistration:
    """Register only explicitly reviewed getter tools, including MCP annotations."""

    def __init__(self, app):
        self.app = app

    def tool(self, *args, **kwargs):
        def register(function):
            if function.__name__ in READ_TOOLS:
                return self.app.tool(annotations=ToolAnnotations(
                    readOnlyHint=True, destructiveHint=False,
                    idempotentHint=True, openWorldHint=True,
                ))(function)
            return function
        return register


def build_app(client, **settings) -> FastMCP:
    app = FastMCP("Garmin Health", instructions=(
        "Read-only personal Garmin history. Paginate get_activities until complete; "
        "compare coverage against count_activities. Use daily sleep and HRV tools "
        "for recent trends. Missing wearable data is unknown, not zero. "
        "Wearable estimates cannot establish recovery from an injury."
    ), **settings)
    registrar = ReadOnlyRegistration(app)
    for module in (activity_management, health_wellness, training, weight_management, nutrition):
        module.configure(client)
        module.register_tools(registrar)
    return app


def authenticated_client():
    directory = Path(os.getenv("GARMINTOKENS", "/data/garmin")).expanduser()
    try:
        seed_tokens(directory)
        client = Garmin(is_cn=os.getenv("GARMIN_IS_CN", "false").lower() == "true")
        client.login(str(directory))
        save_tokens(client.garth, directory)
    except Exception:
        print("Garmin session unavailable; check secret setup or reauthenticate.", file=sys.stderr)
        raise SystemExit(1) from None
    return PersistentGarmin(client, directory)


def readonly_main() -> None:
    build_app(authenticated_client()).run(transport="stdio")


def tunnel_command():
    if not re.fullmatch(r"tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}",
                        os.getenv("CONTROL_PLANE_TUNNEL_ID", "")):
        raise ValueError("Set a valid CONTROL_PLANE_TUNNEL_ID")
    if not os.getenv("CONTROL_PLANE_API_KEY"):
        raise ValueError("Set CONTROL_PLANE_API_KEY in the host's secret store")
    return ["tunnel-client", "run", "--mcp.command=garmin-mcp-readonly",
            "--health.listen-addr=127.0.0.1:8080", "--log.level=warn"]


def main() -> None:
    if os.getenv("GARMIN_TRANSPORT", "tunnel") == "https":
        from garmin_mcp.http_runtime import main as http_main
        http_main()
        return
    try:
        command = tunnel_command()
        seed_tokens(Path(os.getenv("GARMINTOKENS", "/data/garmin")).expanduser())
    except ValueError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    os.execvp(command[0], command)
