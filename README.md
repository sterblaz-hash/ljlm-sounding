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
