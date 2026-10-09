# C-LAEF point API probe

Checked: 2026-10-09T06:23:20.432029+00:00

Deterministic-only variable families: pt, sy

Ensemble statistics use separate parameter names under `features[0].properties.parameters`; each has `name`, `unit`, and `data` aligned with top-level `timestamps`.

## deterministic

Source: https://dataset.api.hub.geosphere.at/v1/timeseries/forecast/nwp-v2-1h-1km/metadata

- last_forecast_reftime: `"2026-10-09T00:00+00:00"`
- available_forecast_reftimes: `["2026-10-09T00:00+00:00", "2026-10-08T21:00+00:00", "2026-10-08T18:00+00:00", "2026-10-08T15:00+00:00", "2026-10-08T12:00+00:00", "2026-10-08T09:00+00:00"]`
- forecast_length: `61`
- frequency: `"1H"`
- max_forecast_offset: `5`
- spatial_resolution_m: `1000`
- Metadata HTTP body: 2971 bytes
- Sample HTTP body: 1545 bytes

| Category | Exact short name | Long name | Description | Unit |
|---|---|---|---|---|
| 10 m wind and gusts | 10fg | maximum 10 metre wind gust in the last forecast interval | maximum 10 metre wind gust in the last forecast interval | m s-1 |
| 10 m wind and gusts | 10u | 10m wind speed in eastward direction | wind speed in eastward direction 10m above ground | m s-1 |
| 10 m wind and gusts | 10v | 10m wind speed in northward direction | wind speed in northward direction | m s-1 |
| 2 m temperature / dew point / humidity | 2r | relative humidity 2m above ground | relative humidity 2m above ground | % |
| 2 m temperature / dew point / humidity | 2t | 2m temperature | air temperature 2m above ground | degree Celsius |
| CAPE / convection / thunderstorms | cape | convective available potential energy | convectively available potential energy | m2 s-2 |
| Pressure | msl | mean sea level pressure | air pressure at mean sea level | Pa |
| Precipitation | pt | severe precipitation type in the last forecast interval | precipitation type | 1 |
| Precipitation | rain | rainfall amount in the last forecast interval | rainfall amount in the last forecast interval | kg m-2 |
| Snowfall / snow-related | sf | snowfall amount in the last forecast interval | snowfall amount in the last forecast interval | kg m-2 |
| Freezing or snow level | snowlmt | snowlimit | height above ground where falling snow melts | m |
| Radiation / sunshine | ssrd | surface global radiation | surface downwelling shortwave is the sum of direct and diffuse solar radiation incident on the surface, and is sometimes called global radiation | W m-2 |
| Radiation / sunshine | sund | sunshine duration in the last forecast interval | length of time for which surface incident radiative flux from solar beam exceeds 120 W m-2 in the last forecast interval | s |
| Other operational variables | sy | weather symbol | codes for GeoSphere Austria weather symbol | 1 |
| Cloud cover | tcc | total cloud cover | total cloud cover | % |
| Precipitation | tp | total precipitation amount in the last forecast interval | total amount of liquid and solid precipitation in the last forecast interval | kg m-2 |

Requested latitude,longitude: 46.0656,14.5122
Returned latitude,longitude: 46.06200000000012,14.508699999999735
Response reference time: 2026-10-09T00:00+00:00

| Valid UTC time | Lead h | Values (units as above) |
|---|---|---|
| 2026-10-09T00:00+00:00 | 0 | 2t=15.0, 2r=95.2, 10u=-0.3, 10v=-0.5, 10fg=None, tp=None, sf=None, snowlmt=2612.8, tcc=100.0, msl=101284.48, cape=185.8, ssrd=None |
| 2026-10-09T01:00+00:00 | 1 | 2t=14.7, 2r=98.82, 10u=1.1, 10v=-1.0, 10fg=2.1, tp=0.016, sf=0.0, snowlmt=2645.8, tcc=100.0, msl=101446.69, cape=24.0, ssrd=0.0 |
| 2026-10-09T02:00+00:00 | 2 | 2t=14.1, 2r=94.83, 10u=0.0, 10v=-1.2, 10fg=2.5, tp=0.0, sf=0.0, snowlmt=2612.8, tcc=98.5, msl=101523.62, cape=6.8, ssrd=0.0 |
| 2026-10-09T03:00+00:00 | 3 | 2t=13.6, 2r=96.07, 10u=0.6, 10v=-1.1, 10fg=1.9, tp=0.001, sf=0.0, snowlmt=2733.0, tcc=100.0, msl=101615.62, cape=25.3, ssrd=0.0 |

## ensemble

Source: https://dataset.api.hub.geosphere.at/v1/timeseries/forecast/ensemble-v2-1h-1km/metadata

- last_forecast_reftime: `"2026-10-09T00:00+00:00"`
- available_forecast_reftimes: `["2026-10-09T00:00+00:00", "2026-10-08T21:00+00:00", "2026-10-08T18:00+00:00", "2026-10-08T15:00+00:00"]`
- forecast_length: `61`
- frequency: `"1H"`
- max_forecast_offset: `3`
- spatial_resolution_m: `1000`
- Metadata HTTP body: 9350 bytes
- Sample HTTP body: 1456 bytes

| Category | Exact short name | Long name | Description | Unit |
|---|---|---|---|---|
| 10 m wind and gusts | 10fg_p10 | maximum 10 metre wind gust (10th percentile) | maximum 10 metre wind gust in the last forecast interval (10th percentile of an ensemble forecast) | m s-1 |
| 10 m wind and gusts | 10fg_p50 | maximum 10 metre wind gust (50th percentile) | maximum 10 metre wind gust in the last forecast interval (50th percentile of an ensemble forecast) | m s-1 |
| 10 m wind and gusts | 10fg_p90 | maximum 10 metre wind gust (90th percentile) | maximum 10 metre wind gust in the last forecast interval (90th percentile of an ensemble forecast) | m s-1 |
| 10 m wind and gusts | 10u_p10 | 10m wind speed in eastward direction (10th percentile) | wind speed in eastward direction 10m above ground (10th percentile of an ensemble forecast) | m s-1 |
| 10 m wind and gusts | 10u_p50 | 10m wind speed in eastward direction (50th percentile) | wind speed in eastward direction 10m above ground (50th percentile of an ensemble forecast) | m s-1 |
| 10 m wind and gusts | 10u_p90 | 10m wind speed in eastward direction (90th percentile) | wind speed in eastward direction 10m above ground (90th percentile of an ensemble forecast) | m s-1 |
| 10 m wind and gusts | 10v_p10 | 10m wind speed in northward direction (10th percentile) | wind speed in northward direction 10m above ground (10th percentile of an ensemble forecast) | m s-1 |
| 10 m wind and gusts | 10v_p50 | 10m wind speed in northward direction (50th percentile) | wind speed in northward direction 10m above ground (50th percentile of an ensemble forecast) | m s-1 |
| 10 m wind and gusts | 10v_p90 | 10m wind speed in northward direction (90th percentile) | wind speed in northward direction 10m above ground (90th percentile of an ensemble forecast) | m s-1 |
| 2 m temperature / dew point / humidity | 2r_p10 | relative humidity 2m above ground (10th percentile) | relative humidity 2m above ground (10th percentile of an ensemble forecast) | % |
| 2 m temperature / dew point / humidity | 2r_p50 | relative humidity 2m above ground (50th percentile) | relative humidity 2m above ground (50th percentile of an ensemble forecast) | % |
| 2 m temperature / dew point / humidity | 2r_p90 | relative humidity 2m above ground (90th percentile) | relative humidity 2m above ground (90th percentile of an ensemble forecast) | % |
| 2 m temperature / dew point / humidity | 2t_p10 | 2m temperature (10th percentile) | air temperature 2m above ground (10th percentile of an ensemble forecast) | degree Celsius |
| 2 m temperature / dew point / humidity | 2t_p50 | 2m temperature (50th percentile) | air temperature 2m above ground (50th percentile of an ensemble forecast) | degree Celsius |
| 2 m temperature / dew point / humidity | 2t_p90 | 2m temperature (90th percentile) | air temperature 2m above ground (90th percentile of an ensemble forecast) | degree Celsius |
| CAPE / convection / thunderstorms | cape_p10 | convective available potential energy (10th percentile) | convectively available potential energy (10th percentile of an ensemble forecast) | m2 s-2 |
| CAPE / convection / thunderstorms | cape_p50 | convective available potential energy (50th percentile) | convectively available potential energy (50th percentile of an ensemble forecast) | m2 s-2 |
| CAPE / convection / thunderstorms | cape_p90 | convective available potential energy (90th percentile) | convectively available potential energy (90th percentile of an ensemble forecast) | m2 s-2 |
| Pressure | msl_p10 | mean sea level pressure (10th percentile) | air pressure at mean sea level (10th percentile of an ensemble forecast) | Pa |
| Pressure | msl_p50 | mean sea level pressure (50th percentile) | air pressure at mean sea level (50th percentile of an ensemble forecast) | Pa |
| Pressure | msl_p90 | mean sea level pressure (90th percentile) | air pressure at mean sea level (90th percentile of an ensemble forecast) | Pa |
| Precipitation | rain_p10 | rainfall amount in the last forecast interval (10th percentile) | rainfall amount in the last forecast interval (10th percentile of an ensemble forecast) | kg m-2 |
| Precipitation | rain_p50 | rainfall amount in the last forecast interval (50th percentile) | rainfall amount in the last forecast interval (50th percentile of an ensemble forecast) | kg m-2 |
| Precipitation | rain_p90 | rainfall amount in the last forecast interval (90th percentile) | rainfall amount in the last forecast interval (90th percentile of an ensemble forecast) | kg m-2 |
| Snowfall / snow-related | sf_p10 | snowfall amount in the last forecast interval (10th percentile) | snowfall amount in the last forecast interval (10th percentile of an ensemble forecast) | kg m-2 |
| Snowfall / snow-related | sf_p50 | snowfall amount in the last forecast interval (50th percentile) | snowfall amount in the last forecast interval (50th percentile of an ensemble forecast) | kg m-2 |
| Snowfall / snow-related | sf_p90 | snowfall amount in the last forecast interval (90th percentile) | snowfall amount in the last forecast interval (90th percentile of an ensemble forecast) | kg m-2 |
| Freezing or snow level | snowlmt_p10 | snowlimit (10th percentile) | height above ground where falling snow melts (10th percentile of an ensemble forecast) | m |
| Freezing or snow level | snowlmt_p50 | snowlimit (50th percentile) | height above ground where falling snow melts (50th percentile of an ensemble forecast) | m |
| Freezing or snow level | snowlmt_p90 | snowlimit (90th percentile) | height above ground where falling snow melts (90th percentile of an ensemble forecast) | m |
| Radiation / sunshine | ssrd_p10 | surface global radiation (10th percentile) | surface downwelling shortwave is the sum of direct and diffuse solar radiation incident on the surface, and is sometimes called global radiation (10th percentile of an ensemble forecast) | W m-2 |
| Radiation / sunshine | ssrd_p50 | surface global radiation (50th percentile) | surface downwelling shortwave is the sum of direct and diffuse solar radiation incident on the surface, and is sometimes called global radiation (50th percentile of an ensemble forecast) | W m-2 |
| Radiation / sunshine | ssrd_p90 | surface global radiation (90th percentile) | surface downwelling shortwave is the sum of direct and diffuse solar radiation incident on the surface, and is sometimes called global radiation (90th percentile of an ensemble forecast) | W m-2 |
| Radiation / sunshine | sund_p10 | sunshine duration in the last forecast interval (10th percentile) | length of time for which surface incident radiative flux from solar beam exceeds 120 W m-2 in the last forecast interval (10th percentile of an ensemble forecast) | s |
| Radiation / sunshine | sund_p50 | sunshine duration in the last forecast interval (50th percentile) | length of time for which surface incident radiative flux from solar beam exceeds 120 W m-2 in the last forecast interval (50th percentile of an ensemble forecast) | s |
| Radiation / sunshine | sund_p90 | sunshine duration in the last forecast interval (90th percentile) | length of time for which surface incident radiative flux from solar beam exceeds 120 W m-2 in the last forecast interval (90th percentile of an ensemble forecast) | s |
| Cloud cover | tcc_p10 | total cloud cover (10th percentile) | total cloud cover (10th percentile of an ensemble forecast) | % |
| Cloud cover | tcc_p50 | total cloud cover (50th percentile) | total cloud cover (50th percentile of an ensemble forecast) | % |
| Cloud cover | tcc_p90 | total cloud cover (90th percentile) | total cloud cover (90th percentile of an ensemble forecast) | % |
| Precipitation | tp_p10 | total precipitation amount in the last forecast interval (10th percentile) | total amount of liquid and solid precipitation in the last forecast interval (10th percentile of an ensemble forecast) | kg m-2 |
| Precipitation | tp_p50 | total precipitation amount in the last forecast interval (50th percentile) | total amount of liquid and solid precipitation in the last forecast interval (50th percentile of an ensemble forecast) | kg m-2 |
| Precipitation | tp_p90 | total precipitation amount in the last forecast interval (90th percentile) | total amount of liquid and solid precipitation in the last forecast interval (90th percentile of an ensemble forecast) | kg m-2 |

Requested latitude,longitude: 46.0656,14.5122
Returned latitude,longitude: 46.06200000000012,14.508699999999735
Response reference time: 2026-10-09T00:00+00:00

| Valid UTC time | Lead h | Values (units as above) |
|---|---|---|
| 2026-10-09T00:00+00:00 | 0 | 2t_p10=13.7, 2t_p50=14.6, 2t_p90=15.2, tp_p10=None, tp_p50=None, tp_p90=None, 10fg_p10=None, 10fg_p50=None, 10fg_p90=None |
| 2026-10-09T01:00+00:00 | 1 | 2t_p10=13.4, 2t_p50=14.6, 2t_p90=15.0, tp_p10=0.0, tp_p50=0.111, tp_p90=3.409, 10fg_p10=1.2, 10fg_p50=2.1, 10fg_p90=3.3 |
| 2026-10-09T02:00+00:00 | 2 | 2t_p10=13.1, 2t_p50=14.4, 2t_p90=15.0, tp_p10=0.0, tp_p50=0.041, tp_p90=2.541, 10fg_p10=1.8, 10fg_p50=2.4, 10fg_p90=3.4 |
| 2026-10-09T03:00+00:00 | 3 | 2t_p10=13.0, 2t_p50=14.2, 2t_p90=14.9, tp_p10=0.0, tp_p50=0.011, tp_p90=2.326, 10fg_p10=1.4, 10fg_p50=2.2, 10fg_p90=3.2 |
