"""Pure operational helpers, intentionally independent of MetPy/ecCodes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


FAST_SEARCH_HOURS = 48
POSITIVE_RECHECK_MINUTES = 20
NEGATIVE_RECHECK_MINUTES = 90
NEGATIVE_FRESH_HOURS = 6
STATUS_HEARTBEAT_HOURS = 3
MARKER_FILE = Path(".ljlm_observations_updated")


def utc_datetime(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            return None
        return result.astimezone(timezone.utc)
    except (ValueError, AttributeError, TypeError):
        return None


def can_skip_negative(cached, filename_time, now):
    """Revisit recent negative BUFR packages, since DWD may replace files.

    Older packages remain cached in normal fast checks. Recovery scans bypass
    this optimization altogether. This gives a bounded correction window.
    """
    if not isinstance(cached, dict) or cached.get("contains_ljlm") is not False:
        return False
    if filename_time is None or filename_time.tzinfo is None:
        return False
    age = now - filename_time.astimezone(timezone.utc)
    if not timedelta(0) <= age <= timedelta(hours=NEGATIVE_FRESH_HOURS):
        return age > timedelta(hours=NEGATIVE_FRESH_HOURS)
    scan = utc_datetime(cached.get("scanned_at"))
    return (scan is not None and timedelta(0) <= now - scan
            < timedelta(minutes=NEGATIVE_RECHECK_MINUTES))


def stable_status_fields(value):
    """Strip heartbeat-only timestamps while retaining substantive status."""
    if isinstance(value, dict):
        return {k: stable_status_fields(v) for k, v in value.items()
                if k not in {"updated_at", "checked_at"}}
    if isinstance(value, list):
        return [stable_status_fields(v) for v in value]
    return value


def published_latest_graphs_current(root=Path(".")):
    """A no-change scan may skip rendering only if all latest plots match.

    This lets the next scan repair a previously interrupted publication.
    """
    profile = read_json_optional(root / "data/latest.json")
    manifest = read_json_optional(root / "diagnostics/latest_products.json")
    if not profile or not manifest:
        return False
    sounding_id = profile.get("sounding_id")
    if not sounding_id or sounding_id != manifest.get("sounding_id"):
        return False
    if not manifest.get("rendered_at"):
        return False
    expected = {
        "skewt": "diagnostics/latest_skewt.png",
        "skewt_zoom": "diagnostics/latest_skewt_zoom.png",
        "lowlevel": "diagnostics/latest_lowlevel.png",
        "thetae": "diagnostics/latest_thetae.png",
        "hodograph": "diagnostics/latest_hodograph.png",
    }
    return all(manifest.get(key) == path and (root / path).is_file()
               for key, path in expected.items())


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
