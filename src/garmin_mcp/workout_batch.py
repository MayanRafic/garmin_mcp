"""Operator-only bounded workout publication, using existing consented delivery.

The host operator supplies an explicitly authorized manifest through the private
environment setting. This adds no public endpoint or generic Garmin write path.
Stable delivery keys provide the same restart/timeout protection as the MCP tool.
"""

import json
import os

from garmin_mcp.cloud import authenticated_client, main as cloud_main
from garmin_mcp.workout_delivery import Delivery, enabled, running_payload


def run_batch(manifest, client, directory):
    if not enabled() or os.getenv("GARMIN_TRANSPORT") != "https":
        raise ValueError("Batch delivery requires the existing enabled HTTPS workout service")
    if not isinstance(manifest, list) or not 1 <= len(manifest) <= 10:
        raise ValueError("Use one to ten explicitly authorized workouts")
    allowed = {"session_key", "calendar_date", "distance_km", "pace_fast_min_per_km", "pace_slow_min_per_km"}
    keys = set()
    for row in manifest:
        if not isinstance(row, dict) or set(row) - allowed or not {"session_key", "calendar_date", "distance_km"} <= set(row):
            raise ValueError("Invalid publication fields")
        if row["session_key"] in keys:
            raise ValueError("Duplicate session key in manifest")
        keys.add(row["session_key"])
        running_payload(row["session_key"], row["calendar_date"], row["distance_km"],
                        row.get("pace_fast_min_per_km"), row.get("pace_slow_min_per_km"))
    delivery = Delivery(client, directory)
    results = []
    for row in manifest:
        receipt = delivery.publish(**row)
        if receipt["status"] == "scheduled":
            receipt = delivery.status(row["session_key"])
        results.append(receipt)
    return results


def main():
    manifest = os.environ.pop("GARMIN_WORKOUT_BATCH_JSON", "").strip()
    if manifest:
        try:
            receipts = run_batch(json.loads(manifest), authenticated_client(),
                                 os.getenv("WORKOUT_DATA_DIR", "/data/workouts"))
            print("AUTHORIZED_WORKOUT_BATCH " + json.dumps(receipts), flush=True)
        except Exception:
            # No exception repr: parsing or upstream errors may contain tokens.
            print("AUTHORIZED_WORKOUT_BATCH failed; inspect receipts before retrying.", flush=True)
    # A failed publication must not take down history reads or the consent server.
    cloud_main()
