"""Pure operational helpers, intentionally independent of MetPy/ecCodes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


FAST_SEARCH_HOURS = 48
POSITIVE_RECHECK_MINUTES = 20
MARKER_FILE = Path(".ljlm_observations_updated")


def utc_datetime(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            return None
        return result.astimezone(timezone.utc)
    except (ValueError, AttributeError, TypeError):
        return None


def can_skip_recent_positive(cached, now, saved_complete):
    """Skip only a recent known-complete positive package in fast mode.

    The time limit forces periodic rechecking of positive packages in case
    a DWD object is replaced without its filename changing. Recovery mode
    always rescans them.
    """
    if not saved_complete or not isinstance(cached, dict):
        return False
    if cached.get("contains_ljlm") is not True:
        return False
    stamp = utc_datetime(cached.get("scanned_at"))
    if stamp is None:
        return False
    age = now - stamp
    return timedelta(0) <= age <= timedelta(minutes=POSITIVE_RECHECK_MINUTES)


def latest_may_advance(candidate, current):
    """Never replace a newer published sounding with an older profile.

    A newer launch may replace the old latest; equal launch times can be
    replaced with equal/better level coverage (reprocessing/metadata upgrades).
    """
    if not isinstance(current, dict) or not current:
        return True
    wanted = utc_datetime(candidate.get("launch_time"))
    existing = utc_datetime(current.get("launch_time"))
    if wanted is None:
        return False
    if existing is None:
        return True
    if wanted < existing:
        return False
    if wanted > existing:
        return True
    new_count = candidate.get("qc", {}).get("profile_level_count", 0)
    old_count = current.get("qc", {}).get("profile_level_count", 0)
    if isinstance(new_count, (int, float)) and isinstance(old_count, (int, float)):
        return new_count >= old_count
    return True


def read_json_optional(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            result = json.load(f)
        return result if isinstance(result, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def write_marker(count):
    MARKER_FILE.write_text(str(int(count)) + "\n", encoding="ascii")


def marker_has_updates():
    try:
        return int(MARKER_FILE.read_text(encoding="ascii").strip()) > 0
    except (OSError, ValueError):
        # Fail closed: missing/invalid marker must never suppress the full
        # publication pipeline.
        return True
