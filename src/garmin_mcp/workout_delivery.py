"""Opt-in running workout delivery with durable, fail-closed retry handling."""

from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading

from mcp.types import ToolAnnotations


def enabled():
    return os.getenv("GARMIN_ENABLE_WORKOUTS", "false").lower() == "true"


def running_payload(key, calendar_date, distance_km, pace_fast_min_per_km=None, pace_slow_min_per_km=None):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key):
        raise ValueError("Use a stable session key of 1–80 letters, digits, hyphens or underscores")
    if date.fromisoformat(calendar_date).isoformat() != calendar_date:
        raise ValueError("Use YYYY-MM-DD")
    if isinstance(distance_km, bool) or not math.isfinite(distance_km) or not 0.5 <= distance_km <= 42.195:
        raise ValueError("Distance must be between 0.5 and 42.195 km")
    if (pace_fast_min_per_km is None) != (pace_slow_min_per_km is None):
        raise ValueError("Supply both pace bounds or neither")
    if pace_fast_min_per_km is not None:
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in (pace_fast_min_per_km, pace_slow_min_per_km)):
            raise ValueError("Pace bounds must be finite minutes per kilometre")
        if not 2 <= pace_fast_min_per_km < pace_slow_min_per_km <= 20:
            raise ValueError("Pace bounds must be ordered fastest to slowest, between 2 and 20 min/km")
    sport = {"sportTypeId": 1, "sportTypeKey": "running"}
    payload = {
        "workoutName": f"TLV {calendar_date} {distance_km:g}km [{key}]",
        "description": "Easy conversational effort. First and last portions easy; slow or walk as needed. Stop if hip pain returns. Distance includes all running warmup and cooldown.",
        "sportType": sport,
        "workoutSegments": [{"segmentOrder": 1, "sportType": sport, "workoutSteps": [{
            "type": "ExecutableStepDTO", "stepOrder": 1,
            "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
            "endCondition": {"conditionTypeId": 3, "conditionTypeKey": "distance"},
            "endConditionValue": round(distance_km * 1000, 3),
            "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
        }]}],
    }
    if pace_fast_min_per_km is not None:
        # Garmin uses speeds in m/s for absolute pace targets. Its pace payload
        # stores the fast boundary in One and the slow boundary in Two.
        edge = min(1000.0, round(distance_km * 1000 / 4, 3))
        def step(order, metres, kind, target):
            return {"type": "ExecutableStepDTO", "stepOrder": order,
                    "stepType": {"stepTypeId": kind[0], "stepTypeKey": kind[1]},
                    "endCondition": {"conditionTypeId": 3, "conditionTypeKey": "distance"},
                    "endConditionValue": metres, **target}
        free = {"targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}}
        pace = {"targetType": {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone"},
                "targetValueOne": 1000 / (60 * pace_fast_min_per_km),
                "targetValueTwo": 1000 / (60 * pace_slow_min_per_km)}
        payload["workoutSegments"][0]["workoutSteps"] = [
            step(1, edge, (1, "warmup"), free),
            step(2, round(distance_km * 1000 - 2 * edge, 3), (3, "interval"), pace),
            step(3, edge, (2, "cooldown"), free),
        ]
        payload["description"] += (
            f" Main step has a broad pace advisory: {_pace_label(pace_fast_min_per_km)}–{_pace_label(pace_slow_min_per_km)}/km. "
            "Warmup/cooldown have no pace alert. Conversational effort takes priority: "
            "slow down or walk rather than speeding up to satisfy an alert when fatigued."
        )
    return payload


def _pace_label(minutes):
    seconds = round(minutes * 60)
    return f"{seconds // 60}:{seconds % 60:02d}"


def verify_pace_steps(expected, observed):
    """Require read-back evidence for every pace step, including free edges."""
    wanted = expected["workoutSegments"][0]["workoutSteps"]
    if not any(s["targetType"]["workoutTargetTypeKey"] == "pace.zone" for s in wanted):
        return True  # Preserve the historical untargeted-run verification.
    try:
        actual = observed["workoutSegments"][0]["workoutSteps"]
        if len(actual) != len(wanted):
            return False
        for a, b in zip(wanted, actual):
            if (a["stepOrder"] != b["stepOrder"] or
                    a["stepType"]["stepTypeKey"] != b["stepType"]["stepTypeKey"] or
                    a["endCondition"]["conditionTypeKey"] != b["endCondition"]["conditionTypeKey"]):
                return False
            if a["targetType"]["workoutTargetTypeKey"] != b["targetType"]["workoutTargetTypeKey"]:
                return False
            if not math.isclose(a["endConditionValue"], float(b["endConditionValue"]), abs_tol=0.01):
                return False
            for field in ("targetValueOne", "targetValueTwo"):
                if field in a and not math.isclose(a[field], float(b[field]), rel_tol=1e-5):
                    return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def schedule_identity(item):
    if not isinstance(item, dict):
        raise ValueError("Unrecognized Garmin calendar item")
    workout = item.get("workout") or {}
    return (str(item.get("workoutId") or workout.get("workoutId") or ""),
            str(item.get("calendarDate") or item.get("date") or "")[:10])


class Delivery:
    def __init__(self, client, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        path = directory / "workout-delivery.sqlite3"
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
        path.chmod(0o600)
        self.db.execute("CREATE TABLE IF NOT EXISTS deliveries (key TEXT PRIMARY KEY, fingerprint TEXT, receipt TEXT)")
        self.client = client
        self.lock = threading.RLock()

    def save(self, key, fingerprint, receipt):
        self.db.execute("INSERT OR REPLACE INTO deliveries VALUES (?, ?, ?)",
                        (key, fingerprint, json.dumps(receipt)))

    def calendar(self, day):
        items = self.client.get_scheduled_workouts_by_period(day, day)
        if not isinstance(items, list):
            raise ValueError("Garmin calendar response was not a list")
        return items

    def publish(self, session_key, calendar_date, distance_km, pace_fast_min_per_km=None, pace_slow_min_per_km=None):
        payload = running_payload(session_key, calendar_date, distance_km, pace_fast_min_per_km, pace_slow_min_per_km)
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        # Reserve before the first remote write. Crashes/timeouts never trigger a
        # blind second upload or schedule, including across process restarts.
        with self.lock:
            receipt = {"status": "needs_review", "session_key": session_key,
                       "date": calendar_date, "distance_km": distance_km,
                       "message": "Delivery outcome uncertain; inspect before retrying. No automatic repeat write."}
            if pace_fast_min_per_km is not None:
                receipt.update(pace_fast_min_per_km=pace_fast_min_per_km, pace_slow_min_per_km=pace_slow_min_per_km)
            try:
                self.db.execute("INSERT INTO deliveries VALUES (?, ?, ?)",
                                (session_key, fingerprint, json.dumps(receipt)))
            except sqlite3.IntegrityError:
                old_fingerprint, raw = self.db.execute(
                    "SELECT fingerprint, receipt FROM deliveries WHERE key=?", (session_key,)).fetchone()
                if old_fingerprint != fingerprint:
                    raise ValueError("Session key already belongs to another workout; remove/review it before replacing")
                return json.loads(raw)
            try:
                result = self.client.upload_workout(payload)
                workout_id = result.get("workoutId") if isinstance(result, dict) else None
                if not isinstance(workout_id, int) or isinstance(workout_id, bool) or workout_id <= 0:
                    raise ValueError("Upload did not return a workout ID")
                receipt["workout_id"] = workout_id
                self.save(session_key, fingerprint, receipt)
                observed = self.client.get_workout_by_id(workout_id)
                if not isinstance(observed, dict) or observed.get("workoutName") != payload["workoutName"]:
                    raise ValueError("Uploaded workout could not be verified")
                if not verify_pace_steps(payload, observed):
                    raise ValueError("Garmin pace steps could not be verified")
                if not any(schedule_identity(i) == (str(workout_id), calendar_date) for i in self.calendar(calendar_date)):
                    self.client.schedule_workout(workout_id, calendar_date)
                if not any(schedule_identity(i) == (str(workout_id), calendar_date) for i in self.calendar(calendar_date)):
                    raise ValueError("Schedule could not be verified")
                receipt.update(status="scheduled", message="Verified in Garmin Connect. Watch delivery still depends on device compatibility and normal sync.")
            except Exception:
                # Library errors can contain sensitive transport data. Retain only
                # known IDs and never claim a failed/ambiguous request did nothing.
                pass
            self.save(session_key, fingerprint, receipt)
            return receipt

    def status(self, session_key):
        with self.lock:
            row = self.db.execute("SELECT receipt FROM deliveries WHERE key=?", (session_key,)).fetchone()
            if not row:
                return {"status": "not_found"}
            receipt = json.loads(row[0])
            if receipt.get("workout_id") and receipt["status"] != "removed":
                present = any(schedule_identity(i) == (str(receipt["workout_id"]), receipt["date"])
                              for i in self.calendar(receipt["date"]))
                return receipt | {"currently_scheduled": present}
            return receipt

    def remove(self, session_key):
        with self.lock:
            row = self.db.execute("SELECT fingerprint, receipt FROM deliveries WHERE key=?", (session_key,)).fetchone()
            if not row:
                raise ValueError("Only workouts created by this connection can be removed")
            fingerprint, raw = row
            receipt = json.loads(raw)
            if receipt["status"] == "removed":
                return receipt
            workout_id = receipt.get("workout_id")
            if not workout_id:
                raise ValueError("Upload outcome unknown; inspect Garmin Connect manually")
            for item in self.calendar(receipt["date"]):
                if schedule_identity(item) == (str(workout_id), receipt["date"]):
                    schedule_id = item.get("workoutScheduleId") or item.get("scheduleId")
                    if not isinstance(schedule_id, int) or isinstance(schedule_id, bool) or schedule_id <= 0:
                        raise ValueError("Cannot identify schedule safely; inspect Garmin Connect manually")
                    self.client.unschedule_workout(schedule_id)
            if any(schedule_identity(i) == (str(workout_id), receipt["date"]) for i in self.calendar(receipt["date"])):
                raise ValueError("Calendar removal not yet verified")
            self.client.delete_workout(workout_id)
            # Exhaust the library to verify removal, rather than treating an
            # arbitrary lookup failure as proof of deletion.
            for start in range(0, 10000, 100):
                page = self.client.get_workouts(start=start, limit=100)
                if not isinstance(page, list):
                    raise ValueError("Cannot verify workout removal")
                if any(str(w.get("workoutId")) == str(workout_id) for w in page):
                    raise ValueError("Workout remains in Garmin library")
                if len(page) < 100:
                    break
            else:
                raise ValueError("Workout library scan incomplete")
            receipt.update(status="removed", message="Removed from Garmin Connect; sync the watch to update device contents.")
            self.save(session_key, fingerprint, receipt)
            return receipt


def register_tools(app, client):
    delivery = Delivery(client, os.getenv("WORKOUT_DATA_DIR", "/data/workouts"))
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)

    @app.tool(annotations=write)
    async def publish_running_workout(session_key: str, calendar_date: str, distance_km: float,
                                      pace_fast_min_per_km: float | None = None,
                                      pace_slow_min_per_km: float | None = None) -> dict:
        """Create and schedule an easy distance run. Reuse the SAME session_key on retries.

        Dates use the athlete's Garmin calendar date. Includes all running warmup/cooldown.
        Requires user authorization; needs_review means inspect, never repush under a new key.
        Success verifies Garmin Connect only, not delivery to the watch.
        Optional pace bounds are decimal minutes/km, fastest then slowest (6.25 = 6:15).
        Both bounds are required together. Pace is advisory; conversational effort wins.
        Targeted runs have untargeted warmup and cooldown within the total distance.
        """
        return delivery.publish(session_key, calendar_date, distance_km, pace_fast_min_per_km, pace_slow_min_per_km)

    @app.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def get_workout_delivery(session_key: str) -> dict:
        """Read a saved delivery receipt and check its current Garmin calendar entry."""
        return delivery.status(session_key)

    @app.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
    async def remove_running_workout(session_key: str) -> dict:
        """Undo only a run created by this connection. Requires user authorization."""
        return delivery.remove(session_key)
