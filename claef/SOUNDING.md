# Manual C-LAEF LJLM sounding

From the repository root, with the renderer requirements and optional `h5py`
installed in the active Python environment:

```sh
python extract_claef.py --run 2026-10-09T00:00Z --lead-hours 12
```

To also publish the 12 UTC verification slot:

```sh
python extract_claef.py --run 2026-10-09T00:00Z --lead-hours 12 --publish-verification
```

The JSON is saved at `models/claef/2026100900/ljlm_f012.json`; graphics and their
manifest are under `models/claef/2026100900/diagnostics/f012/`. Publication copies
the sounding, graphics and manifest into `models/claef/latest/12/`. A 12 UTC run
at +12 h publishes into `latest/00/` on the next valid date. Historical reruns
cannot replace a newer valid time in a slot. Publication requires an explicit
exact 00 or 12 UTC run and exactly +12 h. An unavailable run fails without trying
another cycle. Public bulk listings retain only a limited set of recent runs.

The source is the deterministic public 11-level pressure product (1000, 950,
925, 900, 850, 800, 700, 600, 500, 400, 300 hPa), sampled at the nearest LJLM grid
point. No levels are interpolated or added above 300 hPa. Pressure levels below
model terrain are excluded and recorded in `excluded_pressure_levels_hpa`.
The synthetic surface row uses model surface pressure/geopotential, 2 m
temperature/humidity and 10 m wind. Its height is model terrain, not station
elevation or the height of every near-surface measurement.

Standard-level T/Td/RH use actual retained 850/700/500 hPa rows. Parcel CAPE/CIN,
tropopause, Bunkers/SRH and detailed inversions are unavailable. Parcel drawing
is disabled explicitly in the profile; existing profiles retain their behavior.
Omega is retained in Pa s-1 at each pressure level. Magnus dew point and
geopotential height divided by standard gravity are approximate derived fields.
Missing source values stay null; they are never interpolated.

Extraction reuses the existing HTTP Range client with its unchanged 64 MiB
transfer, 80-request, 16 MiB range and 32 MiB decoded-chunk ceilings, ETag checks,
rate limit and no-retry policy. Exceeding a limit fails without a full-file
fallback. JSON replacement is atomic; latest publication starts only after
rendering completes. Rendering failure may leave the archive JSON, but does not
publish an incomplete latest slot. `h5py` remains an optional dependency for the
older proof-of-concept CLI and is required for this sounding command.

This manual step does not update workflows or point-cache products.
