const LJLM_CONFIG = Object.freeze({

  RAW_BASE: 'https://raw.githubusercontent.com/sterblaz-hash/ljlm-sounding/main',

  MANIFEST_PATH: 'data/dashboard_manifest.json',

  STATUS_PATH: 'data/status.json',

  LATEST_PATH: 'data/latest.json',

  PRODUCTS_PATH: 'diagnostics/latest_products.json',

  CACHE_SECONDS: 45

});





function doGet() {

  return HtmlService

    .createTemplateFromFile('Index')

    .evaluate()

    .setTitle('LJLM Sounding')

    .addMetaTag('viewport', 'width=device-width, initial-scale=1');

}





function getDashboardData(archiveFile) {

  const status = fetchJsonOptional_(LJLM_CONFIG.STATUS_PATH);

  const products = fetchJsonOptional_(LJLM_CONFIG.PRODUCTS_PATH);



  let profilePath = LJLM_CONFIG.LATEST_PATH;

  let usingLatest = true;



  if (archiveFile && String(archiveFile).trim()) {

    profilePath = String(archiveFile).trim();

    usingLatest = profilePath === LJLM_CONFIG.LATEST_PATH;

  }



  const profile = fetchJson_(profilePath);



  return buildDashboardView_(

    profile,

    status,

    products,

    profilePath,

    usingLatest

  );

}





function buildDashboardView_(

  profile,

  status,

  products,

  profilePath,

  usingLatest

) {

  const parameters = profile.parameters || {};

  const standard = parameters.standard_levels || {};

  const metpy = parameters.metpy || {};

  const moistureTransport = metpy.moisture_transport || {};

  const climatology = parameters.climatology || {};

  const inversions = parameters.inversions || {};

  const changes = parameters.change || {};



  const version = (

    profile.processed_at ||

    profile.retrieved_at ||

    profile.launch_time ||

    new Date().toISOString()

  );



  return {

    station: profile.station || 14015,

    stationName: profile.station_name || 'Ljubljana',

    nominalDate: profile.nominal_date || '',

    term: profile.term || '',

    launchTime: profile.launch_time || '',

    retrievedAt: profile.retrieved_at || '',

    processedAt: profile.processed_at || '',

    profilePath: profilePath,



    soundingStatus: resolveSoundingStatus_(profile, status),



    images: buildImageUrls_(

      profile,

      products,

      version,

      usingLatest

    ),



    metrics: [

      metric_(

        'T850',

        nestedValue_(standard, 't850', 'value'),

        '°C',

        climPercentile_(climatology, 't850')

      ),

      metric_(

        'T700',

        nestedValue_(standard, 't700', 'value'),

        '°C',

        climPercentile_(climatology, 't700')

      ),

      metric_(

        'T500',

        nestedValue_(standard, 't500', 'value'),

        '°C',

        climPercentile_(climatology, 't500')

      ),

      metric_(

        'PWAT',

        metpy.pwat_mm,

        'mm',

        climPercentile_(climatology, 'pwat_mm')

      ),

      metric_(

        'Freezing level',

        metpy.freezing_level_msl_m,

        'm',

        climPercentile_(climatology, 'freezing_level_msl_m')

      ),

      metric_(

        'MUCAPE',

        metpy.mucape_jkg,

        'J/kg',

        climPercentile_(climatology, 'mucape_jkg')

      ),

      metric_(

        '0–6 km shear',

        metpy.shear_0_6km_ms,

        'm/s',

        climPercentile_(climatology, 'shear_0_6km_ms')

      ),

      metric_(

        'SFC–700 shear',

        metpy.shear_sfc_700_ms,

        'm/s',

        climPercentile_(climatology, 'shear_sfc_700_ms')

      ),

      metric_(

        'Total IVT',

        nestedValue_(moistureTransport, 'ivt', 'magnitude_kg_m1_s1'),

        'kg m⁻¹ s⁻¹',

        climPercentile_(climatology, 'ivt_kg_m1_s1')

      ),

      metric_(

        'q850',

        nestedValue_(

          moistureTransport.humidity_profile || {},

          '850',

          'specific_humidity_gkg'

        ),

        'g/kg',

        climPercentile_(climatology, 'q850_gkg')

      ),

      metric_(

        'Z500',

        metpy.z500_m,

        'm',

        climPercentile_(climatology, 'z500_m')

      ),

      metric_(

        '925–500 thickness',

        metpy.thickness_925_500_m,

        'm',

        climPercentile_(climatology, 'thickness_925_500_m')

      )

    ],



    additionalDiagnostics: [
      ['lifted_index_c', 'Lifted Index', '°C'],
      ['sbcape_jkg', 'SBCAPE', 'J/kg'],
      ['sbcin_jkg', 'SBCIN', 'J/kg'],
      ['mlcape_jkg', 'MLCAPE', 'J/kg'],
      ['mlcin_jkg', 'MLCIN', 'J/kg'],
      ['mucin_jkg', 'MUCIN', 'J/kg'],
      ['shear_0_1km_ms', '0–1 km shear', 'm/s'],
      ['shear_0_3km_ms', '0–3 km shear', 'm/s'],
      ['lapse_rate_850_500_c_per_km', '850–500 hPa lapse rate', '°C/km'],
      ['lapse_rate_700_500_c_per_km', '700–500 hPa lapse rate', '°C/km']
    ].map(function(def) {
      return metric_(def[1], metpy[def[0]], def[2], climPercentile_(climatology, def[0]));
    }),

    moistureTransport:

      buildMoistureTransport_(metpy),



    orographicTransport:

      buildOrographicTransport_(metpy),



    climatology: buildClimatology_(climatology),



    climatologyMeta: {

      available: climatology.available === true,

      referencePeriod: climatology.reference_period || '',

      window: climatology.window || '',

      caveat: climatology.historical_time_caveat || ''

    },



    inversions: buildInversions_(inversions),



    changes: {

      previousTerm: buildChangeBlock_(

        changes.previous_term,

        'Previous sounding'

      ),

      previousDay: buildChangeBlock_(

        changes.previous_day_same_term,

        'Same term −24 h'

      )

    },



    archiveOptions: buildArchiveOptions_(status),



    trajectory: {

      available:

        !!(profile.trajectory && profile.trajectory.available),

      maxHeightM:

        profile.trajectory

          ? profile.trajectory.max_height_m

          : null,

      durationS:

        profile.trajectory

          ? profile.trajectory.duration_s

          : null

    }

  };

}





function metric_(label, value, unit, percentile) {

  return {

    label: label,

    value: finiteOrNull_(value),

    unit: unit,

    percentile: finiteOrNull_(percentile)

  };

}





function buildMoistureTransport_(metpy) {

  const mt = metpy.moisture_transport || {};

  const humidity = mt.humidity_profile || {};

  const ivt = mt.ivt || {};

  const layers = mt.layer_ivt || {};



  function humidityValue_(key) {

    const item = humidity[key] || {};

    return finiteOrNull_(

      item.specific_humidity_gkg

    );

  }



  function layerItem_(key, label) {

    const item = layers[key] || {};



    return {

      key: key,

      label: label,

      available: item.available === true,

      magnitude:

        finiteOrNull_(item.magnitude_kg_m1_s1),

      direction:

        finiteOrNull_(item.transport_to_direction_deg),

      bottomPressure:

        finiteOrNull_(item.bottom_pressure_hpa),

      topPressure:

        finiteOrNull_(item.top_pressure_hpa)

    };

  }



  return {

    available:

      (

        mt.available === true ||

        ivt.available === true ||

        Object.keys(layers).length > 0

      ),



    error: mt.error || '',



    total: {

      available: ivt.available === true,

      magnitude:

        finiteOrNull_(ivt.magnitude_kg_m1_s1),

      direction:

        finiteOrNull_(ivt.transport_to_direction_deg),

      bottomPressure:

        finiteOrNull_(ivt.bottom_pressure_hpa),

      topPressure:

        finiteOrNull_(ivt.top_pressure_hpa)

    },



    layers: [

      layerItem_('surface_850', 'SFC–850'),

      layerItem_('surface_700', 'SFC–700'),

      layerItem_('850_700', '850–700'),

      layerItem_('700_500', '700–500')

    ],



    humidity: [

      {

        label: 'q surface',

        value: humidityValue_('surface')

      },

      {

        label: 'q925',

        value: humidityValue_('925')

      },

      {

        label: 'q850',

        value: humidityValue_('850')

      },

      {

        label: 'q700',

        value: humidityValue_('700')

      }

    ]

  };

}





function buildOrographicTransport_(metpy) {

  const mt = metpy.moisture_transport || {};

  const block = mt.orographic_cross_barrier || {};

  const barriers = block.barriers || {};



  const order = [

    'dinaric_west',

    'julian_south',

    'julian_central',

    'kamnik_savinja'

  ];



  return {

    available:

      (

        block.available === true &&

        Object.keys(barriers).length > 0

      ),



    version: finiteOrNull_(block.version),

    primaryMetric: block.primary_metric || '',

    interpretation: block.interpretation || '',



    rows: order

      .map(function(key) {

        const barrier = barriers[key];



        if (!barrier) {

          return null;

        }



        let primary = null;

        let layerLabel = '';

        let mode = '';



        const terrain =

          barrier.terrain_capped_ivt || null;



        if (

          terrain &&

          terrain.available !== false &&

          finiteOrNull_(

            terrain.magnitude_kg_m1_s1

          ) !== null

        ) {

          primary = terrain;



          const targetHeight =

            finiteOrNull_(

              barrier.representative_barrier_height_msl_m

            );



          layerLabel =

            targetHeight !== null

              ? (

                  'SFC → ' +

                  Math.round(targetHeight) +

                  ' m MSL'

                )

              : 'Terrain-capped layer';



          mode = 'terrain';

        } else {

          // Compatibility with orographic V1/V2 JSON files.

          const legacy =

            barrier.ivt || {};



          if (

            key === 'dinaric_west' &&

            legacy.surface_850

          ) {

            primary = legacy.surface_850;

            layerLabel = 'SFC–850 hPa';

          } else if (legacy.surface_700) {

            primary = legacy.surface_700;

            layerLabel = 'SFC–700 hPa';

          } else if (legacy.surface_850) {

            primary = legacy.surface_850;

            layerLabel = 'SFC–850 hPa';

          }



          mode = primary ? 'pressure_fallback' : '';

        }



        if (!primary) {

          return {

            key: key,

            label: barrier.label || key,

            normalAzimuth:

              finiteOrNull_(

                barrier.upslope_normal_azimuth_deg

              ),

            layerLabel: '—',

            mode: '',

            magnitude: null,

            signed: null,

            upslope: null,

            fractionPct: null,

            angleDeg: null,

            topPressure: null,

            barrierHeight: finiteOrNull_(

              barrier.representative_barrier_height_msl_m

            )

          };

        }



        return {

          key: key,

          label: barrier.label || key,

          normalAzimuth:

            finiteOrNull_(

              barrier.upslope_normal_azimuth_deg

            ),

          layerLabel: layerLabel,

          mode: mode,

          magnitude:

            finiteOrNull_(

              primary.magnitude_kg_m1_s1

            ),

          signed:

            finiteOrNull_(

              primary.signed_cross_barrier_kg_m1_s1

            ),

          upslope:

            finiteOrNull_(

              primary.upslope_kg_m1_s1

            ),

          fractionPct:

            finiteOrNull_(primary.signed_fraction) === null
              ? null
              : finiteOrNull_(primary.signed_fraction) * 100,

          angleDeg:

            finiteOrNull_(

              primary.angle_to_upslope_normal_deg

            ),

          topPressure:

            finiteOrNull_(

              primary.top_pressure_hpa

            ),

          barrierHeight:

            finiteOrNull_(

              barrier.representative_barrier_height_msl_m

            )

        };

      })

      .filter(function(item) {

        return item !== null;

      })

  };

}





function buildClimatology_(climatology) {

  const p = climatology.parameters || {};



  const definitions = [

    ['t850', 'T850', '°C'],

    ['t700', 'T700', '°C'],

    ['t500', 'T500', '°C'],

    ['pwat_mm', 'PWAT', 'mm'],

    ['lifted_index_c', 'Lifted Index', '°C'],

    ['freezing_level_msl_m', 'Freezing level', 'm'],

    ['q_surface_gkg', 'q surface', 'g/kg'],

    ['q925_gkg', 'q925', 'g/kg'],

    ['q850_gkg', 'q850', 'g/kg'],

    ['ivt_kg_m1_s1', 'Total IVT', 'kg m⁻¹ s⁻¹'],

    ['wind_speed_925_ms', 'Wind 925', 'm/s'],

    ['wind_speed_850_ms', 'Wind 850', 'm/s'],

    ['wind_speed_700_ms', 'Wind 700', 'm/s'],

    ['wind_speed_500_ms', 'Wind 500', 'm/s'],

    ['wind_speed_300_ms', 'Wind 300', 'm/s'],

    ['shear_0_1km_ms', '0–1 km shear', 'm/s'],

    ['shear_0_3km_ms', '0–3 km shear', 'm/s'],

    ['shear_0_6km_ms', '0–6 km shear', 'm/s'],

    ['shear_sfc_700_ms', 'SFC–700 shear', 'm/s'],

    ['z500_m', 'Z500', 'm'],

    ['thickness_925_500_m', '925–500 thickness', 'm'],

    ['mucape_jkg', 'MUCAPE', 'J/kg'],

    ['lapse_rate_850_500_c_per_km', '850–500 hPa lapse rate', '°C/km'],

    ['lapse_rate_700_500_c_per_km', '700–500 hPa lapse rate', '°C/km']

  ];



  return definitions

    .map(function(def) {

      const item = p[def[0]];

      if (!item) {

        return null;

      }



      return {

        key: def[0],

        label: def[1],

        unit: def[2],

        value: finiteOrNull_(item.value),

        percentile: finiteOrNull_(item.percentile),

        n: finiteOrNull_(item.n),

        p10: finiteOrNull_(item.p10),

        p50: finiteOrNull_(item.p50),

        p90: finiteOrNull_(item.p90),

        p99: finiteOrNull_(item.p99),

        historicalMin:

          finiteOrNull_(item.historical_min),

        historicalMinDate:

          item.historical_min_date || '',

        historicalMax:

          finiteOrNull_(item.historical_max),

        historicalMaxDate:

          item.historical_max_date || ''

      };

    })

    .filter(function(item) {

      return item !== null;

    });

}





function buildInversions_(inversions) {

  if (!inversions || inversions.available !== true) {

    return [];

  }



  return (inversions.layers || [])

    .map(function(layer) {

      return {

        type: layer.type || '',

        baseAglM: finiteOrNull_(layer.base_agl_m),

        topAglM: finiteOrNull_(layer.top_agl_m),

        baseMslM: finiteOrNull_(layer.base_msl_m),

        topMslM: finiteOrNull_(layer.top_msl_m),

        depthM: finiteOrNull_(layer.depth_m),

        deltaTC: finiteOrNull_(layer.delta_temperature_c),

        gradientCKm:

          finiteOrNull_(layer.temperature_gradient_c_per_km),

        strength:

          layer.temperature_inversion_strength || '',

        deltaTdC:

          finiteOrNull_(layer.delta_dewpoint_c),

        deltaThetaEK:

          finiteOrNull_(layer.delta_theta_e_k),

        boundaryStrength:

          layer.thermodynamic_boundary_strength || ''

      };

    })

    .slice(0, 8);

}





function buildChangeBlock_(block, label) {

  if (!block || block.available !== true) {

    return {

      available: false,

      label: label,

      rows: []

    };

  }



  const p = block.parameters || {};



  const definitions = [

    ['t850_c', 'T850'],

    ['t700_c', 'T700'],

    ['t500_c', 'T500'],

    ['pwat_mm', 'PWAT'],

    ['freezing_level_msl_m', 'Freezing level'],

    ['mucape_jkg', 'MUCAPE'],

    ['shear_0_6km_ms', '0–6 km shear'],

    ['ivt_kg_m1_s1', 'IVT'],

    ['lapse_rate_850_500_c_per_km', '850–500 lapse rate']

  ];



  return {

    available: true,

    label: label,

    previousLaunchTime:

      block.previous_launch_time || '',

    previousNominalDate:

      block.previous_nominal_date || '',

    previousTerm:

      block.previous_term || '',

    rows: definitions

      .map(function(def) {

        const item = p[def[0]];

        if (!item) {

          return null;

        }



        return {

          key: def[0],

          label: def[1],

          current: finiteOrNull_(item.current),

          previous: finiteOrNull_(item.previous),

          delta: finiteOrNull_(item.delta),

          unit: normalizeUnit_(item.unit || '')

        };

      })

      .filter(function(item) {

        return item !== null;

      })

  };

}





function normalizeUnit_(unit) {

  const map = {

    'degC': '°C',

    'degC/km': '°C/km',

    'kg m-1 s-1': 'kg m⁻¹ s⁻¹',

    'g/kg': 'g/kg'

  };

  return map[unit] || unit;

}





function buildImageUrls_(

  profile,

  products,

  version,

  usingLatest

) {

  let paths;



  if (

    usingLatest &&

    products &&

    products.skewt &&

    products.hodograph

  ) {

    paths = {

      skewt:

        products.skewt ||

        'diagnostics/latest_skewt.png',

      skewtZoom:

        products.skewt_zoom ||

        'diagnostics/latest_skewt_zoom.png',

      thetae:

        products.thetae ||

        'diagnostics/latest_thetae.png',

      hodograph:

        products.hodograph ||

        'diagnostics/latest_hodograph.png'

    };

  } else {

    const date = String(

      profile.nominal_date || ''

    ).replace(/-/g, '');



    const parts = String(

      profile.nominal_date || ''

    ).split('-');



    const yyyy =

      parts.length >= 1

        ? parts[0]

        : 'unknown';



    const mm =

      parts.length >= 2

        ? parts[1]

        : 'unknown';



    const term =

      String(profile.term || 'unknown')

        .replace(/\//g, '-');



    const prefix =

      'diagnostics/' +

      yyyy + '/' +

      mm + '/' +

      date + '_' +

      term;



    paths = {

      skewt: prefix + '_skewt.png',

      skewtZoom:

        prefix + '_skewt_zoom.png',

      thetae:

        prefix + '_thetae.png',

      hodograph:

        prefix + '_hodograph.png'

    };

  }



  return {

    skewt:

      rawUrl_(paths.skewt, version),

    skewtZoom:

      rawUrl_(paths.skewtZoom, version),

    thetae:

      rawUrl_(paths.thetae, version),

    hodograph:

      rawUrl_(paths.hodograph, version)

  };

}





function resolveSoundingStatus_(

  profile,

  status

) {

  const key =

    String(profile.nominal_date || '') +

    '_' +

    String(profile.term || '');



  if (

    status &&

    status.terms &&

    status.terms[key]

  ) {

    return status.terms[key].status || 'ok';

  }



  return 'ok';

}





function buildArchiveOptions_(status) {

  if (!status || !status.terms) {

    return [];

  }



  return Object.keys(status.terms)

    .map(function(key) {

      const item = status.terms[key];



      if (

        !item ||

        item.status !== 'ok' ||

        !item.archive_file

      ) {

        return null;

      }



      return {

        key: key,

        nominalDate:

          item.nominal_date || '',

        term:

          item.term || '',

        launchTime:

          item.launch_time || '',

        archiveFile:

          item.archive_file

      };

    })

    .filter(function(item) {

      return item !== null;

    })

    .sort(function(a, b) {

      const aa =

        a.nominalDate +

        '_' +

        a.term;



      const bb =

        b.nominalDate +

        '_' +

        b.term;



      return bb.localeCompare(aa);

    });

}





function climPercentile_(

  climatology,

  key

) {

  if (

    !climatology ||

    !climatology.parameters ||

    !climatology.parameters[key]

  ) {

    return null;

  }



  return climatology

    .parameters[key]

    .percentile;

}





function nestedValue_(

  object,

  key,

  subkey

) {

  if (

    !object ||

    !object[key]

  ) {

    return null;

  }



  return object[key][subkey];

}





function finiteOrNull_(value) {

  const n = Number(value);



  if (

    value === null ||

    value === undefined ||

    value === '' ||

    !isFinite(n)

  ) {

    return null;

  }



  return n;

}





function rawUrl_(path, version) {

  return (

    LJLM_CONFIG.RAW_BASE +

    '/' +

    String(path)

      .replace(/^\/+/, '') +

    '?v=' +

    encodeURIComponent(

      String(version || Date.now())

    )

  );

}





function fetchJson_(path) {

  const result =

    fetchJsonResponse_(path);



  if (!result.ok) {

    throw new Error(

      'Could not fetch ' +

      path +

      ' (HTTP ' +

      result.code +

      ')'

    );

  }



  return result.data;

}





function fetchJsonOptional_(path) {

  try {

    const result =

      fetchJsonResponse_(path);



    return result.ok

      ? result.data

      : null;

  } catch (error) {

    return null;

  }

}





function fetchJsonResponse_(path) {

  const url =

    LJLM_CONFIG.RAW_BASE +

    '/' +

    String(path)

      .replace(/^\/+/, '') +

    '?ts=' +

    Date.now();



  const response =

    UrlFetchApp.fetch(

      url,

      {

        muteHttpExceptions: true,

        followRedirects: true

      }

    );



  const code =

    response.getResponseCode();



  if (

    code < 200 ||

    code >= 300

  ) {

    return {

      ok: false,

      code: code,

      data: null

    };

  }



  return {

    ok: true,

    code: code,

    data: JSON.parse(

      response.getContentText()

    )

  };

}

// ============================================================
// ICON-D2 +12 h VERIFICATION
// ============================================================

function getSoundingComparison() {
  const obs = fetchJson_(LJLM_CONFIG.LATEST_PATH);

  const term = String(obs.term || '').padStart(2, '0');

  if (term !== '00' && term !== '12') {
    return {
      available: false,
      status: 'special_sounding',
      message:
        'The latest sounding is not a regular 00 or 12 UTC sounding.',
      observation: comparisonObservationMeta_(obs)
    };
  }

  const obsValidTime =
    comparisonObservationValidTime_(obs);

  const modelPath =
    'models/icon-d2/latest/' +
    term +
    '/sounding.json';

  const model = fetchJsonOptional_(modelPath);

  if (!model) {
    return {
      available: false,
      status: 'model_missing',
      message:
        'The matching ICON-D2 +12 h forecast is not available yet.',
      observationValidTime: obsValidTime,
      observation: comparisonObservationMeta_(obs)
    };
  }

  const modelValidTime =
    model.valid_time || '';

  if (
    !comparisonSameInstant_(
      obsValidTime,
      modelValidTime
    )
  ) {
    return {
      available: false,
      status: 'time_mismatch',
      message:
        'The latest observation and ICON-D2 forecast do not verify at the same time yet.',
      observationValidTime: obsValidTime,
      modelValidTime: modelValidTime,
      observation: comparisonObservationMeta_(obs),
      model: comparisonModelMeta_(model)
    };
  }

  const pressures = [925, 850, 700, 500, 300];

  const levels =
    pressures.map(function(pressure) {
      const observed =
        comparisonInterpolateProfile_(
          obs.levels || [],
          pressure
        );

      const forecast =
        comparisonInterpolateProfile_(
          model.levels || [],
          pressure
        );

      if (!observed || !forecast) {
        return {
          pressureHpa: pressure,
          available: false
        };
      }

      const du =
        comparisonDifference_(
          forecast.uMs,
          observed.uMs
        );

      const dv =
        comparisonDifference_(
          forecast.vMs,
          observed.vMs
        );

      const vectorError =
        (
          du !== null &&
          dv !== null
        )
          ? Math.sqrt(
              du * du +
              dv * dv
            )
          : null;

      return {
        pressureHpa: pressure,
        available: true,

        observation: observed,
        model: forecast,

        difference: {
          temperatureC:
            comparisonDifference_(
              forecast.temperatureC,
              observed.temperatureC
            ),

          dewpointC:
            comparisonDifference_(
              forecast.dewpointC,
              observed.dewpointC
            ),

          windSpeedMs:
            comparisonDifference_(
              forecast.windSpeedMs,
              observed.windSpeedMs
            ),

          uMs: du,
          vMs: dv,

          windVectorErrorMs:
            finiteOrNull_(vectorError)
        }
      };
    });

  const obsMetpy =
    (
      (
        obs.parameters || {}
      ).metpy || {}
    );

  const modelMetpy =
    (
      (
        model.parameters || {}
      ).metpy || {}
    );

  const diagnostics = [
    comparisonMetric_(
      'PWAT',
      obsMetpy.pwat_mm,
      modelMetpy.pwat_mm,
      'mm'
    ),

    comparisonMetric_(
      'Lifted Index',
      obsMetpy.lifted_index_c,
      modelMetpy.lifted_index_c,
      '°C'
    ),

    comparisonMetric_(
      'MUCAPE',
      obsMetpy.mucape_jkg,
      modelMetpy.mucape_jkg,
      'J/kg'
    ),

    comparisonMetric_(
      'Freezing level',
      obsMetpy.freezing_level_msl_m,
      modelMetpy.freezing_level_msl_m,
      'm'
    ),

    comparisonMetric_(
      '0–1 km shear',
      obsMetpy.shear_0_1km_ms,
      modelMetpy.shear_0_1km_ms,
      'm/s'
    ),

    comparisonMetric_(
      '0–3 km shear',
      obsMetpy.shear_0_3km_ms,
      modelMetpy.shear_0_3km_ms,
      'm/s'
    ),

    comparisonMetric_(
      '0–6 km shear',
      obsMetpy.shear_0_6km_ms,
      modelMetpy.shear_0_6km_ms,
      'm/s'
    ),

    comparisonMetric_(
      'Total IVT',
      comparisonNestedFinite_(
        obsMetpy,
        [
          'moisture_transport',
          'ivt',
          'magnitude_kg_m1_s1'
        ]
      ),
      comparisonNestedFinite_(
        modelMetpy,
        [
          'moisture_transport',
          'ivt',
          'magnitude_kg_m1_s1'
        ]
      ),
      'kg m⁻¹ s⁻¹'
    )
  ];

  const obsVersion =
    (
      obs.processed_at ||
      obs.retrieved_at ||
      obs.launch_time ||
      Date.now()
    );

  const modelVersion =
    (
      model.processed_at ||
      model.valid_time ||
      Date.now()
    );

  return {
    available: true,
    status: 'matched',
    convention: 'Difference = ICON-D2 − OBS',
    validTime: obsValidTime,

    observation:
      comparisonObservationMeta_(obs),

    model:
      comparisonModelMeta_(model),

    levels: levels,
    diagnostics: diagnostics,

    rmse: {
      temperatureC:
        comparisonRmse_(
          levels,
          'temperatureC'
        ),

      dewpointC:
        comparisonRmse_(
          levels,
          'dewpointC'
        ),

      windSpeedMs:
        comparisonRmse_(
          levels,
          'windSpeedMs'
        )
    },

    images: {
      observation: {
        full:
          rawUrl_(
            'diagnostics/latest_skewt.png',
            obsVersion
          ),

        zoom:
          rawUrl_(
            'diagnostics/latest_skewt_zoom.png',
            obsVersion
          ),

        thetae:
          rawUrl_(
            'diagnostics/latest_thetae.png',
            obsVersion
          ),

        hodograph:
          rawUrl_(
            'diagnostics/latest_hodograph.png',
            obsVersion
          )
      },

      model: {
        full:
          rawUrl_(
            'models/icon-d2/latest/' +
            term +
            '/skewt.png',
            modelVersion
          ),

        zoom:
          rawUrl_(
            'models/icon-d2/latest/' +
            term +
            '/lowlevel.png',
            modelVersion
          ),

        thetae:
          rawUrl_(
            'models/icon-d2/latest/' +
            term +
            '/thetae.png',
            modelVersion
          ),

        hodograph:
          rawUrl_(
            'models/icon-d2/latest/' +
            term +
            '/hodograph.png',
            modelVersion
          )
      }
    }
  };
}


function comparisonObservationMeta_(profile) {
  return {
    station:
      profile.station || 14015,

    stationName:
      profile.station_name || 'Ljubljana',

    soundingId:
      profile.sounding_id || '',

    nominalDate:
      profile.nominal_date || '',

    term:
      String(profile.term || ''),

    launchTime:
      profile.launch_time || ''
  };
}


function comparisonModelMeta_(profile) {
  return {
    model:
      profile.model || 'ICON-D2',

    soundingId:
      profile.sounding_id || '',

    runTime:
      profile.run_time || '',

    validTime:
      profile.valid_time || '',

    leadHours:
      finiteOrNull_(profile.lead_hours),

    gridLatitude:
      finiteOrNull_(profile.grid_latitude),

    gridLongitude:
      finiteOrNull_(profile.grid_longitude)
  };
}


function comparisonObservationValidTime_(profile) {
  const nominalDate =
    String(
      profile.nominal_date || ''
    );

  const term =
    String(
      profile.term || ''
    ).padStart(2, '0');

  if (
    !nominalDate ||
    (term !== '00' && term !== '12')
  ) {
    return '';
  }

  return (
    nominalDate +
    'T' +
    term +
    ':00:00Z'
  );
}


function comparisonSameInstant_(a, b) {
  if (!a || !b) {
    return false;
  }

  const aa =
    new Date(a).getTime();

  const bb =
    new Date(b).getTime();

  return (
    isFinite(aa) &&
    isFinite(bb) &&
    aa === bb
  );
}


function comparisonInterpolateProfile_(
  levels,
  targetPressure
) {
  if (
    !Array.isArray(levels) ||
    levels.length < 2
  ) {
    return null;
  }

  const rows =
    levels
      .filter(function(row) {
        return (
          row &&
          finiteOrNull_(
            row.pressure_hpa
          ) !== null
        );
      })
      .slice()
      .sort(function(a, b) {
        return (
          Number(b.pressure_hpa) -
          Number(a.pressure_hpa)
        );
      });

  for (
    let i = 0;
    i < rows.length - 1;
    i++
  ) {
    const lower = rows[i];
    const upper = rows[i + 1];

    const p1 =
      Number(lower.pressure_hpa);

    const p2 =
      Number(upper.pressure_hpa);

    if (
      p1 >= targetPressure &&
      p2 <= targetPressure
    ) {
      const wind1 =
        comparisonWindComponents_(lower);

      const wind2 =
        comparisonWindComponents_(upper);

      const result = {
        pressureHpa:
          targetPressure,

        temperatureC:
          comparisonInterpLogP_(
            lower,
            upper,
            targetPressure,
            'temperature_c'
          ),

        dewpointC:
          comparisonInterpLogP_(
            lower,
            upper,
            targetPressure,
            'dewpoint_c'
          ),

        heightM:
          comparisonInterpLogP_(
            lower,
            upper,
            targetPressure,
            'height_m'
          ),

        windSpeedMs: null,
        windDirectionDeg: null,
        uMs: null,
        vMs: null
      };

      if (wind1 && wind2) {
        const u =
          comparisonInterpLogP_(
            {
              pressure_hpa:
                lower.pressure_hpa,
              value:
                wind1.uMs
            },
            {
              pressure_hpa:
                upper.pressure_hpa,
              value:
                wind2.uMs
            },
            targetPressure,
            'value'
          );

        const v =
          comparisonInterpLogP_(
            {
              pressure_hpa:
                lower.pressure_hpa,
              value:
                wind1.vMs
            },
            {
              pressure_hpa:
                upper.pressure_hpa,
              value:
                wind2.vMs
            },
            targetPressure,
            'value'
          );

        if (
          finiteOrNull_(u) !== null &&
          finiteOrNull_(v) !== null
        ) {
          result.uMs = u;
          result.vMs = v;

          result.windSpeedMs =
            Math.sqrt(
              u * u +
              v * v
            );

          result.windDirectionDeg =
            comparisonWindDirectionFromUV_(
              u,
              v
            );
        }
      }

      [
        'temperatureC',
        'dewpointC',
        'heightM',
        'windSpeedMs',
        'windDirectionDeg',
        'uMs',
        'vMs'
      ].forEach(function(key) {
        result[key] =
          finiteOrNull_(
            result[key]
          );
      });

      return result;
    }
  }

  return null;
}


function comparisonInterpLogP_(
  a,
  b,
  targetPressure,
  field
) {
  const y1 =
    finiteOrNull_(a[field]);

  const y2 =
    finiteOrNull_(b[field]);

  if (
    y1 === null ||
    y2 === null
  ) {
    return null;
  }

  const p1 =
    Math.log(
      Number(a.pressure_hpa)
    );

  const p2 =
    Math.log(
      Number(b.pressure_hpa)
    );

  const pt =
    Math.log(
      Number(targetPressure)
    );

  if (p1 === p2) {
    return y1;
  }

  const fraction =
    (pt - p1) /
    (p2 - p1);

  return (
    y1 +
    fraction *
    (y2 - y1)
  );
}


function comparisonWindComponents_(row) {
  const directU =
    finiteOrNull_(row.u_ms);

  const directV =
    finiteOrNull_(row.v_ms);

  if (
    directU !== null &&
    directV !== null
  ) {
    return {
      uMs: directU,
      vMs: directV
    };
  }

  const speed =
    finiteOrNull_(
      row.wind_speed_ms
    );

  const direction =
    finiteOrNull_(
      row.wind_direction_deg
    );

  if (
    speed === null ||
    direction === null
  ) {
    return null;
  }

  const radians =
    direction *
    Math.PI /
    180;

  return {
    uMs:
      -speed *
      Math.sin(radians),

    vMs:
      -speed *
      Math.cos(radians)
  };
}


function comparisonWindDirectionFromUV_(u, v) {
  if (
    finiteOrNull_(u) === null ||
    finiteOrNull_(v) === null
  ) {
    return null;
  }

  let direction =
    Math.atan2(
      -u,
      -v
    ) *
    180 /
    Math.PI;

  if (direction < 0) {
    direction += 360;
  }

  return direction;
}


function comparisonDifference_(model, observation) {
  const mm =
    finiteOrNull_(model);

  const oo =
    finiteOrNull_(observation);

  if (
    mm === null ||
    oo === null
  ) {
    return null;
  }

  return mm - oo;
}


function comparisonMetric_(
  label,
  observation,
  model,
  unit
) {
  const obsValue =
    finiteOrNull_(observation);

  const modelValue =
    finiteOrNull_(model);

  return {
    label: label,
    unit: unit,
    observation: obsValue,
    model: modelValue,
    difference:
      (
        obsValue !== null &&
        modelValue !== null
      )
        ? modelValue - obsValue
        : null
  };
}


function comparisonNestedFinite_(
  object,
  path
) {
  let current = object;

  for (
    let i = 0;
    i < path.length;
    i++
  ) {
    if (
      !current ||
      !Object.prototype.hasOwnProperty.call(
        current,
        path[i]
      )
    ) {
      return null;
    }

    current =
      current[path[i]];
  }

  return finiteOrNull_(current);
}


function comparisonRmse_(
  levels,
  key
) {
  const values = [];

  (levels || [])
    .forEach(function(row) {
      if (
        !row ||
        row.available !== true ||
        !row.difference
      ) {
        return;
      }

      const value =
        finiteOrNull_(
          row.difference[key]
        );

      if (value !== null) {
        values.push(value);
      }
    });

  if (!values.length) {
    return null;
  }

  const mse =
    values.reduce(
      function(sum, value) {
        return (
          sum +
          value * value
        );
      },
      0
    ) /
    values.length;

  return Math.sqrt(mse);
}


function testSoundingComparison() {
  Logger.log(
    JSON.stringify(
      getSoundingComparison(),
      null,
      2
    )
  );
}


// Only compact observation windows may be requested by the browser.
function getDashboardTimeseries(windowKey) {
  if (['7d', '30d', '90d', '1y'].indexOf(windowKey) === -1) {
    throw new Error('Unknown observation window.');
  }
  const manifest = fetchJson_(LJLM_CONFIG.MANIFEST_PATH);
  const series = manifest.timeseries || {};
  const path = (series.windows || {})[windowKey];
  if (series.available !== true || path !== 'timeseries/ljlm/latest_' + windowKey + '.json') {
    throw new Error('Observation window is unavailable.');
  }
  const cache = CacheService.getScriptCache();
  const cacheKey = 'ljlm-timeseries:' + windowKey;
  const cached = cache.get(cacheKey);
  if (cached) {
    const result = JSON.parse(cached);
    result.variables = series.variables || [];
    return result;
  }
  const data = fetchJson_(path);
  if (!Array.isArray(data.records)) {
    throw new Error('Invalid observation time series.');
  }
  const result = {
    window: windowKey,
    variables: series.variables || [],
    generatedAt: data.generated_at || '',
    startTime: data.start_time || '',
    endTime: data.end_time || '',
    records: data.records
  };
  const encoded = JSON.stringify(result);
  // Apps Script cache entries are limited to 100 KB; larger windows still load.
  if (Utilities.newBlob(encoded).getBytes().length < 95000) {
    cache.put(cacheKey, encoded, LJLM_CONFIG.CACHE_SECONDS);
  }
  return result;
}
