# C-LAEF point-cache publisher

Run `python3 claef_point_forecast.py`. The point builder uses only Python's standard library and the validated helpers in `probe_claef.py`; no packages from `requirements.txt` are required. Output is `models/claef/latest/point.json`, with a small `status.json` beside it.

Schema version 2 adds explicit `forecast_start`, `forecast_end`, and `forecast_hour_count`. Dataset identifiers, source reference times, requested coordinates, each source's returned grid point, retrieval time, native units, temporal semantics, and per-record valid times/lead hours remain explicit. Deterministic values and P10/P50/P90 values are separate. No meteorological conclusions or new derived wind quantities are added.

The existing verified point output was migrated locally by adding these schema fields without fetching or changing any forecast values or its original `retrieved_at`. The successful status product uses that same reference/retrieval time and count.

The new `.github/workflows/update-claef-point.yml` prepares manual dispatch and hourly `25 * * * *` runs using Python 3.11. It tests offline before retrieving or publishing, installs no unnecessary packages, and stages exactly `models/claef/latest/point.json` and `models/claef/latest/status.json`. Local cache-gate and lock files, profile experiments, probe samples, and other model products are excluded. Unchanged output exits without a commit. Rebase conflicts or persistent push failures stop publication; no force push is used. The per-branch concurrency group does not cancel an active publisher.

The script makes two metadata requests per refresh and two additional small point requests only for a new cycle or changed schema/inventory. A persistent local 15-minute gate also covers failed requests; HTTP 429 reset information can extend it. A GitHub runner's temporary gate is intentionally not published: the hourly schedule and same-cycle cache check keep its volume low (normally two requests/hour; four when refreshing). There is no automatic API retry loop. Matching deterministic/ensemble cycles are selected from advertised available reference times, and response cycles, units and complete coverage are validated before replacement. The cache cannot be downgraded to an older cycle.

`status.json` is deliberately small:

- `status`: `ok`, `unavailable`, or `error`.
- `model_reference_time`, `retrieved_at`: identify the last stored forecast, not the latest attempted request.
- `forecast_hour_count`: number of stored valid times.
- On failure only: `attempted_at`, `message`, and `previous_forecast_preserved`.

Successful same-cycle runs do not update retrieval timestamps or cause status-only heartbeat commits. An error may change local status, but the nonzero builder exit prevents the workflow's publisher step from running. The previous valid point file remains intact; the Actions run reports the failure. No empty or zero-filled replacement is published. Point and status writes each use an fsynced same-directory temporary file and atomic replacement. The point product is self-contained and remains authoritative if the two separate atomic writes are interrupted between them.

Native precipitation and snowfall amounts are retained for the last hourly forecast interval, not interpreted as cumulative since initialization. +0 nulls stay null. Exact radiation averaging bounds are not specified by point metadata; no snow-mass-to-depth conversion or summation of marginal percentiles is performed.

Local verification: `python3 test_claef_point_forecast.py`, workflow YAML/shell syntax checks, and `git diff --check`. The workflow has not been run on GitHub and will become active only when its files are committed to the appropriate branch. No commit was made in this task.
