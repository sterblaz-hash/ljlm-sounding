"""Build compact LJLM model-error archives and rolling statistics; stdlib only."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile

VERSION = 1
STATION = {'station': 14015, 'station_name': 'Ljubljana'}
SOURCE = Path('verification/models')
BASE = SOURCE / 'timeseries'
MODELS = ('ICON-D2', 'C-LAEF')
PRESSURES = (925, 850, 700, 500, 300)
SCALARS = ('temperature_c', 'dewpoint_c', 'relative_humidity_pct', 'height_m', 'u_ms', 'v_ms')
METRICS = (*SCALARS, 'wind_vector_error_ms')
WINDOWS = {'7d': 7, '30d': 30, '90d': 90, '1y': 365}
SOURCE_PATTERN = re.compile(r'(\d{4})/(\d{2})/(\d{8})_(00|12)_comparison\.json')


def iso(value):
    return value.isoformat().replace('+00:00', 'Z')


def utc_time(value, regular=False):
    if not isinstance(value, str):
        raise ValueError('Timestamp must be a string')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError(f'Invalid timestamp: {value!r}') from error
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise ValueError('Timestamp must be timezone-aware UTC')
    if regular and (result.hour not in (0, 12) or result.minute or result.second or result.microsecond):
        raise ValueError('Valid/run time must be exact 00 or 12 UTC')
    return result


def finite(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('Error metric must be a finite number or null')
    try:
        valid = math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError('Nonfinite error metric')
    return value


def normalize_errors(rows):
    """Validate the fixed model/pressure matrix; missing metrics remain null."""
    if not isinstance(rows, dict):
        raise ValueError('Missing error matrix')
    result = {}
    for model in MODELS:
        if not isinstance(rows.get(model), dict):
            raise ValueError(f'Missing errors for {model}')
        result[model] = {}
        for pressure in PRESSURES:
            values = rows[model].get(str(pressure), {})
            if not isinstance(values, dict):
                raise ValueError('Invalid pressure error row')
            metrics = {metric: finite(values.get(metric)) for metric in METRICS}
            vector = metrics['wind_vector_error_ms']
            if vector is not None and vector < 0:
                raise ValueError('Wind vector error must be non-negative')
            result[model][str(pressure)] = metrics
    return result


def extract_record(source):
    if not isinstance(source, dict) or not isinstance(source.get('version'), int) or isinstance(source['version'], bool) or source['version'] < 1:
        raise ValueError('Missing/invalid comparison version')
    valid = utc_time(source.get('valid_time'), regular=True)
    obs = source.get('observation_sounding_id')
    if not isinstance(obs, str) or not obs.strip():
        raise ValueError('Missing observation_sounding_id')
    if source.get('station', 14015) != 14015:
        raise ValueError('Comparison is not for LJLM')
    models = source.get('models')
    if not isinstance(models, dict):
        raise ValueError('Missing model metadata')
    runs = {}
    for model in MODELS:
        metadata = models.get(model)
        if not isinstance(metadata, dict):
            raise ValueError(f'Missing model: {model}')
        run = utc_time(metadata.get('run_time'), regular=True)
        if metadata.get('lead_hours') != 12 or run + timedelta(hours=12) != valid:
            raise ValueError(f'{model} is not an exact +12 h verification')
        runs[model] = iso(run)
    comparisons = source.get('standard_pressure_comparisons')
    if not isinstance(comparisons, list):
        raise ValueError('Missing standard_pressure_comparisons')
    errors = {model: {} for model in MODELS}
    seen = set()
    for row in comparisons:
        if not isinstance(row, dict):
            raise ValueError('Invalid pressure comparison')
        pressure = row.get('pressure_hpa')
        if isinstance(pressure, bool) or not isinstance(pressure, (int, float)):
            raise ValueError('Invalid pressure')
        if pressure not in PRESSURES:
            continue
        if pressure in seen:
            raise ValueError('Duplicate pressure comparison')
        seen.add(pressure)
        values = row.get('model_minus_obs')
        if not isinstance(values, dict):
            raise ValueError('Missing model_minus_obs')
        for model in MODELS:
            if not isinstance(values.get(model), dict):
                raise ValueError(f'Missing model_minus_obs for {model}')
            errors[model][str(int(pressure))] = values[model]
    return dict(valid_time=iso(valid), observation_sounding_id=obs,
                verification_date=valid.strftime('%Y-%m-%d'), term=valid.strftime('%H'),
                run_times=runs, errors=normalize_errors(errors))


def normalize_record(record):
    if not isinstance(record, dict):
        raise ValueError('Invalid archived record')
    # Reuse source validation without recomputing any errors.
    valid = utc_time(record.get('valid_time'), regular=True)
    if record.get('verification_date') != valid.strftime('%Y-%m-%d') or record.get('term') != valid.strftime('%H'):
        raise ValueError('Archived date/term mismatch')
    runs = record.get('run_times')
    if not isinstance(runs, dict):
        raise ValueError('Missing archived run times')
    errors = normalize_errors(record.get('errors'))
    return extract_record(dict(version=VERSION, valid_time=record['valid_time'],
        observation_sounding_id=record.get('observation_sounding_id'),
        models={model: {'run_time': runs.get(model), 'lead_hours': 12} for model in MODELS},
        standard_pressure_comparisons=[{'pressure_hpa': p,
            'model_minus_obs': {model: errors[model][str(p)] for model in MODELS}} for p in PRESSURES]))


def sorted_records(records):
    by_time = {}
    for record in records:
        record = normalize_record(record)
        by_time[record['valid_time']] = record
    return sorted(by_time.values(), key=lambda row: row['valid_time'])


def statistics(records):
    result = {}
    for model in MODELS:
        result[model] = {}
        for pressure in PRESSURES:
            result[model][str(pressure)] = {}
            for metric in METRICS:
                values = [finite(row['errors'][model][str(pressure)].get(metric)) for row in records]
                values = [value for value in values if value is not None]
                n = len(values)
                # Scale before squaring/summing to avoid overflow for finite input.
                scale = max(map(abs, values), default=0)
                scaled = [value / scale for value in values] if scale else [0] * n
                summary = {'n': n}
                if metric in SCALARS:
                    summary['bias'] = scale * (math.fsum(scaled) / n) if n else None
                summary['mae'] = scale * (math.fsum(map(abs, scaled)) / n) if n else None
                summary['rmse'] = scale * math.sqrt(math.fsum(v * v for v in scaled) / n) if n else None
                result[model][str(pressure)][metric] = summary
    return result


def safe_path(path):
    """Reject symlinks in the path itself and every existing ancestor."""
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise ValueError(f'Symlink refused: {part}')
    return path


def read_json(path):
    path = safe_path(path)
    def reject(value):
        raise ValueError(f'Nonfinite JSON constant: {value}')
    return json.loads(path.read_text(encoding='utf-8'), parse_constant=reject)


def output_path(root, relative):
    root = safe_path(root)
    path = safe_path(root / relative)
    if not path.is_relative_to(root / BASE):
        raise ValueError('Output must be inside verification/models/timeseries')
    return path


def write_json(root, path, content):
    """Install pre-serialized JSON atomically with a unique same-directory temp."""
    path = output_path(root, path.relative_to(safe_path(root)))
    if path.exists() and path.read_text(encoding='utf-8') == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.' + path.name + '.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        safe_path(path)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink()


def source_record(root, path):
    root = safe_path(root)
    path = safe_path(path if Path(path).is_absolute() else root / path)
    try:
        relative = path.relative_to(root / SOURCE).as_posix()
    except ValueError as error:
        raise ValueError('Source must be in verification/models/YYYY/MM') from error
    match = SOURCE_PATTERN.fullmatch(relative)
    if not match:
        raise ValueError('Not an archived 00/12 comparison filename')
    record = extract_record(read_json(path))
    valid = utc_time(record['valid_time'])
    if relative != valid.strftime('%Y/%m/%Y%m%d_%H_comparison.json'):
        raise ValueError('Comparison filename differs from valid time')
    return record


def source_files(root):
    """Visit only numeric archive directories, never following symlinks."""
    base = safe_path(root / SOURCE)
    if not base.exists():
        return
    for year in sorted(base.iterdir()):
        if year.is_symlink() or not re.fullmatch(r'\d{4}', year.name) or not year.is_dir():
            continue
        for month in sorted(year.iterdir()):
            if month.is_symlink() or not re.fullmatch(r'\d{2}', month.name) or not month.is_dir():
                continue
            for path in sorted(month.iterdir()):
                if not path.is_symlink() and SOURCE_PATTERN.fullmatch(path.relative_to(base).as_posix()) and path.is_file():
                    yield path


def load_archive(root):
    archive = output_path(root, BASE / 'archive')
    groups = {}
    if not archive.exists():
        return groups
    for year in sorted(archive.iterdir()):
        safe_path(year)
        if not re.fullmatch(r'\d{4}', year.name) or not year.is_dir():
            continue
        for path in sorted(year.iterdir()):
            safe_path(path)
            if not re.fullmatch(r'\d{2}\.json', path.name) or not path.is_file():
                continue
            data = read_json(path)
            if not isinstance(data, dict) or data.get('version') != VERSION or any(data.get(k) != v for k, v in STATION.items()) or not isinstance(data.get('records'), list):
                raise ValueError(f'Invalid existing archive: {path}')
            records = sorted_records(data['records'])
            month = f'{year.name}/{path.stem}'
            if any(utc_time(row['valid_time']).strftime('%Y/%m') != month for row in records):
                raise ValueError(f'Archive month mismatch: {path}')
            groups[month] = records
    return groups


def build(root, *, update=None, backfill=False, recent_hours=None, as_of=None):
    root = safe_path(root)
    now = utc_time(as_of) if isinstance(as_of, str) else (as_of or datetime.now(timezone.utc))
    now = utc_time(iso(now))
    if sum((update is not None, backfill, recent_hours is not None)) != 1:
        raise ValueError('Choose exactly one update/backfill/recent mode')
    if recent_hours is not None and (isinstance(recent_hours, bool) or not isinstance(recent_hours, int) or recent_hours <= 0):
        raise ValueError('Recent synchronization hours must be a positive integer')
    recovered, skipped = [], []
    if update is not None:
        recovered.append(source_record(root, update))
    else:
        start = now - timedelta(hours=recent_hours) if recent_hours is not None else None
        for path in source_files(root):
            # Use the filename to bound recent recovery before reading the body.
            if start is not None:
                try:
                    stamp = datetime.strptime(path.name, '%Y%m%d_%H_comparison.json').replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if not start <= stamp <= now:
                    continue
            try:
                recovered.append(source_record(root, path))
            except (ValueError, OSError) as error:
                skipped.append(f'{path}: {error}')
    groups = load_archive(root)
    changed = set()
    for record in recovered:
        month = utc_time(record['valid_time']).strftime('%Y/%m')
        groups.setdefault(month, []).append(record)
        changed.add(month)
    groups = {month: sorted_records(records) for month, records in sorted(groups.items())}
    outputs = {}
    for month in sorted(changed):
        records = groups[month]
        outputs[BASE / 'archive' / (month + '.json')] = {
            'version': VERSION, **STATION, 'record_count': len(records), 'records': records}
    records = sorted_records(row for month in groups.values() for row in month)
    for label, days in WINDOWS.items():
        start = now - timedelta(days=days)
        selected = [row for row in records if start <= utc_time(row['valid_time']) <= now]
        outputs[BASE / f'latest_{label}.json'] = {
            'version': VERSION, **STATION, 'generated_at': iso(now),
            'start_time': iso(start), 'end_time': iso(now),
            'record_count': len(selected), 'records': selected, 'statistics': statistics(selected)}
    # Validate every destination and serialize everything before the first write.
    prepared = [(output_path(root, path), json.dumps(data, separators=(',', ':'),
                ensure_ascii=False, allow_nan=False) + '\n') for path, data in outputs.items()]
    for path, content in prepared:
        write_json(root, path, content)
    return skipped


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--update', type=Path)
    mode.add_argument('--backfill', action='store_true')
    mode.add_argument('--sync-recent-hours', type=int)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--as-of', help='Timezone-aware UTC rolling-window end')
    args = parser.parse_args(argv)
    try:
        skipped = build(args.root, update=args.update, backfill=args.backfill,
                        recent_hours=args.sync_recent_hours, as_of=args.as_of)
    except (ValueError, OSError, OverflowError) as error:
        parser.exit(1, f'Verification data build failed: {error}\n')
    for warning in skipped:
        print(f'Skipped invalid comparison: {warning}', file=sys.stderr)


if __name__ == '__main__':
    main()
