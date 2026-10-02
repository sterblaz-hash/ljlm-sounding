# ljlm-sounding
Automatic Ljubljana 14015 radiosonde retrieval and climatology

## Native ICON-D2 model sounding

```sh
python3 extract_icon_d2.py --lead-hours 3
# Or select an explicit live three-hour UTC run:
python3 extract_icon_d2.py --run 2026-09-30T09:00:00Z --lead-hours 3
```

Requires `requirements.txt`. The default selects the newest published cycle with
that lead available; an incomplete run fails without publishing a partial JSON.
DWD retains a rolling live inventory, so old runs may no longer be retrievable.

Outputs are isolated under `models/icon-d2/YYYYMMDDHH/`:

- `ljlm_f003.json`: native profile, provenance, and shared sounding diagnostics.
- `diagnostics/f003/`: Skew-T, low-level, theta-e and hodograph PNGs and manifest.

`icon_d2_native.py` holds the retrieval/GRIB decoding shared with
`test_icon_d2_sources.py`. `extract_icon_d2.py` adds HHL midpoint heights and
relative humidity, calls diagnostic functions from `extract_ljlm.py`, and uses
`render_ljlm.render_products` with an explicit model output directory.
No observational save, climatology, or comparison-to-observation workflow runs.

LJLM coordinates are 46.06562 N, 14.51221 E (the observed station location).
Native vertical levels are sampled at the nearest point of DWD's regular
latitude/longitude grid. Full-level heights use adjacent native HHL interface
midpoints. Surface-based diagnostics and AGL plots start at the lowest full
model level, not the actual ground; the JSON records model terrain separately.
The profile extends to 100 hPa with extra levels for interpolation. These are
model diagnostics, with no observational climatology ranking or balloon track.

## Operational +12 h forecasts for radiosonde verification

The separate `update-icon-d2-forecast.yml` workflow runs at **01:20 and
13:20 UTC**, targeting only:

| Model run | Lead | Verifying radiosonde term |
| --- | --- | --- |
| 00 UTC | +12 h | Same-day 12 UTC |
| 12 UTC | +12 h | Next-day 00 UTC |

The workflow supplies an explicit run timestamp. It retries that same timestamp
up to three times, five minutes apart, then fails if unavailable. It never calls
the extractor's automatic latest-cycle selection. Scheduling allows for the
roughly one-hour publication delay seen in the DWD live inventory; GitHub Actions
may start scheduled jobs late.

Manual testing: choose **Update ICON-D2 LJLM +12 h forecasts → Run workflow**,
select `00` or `12`, and optionally enter a UTC run date (`YYYY-MM-DD`). A blank
date selects the most recent occurrence of that chosen cycle. Old dates may have
expired from DWD's live inventory. Local equivalent:

```sh
python3 extract_icon_d2.py --run 2026-09-30T00:00:00Z --lead-hours 12 --publish-verification
python3 extract_icon_d2.py --run 2026-09-30T12:00:00Z --lead-hours 12 --publish-verification
python3 -m unittest test_icon_d2_pipeline test_icon_d2_forecast -v
```

Products remain separate from observations:

```text
models/icon-d2/YYYYMMDDHH/ljlm_f012.json
models/icon-d2/YYYYMMDDHH/diagnostics/f012/...
models/icon-d2/latest/00/sounding.json
models/icon-d2/latest/00/latest_products.json
models/icon-d2/latest/00/{skewt,lowlevel,skewt_zoom,thetae,hodograph}.png
models/icon-d2/latest/12/... (same files)
```

`latest/00` and `latest/12` refer to **valid time**, not run time. Both the profile
and graphics manifest retain `run_time`, `valid_time`, and `lead_hours: 12`.
Pair observations by the forecast's UTC valid date and term (not actual balloon
launch time). The latest manifest also includes `verification_date` and
`verification_term`. Always check these dates: a failed update leaves the last
successful forecast in place. Historical manual reruns cannot replace a newer
latest forecast.

Latest slots update only after extraction and rendering succeed. When installed,
the workflow uploads a 30-day artifact and commits only the requested `f012`
archive products and model latest slots to its branch. It does not stage or
change observational data, climatology, or diagnostics. Manual dispatch also
publishes model products; there is no automatic observed-versus-model scoring yet.

## Compact dashboard data

`build_dashboard_data.py` uses only the Python standard library and copies existing
OBS diagnostics without recalculating soundings or changing source JSON.

```sh
python build_dashboard_data.py --backfill
python build_dashboard_data.py --update data/latest.json
python -m unittest test_dashboard_data -v
```

Backfill explicitly reads `data/YYYY/MM/*.json`, rebuilds each source month's
compact summary, then overlays `data/latest.json`. Operational updates replace
one record by `sounding_id` in `timeseries/ljlm/archive/YYYY/MM.json`; they never
scan the full profile archive. Legacy profiles without an ID get one from their
stored nominal date and term. Missing/nonfinite metrics become JSON `null`, while
valid zero and negative values are retained. Orographic fields use the signed
**terrain-capped** IVT and signed fraction. IVT direction is the transport-to
azimuth in degrees, as stored in the source.

The four windows (`latest_7d.json`, `latest_30d.json`, `latest_90d.json`, and
`latest_1y.json`) read only compact summaries for the intersecting months (at most
13). They use inclusive UTC bounds from current time minus 7/30/90/365 days to
current time, based on **nominal date + term**, and sort chronologically. Each record includes `valid_time` as the nominal UTC timestamp (for example,
`2026-10-02T00:00:00Z`). Actual launch time is retained separately; 00 and 12 UTC records remain distinct.
`start_time` and `end_time` describe these requested bounds, even for empty or
partially populated windows. `--as-of 2026-10-02T12:00:00Z` provides a reproducible
window end; `--root PATH` selects a different repository root. Repeated runs are
record-idempotent; window timestamps and membership advance with current time.

`data/dashboard_manifest.json` advertises only existing latest OBS, status,
diagnostic, ICON-D2 +12 h slot, and window products. Its `variables` list describes
the supported numeric fields; individual records may have null values. Paths are
relative to the repository/web root. The manifest is refreshed by the OBS job;
ICON-D2 generation does not write OBS time-series. Initial backfill is needed to
include observations predating installation of the incremental update.

Examples: [compact record and manifest](dashboard_data_examples.md).

Historical climatology includes `lifted_index_c` in °C, using the same MetPy
surface-parcel definition as the operational sounding: `parcel_profile()` from
the lowest valid common pressure/temperature/dewpoint level, then
`lifted_index()` at 500 hPa. Common levels must start within 50 hPa of the
sounding bottom and bracket 500 hPa. Existing thermodynamic QC applies; only
GOOD profiles contribute to daily medians and climatological statistics.
Negative and zero LI values remain valid. Rebuild with `python build_climatology.py`;
LI then participates in observational climatology comparisons. Historical launch
times differ from the current 00/12 UTC schedule, as documented in the product metadata.
