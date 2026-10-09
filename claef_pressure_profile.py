#!/usr/bin/env python3
"""Pressure-level proof of concept: one LJLM point, one time, bounded HTTP Range.

Requires optional h5py (and its NumPy dependency); no full-file download fallback.
Default +12 h, newest file in official deterministic listing. Hard ceilings:
64 MiB body bytes, 80 HTTP requests, 16 MiB per range, 32 MiB decoded chunk.
No retries, <=1 request/s, 15s timeout; existing output survives any failure.
"""
import argparse
from datetime import datetime, timedelta, timezone
import io
import json
import math
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from claef_point_forecast import atomic_json, utc
from probe_claef import BULK, ProbeError, parse_listing, parse_time

DATASET = 'nwp-v2-1h-1km'
GRID_METADATA_URL = 'https://dataset.api.hub.geosphere.at/v1/grid/forecast/' + DATASET + '/metadata'
LISTING_URL = BULK + '?list-type=2&max-keys=100&delimiter=/&prefix=resources/' + DATASET + '/filelisting/'
DEFAULT_OUTPUT = Path(__file__).resolve().parent / 'models/claef/profiles/latest/profile_f012.json'
MAX_TOTAL = 64 * 1024 * 1024
MAX_REQUESTS = 80
MAX_RANGE = 16 * 1024 * 1024
MAX_DECODED_CHUNK = 32 * 1024 * 1024
METADATA_BLOCK = 64 * 1024
VARIABLES = {'t': ('air_temperature', 'degree Celsius'), 'r': ('relative_humidity', '%'),
             'u': ('eastward_wind', 'm s-1'), 'v': ('northward_wind', 'm s-1'),
             'z': ('geopotential', 'm2 s-2')}
STANDARD_GRAVITY = 9.80665


class SafetyRefusal(ProbeError):
    """Limits or server behavior would require unsafe/oversized access."""


class TransferClient:
    def __init__(self):
        self.requests = 0
        self.bytes = 0
        self.last_request = None
        self.range_log = []

    def get(self, url, start=None, end=None, total=None, etag=None):
        limit = METADATA_BLOCK if start is None else end - start + 1
        if limit <= 0 or limit > MAX_RANGE or self.bytes + limit > MAX_TOTAL or self.requests >= MAX_REQUESTS:
            raise SafetyRefusal('Transfer/request budget exceeded; refusing bulk download.')
        if start is not None and (start < 0 or end >= total or limit == total):
            raise SafetyRefusal('Invalid range or full-file read refused.')
        if self.last_request is not None:
            time.sleep(max(0, 1 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        headers = {'User-Agent': 'LJLM-CLAEF-PressureProfile/0.1 (bounded point research)',
                   'Accept-Encoding': 'identity'}
        if start is not None:
            headers['Range'] = f'bytes={start}-{end}'
            headers['If-Match'] = '"' + etag.strip('"') + '"'
        self.requests += 1
        try:
            with urlopen(Request(url, headers=headers), timeout=15) as response:
                if start is not None:
                    expected = f'bytes {start}-{end}/{total}'
                    if response.status != 206 or response.headers.get('Content-Range') != expected:
                        raise SafetyRefusal('Server ignored or changed Range; full download refused.')
                    if response.headers.get('ETag', '').strip('"') != etag.strip('"'):
                        raise SafetyRefusal('Remote object changed or ETag missing.')
                length = response.headers.get('Content-Length')
                if length is not None and int(length) > limit:
                    raise SafetyRefusal('Oversized Content-Length; body not read.')
                if response.headers.get('Content-Encoding', 'identity') != 'identity':
                    raise SafetyRefusal('Unexpected content encoding.')
                body = response.read(limit + 1)
                self.bytes += len(body)
                if len(body) > limit or (start is not None and len(body) != limit):
                    raise SafetyRefusal('Oversized or truncated response.')
            if start is not None:
                self.range_log.append({'start': start, 'end': end, 'bytes': len(body)})
            return body
        except HTTPError as error:
            raise ProbeError(f'GeoSphere HTTP {error.code}; stopping without retry.') from error
        except (URLError, OSError, ValueError) as error:
            raise ProbeError(f'Range request failed: {error}') from error


class RangeFile(io.RawIOBase):
    """h5py file-like reader with bounded range reads and reusable small blocks."""
    def __init__(self, client, selected, prefix=None):
        self.client, self.selected, self.position = client, selected, 0
        self.size = selected['bytes']
        self.url = BULK + selected['key']
        self.blocks = {}
        if prefix is not None:
            self.seed_prefix(prefix)

    def seed_prefix(self, prefix):
        for offset in range(0, len(prefix), METADATA_BLOCK):
            self.blocks[offset] = prefix[offset:offset + METADATA_BLOCK]

    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.position

    def seek(self, offset, whence=0):
        origins = {0: 0, 1: self.position, 2: self.size}
        position = origins[whence] + offset
        if position < 0:
            raise SafetyRefusal('Negative range-file seek.')
        self.position = position
        return position

    def fetch(self, start, end):
        return self.client.get(self.url, start, end, self.size, self.selected['etag'])

    def readinto(self, buffer):
        length = len(buffer)
        if not length: return 0
        if self.position + length > self.size or length > MAX_RANGE:
            raise SafetyRefusal('HDF5 requested oversized or out-of-bounds read.')
        start = self.position
        if length <= METADATA_BLOCK:
            end = start + length
            cursor, pieces = start, []
            while cursor < end:
                block_start = cursor // METADATA_BLOCK * METADATA_BLOCK
                if block_start not in self.blocks:
                    self.blocks[block_start] = self.fetch(block_start, min(block_start + METADATA_BLOCK, self.size) - 1)
                block = self.blocks[block_start]
                count = min(end - cursor, len(block) - (cursor - block_start))
                if count <= 0: raise SafetyRefusal('Incomplete metadata block.')
                pieces.append(block[cursor - block_start:cursor - block_start + count])
                cursor += count
            data = b''.join(pieces)
        elif start == 0:
            data = self.fetch(start, start + length - 1)
            self.seed_prefix(data)
        else:
            data = self.fetch(start, start + length - 1)
        buffer[:] = data
        self.position += length
        return length


def text(value):
    return value.decode() if isinstance(value, bytes) else str(value)


def metadata_for(variable):
    result = {key: text(variable.attrs[key]) for key in ('long_name', 'description', 'units', 'standard_name') if key in variable.attrs}
    result.update({'dimensions': [list(dim.keys())[0] if len(dim) == 1 else None for dim in variable.dims],
                   'shape': list(variable.shape), 'dtype': str(variable.dtype),
                   'chunks': list(variable.chunks) if variable.chunks else None,
                   'compression': variable.compression,
                   'scale_factor': float(variable.attrs.get('scale_factor', 1)),
                   'add_offset': float(variable.attrs.get('add_offset', 0))})
    fill = variable.attrs.get('_FillValue')
    result['fill_value'] = 'NaN' if fill is not None and not math.isfinite(float(fill)) else (float(fill) if fill is not None else None)
    return result


def number(value):
    if value is None: return None
    value = float(value)
    return value if math.isfinite(value) else None


def native_values(variable, values):
    fill = variable.attrs.get('_FillValue')
    result = []
    for value in values:
        value = number(value)
        if value is not None and fill is not None and value == float(fill): value = None
        result.append(value)
    return result


def dew_point_c(temperature, humidity):
    """Magnus approximation over liquid water; no RH clipping or ice correction."""
    if temperature is None or humidity is None or not -80 <= temperature <= 60 or not 0 < humidity <= 100:
        return None
    gamma = math.log(humidity / 100) + 17.625 * temperature / (243.04 + temperature)
    return 243.04 * gamma / (17.625 - gamma)


def wind_from_uv(u, v):
    if u is None or v is None: return None, None
    speed = math.hypot(u, v)
    return speed, (math.degrees(math.atan2(-u, -v)) % 360 if speed > 0 else None)


def height_from_geopotential(value, unit, standard_name):
    if unit != 'm2 s-2' or standard_name != 'geopotential':
        raise ProbeError('Unsupported geopotential metadata; no ambiguous height conversion.')
    return value / STANDARD_GRAVITY if value is not None else None


def build_levels(pressures, values, source_metadata, surface_pressure_pa=None):
    if source_metadata['plev']['units'] != 'hPa':
        raise ProbeError('Unsupported pressure unit; preserving only explicitly hPa levels.')
    if any(p is None or not math.isfinite(p) or p <= 0 for p in pressures) or len(set(pressures)) != len(pressures):
        raise ProbeError('Invalid or duplicate pressure levels.')
    for name, (standard_name, units) in VARIABLES.items():
        meta = source_metadata[name]
        if meta.get('units') != units or meta.get('standard_name') != standard_name:
            raise ProbeError(f'Unexpected units/standard_name for {name}.')
        if meta.get('scale_factor', 1) != 1 or meta.get('add_offset', 0) != 0:
            raise ProbeError('Packed variables not supported by this proof of concept.')
        if len(values[name]) != len(pressures): raise ProbeError('Pressure/value length mismatch.')
    levels = []
    for index in sorted(range(len(pressures)), key=lambda i: -pressures[i]):
        p = pressures[index]
        t, r, u, v, z = (number(values[name][index]) for name in ('t', 'r', 'u', 'v', 'z'))
        speed, direction = wind_from_uv(u, v)
        levels.append({'pressure_hpa': p, 'temperature': t, 'relative_humidity': r,
                       'u': u, 'v': v, 'geopotential': z,
                       'wind_speed': speed, 'wind_direction': direction,
                       'height_m': height_from_geopotential(z, source_metadata['z']['units'], source_metadata['z']['standard_name']),
                       'dew_point': dew_point_c(t, r),
                       'below_model_surface': p * 100 > surface_pressure_pa if surface_pressure_pa is not None else None})
    return levels


def reference_from_time(units, calendar):
    match = re.fullmatch(r'hours since (.+)', units)
    if not match or calendar not in ('standard', 'gregorian', 'proleptic_gregorian'):
        raise ProbeError('Unsupported CF time units/calendar.')
    origin = match[1].replace(' ', 'T', 1)
    if not re.search(r'(Z|[+-]\d\d:\d\d)$', origin): origin += 'Z'
    return utc(origin)


def inspect_and_extract(remote, lead_hours):
    try:
        import h5py
        import numpy as np
    except ImportError as error:
        raise ProbeError('Install optional h5py and NumPy for the profile experiment.') from error
    with h5py.File(remote, 'r') as nc:
        metadata = {name: metadata_for(nc[name]) for name in (*VARIABLES, 'plev', 'time', 'latitude', 'longitude', 'sp')}
        globals_ = {name: text(nc.attrs[name]) for name in ('crs', 'grid_mapping', 'date', 'run', 'spatial_resolution', 'freq', 'forecast_freq') if name in nc.attrs}
        if globals_.get('crs') != 'EPSG:4326': raise ProbeError('Unsupported grid CRS.')
        if metadata['latitude']['units'] != 'degrees_north' or metadata['longitude']['units'] != 'degrees_east':
            raise ProbeError('Unsupported geographic coordinate units.')
        for name in VARIABLES:
            variable = nc[name]
            if metadata[name]['dimensions'] != ['time', 'plev', 'latitude', 'longitude']:
                raise ProbeError(f'Unexpected dimensions for {name}.')
            if not variable.chunks or math.prod(variable.chunks) * variable.dtype.itemsize > MAX_DECODED_CHUNK:
                raise SafetyRefusal('Unsupported or excessive decoded chunk size.')
        if metadata['sp']['units'] != 'Pa' or metadata['sp']['dimensions'] != ['time', 'latitude', 'longitude']:
            raise ProbeError('Unexpected surface-pressure metadata.')
        if not nc['sp'].chunks or math.prod(nc['sp'].chunks) * nc['sp'].dtype.itemsize > MAX_DECODED_CHUNK:
            raise SafetyRefusal('Surface-pressure chunk is too large.')
        pressures, times = nc['plev'][:].tolist(), nc['time'][:].tolist()
        reference = reference_from_time(metadata['time']['units'], text(nc['time'].attrs.get('calendar', 'standard')))
        stamp = datetime.strptime(Path(remote.selected['key']).stem.replace('nwp_', ''), '%Y%m%d%H').replace(tzinfo=timezone.utc)
        if utc(stamp.isoformat()) != reference or globals_.get('date') != stamp.strftime('%Y%m%d') or globals_.get('run') != stamp.strftime('%H'):
            raise ProbeError('Filename/global/CF reference-time mismatch.')
        matches = [i for i, offset in enumerate(times) if offset == lead_hours]
        if len(matches) != 1: raise ProbeError('Requested lead does not exist uniquely; no time interpolation.')
        ti = matches[0]
        latitude, longitude = nc['latitude'][:], nc['longitude'][:]
        if not np.isfinite(latitude).all() or not np.isfinite(longitude).all(): raise ProbeError('Invalid coordinate arrays.')
        if not (min(latitude) <= 46.0656 <= max(latitude) and min(longitude) <= 14.5122 <= max(longitude)):
            raise ProbeError('Requested point lies outside grid.')
        yi, xi = int(np.argmin(abs(latitude - 46.0656))), int(np.argmin(abs(longitude - 14.5122)))
        # All metadata, dimensions, packing and units are checked before field reads.
        build_levels(pressures, {name: [None] * len(pressures) for name in VARIABLES}, metadata)
        values = {name: native_values(nc[name], nc[name][ti, :, yi, xi].tolist()) for name in VARIABLES}
        surface_pressure = native_values(nc['sp'], [nc['sp'][ti, yi, xi]])[0]
        levels = build_levels(pressures, values, metadata, surface_pressure)
        if any(not any(level[name] is not None for level in levels) for name in ('temperature', 'relative_humidity', 'u', 'v', 'geopotential')):
            raise ProbeError('Empty required profile field; previous output preserved.')
        return {'schema_version': 1, 'dataset': DATASET, 'model': 'C-LAEF AlpeAdria',
                'station': {'icao': 'LJLM', 'wmo': 14015, 'name': 'Ljubljana'},
                'reference_time': reference, 'valid_time': utc((parse_time(reference) + timedelta(hours=lead_hours)).isoformat()),
                'lead_hours': lead_hours, 'requested_location': {'latitude': 46.0656, 'longitude': 14.5122},
                'grid_point': {'latitude': float(latitude[yi]), 'longitude': float(longitude[xi]), 'latitude_index': yi, 'longitude_index': xi},
                'grid_metadata': globals_, 'time_coordinate': {'units': metadata['time']['units'], 'calendar': text(nc['time'].attrs.get('calendar', 'standard')), 'reference_timezone': 'UTC (GeoSphere forecast cycle convention)', 'offsets': times, 'selected_index': ti},
                'source_variables': metadata, 'pressure_levels_hpa': [level['pressure_hpa'] for level in levels],
                'surface_pressure_pa': surface_pressure, 'levels': levels,
                'derived_fields': {
                    'wind_speed': {'unit': 'm s-1', 'method': 'hypot(u, v)'},
                    'wind_direction': {'unit': 'degree', 'method': 'meteorological FROM direction: atan2(-u,-v) modulo 360; calm is null'},
                    'height_m': {'unit': 'm', 'method': 'geopotential / 9.80665 m s-2; geopotential height, not geometric altitude'},
                    'dew_point': {'unit': 'degree Celsius', 'method': 'Magnus over liquid water, a=17.625 b=243.04 C; -80<=T<=60 C, 0<RH<=100%; outside domain -> null; no ice correction'},
                    'below_model_surface': {'method': 'pressure_hpa * 100 > model surface pressure in Pa; native values retained; exclude flagged levels from comparisons'}},
                'source': {'url': remote.url, 'etag': remote.selected['etag'], 'file_size_bytes': remote.size},
                'limitations': ['11-level sparse pressure product; no pressure interpolation', 'No CAPE/CIN, detailed inversion, tropopause or full parcel/Skew-T diagnostics', 'Geopotential height uses standard gravity; dew point is an approximation with phase uncertainty']}


def extract(output=DEFAULT_OUTPUT, lead_hours=12, client=None, discovery=None, prefix=None, reference_time=None):
    if not isinstance(lead_hours, int) or not 0 <= lead_hours <= 60: raise ProbeError('Lead must be an integer 0–60 h.')
    client = client or TransferClient()
    if discovery is None:
        grid = json.loads(client.get(GRID_METADATA_URL))
        files = parse_listing(client.get(LISTING_URL))
    else:
        grid, files = discovery
    if not isinstance(grid.get('parameters'), list): raise ProbeError('Invalid official grid metadata.')
    files = [item for item in files if re.fullmatch(r'resources/nwp-v2-1h-1km/filelisting/nwp_\d{10}\.nc', item['key'])]
    if not files: raise ProbeError('No suitable deterministic NetCDF file.')
    if reference_time is not None:
        stamp = parse_time(reference_time).astimezone(timezone.utc).strftime('%Y%m%d%H')
        if parse_time(reference_time).minute or parse_time(reference_time).second or parse_time(reference_time).microsecond:
            raise ProbeError('Reference time must be a whole UTC hour.')
        files = [item for item in files if item['key'].endswith('nwp_' + stamp + '.nc')]
        if not files:
            raise ProbeError('Requested cycle is absent from public listing; no substitute cycle.')
    selected = max(files, key=lambda item: item['key'])
    if prefix is None:
        prefix = client.get(BULK + selected['key'], 0, 1024 * 1024 - 1, selected['bytes'], selected['etag'])
    remote = RangeFile(client, selected, prefix)
    try:
        product = inspect_and_extract(remote, lead_hours)
    finally:
        remote.close()
    product['retrieved_at'] = utc(datetime.now(timezone.utc).isoformat())
    product['remote_access'] = {'method': 'HTTP Range + h5py fileobj; compressed chunks decompressed locally',
                                'grid_api_parameter_names': [item['name'] for item in grid['parameters']],
                                'grid_api_latest_reference_time': grid.get('last_forecast_reftime'),
                                'http_requests': client.requests, 'body_bytes_transferred': client.bytes,
                                'ranges': client.range_log,
                                'hard_limits': {'body_bytes': MAX_TOTAL, 'requests': MAX_REQUESTS, 'range_bytes': MAX_RANGE, 'decoded_chunk_bytes': MAX_DECODED_CHUNK}}
    size = atomic_json(output, product)
    return {'reference_time': product['reference_time'], 'valid_time': product['valid_time'],
            'pressure_level_count': len(product['levels']), 'http_requests': client.requests,
            'body_bytes_transferred': client.bytes, 'json_bytes': size}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lead-hours', type=int, default=12)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--reference-time', help='Optional exact UTC cycle, e.g. 2026-10-09T00:00:00Z; never substitutes another cycle')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(extract(args.output, args.lead_hours, reference_time=args.reference_time)))
        return 0
    except (ProbeError, OSError, ValueError, KeyError, RuntimeError, TypeError) as error:
        print(f'C-LAEF profile stopped safely; previous output preserved: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
