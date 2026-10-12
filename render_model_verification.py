"""Exactly matched LJLM OBS / ICON-D2 / C-LAEF +12 h verification.

Latest products are regular files published by swapping a completed directory.
The two directory renames leave a brief absence window; failures restore the old directory.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import shutil
import tempfile

import numpy as np
from metpy.plots import SkewT
from metpy.units import units
import render_ljlm as style

STANDARD_LEVELS = (925, 850, 700, 500, 300)
FIELDS = ('temperature_c', 'dewpoint_c', 'relative_humidity_pct', 'height_m', 'u_ms', 'v_ms')
NAMES = ('OBS', 'ICON-D2', 'C-LAEF')


def utc(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp requires timezone')
    return result.astimezone(timezone.utc)


def matching_time(obs, icon, claef):
    try:
        if obs.get('term') not in ('00', '12'):
            return None
        nominal = utc(f"{obs['nominal_date']}T{obs['term']}:00:00Z")
        for model in (icon, claef):
            if (model.get('lead_hours') != 12 or utc(model['valid_time']) != nominal
                    or utc(model['run_time']) + timedelta(hours=12) != nominal):
                return None
            if model.get('station', obs.get('station')) != obs.get('station'):
                return None
        return nominal
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def number(value):
    return float(value) if style._finite(value) else None


def usable_rows(data):
    rows = [r for r in data['levels'] if style._finite(r.get('pressure_hpa'))
            and 10 <= float(r['pressure_hpa']) <= 1100 and not r.get('below_model_surface', False)]
    rows.sort(key=lambda r: float(r['pressure_hpa']), reverse=True)
    # Deterministic first native row at each pressure; preserve its null fields.
    return list({float(r['pressure_hpa']): r for r in reversed(rows)}.values())[::-1]


def row_values(row):
    values = {f: number(row.get(f)) for f in FIELDS}
    # Direct u/v take precedence; derive only when both are absent, from native wind.
    if row.get('u_ms') is None and row.get('v_ms') is None:
        profile = style.Profile(np.array([row['pressure_hpa']]), np.array([np.nan]),
                                np.array([np.nan]), np.array([np.nan]),
                                np.array([number(row.get('wind_speed_ms'))], dtype=float),
                                np.array([number(row.get('wind_direction_deg'))], dtype=float))
        u, v = style._wind_components(profile)
        values.update(u_ms=number(u[0]), v_ms=number(v[0]))
    return values


def sample(data, pressure, native_only=False):
    rows = usable_rows(data)
    exact = [r for r in rows if float(r['pressure_hpa']) == pressure
             and (not native_only or r.get('level_type') == 'pressure')]
    if exact:
        return row_values(exact[0]), 'native_exact_pressure'
    empty = dict.fromkeys(FIELDS)
    if native_only:
        return empty, 'unavailable_native_pressure'
    # Use adjacent native pressures only: never bridge a missing variable.
    for lower, upper in zip(rows, rows[1:]):
        if float(lower['pressure_hpa']) > pressure > float(upper['pressure_hpa']):
            a, b = row_values(lower), row_values(upper)
            p = np.array([lower['pressure_hpa'], upper['pressure_hpa']], dtype=float)
            return {f: number(style._interp_logp(p, np.array([a[f], b[f]], dtype=float), [pressure])[0])
                    for f in FIELDS}, 'log_pressure_adjacent_native_rows'
    return empty, 'outside_native_pressure_range'


def native_surface(data):
    rows = usable_rows(data)
    explicit = [r for r in rows if r.get('level_type') == 'model_surface']
    return {'source_method': 'explicit_model_surface' if explicit else 'highest_pressure_native_row',
            'row': (explicit or rows)[0] if rows else None}


def comparison(obs, icon, claef, valid):
    records = []
    for pressure in STANDARD_LEVELS:
        values, methods = {}, {}
        for name, data in zip(NAMES, (obs, icon, claef)):
            values[name], methods[name] = sample(data, pressure, name == 'C-LAEF')
        biases = {}
        for name in NAMES[1:]:
            bias = {f: values[name][f] - values['OBS'][f]
                    if values[name][f] is not None and values['OBS'][f] is not None else None for f in FIELDS}
            bias['wind_vector_error_ms'] = (math.hypot(bias['u_ms'], bias['v_ms'])
                                           if bias['u_ms'] is not None and bias['v_ms'] is not None else None)
            biases[name] = bias
        records.append(dict(pressure_hpa=pressure, values=values, source_methods=methods, model_minus_obs=biases))
    return dict(version=1, station=obs.get('station', 14015), valid_time=valid.isoformat().replace('+00:00', 'Z'),
                observation_sounding_id=obs.get('sounding_id'),
                models={name: {k: data.get(k) for k in ('sounding_id', 'run_time', 'lead_hours')}
                        for name, data in zip(NAMES[1:], (icon, claef))},
                standard_pressure_comparisons=records,
                native_surfaces={name: native_surface(data) for name, data in zip(NAMES, (obs, icon, claef))},
                methods={'bias': 'model minus OBS; null if either value unavailable',
                         'humidity': 'native relative humidity only; no derivation or substitution',
                         'wind': 'native u/v preferred; otherwise native speed/direction converted before interpolation',
                         'surface': 'separate native rows; no surface bias',
                         'plot': 'native T/Td samples; C-LAEF dotted with native markers; no dense resampling'},
                generated_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'))


def render_overlay(obs, icon, claef, valid, output):
    rows = [usable_rows(d) for d in (obs, icon, claef)]
    if any(len(r) < 2 for r in rows):
        raise ValueError('Each profile requires at least two native pressure rows')
    bottom = min(float(r[0]['pressure_hpa']) for r in rows)
    top = max(100., *(float(r[-1]['pressure_hpa']) for r in rows))
    if top >= bottom:
        raise ValueError('No common pressure range')
    fig = style.plt.figure(figsize=(12, 9), dpi=170)
    try:
        fig.subplots_adjust(left=.08, right=.65, top=.83, bottom=.10)
        skew = SkewT(fig, rotation=45, aspect='auto')
        skew.plot_dry_adiabats(alpha=.24, linewidth=.55, color='#C7A69A')
        skew.plot_moist_adiabats(alpha=.24, linewidth=.55, color='#7DB6D8')
        skew.plot_mixing_lines(alpha=.24, linewidth=.50, color='#86BE9A')
        for name, data, native, line, column in zip(NAMES, (obs, icon, claef), rows, ('-', '--', ':'), (1.06, 1.24, 1.42)):
            visible = [r for r in native if top <= float(r['pressure_hpa']) <= bottom]
            p = np.array([r['pressure_hpa'] for r in visible])
            for field, color, label in [('temperature_c', style.TEMP, 'T'), ('dewpoint_c', style.DEW, 'Td')]:
                values = np.array([number(r.get(field)) for r in visible], dtype=float)
                skew.plot(p * units.hPa, values * units.degC, color=color, linestyle=line,
                          marker='o' if name == 'C-LAEF' else None, markersize=3, linewidth=2, label=f'{name} {label}')
            targets = p if name == 'C-LAEF' else style._dense_barb_pressures(bottom, top)
            winds = [sample(data, float(t), name == 'C-LAEF')[0] for t in targets]
            good = [(t, w) for t, w in zip(targets, winds) if w['u_ms'] is not None and w['v_ms'] is not None]
            if good:
                skew.plot_barbs(np.array([t for t, w in good]) * units.hPa,
                                (np.array([w['u_ms'] for t, w in good]) * units('m/s')).to('knots'),
                                (np.array([w['v_ms'] for t, w in good]) * units('m/s')).to('knots'),
                                xloc=column, color=style.INK, linewidth=.65, length=5)
            skew.ax.text(column, 1.025, name, transform=skew.ax.transAxes, ha='center', fontsize=9, weight='bold')
        skew.ax.set_ylim(bottom, top)
        skew.ax.set_xlim(-45, 45)
        ticks = [p for p in style.STANDARD_PRESSURES if top <= p <= bottom]
        skew.ax.set_yticks(ticks)
        skew.ax.set_yticklabels([str(p) for p in ticks])
        skew.ax.set_xlabel('Temperature (°C)')
        skew.ax.set_ylabel('Pressure (hPa)')
        skew.ax.grid(color=style.GRID, linewidth=.6)
        skew.ax.legend(loc='upper left', fontsize=9)
        fig.suptitle('LJLM · OBS vs ICON-D2 vs C-LAEF', fontsize=17, weight='bold', y=.98)
        fig.text(.08, .875, f"Valid {valid:%d %b %Y %H UTC}\n" + '\n'.join(
            f"{name} run {utc(data['run_time']):%d %b %Y %H UTC} +12 h"
            for name, data in zip(NAMES[1:], (icon, claef))), fontsize=10)
        fig.text(.08, .035, 'Solid: OBS · Dashed: ICON-D2 · Dotted + native points: C-LAEF · Wind barbs in kt', color=style.MUTED)
        fig.savefig(output)
    finally:
        style.plt.close(fig)


def publish(bundle, root, valid, term):
    """Swap a completed regular directory; restore latest and archives on failure."""
    latest = root / 'latest' / term
    if latest.is_symlink() or (latest.exists() and not latest.is_dir()):
        raise ValueError('Latest term must be absent or a normal directory')
    archive = root / valid.strftime('%Y/%m')
    archive.mkdir(parents=True, exist_ok=True)
    latest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f'.{term}-publish-', dir=latest.parent))
    pending_latest = staging / 'completed'
    previous_latest = staging / 'previous'
    backups = {}
    keep_backup = False
    try:
        pending_latest.mkdir()
        for filename in ('comparison.json', 'skewt_overlay.png'):
            shutil.copyfile(bundle / filename, pending_latest / filename)
        for filename in ('comparison.json', 'skewt_overlay.png'):
            target = archive / f'{valid:%Y%m%d}_{term}_{filename}'
            backups[target] = target.read_bytes() if target.exists() else None
            pending = bundle / ('archive_' + filename)
            shutil.copyfile(bundle / filename, pending)
            pending.replace(target)
        # Recheck before moving latest; never follow an unexpected symlink.
        if latest.is_symlink():
            raise ValueError('Latest term unexpectedly became a symlink')
        if latest.exists():
            latest.replace(previous_latest)
        pending_latest.replace(latest)
    except Exception:
        if previous_latest.exists():
            try:
                previous_latest.replace(latest)
            except Exception:
                # Keep the old directory recoverable if the filesystem also rejects rollback.
                keep_backup = True
                raise
        for target, previous in backups.items():
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                pending = bundle / ('restore_' + target.name)
                pending.write_bytes(previous)
                pending.replace(target)
        raise
    finally:
        if not keep_backup:
            shutil.rmtree(staging, ignore_errors=True)


def render_verification(obs_path='data/latest.json', icon_path=None, claef_path=None, output_root='verification/models'):
    bundle = None
    try:
        obs = json.loads(Path(obs_path).read_text())
        if obs.get('term') not in ('00', '12'):
            raise ValueError('Observation is not a 00/12 UTC term')
        icon = json.loads(Path(icon_path or f"models/icon-d2/latest/{obs['term']}/sounding.json").read_text())
        claef = json.loads(Path(claef_path or f"models/claef/latest/{obs['term']}/sounding.json").read_text())
        valid = matching_time(obs, icon, claef)
        if valid is None:
            raise ValueError('Three-way nominal/valid/run/+12 h match failed')
        result = comparison(obs, icon, claef, valid)
        root = Path(output_root)
        root.mkdir(parents=True, exist_ok=True)
        bundle = Path(tempfile.mkdtemp(prefix='.verification-', dir=root))
        (bundle / 'comparison.json').write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + '\n')
        if json.loads((bundle / 'comparison.json').read_text()) != result:
            raise ValueError('Comparison JSON validation failed')
        render_overlay(obs, icon, claef, valid, bundle / 'skewt_overlay.png')
        from PIL import Image
        with Image.open(bundle / 'skewt_overlay.png') as png:
            if png.format != 'PNG':
                raise ValueError('Renderer did not produce PNG')
            png.verify()
        publish(bundle, root, valid, obs['term'])
        print(f"Verification rendered: {root / 'latest' / obs['term']}")
        return result
    except Exception as error:
        print(f'Verification skipped; previous latest preserved: {error}')
        return None
    finally:
        if bundle is not None:
            shutil.rmtree(bundle, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--obs', default='data/latest.json')
    parser.add_argument('--icon')
    parser.add_argument('--claef')
    parser.add_argument('--output-root', default='verification/models')
    args = parser.parse_args()
    render_verification(args.obs, args.icon, args.claef, args.output_root)


if __name__ == '__main__':
    main()
