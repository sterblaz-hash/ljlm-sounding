#!/usr/bin/env python3
"""Read-only health report for published LJLM OBS, ICON-D2 and C-LAEF products.

Reads published local data and optionally queries GitHub Actions. Never runs model extraction.
Writes only its own data/system_health.json report. Publication deadlines are provisional.
"""
import argparse
import os
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from datetime import datetime, timedelta, time, timezone
import json
from pathlib import Path

UTC = timezone.utc
# Conservative deadline after the nominal valid UTC time; adjust after monitoring.
DEADLINE_HOURS = {'obs': 4, 'icon_d2': 4, 'claef_sounding': 7}
PRODUCTS = (
    ('obs', 'OBS Ljubljana', 'data/status.json'),
    ('icon_d2', 'ICON-D2 +12 h', 'models/icon-d2/latest/{slot}/sounding.json'),
    ('claef_sounding', 'C-LAEF +12 h', 'models/claef/latest/{slot}/sounding.json'),
)
POINT_FILE = 'models/claef/latest/status.json'
POINT_PRODUCT = 'models/claef/latest/point.json'
POINT_MAX_AGE_HOURS = 12
REPO = 'sterblaz-hash/ljlm-sounding'
WORKFLOWS = (
    ('obs', 'OBS Ljubljana', 'update-sounding.yml'),
    ('icon_d2', 'ICON-D2 +12 h', 'update-icon-d2-forecast.yml'),
    ('claef_sounding', 'C-LAEF +12 h', 'update-claef-sounding.yml'),
    ('claef_point', 'C-LAEF point', 'update-claef-point.yml'),
)


def parse_utc(value):
    if not isinstance(value, str):
        raise ValueError('Timestamp is missing or not a string')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() is None:
        raise ValueError('Timestamp has no UTC offset')
    return result.astimezone(UTC)


def iso(value):
    return value.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def read_json(root, relative):
    try:
        path = root / relative
        with path.open('r', encoding='utf-8') as source:
            return json.load(source), None
    except (OSError, ValueError, UnicodeError) as exc:
        return None, f'Cannot read {relative}: {type(exc).__name__}'


def expected_valid(now, slot, hours):
    """Most recent nominal 00/12 UTC term whose publication deadline passed."""
    h = int(slot)
    expected = datetime.combine(now.date(), time(h), tzinfo=UTC)
    if now < expected + timedelta(hours=hours):
        expected -= timedelta(days=1)
    return expected


def next_expected(now, slot, hours):
    """Return next nominal UTC term and its provisional publication deadline."""
    today = datetime.combine(now.date(), time(int(slot)), tzinfo=UTC)
    due = today + timedelta(hours=hours)
    if now >= due:
        today += timedelta(days=1)
        due += timedelta(days=1)
    return iso(today), iso(due)


def model_term(root, kind, label, pattern, slot, now):
    relative = pattern.format(slot=slot)
    expected = expected_valid(now, slot, DEADLINE_HOURS[kind])
    product, error = read_json(root, relative)
    next_term, next_due = next_expected(now, slot, DEADLINE_HOURS[kind])
    base = dict(id=f'{kind}_{slot}', name=f'{label} · {slot} UTC', kind=kind, slot=slot,
                expected_valid=iso(expected), upcoming_valid=next_term, upcoming_due_at=next_due,
                source=relative)
    if error or not isinstance(product, dict):
        return {**base, 'status': 'unavailable', 'actual_valid': None,
                'detail': error or 'Invalid JSON product'}
    try:
        actual = parse_utc(product['valid_time'])
        run = parse_utc(product['run_time'])
        lead = product['lead_hours']
        if lead != 12 or actual != run + timedelta(hours=12):
            raise ValueError('Invalid model reference time / +12 h lead')
        if not isinstance(product.get('levels'), list) or not product['levels']:
            raise ValueError('Missing sounding levels')
    except (KeyError, TypeError, ValueError) as exc:
        return {**base, 'status': 'invalid', 'actual_valid': product.get('valid_time'),
                'detail': f'Invalid sounding metadata: {exc}'}
    if actual < expected:
        return {**base, 'status': 'stale', 'actual_valid': iso(actual),
                'detail': 'Published model slot is older than the latest expected term'}
    return {**base, 'status': 'ok', 'actual_valid': iso(actual),
            'detail': 'Published +12 h forecast covers the expected term'}


def obs_term(root, slot, now):
    expected = expected_valid(now, slot, DEADLINE_HOURS['obs'])
    relative = 'data/status.json'
    next_term, next_due = next_expected(now, slot, DEADLINE_HOURS['obs'])
    base = dict(id=f'obs_{slot}', name=f'OBS Ljubljana · {slot} UTC', kind='obs', slot=slot,
                expected_valid=iso(expected), upcoming_valid=next_term, upcoming_due_at=next_due,
                source=relative)
    status_data, error = read_json(root, relative)
    if error or not isinstance(status_data, dict):
        return {**base, 'status': 'unavailable', 'actual_valid': None,
                'detail': error or 'Invalid OBS status JSON'}
    terms = status_data.get('terms')
    if not isinstance(terms, dict):
        return {**base, 'status': 'invalid', 'actual_valid': None,
                'detail': 'OBS status has no terms mapping'}
    eligible = []
    for key, item in terms.items():
        if not isinstance(item, dict) or item.get('term') != slot or item.get('status') != 'ok':
            continue
        try:
            dt = parse_utc(item['nominal_date'] + 'T' + slot + ':00:00Z')
        except (TypeError, ValueError, KeyError):
            continue
        if dt <= now:
            eligible.append(dt)
    actual = max(eligible) if eligible else None
    matching_key = f'{expected:%Y-%m-%d}_{slot}'
    matching = terms.get(matching_key)
    if isinstance(matching, dict) and matching.get('status') == 'ok':
        return {**base, 'status': 'ok', 'actual_valid': iso(expected),
                'detail': 'OBS term registered as ok'}
    if actual is not None and actual >= expected:
        return {**base, 'status': 'ok', 'actual_valid': iso(actual),
                'detail': 'A newer OBS term is registered as ok'}
    return {**base, 'status': 'stale', 'actual_valid': iso(actual) if actual else None,
            'detail': f'Expected OBS term {matching_key} is not registered as ok'
                      + (f" (registered: {matching.get('status')})" if isinstance(matching, dict) else '')}


def point_status(root, now):
    base = dict(id='claef_point', name='C-LAEF point forecast', kind='claef_point', slot=None,
                expected_valid=None, source=POINT_FILE)
    data, error = read_json(root, POINT_FILE)
    if error or not isinstance(data, dict):
        return {**base, 'status': 'unavailable', 'actual_valid': None,
                'detail': error or 'Invalid point status JSON'}
    try:
        cycle = parse_utc(data['model_reference_time'])
        retrieved = parse_utc(data['retrieved_at'])
        if cycle > now or retrieved > now + timedelta(minutes=5):
            raise ValueError('Future cycle or retrieval time')
    except (KeyError, TypeError, ValueError) as exc:
        return {**base, 'status': 'invalid', 'actual_valid': data.get('model_reference_time'),
                'detail': f'Invalid point forecast metadata: {exc}'}
    product, product_error = read_json(root, POINT_PRODUCT)
    if product_error or not isinstance(product, dict):
        return {**base, 'status': 'unavailable', 'actual_valid': iso(cycle),
                'detail': product_error or 'Missing/invalid point forecast product'}
    try:
        product_cycle = parse_utc(product['reference_time'])
        first = parse_utc(product['forecast_start'])
        last = parse_utc(product['forecast_end'])
        records = product['records']
        if (product_cycle != cycle or first > now or last < now
                or not isinstance(records, list) or not records
                or len(records) != product['forecast_hour_count']
                or parse_utc(records[0]['valid_time']) != first
                or parse_utc(records[-1]['valid_time']) != last):
            raise ValueError('Point product does not match status or cover current time')
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        return {**base, 'status': 'invalid', 'actual_valid': iso(cycle),
                'detail': f'Invalid point product: {exc}'}
    age = (now - cycle).total_seconds() / 3600
    state = data.get('status')
    if age > POINT_MAX_AGE_HOURS:
        label, detail = 'stale', 'Point forecast cycle older than 12 hours'
    elif state == 'ok':
        label, detail = 'ok', 'Recently retrieved point forecast'
    elif state == 'warning':
        label, detail = 'warning', 'Last forecast preserved after a temporary retrieval error'
    else:
        label, detail = 'error', f'Point forecast publisher reports {state!r}'
    return {**base, 'status': label, 'actual_valid': iso(cycle),
            'age_hours': round(age, 2), 'detail': detail,
            'reported_error': data.get('message') if state in ('warning','error') else None}



def archive_inventory(root, now, lookback_days=7):
    """Check expected, already-due OBS and +12 h model archive products.

    A missing archived file does not prove that a source model cycle never ran.
    This is deliberately a bounded existence/metadata check, not a scan or rebuild.
    """
    if not isinstance(lookback_days, int) or not 1 <= lookback_days <= 31:
        raise ValueError('lookback_days must be an integer from 1 to 31')
    start = now - timedelta(days=lookback_days)
    rows, gaps = [], []
    obs, obs_error = read_json(root, 'data/status.json')
    terms = obs.get('terms', {}) if isinstance(obs, dict) else {}
    for kind in ('obs', 'icon_d2', 'claef_sounding'):
        deadline = timedelta(hours=DEADLINE_HOURS[kind])
        checked, missing, invalid = 0, 0, 0
        for day_offset in range(lookback_days + 2):
            day = now.date() - timedelta(days=day_offset)
            for slot in ('00', '12'):
                valid = datetime.combine(day, time(int(slot)), tzinfo=UTC)
                if not (start <= valid and valid + deadline <= now):
                    continue
                checked += 1
                if kind == 'obs':
                    entry = terms.get(f'{valid:%Y-%m-%d}_{slot}') if isinstance(terms, dict) else None
                    rel = f'data/{valid:%Y/%m/%Y%m%d}_{slot}.json'
                    if not isinstance(entry, dict) or entry.get('status') != 'ok':
                        problem = 'obs_not_registered'
                    elif not (root / rel).is_file():
                        problem = 'archive_file_missing'
                    else:
                        problem = None
                    if obs_error and problem == 'obs_not_registered':
                        problem = 'obs_status_unavailable'
                else:
                    run = valid - timedelta(hours=12)
                    model_dir = 'icon-d2' if kind == 'icon_d2' else 'claef'
                    rel = f'models/{model_dir}/{run:%Y%m%d%H}/ljlm_f012.json'
                    product, error = read_json(root, rel)
                    if error:
                        problem = 'archive_file_missing' if not (root / rel).exists() else 'archive_file_unreadable'
                    elif not isinstance(product, dict):
                        problem = 'invalid_archive_metadata'
                    else:
                        try:
                            if (parse_utc(product['valid_time']) != valid
                                    or parse_utc(product['run_time']) != run
                                    or product['lead_hours'] != 12
                                    or not isinstance(product['levels'], list)
                                    or not product['levels']):
                                raise ValueError('Archive term/lead/levels mismatch')
                        except (KeyError, TypeError, ValueError):
                            problem = 'invalid_archive_metadata'
                        else:
                            problem = None
                if problem:
                    item = dict(kind=kind, slot=slot, valid_time=iso(valid),
                                expected_run_time=iso(valid - timedelta(hours=12)) if kind != 'obs' else None,
                                file=rel, issue_code=problem, cause='undetermined')
                    gaps.append(item)
                    if problem in ('invalid_archive_metadata', 'archive_file_unreadable'):
                        invalid += 1
                    else:
                        missing += 1
        rows.append(dict(kind=kind, expected=checked, available=checked - missing - invalid,
                         missing=missing, invalid=invalid,
                         status='ok' if not missing and not invalid else 'warning'))
    return dict(window_start=iso(start), window_end=iso(now), days=lookback_days,
                status='warning' if gaps else 'ok',
                streams=rows, gap_count=len(gaps), gaps=sorted(gaps, key=lambda x: (x['valid_time'], x['kind'])),
                note='Gaps refer to local published archive files; origin/source causality is not inferred.')


def fetch_github_runs(repo=REPO, token=None, timeout=12):
    """Optional, read-only public GitHub Actions API lookup (four bounded requests)."""
    if repo != REPO:
        raise ValueError('Unexpected repository; refusing unrelated GitHub API calls')
    result, errors = {}, {}
    for key, _label, workflow in WORKFLOWS:
        url = f'https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs?branch=main&per_page=12'
        headers = {'User-Agent': 'LJLM-Sounding-Health/2.0',
                   'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        try:
            with urlopen(Request(url, headers=headers), timeout=timeout) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('GitHub API response exceeds 1 MiB')
            obj = json.loads(raw)
            if not isinstance(obj, dict) or not isinstance(obj.get('workflow_runs'), list):
                raise ValueError('Invalid GitHub Actions response')
            result[key] = obj['workflow_runs']
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            # Do not include GitHub credentials or full response bodies in JSON.
            errors[key] = f'{type(error).__name__}' + (
                f' HTTP {error.code}' if isinstance(error, HTTPError) else '')
    return result, errors


def action_health(runs_by_key=None, errors=None, now=None):
    """Summarize workflow outcomes without confusing success with source coverage.

    Failure stage and cause require individual job logs, which are NOT fetched.
    """
    now = now or datetime.now(UTC)
    errors = errors or {}
    if runs_by_key is None:
        return dict(status='not_checked', workflows=[],
                    note='GitHub Actions were not queried. Use --github-actions for an online check.')
    summaries = []
    for key, name, filename in WORKFLOWS:
        base = dict(id=key, name=name, workflow=filename,
                    latest_run=None, recent_failures=[], last_success=None)
        if key in errors:
            summaries.append({**base, 'status': 'unavailable', 'detail':errors[key]})
            continue
        arr = runs_by_key.get(key, [])
        if not isinstance(arr, list):
            summaries.append({**base, 'status': 'unavailable', 'detail':'Invalid runs array'})
            continue
        usable = []
        for run in arr:
            if not isinstance(run, dict):
                continue
            try:
                created = parse_utc(run['created_at'])
            except (ValueError, KeyError):
                continue
            if created > now + timedelta(minutes=5):
                continue
            usable.append((created, run))
        usable.sort(key=lambda item: item[0], reverse=True)
        if not usable:
            summaries.append({**base, 'status':'unknown', 'detail':'No dated runs returned'})
            continue
        def compact(item):
            dt, run = item
            return dict(id=run.get('id'), created_at=iso(dt),
                        conclusion=run.get('conclusion'), status=run.get('status'),
                        url=run.get('html_url'), event=run.get('event'))
        latest = compact(usable[0])
        successes = [pair for pair in usable if pair[1].get('conclusion') == 'success']
        recent_fails = [compact(pair) for pair in usable
                        if pair[1].get('conclusion') in ('failure', 'timed_out', 'cancelled')
                        and timedelta(0) <= now - pair[0] <= timedelta(hours=36)]
        conclusion, state = latest['conclusion'], latest['status']
        if state in ('queued','in_progress','requested','waiting','pending'):
            status = 'running'
        elif conclusion == 'success':
            status = 'warning' if recent_fails else 'ok'
        elif conclusion in ('failure', 'timed_out', 'cancelled'):
            status = 'error'
        else:
            status = 'unknown'
        summaries.append({**base, 'status':status, 'latest_run':latest,
                          'last_success':compact(successes[0]) if successes else None,
                          'recent_failures':recent_fails,
                          'detail':'Run result only; individual job logs not inspected'})
    statuses = [s['status'] for s in summaries]
    overall = ('error' if 'error' in statuses else 'unavailable' if 'unavailable' in statuses
               else 'warning' if 'warning' in statuses else 'running' if 'running' in statuses
               else 'ok' if statuses and all(x=='ok' for x in statuses) else 'unknown')
    return dict(status=overall, workflows=summaries,
                note='Actions results and published data are separate; no source-cause inference from run outcome.')


def annotate_issues(results, actions):
    lookup = {x['id']:x for x in actions.get('workflows', [])}
    for item in results:
        kind, status = item['kind'], item['status']
        item['issue_code'] = {'stale':'publication_lag', 'invalid':'invalid_product',
                              'unavailable':'product_unavailable', 'error':'reported_failure',
                              'warning':'reported_warning'}.get(status)
        item['cause'] = 'undetermined' if status != 'ok' else None
        a = lookup.get(kind)
        item['github_actions_status'] = a['status'] if a else 'not_checked'
        if kind == 'claef_point' and status in ('warning', 'error'):
            message = item.get('reported_error') or ''
            if isinstance(message, str) and 'GeoSphere HTTP ' in message:
                # This classifies a reported HTTP response, not a production outage.
                item['cause'] = 'source_http_response_reported'
    return results


def collect(root, now, *, lookback_days=7, action_runs=None, action_errors=None):
    results = []
    for kind, label, pattern in PRODUCTS:
        for slot in ('00', '12'):
            if kind == 'obs':
                results.append(obs_term(root, slot, now))
            else:
                results.append(model_term(root, kind, label, pattern, slot, now))
    results.append(point_status(root, now))
    actions = action_health(action_runs, action_errors, now)
    annotate_issues(results, actions)
    counts = {state: sum(item['status'] == state for item in results)
              for state in ('ok', 'warning', 'stale', 'unavailable', 'invalid', 'error')}
    archives = archive_inventory(root, now, lookback_days)
    # Historical gaps must remain visible but must not keep the *current*
    # operational health red after successful recovery. The archive has its
    # own status and gap list, separate from the seven current checks.
    overall = ('attention' if (any(item['status'] in ('stale', 'unavailable', 'invalid', 'error')
                              for item in results) or actions['status'] == 'error')
               else 'warning' if counts['warning'] or actions['status'] in ('warning', 'unavailable')
               else 'ok')
    return dict(schema_version=2, station=14015, station_name='Ljubljana',
                checked_at=iso(now), status=overall, checks=results, counts=counts,
                archive=archives, github_actions=actions,
                notes=[
                    'All product checks use local files; update your checkout before checking.',
                    'Publication deadlines are provisional UTC assumptions (OBS/ICON-D2 +4 h, C-LAEF +7 h).',
                    'Historical archive gaps are tracked separately from current operational status.',
                    'A missing model archive is not proof a model cycle was never produced.',
                    'GitHub Actions outcomes do not identify source vs transfer vs processing cause without job logs.',
                    'Historic IGRA multi-station archives are out of scope and never modified.',
                ])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--as-of', help='UTC ISO timestamp; default is now')
    parser.add_argument('--days', type=int, default=7, help='Archive lookback: 1–31 days, default 7')
    parser.add_argument('--json', action='store_true', help='Print full JSON instead of table')
    parser.add_argument('--output', type=Path, help='Save JSON report; default: data/system_health.json')
    parser.add_argument('--github-actions', action='store_true',
                        help='Query GitHub Actions API (4 public read-only requests)')
    args = parser.parse_args(argv)
    if args.days < 1 or args.days > 31:
        parser.error('--days must be between 1 and 31')
    now = parse_utc(args.as_of) if args.as_of else datetime.now(UTC)
    root = args.root.resolve()
    runs, errors = fetch_github_runs(token=os.environ.get('GITHUB_TOKEN')) if args.github_actions else (None, None)
    report = collect(root, now, lookback_days=args.days, action_runs=runs, action_errors=errors)
    formatted = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    output = args.output or root / 'data/system_health.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(formatted, encoding='utf-8')
    if args.json:
        print(formatted, end='')
    else:
        print('LJLM Sounding — health v2', iso(now), '(UTC)')
        print(f"{'Vir':33} {'Stanje':12} {'Zadnji objavljeni termin':21} {'Pričakovani termin'}")
        print('-' * 97)
        for item in report['checks']:
            print(f"{item['name'][:33]:33} {item['status'][:12]:12} "
                  f"{(item['actual_valid'] or '—')[:20]:21} {(item['expected_valid'] or '—')[:20]}")
        print(f"\nArhiv zadnjih {args.days} dni: {report['archive']['gap_count']} manjkajočih/neveljavnih terminov")
        for gap in report['archive']['gaps'][:8]:
            print('  -', gap['kind'], gap['valid_time'], gap['issue_code'])
        if len(report['archive']['gaps']) > 8:
            print(f"  ... in še {len(report['archive']['gaps']) - 8} terminov; podrobnosti v JSON")
        print('GitHub Actions:', report['github_actions']['status'])
        for job in report['github_actions']['workflows']:
            print('  -', job['name'], job['status'])
        print('JSON:', output)
        print('Opomba: manjkajoč arhivski termin ne dokazuje napake pri viru ali prenosu.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
