# Dashboard data examples

Examples from the initial archive build.

## Compact record

```json
{
  "sounding_id": "20261002_00",
  "nominal_date": "2026-10-02",
  "term": "00",
  "launch_time": "2026-10-01T23:30:00Z",
  "processed_at": "2026-10-02T02:02:10.770642Z",
  "valid_time": "2026-10-02T00:00:00Z",
  "pwat_mm": 12.37,
  "freezing_level_msl_m": 3867.0,
  "lifted_index_c": 15.22,
  "sbcape_jkg": 0.0,
  "sbcin_jkg": 0.0,
  "mlcape_jkg": 0.0,
  "mlcin_jkg": 0.0,
  "mucape_jkg": 0.0,
  "mucin_jkg": 0.0,
  "lapse_rate_850_500_c_per_km": 5.11,
  "lapse_rate_700_500_c_per_km": 6.22,
  "shear_0_1km_ms": 5.63,
  "shear_0_3km_ms": 2.55,
  "shear_0_6km_ms": 1.68,
  "shear_sfc_700_ms": 2.15,
  "z500_m": 5855.0,
  "thickness_925_500_m": 4945.0,
  "t850_c": 8.07,
  "td850_c": -1.26,
  "t700_c": 2.87,
  "td700_c": -10.72,
  "t500_c": -13.59,
  "td500_c": -29.65,
  "q_surface_gkg": 6.33,
  "q925_gkg": 4.8,
  "q850_gkg": 4.09,
  "q700_gkg": 2.41,
  "ivt_kg_m1_s1": 44.4,
  "ivt_direction_deg": 269.4,
  "dinaric_west_signed_ivt": -19.7,
  "dinaric_west_signed_fraction": -0.789,
  "julian_south_signed_ivt": -27.0,
  "julian_south_signed_fraction": -0.788,
  "julian_central_signed_ivt": -10.0,
  "julian_central_signed_fraction": -0.284,
  "kamnik_savinja_signed_ivt": -4.1,
  "kamnik_savinja_signed_fraction": -0.117
}
```

## Manifest

```json
{
  "version": 1,
  "station": 14015,
  "station_name": "Ljubljana",
  "observations": {
    "latest_profile": "data/latest.json",
    "status": "data/status.json",
    "diagnostics": {
      "index": "diagnostics/latest_products.json",
      "skewt": "diagnostics/latest_skewt.png",
      "skewt_zoom": "diagnostics/latest_skewt_zoom.png",
      "lowlevel": "diagnostics/latest_lowlevel.png",
      "thetae": "diagnostics/latest_thetae.png",
      "hodograph": "diagnostics/latest_hodograph.png"
    }
  },
  "icon_d2": {
    "lead_hours": 12,
    "slots": {
      "00": {
        "profile": "models/icon-d2/latest/00/sounding.json",
        "diagnostics": "models/icon-d2/latest/00/latest_products.json"
      },
      "12": {
        "profile": "models/icon-d2/latest/12/sounding.json",
        "diagnostics": "models/icon-d2/latest/12/latest_products.json"
      }
    }
  },
  "timeseries": {
    "available": true,
    "windows": {
      "7d": "timeseries/ljlm/latest_7d.json",
      "30d": "timeseries/ljlm/latest_30d.json",
      "90d": "timeseries/ljlm/latest_90d.json",
      "1y": "timeseries/ljlm/latest_1y.json"
    },
    "variables": [
      "pwat_mm",
      "freezing_level_msl_m",
      "lifted_index_c",
      "sbcape_jkg",
      "sbcin_jkg",
      "mlcape_jkg",
      "mlcin_jkg",
      "mucape_jkg",
      "mucin_jkg",
      "lapse_rate_850_500_c_per_km",
      "lapse_rate_700_500_c_per_km",
      "shear_0_1km_ms",
      "shear_0_3km_ms",
      "shear_0_6km_ms",
      "shear_sfc_700_ms",
      "z500_m",
      "thickness_925_500_m",
      "t850_c",
      "td850_c",
      "t700_c",
      "td700_c",
      "t500_c",
      "td500_c",
      "q_surface_gkg",
      "q925_gkg",
      "q850_gkg",
      "q700_gkg",
      "ivt_kg_m1_s1",
      "ivt_direction_deg",
      "dinaric_west_signed_ivt",
      "dinaric_west_signed_fraction",
      "julian_south_signed_ivt",
      "julian_south_signed_fraction",
      "julian_central_signed_ivt",
      "julian_central_signed_fraction",
      "kamnik_savinja_signed_ivt",
      "kamnik_savinja_signed_fraction"
    ]
  }
}
```
