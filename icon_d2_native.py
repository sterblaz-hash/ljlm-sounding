"""Shared DWD ICON-D2 native model-level retrieval and conversions."""
from __future__ import annotations
import bz2
import json
import math
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import numpy as np
import metpy.calc as mpcalc
from metpy.units import units
from eccodes import codes_get, codes_get_array, codes_grib_new_from_file, codes_release
DWD_BASE = "https://opendata.dwd.de/weather/nwp/icon-d2/grib"
G0 = 9.80665
EPSILON = 0.62195691
USER_AGENT = "ljlm-sounding-icon-d2/0.1"

def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str) -> datetime:
    value = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fetch_bytes(url: str, timeout: int = 90) -> bytes:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_json(url: str, timeout: int = 90) -> dict[str, Any]:
    return json.loads(fetch_bytes(url, timeout=timeout).decode("utf-8"))


def safe_float(value: Any) -> float | None:
    try:
        x = float(value)
        if math.isfinite(x) and abs(x) < 1e30:
            return x
    except Exception:
        pass
    return None


def round_or_none(x: Any, ndigits: int = 2) -> float | None:
    x = safe_float(x)
    return None if x is None else round(x, ndigits)


def q_from_dewpoint(p_hpa: float, td_c: float) -> float | None:
    try:
        e = float(mpcalc.saturation_vapor_pressure(td_c * units.degC).to("hPa").magnitude)
        w = EPSILON * e / (p_hpa - e)
        return w / (1.0 + w)
    except Exception:
        return None


def dewpoint_from_q(p_hpa: float, q_kgkg: float) -> float | None:
    try:
        if q_kgkg <= 0 or q_kgkg >= 1:
            return None
        w = q_kgkg / (1.0 - q_kgkg)
        e = p_hpa * w / (EPSILON + w)
        return float(mpcalc.dewpoint(e * units.hPa).to("degC").magnitude)
    except Exception:
        return None


def theta_thetae(p_hpa: float, t_c: float, td_c: float | None) -> tuple[float | None, float | None]:
    theta = thetae = None
    try:
        theta = float(
            mpcalc.potential_temperature(p_hpa * units.hPa, t_c * units.degC)
            .to("kelvin").magnitude
        )
    except Exception:
        pass

    if td_c is not None:
        try:
            thetae = float(
                mpcalc.equivalent_potential_temperature(
                    p_hpa * units.hPa,
                    t_c * units.degC,
                    td_c * units.degC,
                ).to("kelvin").magnitude
            )
        except Exception:
            pass
    return theta, thetae


def uv_from_speed_dir(speed_ms: float, direction_deg: float) -> tuple[float, float]:
    r = math.radians(direction_deg)
    return -speed_ms * math.sin(r), -speed_ms * math.cos(r)


def speed_dir_from_uv(u: float, v: float) -> tuple[float, float]:
    speed = math.hypot(u, v)
    direction = (math.degrees(math.atan2(-u, -v)) + 360.0) % 360.0
    return speed, direction


def dwd_model_level_url(run: datetime, lead_hours: int, var: str, level: int) -> str:
    cycle = f"{run.hour:02d}"
    stamp = run.strftime("%Y%m%d%H")
    return (
        f"{DWD_BASE}/{cycle}/{var}/"
        f"icon-d2_germany_regular-lat-lon_model-level_"
        f"{stamp}_{lead_hours:03d}_{level}_{var}.grib2.bz2"
    )


def dwd_pressure_level_url(run: datetime, lead_hours: int, var: str, pressure_hpa: int) -> str:
    cycle = f"{run.hour:02d}"
    stamp = run.strftime("%Y%m%d%H")
    return (
        f"{DWD_BASE}/{cycle}/{var}/"
        f"icon-d2_germany_regular-lat-lon_pressure-level_"
        f"{stamp}_{lead_hours:03d}_{pressure_hpa}_{var}.grib2.bz2"
    )


def verify_dwd_run_is_live(run: datetime) -> None:
    """DWD Open Data keeps the current run for each cycle, not a full archive."""
    cycle = f"{run.hour:02d}"
    url = f"{DWD_BASE}/{cycle}/p/"
    html = fetch_bytes(url, timeout=90).decode("utf-8", errors="ignore")
    stamp = run.strftime("%Y%m%d%H")
    if stamp not in html:
        candidates = sorted(set(re.findall(r"model-level_(\d{10})_", html)))
        available = ", ".join(candidates[-3:]) if candidates else "unknown"
        raise RuntimeError(
            "Requested DWD native run is no longer/currently not available in the live Open Data tree. "
            f"Requested: {stamp}; visible in cycle {cycle}: {available}. "
            "Use a current LJLM sounding or override --run/--valid accordingly."
        )


def _decode_grib(payload_bz2: bytes) -> tuple[Any, bytes]:
    raw = bz2.decompress(payload_bz2)
    tmp = tempfile.NamedTemporaryFile(suffix=".grib2", delete=False)
    tmp.write(raw)
    tmp.close()
    f = open(tmp.name, "rb")
    gid = codes_grib_new_from_file(f)
    if gid is None:
        f.close()
        os.unlink(tmp.name)
        raise RuntimeError("No GRIB message found")
    return (gid, (f, tmp.name))


def _close_grib(gid: Any, holder: tuple[Any, str]) -> None:
    f, name = holder
    try:
        codes_release(gid)
    finally:
        f.close()
        try:
            os.unlink(name)
        except OSError:
            pass


def nearest_grid_index_from_payload(payload_bz2: bytes, latitude: float, longitude: float) -> tuple[int, float, float]:
    gid, holder = _decode_grib(payload_bz2)
    try:
        lats = np.asarray(codes_get_array(gid, "latitudes"), dtype=float)
        lons = np.asarray(codes_get_array(gid, "longitudes"), dtype=float)
        lon_delta = ((lons - longitude + 180.0) % 360.0) - 180.0
        metric = (lats - latitude) ** 2 + (lon_delta * math.cos(math.radians(latitude))) ** 2
        idx = int(np.nanargmin(metric))
        return idx, float(lats[idx]), float(lons[idx])
    finally:
        _close_grib(gid, holder)


def value_from_payload(payload_bz2: bytes, grid_index: int) -> tuple[float | None, dict[str, Any]]:
    gid, holder = _decode_grib(payload_bz2)
    try:
        values = np.asarray(codes_get_array(gid, "values"), dtype=float)
        value = safe_float(values[grid_index]) if grid_index < values.size else None
        meta = {}
        for key in ("units", "level", "step", "dataDate", "dataTime", "shortName", "typeOfLevel"):
            try:
                meta[key] = codes_get(gid, key)
            except Exception:
                pass
        return value, meta
    finally:
        _close_grib(gid, holder)


def fetch_one_dwd_value(url: str, grid_index: int) -> tuple[float | None, dict[str, Any], int]:
    payload = fetch_bytes(url, timeout=120)
    value, meta = value_from_payload(payload, grid_index)
    return value, meta, len(payload)


def fetch_dwd_native_profile(
    latitude: float,
    longitude: float,
    run: datetime,
    valid_time: datetime,
    top_hpa: float,
    workers: int,
) -> dict[str, Any]:
    lead_hours = int((valid_time - run).total_seconds() / 3600)
    if lead_hours < 0:
        raise ValueError("lead_hours cannot be negative")

    verify_dwd_run_is_live(run)

    t_start = time.perf_counter()
    compressed_bytes = 0

    # Establish the regular-grid point once from the near-surface pressure file.
    seed_url = dwd_model_level_url(run, lead_hours, "p", 65)
    seed_payload = fetch_bytes(seed_url, timeout=120)
    compressed_bytes += len(seed_payload)
    grid_index, grid_lat, grid_lon = nearest_grid_index_from_payload(seed_payload, latitude, longitude)
    p65, p65_meta = value_from_payload(seed_payload, grid_index)

    pressure_by_level: dict[int, float | None] = {65: p65}
    p_meta_by_level: dict[int, dict[str, Any]] = {65: p65_meta}

    # First fetch pressure at all 65 model levels. This lets us avoid downloading
    # T/QV/U/V above the requested top pressure.
    def get_p(level: int):
        url = dwd_model_level_url(run, lead_hours, "p", level)
        v, meta, nbytes = fetch_one_dwd_value(url, grid_index)
        return level, v, meta, nbytes

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(get_p, level) for level in range(1, 65)]
        for fut in as_completed(futures):
            level, value, meta, nbytes = fut.result()
            pressure_by_level[level] = value
            p_meta_by_level[level] = meta
            compressed_bytes += nbytes

    # Convert pressure Pa -> hPa when necessary.
    p_hpa_by_level: dict[int, float] = {}
    for level, value in pressure_by_level.items():
        if value is None:
            continue
        p_hpa = value / 100.0 if value > 2000 else value
        if math.isfinite(p_hpa):
            p_hpa_by_level[level] = p_hpa

    if not p_hpa_by_level:
        raise RuntimeError("DWD native pressure profile is empty")

    # Include one level above the requested top where possible to support interpolation.
    selected = sorted(
        [lvl for lvl, p in p_hpa_by_level.items() if p >= max(10.0, top_hpa - 40.0)]
    )
    if not selected:
        raise RuntimeError(f"No DWD model levels found below {top_hpa} hPa top")

    # Fetch native thermodynamic/wind variables only on selected levels.
    raw: dict[int, dict[str, float | None]] = {
        lvl: {"p_hpa": p_hpa_by_level[lvl]} for lvl in selected
    }

    tasks = []
    for level in selected:
        for var in ("t", "qv", "u", "v"):
            tasks.append((level, var))

    def get_var(level: int, var: str):
        url = dwd_model_level_url(run, lead_hours, var, level)
        v, meta, nbytes = fetch_one_dwd_value(url, grid_index)
        return level, var, v, meta, nbytes

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(get_var, level, var) for level, var in tasks]
        for fut in as_completed(futures):
            level, var, value, meta, nbytes = fut.result()
            compressed_bytes += nbytes
            raw[level][var] = value
            raw[level][f"{var}_units"] = meta.get("units")

    levels = []
    for model_level in selected:
        item = raw[model_level]
        p_hpa = item.get("p_hpa")
        t = item.get("t")
        qv = item.get("qv")
        u = item.get("u")
        v = item.get("v")

        if p_hpa is None or t is None:
            continue

        t_c = float(t) - 273.15 if float(t) > 150 else float(t)
        q = safe_float(qv)
        if q is not None and q > 0.2:  # defensive: g/kg -> kg/kg
            q /= 1000.0

        td_c = dewpoint_from_q(p_hpa, q) if q is not None else None
        theta, thetae = theta_thetae(p_hpa, t_c, td_c)

        ws = wd = None
        if u is not None and v is not None:
            ws, wd = speed_dir_from_uv(float(u), float(v))

        levels.append({
            "model_level": model_level,
            "pressure_hpa": round(p_hpa, 3),
            "temperature_c": round_or_none(t_c, 3),
            "dewpoint_c": round_or_none(td_c, 3),
            "specific_humidity_gkg": round_or_none(q * 1000.0 if q is not None else None, 4),
            "potential_temperature_k": round_or_none(theta, 3),
            "equivalent_potential_temperature_k": round_or_none(thetae, 3),
            "u_ms": round_or_none(u, 3),
            "v_ms": round_or_none(v, 3),
            "wind_speed_ms": round_or_none(ws, 3),
            "wind_direction_deg": round_or_none(wd, 2),
        })

    levels.sort(key=lambda x: x["pressure_hpa"], reverse=True)

    # Synoptic fields requested for the project: Z500 and 1000-500 hPa thickness.
    # FI is geopotential; convert to geopotential height by dividing by g0.
    synoptic = {}
    for pressure in (1000, 500):
        url = dwd_pressure_level_url(run, lead_hours, "fi", pressure)
        try:
            payload = fetch_bytes(url, timeout=120)
            compressed_bytes += len(payload)
            # Pressure-level regular grid may be the same grid, but determine nearest
            # independently to avoid silently assuming array identity.
            fi_index, _, _ = nearest_grid_index_from_payload(payload, latitude, longitude)
            fi, meta = value_from_payload(payload, fi_index)
            if fi is None:
                z = None
            else:
                unit_text = str(meta.get("units", "")).lower()
                if "m2" in unit_text or "m**2" in unit_text or "s-2" in unit_text or "s**-2" in unit_text:
                    z = fi / G0
                else:
                    # DWD FI is normally geopotential; numerical fallback prevents
                    # accidental 55 km Z500 if the units metadata is unusual.
                    z = fi / G0 if pressure == 500 and fi > 20000 else fi
            synoptic[f"z{pressure}_m"] = round_or_none(z, 1)
        except HTTPError as exc:
            if exc.code == 404:
                synoptic[f"z{pressure}_m"] = None
            else:
                raise

    z500 = synoptic.get("z500_m")
    z1000 = synoptic.get("z1000_m")
    thickness = z500 - z1000 if z500 is not None and z1000 is not None else None

    elapsed = time.perf_counter() - t_start
    return {
        "source": "dwd-open-data-native",
        "model": "icon_d2",
        "run_time": utc_iso(run),
        "valid_time": utc_iso(valid_time),
        "lead_hours": lead_hours,
        "requested_latitude": latitude,
        "requested_longitude": longitude,
        "grid_latitude": round(grid_lat, 5),
        "grid_longitude": round(grid_lon, 5),
        "grid_index": grid_index,
        "native_levels_count": len(levels),
        "native_levels_selected": selected,
        "top_pressure_requested_hpa": top_hpa,
        "compressed_download_mb": round(compressed_bytes / 1024 / 1024, 2),
        "fetch_seconds": round(elapsed, 2),
        "z500_m": round_or_none(z500, 1),
        "thickness_1000_500_m": round_or_none(thickness, 1),
        "levels": levels,
    }



def add_native_heights(profile: dict[str, Any], workers: int = 4) -> None:
    """Attach full-level midpoint heights from adjacent DWD HHL interfaces."""
    run = parse_utc(profile['run_time'])
    base = f"{DWD_BASE}/{run.hour:02d}/hhl/"
    listing = fetch_bytes(base).decode('utf-8')
    stamp = run.strftime('%Y%m%d%H')
    files = dict((int(level), name) for name, level in re.findall(
        rf'href="(icon-d2_germany_regular-lat-lon_time-invariant_{stamp}_000_(\d+)_hhl.grib2.bz2)"', listing))
    needed = {i for row in profile['levels'] for i in (row['model_level'], row['model_level'] + 1)}
    if not needed <= files.keys():
        raise RuntimeError('Native HHL interfaces are incomplete for the requested run')

    def get_height(level):
        payload = fetch_bytes(base + files[level], timeout=120)
        index, lat, lon = nearest_grid_index_from_payload(
            payload, profile['requested_latitude'], profile['requested_longitude'])
        if abs(lat - profile['grid_latitude']) > 1e-4 or abs(lon - profile['grid_longitude']) > 1e-4:
            raise RuntimeError('HHL and thermodynamic grid points differ')
        value, meta = value_from_payload(payload, index)
        if value is None or meta.get('units') != 'm' or int(meta['level']) != level:
            raise RuntimeError(f'Invalid native HHL interface {level}: {meta}')
        return level, value

    with ThreadPoolExecutor(max_workers=workers) as executor:
        heights = dict(executor.map(get_height, sorted(needed)))
    for row in profile['levels']:
        k = row['model_level']
        if heights[k] <= heights[k + 1]:
            raise RuntimeError('HHL interfaces are not ordered vertically')
        row['height_m'] = round((heights[k] + heights[k + 1]) / 2, 2)
    profile['height_method'] = 'Midpoint of adjacent native HHL interfaces, metres above mean sea level'
    profile['grid_surface_height_m'] = round(heights[66], 2)
    profile['height_source_urls'] = [base + files[k] for k in sorted(needed)]
