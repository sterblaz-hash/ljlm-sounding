# LJLM sounding dashboard

These files are the source of the production LJLM sounding dashboard:

- `Code.gs` is the Google Apps Script backend.
- `Index.html` is the browser dashboard UI.

Deployment to Google Apps Script is manual for now.

The **Potek** view loads compact observation summaries only when opened. It
supports 7-day, 30-day, 90-day, and 1-year windows resolved through
`data/dashboard_manifest.json`; it does not load individual archived profiles
or the large daily climatology file. Loaded windows are reused for the browser
session. Reload the dashboard to obtain fresh windows. The backend uses a
45-second script cache for summaries that fit Apps Script's cache entry limit.

Graphs use `valid_time` (nominal UTC), preserve missing values and absent terms
as gaps, and retain signed IVT values. Signed fractions are displayed as percent
without changing stored data. Select a point by mouse, touch, or keyboard for
its UTC time and value. The sounding view has a collapsible additional
diagnostics section; climatology comparisons use only the compact parameters
embedded in each profile, including Lifted Index when available.

For manual deployment, copy `Code.gs` and `Index.html` into the corresponding
Google Apps Script project files and update the web app deployment. No automatic
deployment is configured here.

Run offline backend and browser regression checks from the repository root:

```sh
node apps-script/test-dashboard.cjs
```

These checks mock Apps Script and the DOM. Verify the deployed web app on desktop
and mobile after manual deployment, including window switching and touch details.

The **Klimatologija** view lazily loads one compact annual parameter curve from
`climatology/dashboard/`, resolved through the dashboard manifest and compact
index. It shows P10–P90 and P25–P75 bands, P50, and the latest equivalent OBS
value at its nominal UTC calendar date. Loaded parameters are cached for the
browser session; reload for fresh observations. No percentile is inferred from
the plotted bands. The historical nominal-time caveat is shown with the source
reference period, window, and smoothing description.

After updating the historical climatology, rebuild its compact dashboard files:

```sh
python3 build_dashboard_climatology.py
```

Normal observation builder runs retain the compact climatology manifest entry
when the index exists. The compact builder reads the historical source locally
without recalculating it; Apps Script never fetches the large daily source.
