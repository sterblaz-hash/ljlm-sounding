# C-LAEF AlpeAdria discovery — LJLM

Inspected 9 October 2026. This is an isolated proof of concept: no production integration, workflow changes, or commits. Run `python3 probe_claef.py` for a fresh four-request point probe. It needs only the Python standard library. `python3 probe_claef.py --inspect-bulk` additionally needs optional `h5py` and makes four more requests: two listings and two bounded 1 MiB NetCDF metadata reads. It refuses a full-file response or any metadata read beyond the cached prefix. Bulk field values have not been downloaded or validated.

## Exact point API inventory

The [inventory](inventory.md) contains every exact short name, long name, description, unit, category, reference-time list, requested/returned coordinate, sample valid time, lead time, and first-hour value. The small sample files preserve the API's GeoJSON structure without a wrapper or renamed fields.

| Category | Deterministic names | Ensemble names (exact names listed in inventory) |
|---|---|---|
| 2 m temperature / humidity | `2t`, `2r` | `2t_p10`, `2t_p50`, `2t_p90`; `2r_p10`, `2r_p50`, `2r_p90` |
| 10 m wind components / gust | `10u`, `10v`, `10fg` | Each of `10u`, `10v`, `10fg` has `_p10`, `_p50`, `_p90` |
| Precipitation | `tp`, `rain`, `pt` | Each of `tp`, `rain` has `_p10`, `_p50`, `_p90`; no `pt` statistics |
| Snowfall / snow level | `sf`, `snowlmt` | Each has `_p10`, `_p50`, `_p90` |
| Cloud cover | `tcc` | `tcc_p10`, `tcc_p50`, `tcc_p90` |
| Pressure | `msl` | `msl_p10`, `msl_p50`, `msl_p90` |
| Convection | `cape` | `cape_p10`, `cape_p50`, `cape_p90` |
| Radiation / sunshine | `ssrd`, `sund` | Each has `_p10`, `_p50`, `_p90` |
| Weather symbol | `sy` | None |

There are **16 deterministic names and 42 ensemble-statistic names**, covering 14 corresponding variable families. `pt` and `sy` are deterministic-only families. Literally, all deterministic short names differ from the ensemble names because the latter have percentile suffixes. No dew-point parameter, freezing-level parameter, snow-depth parameter, surface-pressure parameter, CIN, lightning density, or pressure-level parameter occurs in either point inventory. `snowlmt` is explicitly **height above ground where falling snow melts**, not the observed MSL freezing level. Wind speed/direction and dew point would need derivation; they are not supplied point variables.

The official [ensemble dataset description](https://data.hub.geosphere.at/de/dataset/ensemble-v2-1h-1km) says the statistics are calculated from 16 perturbed members plus one control run, and only statistics are exposed by API. P10/P50/P90 are **separate parameter keys**, e.g. `features[0].properties.parameters.2t_p10.data`. Each entry has `name`, `unit`, and `data`; arrays align with the shared top-level `timestamps`. There is no member or percentile dimension in the point response.

## Reference times, point geometry, and samples

Both metadata snapshots reported `last_forecast_reftime = 2026-10-09T00:00+00:00`, `frequency = 1H`, `forecast_length = 61`, and spatial resolution 1000 m. Deterministic available reference times: 9 October 00 UTC and 8 October 21, 18, 15, 12, 09 UTC (`max_forecast_offset = 5`). Ensemble available reference times: 9 October 00 UTC and 8 October 21, 18, 15 UTC (`max_forecast_offset = 3`). These lists are rolling snapshots, not a historical archive. GeoSphere documents forecast length as a number of time steps; its [dataset description](https://data.hub.geosphere.at/de/dataset/nwp-v2-1h-1km) specifies a 60-hour horizon and three-hour updates. Do not interpret 61 time steps as 61 hours of elapsed lead time.

Requests used `lat_lon=46.0656,14.5122`, `forecast_offset=0`, explicit UTC start 00:00 and end 03:00, and only metadata-confirmed parameters. Both returned `reference_time = 2026-10-09T00:00+00:00`. Returned GeoJSON coordinates are **longitude, latitude**, `[14.508699999999735, 46.06200000000012]`, approximately 46.0620°N, 14.5087°E. The requested location differs from the returned model grid point. Valid times were 00, 01, 02, 03 UTC on 9 October, giving leads 0, 1, 2, 3 h. The end bound was inclusive in these responses.

| Variable | Unit | +0 h | +1 h | +2 h | +3 h |
|---|---|---:|---:|---:|---:|
| Deterministic `2t` | degree Celsius | 15.0 | 14.7 | 14.1 | 13.6 |
| Deterministic `2r` | % | 95.2 | 98.82 | 94.83 | 96.07 |
| Deterministic `10fg` | m s-1 | null | 2.1 | 2.5 | 1.9 |
| Deterministic `tp` | kg m-2 | null | 0.016 | 0.0 | 0.001 |
| Deterministic `snowlmt` | m AGL | 2612.8 | 2645.8 | 2612.8 | 2733.0 |
| Ensemble `2t_p10` | degree Celsius | 13.7 | 13.4 | 13.1 | 13.0 |
| Ensemble `2t_p50` | degree Celsius | 14.6 | 14.6 | 14.4 | 14.2 |
| Ensemble `2t_p90` | degree Celsius | 15.2 | 15.0 | 15.0 | 14.9 |
| Ensemble `tp_p90` | kg m-2 | null | 3.409 | 2.541 | 2.326 |

All requested fields and values, including wind components, pressure, CAPE, clouds, snow and radiation, are in [deterministic_sample.json](deterministic_sample.json), [ensemble_sample.json](ensemble_sample.json), and the inventory. Missing values are preserved as `null`, never converted to zero. Top-level `datapoints` is 48 deterministic and 36 ensemble, including null values.

## Vertical profiles: API versus bulk

The official bulk [deterministic listing](https://public.hub.geosphere.at/public/datahub.html?id=nwp-v2-1h-1km/filelisting) and [ensemble listing](https://public.hub.geosphere.at/public/datahub.html?id=ensemble-v2-1h-1km/filelisting) were inspected. Both listings were complete (`IsTruncated=false`). Deterministic had three files; ensemble had 54 files: 17 member files plus one statistics file for each of three cycles. Only NetCDF files were listed; no GRIB files were found in those directories.

To establish actual variables, HTTP Range metadata reads inspected the deterministic `nwp_2026100900.nc` and ensemble `ensemble_stats_2026100900.nc`. The deterministic inspection initially read 64 KiB, then extended to 1 MiB because HDF5 directory metadata extended beyond 64 KiB. The ensemble statistics inspection read 1 MiB. Total bulk bytes transferred: **2 MiB**, with no full bulk file download. [bulk_inventory.json](bulk_inventory.json) records source file keys, sizes, ETags, variable attributes, dimensions, and pressure coordinates. The optional probe reproduces this using one bounded request per header and refuses reads beyond the prefix.

| Variable sought | A: point/timeseries API | B: inspected public bulk NetCDF | C: absent from inspected public products |
|---|---|---|---|
| Pressure-level temperature | Not exposed | `t`, degree Celsius | — |
| Pressure-level humidity | Not exposed | `r`, relative humidity, % | Direct dew point / specific humidity absent |
| Pressure-level horizontal wind | Not exposed | `u`, `v`, m s-1 | — |
| Geopotential / height | Not exposed | `z`, geopotential, m2 s-2; derive height via `z/g` | Direct pressure-level height absent |
| Vertical motion | Not exposed | `w`, omega, Pa s-1 | — |
| Native model levels | Not exposed | No native/hybrid level dimension in inspected files | No public native-level source identified |
| Ensemble vertical percentiles | Not exposed | Statistics file has no pressure dimension or vertical fields | Not supplied by inspected statistics product |

Deterministic pressure coordinates are exactly **1000, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300 hPa**. Each of `t`, `r`, `u`, `v`, `z`, `w` has dimensions `(time, plev, latitude, longitude)` and shape `(61, 11, 945, 1300)`. Other dimensions are time, latitude and longitude; there are no native model levels. The ensemble control `ensemble_mem00_2026100900.nc` has the same size and ETag as the deterministic file, evidence that it contains the same object content. Other perturbed-member headers were not inspected, so their exact vertical inventories are not claimed as independently verified. The statistics file contains 42 surface percentile fields with shape `(61, 945, 1300)` and three coordinate variables.

Additional operational fields confirmed in deterministic bulk only: `sp`, `zsurf`, `tsurf`, `sd`, `lcc`, `mcc`, `hcc`, `sai`, `cin`, `litotint`, `max_2t`, `min_2t`, `max_10efg`, `max_10nfg`, `ssr`, `str`, `strd`. Their exact names, descriptions, units and shapes are in the compact bulk inventory. These must not be sent to the point API without future metadata confirmation.

**A sparse LJLM pressure-level forecast profile is technically plausible via bulk; a full high-resolution sounding comparable to the observed radiosonde is not supported by the point API or these 11 levels.** Derive dew point from `t/r` and geopotential height from `z`. The pressure-level product stops at 300 hPa and cannot resolve thin inversions or detailed parcel diagnostics reliably. Below-ground levels require masking using model surface pressure/orography. No meteorological bulk values, missing-value masks, spatial chunk layout, or extraction bandwidth were tested; profile extraction feasibility/performance remains a separate experiment. Absence claims here apply to the inspected official resources, not every conceivable public provider.

## Payload sizes and API caveats

Measured uncompressed HTTP bodies: deterministic metadata **2,971 B**, ensemble metadata **9,350 B**; deterministic sample **1,545 B** (12 parameters × 4 times × 1 point), ensemble sample **1,456 B** (9 parameters × 4 times × 1 point). Saved compact JSON is essentially the same size, plus a newline. No large raw metadata is saved. These sizes are measured for the selected four-hour samples, not estimates of full forecast payloads.

Listed latest bulk sizes: deterministic **9,454,655,152 B** (~9.45 GB decimal), ensemble statistics **7,417,979,064 B** (~7.42 GB); member files are also roughly 9.4 GB. Point payloads are small; whole-domain downloads would be a different operational commitment.

- [Published limits](https://dataset.api.hub.geosphere.at/v1/docs/user-guide/request-limit.html): 5 requests/second and 240/hour; JSON/CSV limits 1,000,000 values. The probe paces at at most one request/second, uses a 15-second timeout, explicit User-Agent, response-size caps, and stops cleanly on HTTP 429 without a retry loop. Respect `Retry-After` / `ratelimit-reset` before a later manual run. No credentials are needed.
- [Forecast selection](https://dataset.api.hub.geosphere.at/v1/docs/user-guide/mode.html) uses rolling `forecast_offset`, not an immutable reference-time selector. Metadata and data can change between requests. Treat response `reference_time` as authoritative, check against discovery time, and align cycles before combining deterministic and ensemble data. This probe reports a cycle mismatch instead of silently relabeling the sample.
- Explicit `start`/`end` avoids the API's default start near the current time. Only a few cycles remain available, so production verification requires retaining your own small point snapshots.
- [Migration guidance](https://public.hub.geosphere.at/public/resources/misc/Hilfestellung_DE.pdf) says precipitation and radiation have been deaccumulated. Keep interval amounts as supplied; do not difference them again. `ssrd` is W m-2, not accumulated J m-2. Sample interval variables have no +0 value.
- `msl` is Pa, not hPa. Snowfall is kg m-2 water-equivalent mass, not centimetres of snow depth. `snowlmt` is AGL; bulk `z`/`zsurf` is geopotential, not metres.
- **Observed metadata mismatch:** ensemble bulk `tcc_p10/p50/p90` attributes say unit `1`, while point metadata says `%`. No bulk cloud values were read; establish conversion from actual values before mixing API and bulk products. This is another reason to preserve source units.
- Marginal wind-component percentiles do not define wind-speed/direction percentiles. Likewise, dew point from separate temperature/RH percentiles is not a dew-point percentile, and summing hourly precipitation percentiles is not the percentile of accumulated precipitation. Joint members are needed for those calculations.
- `pt` and `sy` are categorical codes, not numerical severity scales. Their flag mappings are missing from point metadata; the migration guidance supplies code tables and directs users to NetCDF attributes. Do not invent code labels from the integers.
- Metadata does not state the parcel definition for `cape`; do not assume equivalence to the dashboard's MUCAPE/SBCAPE. No direct freezing-level or thunderstorm-probability parameter is listed.
- GeoSphere announces older endpoints will close on **4 November 2026**; this probe already targets the requested 1 km v2 resources. The [public data license](https://dataset.api.hub.geosphere.at/v1/docs/) requires CC BY 4.0 attribution.

## Recommended next step

Keep the first production design limited to an independently stored surface point forecast and uncertainty series, with an explicit schema for reference time, valid time, lead, requested/grid coordinates, source units, nulls and percentile identity. Validate cycle matching and interval semantics before considering dashboard wiring. For soundings, do a separate bounded bulk-access experiment measuring LJLM extraction cost, chunk layout and below-ground masks for `t/r/u/v/z/sp`; request an official point-profile/subsetting route if large domain chunks make access inefficient. Do not claim a full sounding or begin routine multi-GB downloads on the basis of this probe.

Validation: `python3 test_claef_probe.py` uses only local small fixtures and mocked HTTP responses. It covers response parsing, units/nulls, coordinate order, authoritative cycles, exact percentile keys, confirmed request parameters, malformed data, timeout/User-Agent, pacing, HTTP errors/429, payload/range bounds and listing truncation. `git diff --check` is also required before handing over.
