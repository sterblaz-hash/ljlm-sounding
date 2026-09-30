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
