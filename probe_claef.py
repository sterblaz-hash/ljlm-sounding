#!/usr/bin/env python3
"""Isolated GeoSphere C-LAEF discovery probe; no dashboard/pipeline imports.

Run: python3 probe_claef.py
Makes four sequential API requests and saves compact, unmodified GeoJSON samples
and a readable inventory. Optional --inspect-bulk lists files and inspects at most
1 MiB of each of two NetCDF headers (requires h5py, never reads model arrays).
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import io
import math
from pathlib import Path
import re
import socket
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

API = 'https://dataset.api.hub.geosphere.at/v1/timeseries/forecast/'
BULK = 'https://public.hub.geosphere.at/datahub/'
RESOURCES = {'deterministic': 'nwp-v2-1h-1km', 'ensemble': 'ensemble-v2-1h-1km'}
LAT_LON = '46.0656,14.5122'
USER_AGENT = 'LJLM-CLAEF-Probe/0.1 (isolated public-data discovery)'
MAX_BYTES = 1024 * 1024
CATEGORIES = {
    '2 m temperature / dew point / humidity': ('2t', '2r'),
    '10 m wind and gusts': ('10u', '10v', '10fg'),
    'Precipitation': ('tp', 'rain', 'pt'),
    'Snowfall / snow-related': ('sf',),
    'Cloud cover': ('tcc',),
    'Pressure': ('msl',),
    'CAPE / convection / thunderstorms': ('cape',),
    'Freezing or snow level': ('snowlmt',),
    'Radiation / sunshine': ('ssrd', 'sund'),
    'Other operational variables': ('sy',),
}
# Candidates are filtered against live metadata before any request is built.
SUBSETS = {
    'deterministic': ('2t', '2r', '10u', '10v', '10fg', 'tp', 'sf',
                      'snowlmt', 'tcc', 'msl', 'cape', 'ssrd'),
    'ensemble': ('2t_p10', '2t_p50', '2t_p90', 'tp_p10', 'tp_p50',
                 'tp_p90', '10fg_p10', '10fg_p50', '10fg_p90'),
}


class ProbeError(Exception):
    """A readable discovery, transport, or response validation failure."""


class Client:
    """Bounded reads, 15s timeout, <=1 request/s; no automatic retries on 429."""
    def __init__(self, timeout=15):
        self.timeout = timeout
        self.last_request = None

    def read(self, url, byte_range=None):
        if self.last_request is not None:
            time.sleep(max(0, 1 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
        if byte_range is not None:
            headers.update({'Range': f'bytes=0-{byte_range - 1}',
                            'Accept': 'application/octet-stream'})
        try:
            with urlopen(Request(url, headers=headers), timeout=self.timeout) as response:
                if byte_range is not None:
                    if response.status != 206:
                        raise ProbeError('Bulk server did not honor Range; refusing full download.')
                    match = re.fullmatch(r'bytes 0-(\d+)/(\d+)',
                                         response.headers.get('Content-Range', ''))
                    if not match or int(match[1]) != byte_range - 1:
                        raise ProbeError('Invalid bulk Content-Range.')
                limit = byte_range if byte_range is not None else MAX_BYTES
                body = response.read(limit + 1)
                if len(body) > limit or (byte_range is not None and len(body) != limit):
                    raise ProbeError('Response exceeded limit or range was incomplete.')
                return body, dict(response.headers)
        except HTTPError as error:
            if error.code == 429:
                reset = error.headers.get('Retry-After') or error.headers.get('ratelimit-reset')
                raise ProbeError(f'HTTP 429: rate limited; stop and retry later (reset: {reset or "unspecified"}).') from error
            raise ProbeError(f'HTTP {error.code} for {url}') from error
        except (URLError, TimeoutError, socket.timeout, OSError) as error:
            raise ProbeError(f'Network request failed: {error}') from error

    def json(self, url):
        body, _ = self.read(url)
        try:
            return json.loads(body), len(body)
        except (ValueError, UnicodeError) as error:
            raise ProbeError('API returned invalid JSON.') from error


def parameter_inventory(metadata):
    entries = metadata.get('parameters')
    if not isinstance(entries, list) or not entries:
        raise ProbeError('Metadata has no parameter inventory.')
    result = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('name'), str) or not entry['name']:
            raise ProbeError('Invalid metadata parameter.')
        if entry['name'] in result:
            raise ProbeError('Duplicate metadata parameter.')
        result[entry['name']] = {key: entry.get(key, '') for key in ('name', 'long_name', 'desc', 'unit')}
    return result


def percentile_groups(names):
    groups = {}
    for name in names:
        match = re.fullmatch(r'(.+)_p(10|50|90)', name)
        if match:
            groups.setdefault(match[1], {})['P' + match[2]] = name
    return groups


def parse_time(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError('Timezone missing')
        return result
    except (ValueError, AttributeError, TypeError) as error:
        raise ProbeError(f'Invalid forecast time: {value!r}') from error


def point_url(resource, metadata, names, hours=3):
    if not 1 <= hours <= 6:
        raise ProbeError('Probe window must be 1–6 hours.')
    inventory = parameter_inventory(metadata)
    if not names or any(name not in inventory for name in names):
        raise ProbeError('Request includes unconfirmed parameters or empty selection.')
    start = parse_time(metadata.get('last_forecast_reftime'))
    query = [('parameters', name) for name in names]
    query += [('lat_lon', LAT_LON), ('forecast_offset', '0'),
              ('start', start.isoformat()), ('end', (start + timedelta(hours=hours)).isoformat())]
    return API + resource + '?' + urlencode(query)


def parse_point(data, expected_names):
    """Interpret actual response reference_time; retain nulls and original units."""
    if data.get('type') != 'FeatureCollection' or len(data.get('features', [])) != 1:
        raise ProbeError('Expected one GeoJSON point feature.')
    feature = data['features'][0]
    geometry = feature.get('geometry', {})
    coordinates = geometry.get('coordinates')
    if geometry.get('type') != 'Point' or not isinstance(coordinates, list) or len(coordinates) != 2:
        raise ProbeError('Invalid point coordinates.')
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in coordinates):
        raise ProbeError('Nonfinite point coordinates.')
    reference = parse_time(data.get('reference_time'))
    timestamps = data.get('timestamps')
    if not isinstance(timestamps, list) or not timestamps:
        raise ProbeError('Missing forecast timestamps.')
    instants = [parse_time(value) for value in timestamps]
    if any(b <= a for a, b in zip(instants, instants[1:])) or instants[0] < reference:
        raise ProbeError('Unordered timestamps or negative leads.')
    parameters = feature.get('properties', {}).get('parameters', {})
    if set(parameters) != set(expected_names):
        raise ProbeError('Returned parameters differ from request.')
    units = {}
    for name, item in parameters.items():
        values = item.get('data')
        if not item.get('unit') or not isinstance(values, list) or len(values) != len(timestamps):
            raise ProbeError(f'Invalid unit or data length for {name}.')
        if any(x is not None and (isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)) for x in values):
            raise ProbeError(f'Invalid value for {name}.')
        units[name] = item['unit']
    return {'requested_lat_lon': LAT_LON, 'grid_latitude': coordinates[1],
            'grid_longitude': coordinates[0], 'reference_time': data['reference_time'],
            'units': units, 'hours': [
                {'valid_time': timestamp, 'lead_hours': (instant - reference).total_seconds() / 3600,
                 'values': {name: item['data'][i] for name, item in parameters.items()}}
                for i, (timestamp, instant) in enumerate(zip(timestamps, instants))]}


def parse_listing(body):
    root = ET.fromstring(body)
    ns = {'s': 'http://s3.amazonaws.com/doc/2006-03-01/'}
    if root.findtext('s:IsTruncated', namespaces=ns) != 'false':
        raise ProbeError('Bulk listing truncated; refusing to infer complete inventory.')
    return [{'key': item.findtext('s:Key', namespaces=ns),
             'bytes': int(item.findtext('s:Size', namespaces=ns)),
             'etag': item.findtext('s:ETag', namespaces=ns)}
            for item in root.findall('s:Contents', ns)]


class BoundedHeader(io.RawIOBase):
    """Present remote logical size while rejecting reads outside cached metadata."""
    def __init__(self, prefix, total_size):
        self.prefix, self.total_size, self.position = prefix, total_size, 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        origin = {0: 0, 1: self.position, 2: self.total_size}[whence]
        position = origin + offset
        if position < 0:
            raise OSError('Negative header seek.')
        self.position = position
        return position

    def readinto(self, buffer):
        end = self.position + len(buffer)
        if end > len(self.prefix):
            raise OSError('Read outside bounded NetCDF metadata prefix.')
        buffer[:] = self.prefix[self.position:end]
        self.position = end
        return len(buffer)


def inspect_bulk(client):
    """Opt-in metadata-only inspection. h5py is optional and never auto-installed.

    A bounded in-memory file presents the remote logical size and refuses reads
    beyond the cached prefix. Only dimensions/attributes and the small pressure
    coordinate are read; no meteorological arrays or full files are downloaded.
    """
    try:
        import h5py
    except ImportError as error:
        raise ProbeError('--inspect-bulk requires optional h5py.') from error
    result = {}
    for kind, resource in RESOURCES.items():
        url = BULK + '?' + urlencode({'list-type': '2', 'max-keys': 100, 'delimiter': '/',
                                      'prefix': 'resources/' + resource + '/filelisting/'})
        body, _ = client.read(url)
        files = parse_listing(body)
        pattern = r'/nwp_\d{10}\.nc$' if kind == 'deterministic' else r'/ensemble_stats_\d{10}\.nc$'
        candidates = [item for item in files if re.search(pattern, item['key'])]
        if not candidates:
            raise ProbeError('No matching NetCDF bulk file.')
        selected = max(candidates, key=lambda item: item['key'])
        prefix, headers = client.read(BULK + selected['key'], byte_range=MAX_BYTES)
        if int(headers.get('Content-Range', headers.get('content-range', '')).split('/')[-1]) != selected['bytes']:
            raise ProbeError('Bulk file changed during header inspection.')
        variables = []
        try:
            with h5py.File(BoundedHeader(prefix, selected['bytes']), 'r') as nc:
                for name, variable in nc.items():
                    row = {'name': name, 'shape': list(variable.shape),
                           'dimensions': [list(dim.keys()) for dim in variable.dims]}
                    for attribute in ('long_name', 'units', 'description', 'standard_name'):
                        if attribute in variable.attrs:
                            value = variable.attrs[attribute]
                            row[attribute] = value.decode() if isinstance(value, bytes) else str(value)
                    variables.append(row)
                pressure = nc.get('plev')
                levels = pressure[:].tolist() if pressure is not None else None
        except (OSError, RuntimeError) as error:
            raise ProbeError('NetCDF metadata outside bounded header or unsupported layout.') from error
        result[kind] = {'listing_url': url, 'file_count': len(files), 'selected_file': selected,
                        'header_bytes_read': len(prefix), 'variables': variables, 'pressure_levels_hpa': levels}
    return result


def inventory_markdown(metadata, payloads, summaries, metadata_sizes):
    lines = ['# C-LAEF point API probe', '', f'Checked: {datetime.now(timezone.utc).isoformat()}', '']
    inventories = {kind: parameter_inventory(meta) for kind, meta in metadata.items()}
    groups = percentile_groups(inventories['ensemble'])
    lines += ['Deterministic-only variable families: ' + ', '.join(sorted(set(inventories['deterministic']) - set(groups))),
              '', 'Ensemble statistics use separate parameter names under `features[0].properties.parameters`; each has `name`, `unit`, and `data` aligned with top-level `timestamps`.', '']
    for kind, meta in metadata.items():
        lines += [f'## {kind}', '', 'Source: ' + API + RESOURCES[kind] + '/metadata', '']
        for key in ('last_forecast_reftime', 'available_forecast_reftimes', 'forecast_length', 'frequency', 'max_forecast_offset', 'spatial_resolution_m'):
            lines.append(f'- {key}: `{json.dumps(meta.get(key))}`')
        lines += [f'- Metadata HTTP body: {metadata_sizes[kind]} bytes',
                  f'- Sample HTTP body: {payloads[kind]} bytes', '',
                  '| Category | Exact short name | Long name | Description | Unit |',
                  '|---|---|---|---|---|']
        for name, item in inventories[kind].items():
            base = re.sub(r'_p(10|50|90)$', '', name)
            category = next((cat for cat, names in CATEGORIES.items() if base in names), 'Other / unclassified')
            row = [category, name, item['long_name'], item['desc'], item['unit']]
            lines.append('| ' + ' | '.join(str(x).replace('|', '\\|') for x in row) + ' |')
        summary = summaries[kind]
        lines += ['', 'Requested latitude,longitude: ' + LAT_LON,
                  f"Returned latitude,longitude: {summary['grid_latitude']},{summary['grid_longitude']}",
                  'Response reference time: ' + summary['reference_time'], '',
                  '| Valid UTC time | Lead h | Values (units as above) |', '|---|---|---|']
        for hour in summary['hours']:
            lines.append(f"| {hour['valid_time']} | {hour['lead_hours']:g} | " + ', '.join(f'{k}={v}' for k, v in hour['values'].items()) + ' |')
        if summary['reference_time'] != meta['last_forecast_reftime']:
            lines += ['', 'WARNING: metadata/sample cycle changed; trust response reference_time and review the window.']
        lines.append('')
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'claef/probe')
    parser.add_argument('--inspect-bulk', action='store_true')
    args = parser.parse_args(argv)
    client = Client()
    metadata, metadata_sizes, payloads, summaries, samples = {}, {}, {}, {}, {}
    try:
        # Four requests total by default; no raw metadata persisted.
        for kind, resource in RESOURCES.items():
            meta, metadata_size = client.json(API + resource + '/metadata')
            inventory = parameter_inventory(meta)
            names = [name for name in SUBSETS[kind] if name in inventory]
            sample, payload_size = client.json(point_url(resource, meta, names))
            summaries[kind] = parse_point(sample, names)
            metadata[kind], metadata_sizes[kind] = meta, metadata_size
            samples[kind], payloads[kind] = sample, payload_size
        bulk = inspect_bulk(client) if args.inspect_bulk else None
        args.output.mkdir(parents=True, exist_ok=True)
        for kind, sample in samples.items():
            (args.output / (kind + '_sample.json')).write_text(json.dumps(sample, separators=(',', ':'), allow_nan=False) + '\n')
        (args.output / 'inventory.md').write_text(inventory_markdown(metadata, payloads, summaries, metadata_sizes))
        if bulk is not None:
            (args.output / 'bulk_inventory.json').write_text(json.dumps(bulk, indent=2) + '\n')
        print(f'Saved small samples and exact inventory to {args.output}')
        for kind, summary in summaries.items():
            print(kind, json.dumps(summary, ensure_ascii=False))
        return 0
    except (ProbeError, ValueError, KeyError, TypeError, OSError) as error:
        print(f'C-LAEF probe failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
