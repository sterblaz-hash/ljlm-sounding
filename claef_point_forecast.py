#!/usr/bin/env python3
"""Central C-LAEF LJLM point cache; independent of dashboard and workflows.

Run python3 claef_point_forecast.py. At most two metadata and two point requests
per refresh, paced one second apart. A persisted 15-minute check interval also
applies after failures; 429 Retry-After/reset can extend it. Existing good output
survives errors. Matching reference times are required; values retain native units.
"""
import argparse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from probe_claef import API, LAT_LON, RESOURCES, ProbeError, parameter_inventory, parse_point, parse_time

PARAMETERS = ('2t', '2r', '10u', '10v', '10fg', 'tp', 'rain', 'sf',
              'snowlmt', 'tcc', 'msl', 'cape', 'ssrd', 'sund')
PERCENTILES = ('P10', 'P50', 'P90')
SCHEMA_VERSION = 2
CHECK_SECONDS = 900
MAX_BYTES = 1024 * 1024
DEFAULT_OUTPUT = Path(__file__).resolve().parent / 'models/claef/latest/point.json'
INTERVAL_KINDS = {'tp': 'interval_amount', 'rain': 'interval_amount',
                  'sf': 'interval_amount', 'sund': 'interval_duration',
                  '10fg': 'interval_maximum', 'ssrd': 'radiation_flux'}


def utc(value):
    return parse_time(value).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def now_utc():
    return datetime.now(timezone.utc)


class RetrievalError(ProbeError):
    def __init__(self, message, retry_at=None):
        super().__init__(message)
        self.retry_at = retry_at


def retry_deadline(headers, now):
    deadlines = []
    retry = headers.get('Retry-After')
    if retry:
        try:
            deadlines.append(now + timedelta(seconds=max(0, float(retry))))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(retry)
                if parsed.tzinfo is not None:
                    deadlines.append(parsed.astimezone(timezone.utc))
            except (ValueError, TypeError, OverflowError):
                pass
    reset = headers.get('ratelimit-reset')
    if reset:
        try:
            deadlines.append(now + timedelta(seconds=max(0, float(reset))))
        except (ValueError, OverflowError):
            pass
    return max(deadlines) if deadlines else None


class ForecastClient:
    def __init__(self):
        self.last_request = None

    def json(self, url):
        if self.last_request is not None:
            time.sleep(max(0, 1 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        request = Request(url, headers={'User-Agent': 'LJLM-CLAEF-PointCache/1.0 (GeoSphere public point forecast)',
                                        'Accept': 'application/json'})
        try:
            with urlopen(request, timeout=15) as response:
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise RetrievalError('API response exceeds 1 MiB cap.')
            data = json.loads(body)
            if not isinstance(data, dict):
                raise RetrievalError('API JSON is not an object.')
            return data
        except HTTPError as error:
            deadline = retry_deadline(error.headers, now_utc()) if error.code == 429 else None
            raise RetrievalError(f'GeoSphere HTTP {error.code}; no automatic retry.', deadline) from error
        except (URLError, TimeoutError, socket.timeout, OSError, ValueError) as error:
            raise RetrievalError(f'GeoSphere request failed: {error}') from error


def atomic_json(path, data):
    """Serialize before touching output; fsync same-directory temp, then replace."""
    encoded = (json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n').encode()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name + '.', suffix='.tmp', delete=False) as handle:
            name = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        name = None
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)
    return len(encoded)


def read_json(path):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def select_cycle(metadata):
    cycles = []
    for kind, meta in metadata.items():
        if meta.get('frequency') != '1H' or not isinstance(meta.get('forecast_length'), int) or not 2 <= meta['forecast_length'] <= 121:
            raise ProbeError(f'Unsupported forecast cadence/length for {kind}.')
        times = meta.get('available_forecast_reftimes')
        if not isinstance(times, list) or not times:
            raise ProbeError(f'Missing available cycles for {kind}.')
        parsed = [utc(value) for value in times]
        if len(set(parsed)) != len(parsed) or utc(meta.get('last_forecast_reftime')) != parsed[0]:
            raise ProbeError(f'Inconsistent reference-time metadata for {kind}.')
        cycles.append(set(parsed))
    common = set.intersection(*cycles)
    if not common:
        raise ProbeError('No common deterministic/ensemble cycle; preserving cache.')
    return max(common, key=parse_time)


def request_names(kind, meta):
    inventory = parameter_inventory(meta)
    candidates = PARAMETERS if kind == 'deterministic' else tuple(base + '_' + p.lower() for base in PARAMETERS for p in PERCENTILES)
    names = [name for name in candidates if name in inventory]
    if not names:
        raise ProbeError(f'No confirmed useful parameters in {kind}.')
    return names


def forecast_url(kind, meta, cycle, names):
    offset = [utc(value) for value in meta['available_forecast_reftimes']].index(cycle)
    if offset > meta.get('max_forecast_offset', -1):
        raise ProbeError('Selected cycle exceeds advertised forecast offset.')
    reference = parse_time(cycle)
    end = reference + timedelta(hours=meta['forecast_length'] - 1)
    query = [('parameters', name) for name in names]
    query += [('lat_lon', LAT_LON), ('forecast_offset', str(offset)),
              ('start', cycle), ('end', utc(end.isoformat()))]
    return API + RESOURCES[kind] + '?' + urlencode(query)


def cache_signature(metadata):
    # Inventory/cadence changes invalidate the same-cycle cache as well.
    relevant = {kind: {'dataset': RESOURCES[kind], 'frequency': meta['frequency'],
                       'forecast_length': meta['forecast_length'],
                       'parameters': {name: parameter_inventory(meta)[name] for name in request_names(kind, meta)}}
                for kind, meta in metadata.items()}
    return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()


def build_product(metadata, responses, cycle, retrieved_at):
    sources, rows_by_source, definitions = {}, {}, {}
    for kind, meta in metadata.items():
        names = request_names(kind, meta)
        parsed = parse_point(responses[kind], names)
        if utc(parsed['reference_time']) != cycle:
            raise ProbeError(f'{kind} response cycle changed during retrieval; preserving cache.')
        inventory = parameter_inventory(meta)
        definitions[kind] = {}
        for name in names:
            entry = inventory[name]
            if not entry['unit'] or entry['unit'] != parsed['units'][name]:
                raise ProbeError(f'Metadata/response unit mismatch for {kind}/{name}.')
            base = name.rsplit('_p', 1)[0] if kind == 'ensemble' else name
            definitions[kind][name] = {
                'unit': entry['unit'], 'long_name': entry['long_name'], 'description': entry['desc'],
                'temporal_semantics': INTERVAL_KINDS.get(base, 'valid_time_value'),
            }
            if base in ('tp', 'rain', 'sf', 'sund', '10fg'):
                definitions[kind][name]['interval'] = 'last forecast interval ending at valid_time; not cumulative since reference_time'
            if base == 'sf':
                definitions[kind][name]['interpretation'] = 'snowfall mass per area; no conversion to snow depth'
            if base == 'ssrd':
                definitions[kind][name]['interval'] = 'deaccumulated radiation flux; exact averaging bounds not supplied by point metadata'
        rows_by_source[kind] = {utc(row['valid_time']): row for row in parsed['hours']}
        sources[kind] = {'dataset': RESOURCES[kind], 'endpoint': API + RESOURCES[kind],
                         'reference_time': cycle, 'latest_advertised_reference_time': utc(meta['last_forecast_reftime']),
                         'forecast_offset': [utc(x) for x in meta['available_forecast_reftimes']].index(cycle),
                         'frequency': meta['frequency'], 'forecast_length': meta['forecast_length'],
                         'grid_point': {'latitude': parsed['grid_latitude'], 'longitude': parsed['grid_longitude']},
                         'available_parameters': names,
                         'unavailable_requested_parameters': [name for name in (PARAMETERS if kind == 'deterministic' else tuple(b + '_' + p.lower() for b in PARAMETERS for p in PERCENTILES)) if name not in names]}
    records = []
    for valid_time in sorted(set.union(*(set(rows) for rows in rows_by_source.values())), key=parse_time):
        lead = (parse_time(valid_time) - parse_time(cycle)).total_seconds() / 3600
        deterministic = rows_by_source['deterministic'].get(valid_time)
        ensemble = rows_by_source['ensemble'].get(valid_time)
        stats = {}
        for base in PARAMETERS:
            available = {p: base + '_' + p.lower() for p in PERCENTILES if base + '_' + p.lower() in definitions['ensemble']}
            if available:
                stats[base] = {p: ensemble['values'][name] if ensemble else None for p, name in available.items()}
        records.append({'valid_time': valid_time, 'lead_hours': lead,
                        'interval_start': utc((parse_time(valid_time) - timedelta(hours=1)).isoformat()) if lead >= 1 else None,
                        'source_time_available': {'deterministic': deterministic is not None, 'ensemble': ensemble is not None},
                        'deterministic': deterministic['values'] if deterministic else {name: None for name in definitions['deterministic']},
                        'ensemble': stats})
    return {'schema_version': SCHEMA_VERSION, 'model': 'C-LAEF AlpeAdria',
            'station': {'icao': 'LJLM', 'wmo': 14015, 'name': 'Ljubljana'},
            'requested_location': {'latitude': 46.0656, 'longitude': 14.5122},
            'reference_time': cycle, 'retrieved_at': utc(retrieved_at),
            'forecast_start': records[0]['valid_time'], 'forecast_end': records[-1]['valid_time'],
            'forecast_hour_count': len(records),
            'cache_signature': cache_signature(metadata), 'sources': sources, 'parameters': definitions,
            'interval_metadata': {
                'source_guidance': 'https://public.hub.geosphere.at/public/resources/misc/Hilfestellung_DE.pdf',
                'source_semantics': 'v2 precipitation and radiation are deaccumulated; source values preserved without differencing, summing or unit conversion',
                'interval_start_basis': 'inferred from advertised 1H cadence and last-forecast-interval descriptions; +0 has no preceding forecast interval',
                'unresolved': 'exact radiation averaging bounds and within-interval distribution are not supplied; snowfall is mass, not snow depth',
            }, 'records': records}


def validate_coverage(meta, response, cycle, names):
    parsed = parse_point(response, names)
    expected = [utc((parse_time(cycle) + timedelta(hours=i)).isoformat()) for i in range(meta['forecast_length'])]
    if utc(parsed['reference_time']) != cycle or [utc(row['valid_time']) for row in parsed['hours']] != expected:
        raise ProbeError('Cycle changed or forecast coverage incomplete; preserving cache.')


def write_status(output, product, error=None, attempted_at=None):
    """Small status; successful same-cycle runs keep identical status bytes."""
    status = {'status': 'error' if error else ('ok' if product else 'unavailable'),
              'model_reference_time': product.get('reference_time') if product else None,
              'retrieved_at': product.get('retrieved_at') if product else None,
              'forecast_hour_count': len(product.get('records', [])) if product else 0}
    if error:
        status.update({'attempted_at': utc(attempted_at), 'message': str(error),
                       'previous_forecast_preserved': bool(product)})
    path = Path(output).with_name('status.json')
    if read_json(path) != status:
        atomic_json(path, status)


def usable_cache(product, metadata):
    """Only skip retrieval for a complete cache matching this builder's schema."""
    try:
        if product['schema_version'] != SCHEMA_VERSION or product['model'] != 'C-LAEF AlpeAdria':
            return False
        if product['requested_location'] != {'latitude': 46.0656, 'longitude': 14.5122}:
            return False
        cycle = utc(product['reference_time'])
        utc(product['retrieved_at'])
        if product['cache_signature'] != cache_signature(metadata):
            return False
        expected = [utc((parse_time(cycle) + timedelta(hours=i)).isoformat())
                    for i in range(max(meta['forecast_length'] for meta in metadata.values()))]
        if (product.get('forecast_start') != expected[0] or product.get('forecast_end') != expected[-1]
                or product.get('forecast_hour_count') != len(expected)):
            return False
        if [row['valid_time'] for row in product['records']] != expected:
            return False
        for kind in RESOURCES:
            source = product['sources'][kind]
            if source['dataset'] != RESOURCES[kind] or source['reference_time'] != cycle:
                return False
            if source['available_parameters'] != request_names(kind, metadata[kind]):
                return False
            if set(product['parameters'][kind]) != set(source['available_parameters']):
                return False
        return all('deterministic' in row and 'ensemble' in row and 'source_time_available' in row
                   for row in product['records'])
    except (KeyError, ValueError, TypeError, ProbeError):
        return False


def refresh(output=DEFAULT_OUTPUT, client=None, now=None):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    state_path = output.with_name('.point_cache_state.json')
    now = now or now_utc()
    with output.with_name('.point.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_json(state_path) or {}
        cached = read_json(output)
        try:
            deadline = parse_time(state['next_check_at']) if state.get('next_check_at') else None
        except ProbeError:
            deadline = None
        if deadline is not None and now < deadline:
            return {'status': 'throttled', 'output_exists': cached is not None, 'next_check_at': state['next_check_at']}
        # Persist check gate BEFORE network I/O, including interruptions/failures.
        next_check = now + timedelta(seconds=CHECK_SECONDS)
        state = {'last_checked_at': utc(now.isoformat()), 'next_check_at': utc(next_check.isoformat())}
        atomic_json(state_path, state)
        client = client or ForecastClient()
        try:
            metadata = {kind: client.json(API + resource + '/metadata') for kind, resource in RESOURCES.items()}
            cycle = select_cycle(metadata)
            if cached and cached.get('schema_version') == SCHEMA_VERSION and cached.get('requested_location') == {'latitude': 46.0656, 'longitude': 14.5122}:
                try:
                    cached_cycle = parse_time(cached.get('reference_time'))
                except ProbeError:
                    cached_cycle = None
                if cached_cycle is not None and cached_cycle > parse_time(cycle):
                    raise ProbeError('Advertised common cycle is older than cache; refusing downgrade.')
                if cached.get('reference_time') == cycle and usable_cache(cached, metadata):
                    write_status(output, cached)
                    return {'status': 'unchanged', 'reference_time': cycle, 'forecast_times': len(cached['records']), 'bytes': output.stat().st_size}
            responses = {}
            for kind, meta in metadata.items():
                names = request_names(kind, meta)
                responses[kind] = client.json(forecast_url(kind, meta, cycle, names))
                validate_coverage(meta, responses[kind], cycle, names)
            product = build_product(metadata, responses, cycle, now_utc().isoformat())
            size = atomic_json(output, product)
            write_status(output, product)
            return {'status': 'written', 'reference_time': cycle, 'forecast_times': len(product['records']), 'bytes': size}
        except (ProbeError, ValueError, KeyError, TypeError, OSError) as error:
            if isinstance(error, RetrievalError) and error.retry_at is not None:
                state['next_check_at'] = utc(max(next_check, error.retry_at).isoformat())
            state['last_error'] = str(error)
            atomic_json(state_path, state)
            write_status(output, read_json(output), error, now.isoformat())
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(refresh(args.output)))
        return 0
    except (ProbeError, ValueError, KeyError, TypeError, OSError) as error:
        print(f'C-LAEF cache refresh failed; existing point.json preserved: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
