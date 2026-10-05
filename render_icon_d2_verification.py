"""Render an exactly matched OBS / ICON-D2 +12 h common Skew-T."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import tempfile

import numpy as np
from metpy.plots import SkewT
from metpy.units import units
import render_ljlm as style


def utc(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('UTC timestamp requires timezone')
    return result.astimezone(timezone.utc)


def matching_time(obs, model):
    try:
        if obs.get('term') not in ('00', '12') or model.get('lead_hours') != 12:
            return None
        nominal = utc(f"{obs['nominal_date']}T{obs['term']}:00:00Z")
        valid = utc(model['valid_time'])
        if valid != nominal or utc(model['run_time']) + timedelta(hours=12) != valid:
            return None
        return nominal
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def common_pressure_bounds(obs, model):
    bottom = min(float(np.max(p.p_hpa)) for p in (obs, model))
    top = max(100., *(float(np.min(p.p_hpa)) for p in (obs, model)))
    if top >= bottom:
        raise ValueError('Profiles have no common pressure range')
    return bottom, top


def barb_values(profile, targets):
    u, v = style._wind_components(profile)
    return (style._interp_logp(profile.p_hpa, u, targets),
            style._interp_logp(profile.p_hpa, v, targets))


def render_overlay(obs, model, valid, output):
    profiles = [style._clean_profile(data.get('levels', [])) for data in (obs, model)]
    bottom, top = common_pressure_bounds(*profiles)
    fig = style.plt.figure(figsize=(10, 9), dpi=170)
    try:
        fig.subplots_adjust(left=.08, right=.74, top=.86, bottom=.10)
        skew = SkewT(fig, rotation=45)
        skew.plot_dry_adiabats(alpha=.24, linewidth=.55, color='#C7A69A')
        skew.plot_moist_adiabats(alpha=.24, linewidth=.55, color='#7DB6D8')
        skew.plot_mixing_lines(alpha=.24, linewidth=.50, color='#86BE9A')
        for profile, label, line, column in zip(profiles, ['OBS', 'ICON-D2'], ['-', '--'], [1.05, 1.23]):
            visible = (profile.p_hpa <= bottom) & (profile.p_hpa >= top)
            for values, color, variable in [(profile.t_c, style.TEMP, 'T'), (profile.td_c, style.DEW, 'Td')]:
                skew.plot(profile.p_hpa[visible] * units.hPa, values[visible] * units.degC,
                          color=color, linestyle=line, linewidth=2, label=f'{label} {variable}')
            targets = style._dense_barb_pressures(bottom, top)
            u, v = barb_values(profile, targets)
            good = np.isfinite(u) & np.isfinite(v)
            skew.plot_barbs(np.asarray(targets)[good] * units.hPa,
                            (u[good] * units('m/s')).to('knots'),
                            (v[good] * units('m/s')).to('knots'), xloc=column,
                            color=style.INK, linewidth=.65, length=5)
            skew.ax.text(column, 1.025, label, transform=skew.ax.transAxes,
                         ha='center', fontsize=9, weight='bold')
        skew.ax.set_ylim(bottom, top)
        skew.ax.set_xlim(-45, 45)
        ticks = [p for p in style.STANDARD_PRESSURES if top <= p <= bottom]
        skew.ax.set_yticks(ticks)
        skew.ax.set_yticklabels([str(p) for p in ticks])
        skew.ax.set_xlabel('Temperature (°C)')
        skew.ax.set_ylabel('Pressure (hPa)')
        skew.ax.grid(color=style.GRID, linewidth=.6)
        skew.ax.legend(loc='upper left', fontsize=9)
        fig.suptitle('LJLM · OBS vs ICON-D2 +12 h', fontsize=17, weight='bold', y=.97)
        fig.text(.08, .915, f"Valid {valid:%d %b %Y %H UTC}\nICON-D2 run {utc(model['run_time']):%d %b %Y %H UTC} +12 h", fontsize=11)
        fig.text(.08, .035, 'Solid: OBS · Dashed: ICON-D2 · Wind barbs in kt', color=style.MUTED)
        fig.savefig(output)
    finally:
        style.plt.close(fig)


def render_verification(obs_path='data/latest.json', model_path=None, output_root='verification/icon-d2'):
    obs_path = Path(obs_path)
    if not obs_path.is_file():
        print('Overlay skipped: observation is missing.')
        return None
    obs = json.loads(obs_path.read_text())
    if obs.get('term') not in ('00', '12'):
        print('Overlay skipped: observation is not a regular 00/12 UTC term.')
        return None
    model_path = Path(model_path or f"models/icon-d2/latest/{obs['term']}/sounding.json")
    if not model_path.is_file():
        print('Overlay skipped: matching model file is missing.')
        return None
    model = json.loads(model_path.read_text())
    valid = matching_time(obs, model)
    if valid is None:
        print('Overlay skipped: OBS nominal time, model valid time, run time or +12 h lead do not match.')
        return None
    root = Path(output_root)
    slot = root / 'latest' / obs['term']
    archive = root / valid.strftime('%Y/%m') / f"{valid:%Y%m%d}_{obs['term']}_skewt_overlay.png"
    latest = slot / 'skewt_overlay.png'
    manifest = dict(version=1, station=obs.get('station', 14015), station_name=obs.get('station_name', 'Ljubljana'),
                    valid_time=valid.isoformat().replace('+00:00', 'Z'), verification_date=obs['nominal_date'],
                    verification_term=obs['term'], observation_sounding_id=obs.get('sounding_id'),
                    model_sounding_id=model.get('sounding_id'), model=model.get('model', 'ICON-D2'),
                    model_run_time=model['run_time'], lead_hours=12,
                    rendered_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                    skewt_overlay=latest.as_posix(), archive_skewt_overlay=archive.as_posix())
    encoded = json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2) + '\n'
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        temporary = Path(directory) / 'overlay.png'
        render_overlay(obs, model, valid, temporary)
        archive.parent.mkdir(parents=True, exist_ok=True)
        slot.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(temporary, archive)
        temporary.replace(latest)
        pending = Path(directory) / 'manifest.json'
        pending.write_text(encoded)
        pending.replace(slot / 'latest_products.json')
    print(f'Overlay rendered: {latest} ({latest.stat().st_size} bytes)')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--obs', default='data/latest.json')
    parser.add_argument('--model')
    parser.add_argument('--output-root', default='verification/icon-d2')
    args = parser.parse_args()
    render_verification(args.obs, args.model, args.output_root)


if __name__ == '__main__':
    main()
