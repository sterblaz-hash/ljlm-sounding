# LJLM C-LAEF pressure-profile experiment

## Result and source

Efficient bounded extraction is feasible, although the file is not optimized for individual points. A single sparse profile was extracted from the official deterministic bulk NetCDF using guarded HTTP Range reads and local HDF5 decompression, with no complete-file download. The example is `models/claef/profiles/latest/profile_f012.json`.

The selected latest listed suitable file was:

- Dataset: `nwp-v2-1h-1km`.
- Object: `resources/nwp-v2-1h-1km/filelisting/nwp_2026100903.nc`.
- [Official bulk listing](https://public.hub.geosphere.at/public/datahub.html?id=nwp-v2-1h-1km/filelisting).
- Model reference: **2026-10-09 03:00 UTC**.
- Lead: **+12 h**; valid **2026-10-09 15:00 UTC**.
- Source object size: **9,442,193,799 bytes** (~9.44 GB).
- ETag: `9f73d47b1260fdd9ec964948dbb593e1-181`.

This latest-cycle example verifies at 15 UTC, not at a regular 00/12 UTC observation. It establishes extraction cost and structure; it is not an observation/model verification result.

## Official access investigation

A. The [official grid forecast metadata](https://dataset.api.hub.geosphere.at/v1/grid/forecast/nwp-v2-1h-1km/metadata) advertises 16 surface parameters: `10fg`, `10u`, `10v`, `2r`, `2t`, `cape`, `msl`, `pt`, `rain`, `sf`, `snowlmt`, `ssrd`, `sund`, `sy`, `tcc`, `tp`. Neither grid nor already validated point metadata advertises the required pressure variables. The [OpenAPI request schema](https://dataset.api.hub.geosphere.at/v1/openapi.json) supports bounding-box/time/parameter subsets for grid data, but has no pressure/native-level selector. There is no documented pressure-profile subset endpoint in the inspected official resources; no guessed-variable API requests were made.

B. The official dataset links to an HTTP object listing and public NetCDF files. No official OPeNDAP, NCSS or other server-side pressure-level slicing service was documented by the inspected links/schema. This is an evidence-limited finding, not a claim that every possible GeoSphere service has been ruled out.

C. The official object server honors byte ranges with HTTP 206 and correct Content-Range/ETag. This was the workable method. The reader uses [h5py's file-like object interface](https://docs.h5py.org/en/stable/high/file.html#python-file-like-objects), a cached 1 MiB metadata prefix, reusable 64 KiB metadata blocks, and exact compressed-chunk reads. It does not use a client that silently downloads the entire remote file. HDF5 necessarily decompresses the containing spatial chunk locally, not just 11 scalar values.

D. No alternative official lightweight profile method was identified. A future official pressure-point or server-subset API would be preferable to retrieving large spatial chunks.

## Exact pressure levels and fields

All **11** pressure coordinates are explicitly:

**1000, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300 hPa**.

The important requested levels **1000, 925, 850, 700, 500 and 300 hPa all exist**. None were interpolated or synthesized. The 1000 hPa level is below the model surface in this example and must be excluded from atmospheric comparisons.

Metadata was read before field values. These exact bulk field descriptions and units were confirmed:

| Short name | Description / long name | Native units | CF standard name |
|---|---|---|---|
| `t` | air temperature on pressure level | `degree Celsius` | `air_temperature` |
| `r` | relative humidity on pressure level | `%` | `relative_humidity` |
| `u` | wind speed in eastward direction on pressure level | `m s-1` | `eastward_wind` |
| `v` | wind speed in northward direction on pressure level | `m s-1` | `northward_wind` |
| `z` | geopotential on pressure level | `m2 s-2` | `geopotential` |

These are bulk names, not public point-API names. Required fields have dimension order **`(time, plev, latitude, longitude)`**, shape **`(61, 11, 945, 1300)`**, float32 storage, NaN fill values, and no scale/offset packing in the inspected file. Actual chunks are **`(1, 11, 472, 650)`**, gzip-compressed. Each decoded pressure-field chunk is **13,499,200 bytes**: all pressure levels, one time, and a large horizontal tile. Chunk shapes and compression are observed storage details, not guaranteed future API contracts.

An additional confirmed `sp` field, description “surface pressure”, native unit `Pa`, CF name `surface_air_pressure`, was read to flag below-ground levels. It has order `(time, latitude, longitude)`, shape `(61, 945, 1300)`, chunks `(5, 472, 650)`; thus its containing chunk includes five forecast times despite selecting one scalar. This extra read is necessary to distinguish real atmospheric pressure levels from subsurface extrapolation.

## Coordinates and forecast time

The inspected file reports **EPSG:4326**, grid mapping `latitude_longitude`, and one-dimensional latitude/longitude coordinate axes with units `degrees_north` and `degrees_east`:

- Latitude: 945 points, **51.498 to 43.002 degrees**, decreasing north-to-south, step approximately 0.009 degrees.
- Longitude: 1300 points, **5.0317 to 22.5682 degrees**, increasing west-to-east, step approximately 0.0135 degrees.
- Requested LJLM: **46.0656°N, 14.5122°E**.
- Nearest actual point: **46.06199999999979°N, 14.508700000000164°E**, indices latitude **604**, longitude **702** (zero-based).

The bulk global `spatial_resolution` attribute is **`0.028`**, which does not match the actual coordinate increments. The API metadata advertises 1000 m and its degree increments match the observed coordinate arrays. Selection therefore uses actual coordinates, not that inconsistent bulk attribute. This product is a geographic output grid; it does not expose native model-level geometry.

The `time` coordinate has units exactly **`hours since 2026-10-09 03:00:00`**, calendar **`standard`**, and values **0, 1, 2, …, 60**. The origin is interpreted as UTC according to the GeoSphere forecast-cycle convention and cross-checked against filename and global `date=20261009`, `run=03`. Time index **12** supplies +12 h. A missing exact forecast time is refused; no temporal interpolation or substitute cycle is used. Global `freq=1H`, `forecast_freq=3H` also match hourly output and three-hour model cycles.

## Native values and marked derivations

The profile preserves `temperature`, `relative_humidity`, `u`, `v`, and native `geopotential` separately at every level, with source metadata/units. NaN/fill values become JSON nulls; missing pressure levels are never invented. Derived fields have an explicit methods/units map:

- `wind_speed = hypot(u,v)` in m s-1.
- `wind_direction = atan2(-u,-v)` modulo 360 degrees: meteorological direction **from** which wind blows. Calm/missing wind has null direction.
- `height_m = z/9.80665`: **geopotential height** in metres, not exact geometric altitude. Native `z` remains unchanged.
- `dew_point`: Magnus approximation over liquid water, coefficients 17.625 and 243.04°C, using T and RH. The [Alduchov–Eskridge paper](https://journals.ametsoc.org/view/journals/apme/35/4/1520-0450_1996_035_0601_imfaos_2_0_co_2.xml) describes the approximation family. The code explicitly limits its working domain and does not clip RH or claim an exact phase correction. Zero/missing/out-of-domain humidity gives null. Bulk RH metadata does not specify water/ice saturation convention, so subzero dew-point comparisons carry phase uncertainty; native RH is the more direct comparison.
- `below_model_surface`: pressure ×100 exceeds the independently read surface pressure. Values are retained for inspection but flagged levels must be excluded from comparison/plotting.

Surface pressure was **98,410.109375 Pa**. The 1000 hPa field contains finite values even though it lies below that surface, illustrating why merely checking NaN is insufficient. This experiment does not diagnose the sounding's weather.

## Comparison readiness and visualization

| Existing LJLM diagnostic | Readiness / restriction |
|---|---|
| T925 / T850 / T700 / T500 | Direct point comparisons at matching valid time, after screening below-surface/null levels. |
| RH at those levels | Direct native RH comparison where observation humidity is defined consistently. |
| Td at those levels | Approximate derived comparison; account for Magnus and RH saturation-phase assumptions. |
| Standard-level wind | Compare native u/v and derived speed/direction at identical pressure levels. |
| Z500 | Compare geopotential height `z/g0`, ensuring the existing diagnostic's height definition is compatible. |
| 850–500 lapse rate | Layer-mean `(T850-T500)/(height500-height850)` in °C/km, with valid endpoints and increasing heights. |
| 700–500 lapse rate | Same endpoint calculation; no claim about unresolved structure inside the layer. |
| Pressure-level shear | Endpoint vector difference, e.g. 925–500 hPa; label the pressure layer explicitly. It is not exact 0–1/3/6 km AGL shear. |

Do **not** derive CAPE/CIN, parcel levels/paths, detailed inversions, tropopause, full Skew-T/parcel diagnostics, precise freezing/snow levels, or vertically resolved moisture transport from these 11 samples. Native model levels, levels above 300 hPa, and thin atmospheric structures are absent. The point API's native CAPE field is a separate product, not a justification to reconstruct CAPE from this sparse profile.

Future visualization should use **discrete T/Td points** and **discrete wind barbs** at the available model pressure levels. Thin connecting lines could guide the eye if clearly labeled as connections between sparse samples; they must not imply native vertical resolution. Exclude below-surface values. Do not present this profile as equivalent to a full observed radiosonde or native ICON-D2 profile. No dashboard implementation was made.

## Transfer accounting and operational practicality

Measured totals for the successful extraction, including its initial discovery and reused metadata prefix:

- **22 HTTP requests**: two small official metadata/listing requests and 20 range requests.
- **24,158,678 actual response-body bytes** (~24.16 MB decimal / 23.04 MiB).
- **10,347 bytes** in the resulting compact profile JSON.
- Source complete file: **9,442,193,799 bytes**. Approximately **0.256%** of that size was transferred, including discovery overhead.
- Required five pressure fields account for five spatial chunks; one extra surface-pressure chunk supports below-ground screening. Metadata/index blocks account for the remaining range requests.

Accounting covers actual model/API response bodies, not HTTP/TLS headers, documentation browsing, or installation of the local optional HDF5 reader. Initial exploratory metadata/prefix reads were reused and counted, rather than silently omitted or downloaded a second time. All ranges are listed in the output for audit.

**Two automated 00/12 UTC verification profiles per day look practical**, at roughly 48 MB/day and 44 requests/day at this measured layout/compression. Those are estimates from one case, not guaranteed payload sizes. Only one profile/time/point was tested; multi-point or many-lead applications could be expensive. Keep the hard guards and remeasure if compression/layout changes.

For future regular verification, explicitly select the **previous 00/12 UTC cycle plus 12 h** that matches the observation's nominal valid time, not simply the latest run. The script supports `--reference-time` and refuses a missing requested cycle. Bulk listings are rolling: retrieve/store the intended run while available and allow for publication delay. No profile workflow was added.

Run the experiment with optional h5py/NumPy installed in a separate environment:

```sh
python3 claef_pressure_profile.py
python3 claef_pressure_profile.py --reference-time 2026-10-09T00:00:00Z --lead-hours 12 --output /tmp/claef_00_f012.json
```

The dated command illustrates exact-cycle selection and may fail once that cycle ages out. There is no complete-download fallback. Guards enforce **64 MiB cumulative body bytes**, **80 HTTP requests**, **16 MiB per range**, **32 MiB decoded chunk**, response HTTP 206, exact Content-Range/ETag, identity encoding, 15-second timeout and at most one request/second. A server ignoring Range or a budget violation causes a safe stop, leaving existing output intact.

## Validation and scope

`python3 test_claef_pressure_profile.py` is offline: ordering, units/packing, dew point, wind direction/calm, geopotential height, missing levels/fill values, exact-time handling, schema, range/cache behavior, 429, byte/request budgets, ETag consistency and preservation of previous output. A tiny compressed HDF5 integration test runs when optional h5py is available; the full suite was also run with that dependency available.

Point-cache tests, YAML/shell syntax checks and `git diff --check` were also run. Only the explicitly requested new point workflow was created; the pressure-profile experiment did not modify workflows, Apps Script, ICON-D2, observational extraction or dashboard code. No commit was made, and no full bulk file was downloaded.
