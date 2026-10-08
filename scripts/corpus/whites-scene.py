#!/usr/bin/env python3
"""Measure Camera Raw's positive Whites before the profile tone curve.

Camera Raw applies Whites to scene values, before the camera profile's tone curve,
and its strength follows how bright the scene is. A darker shot (less light on the
sensor) and the Exposure slider are not interchangeable: the same image level gets
different curves. This script measures the curve over both.

`charts DIR` writes synthetic-d65 shot at sensor exposures -7 to +1 EV into DIR
(`cargo test --test color write_sensor_charts -- --ignored`).

`render --charts DIR --refs FILE` has Photoshop 2026 (Camera Raw) render each of
those charts at Exposure -4, -2, 0, +2 and +4 with Whites 0, +25, +50 and +100
(--sensor limits the sensor exposures) and adds the patch means to FILE (outside the
repository). It refuses to start while Photoshop has documents open.

`table --refs FILE` reads the chart's neutral patches, undoes the profile tone curve
(the DNG default curve the synthetic profile uses) and writes the curves, in log2 of
scene value, to crates/rawmakase-engine/src/develop/whites_scene_data.rs.

`offset --photos MANIFEST...` fits how the photo's highlights select a curve: for
each photo with Camera Raw renders `default` and `whites100` (Adobe Standard, which
uses the same default tone curve) it finds the sensor exposure whose curve explains
Camera Raw best and prints its median offset from log2 of the 98th percentile of
scene luminance (`SCENE_KEY` in basic_tone.rs). The
manifests are scripts/cameras/parity.py captures; photos stay outside the repository.
The key here is measured on Camera Raw's own default render; RAWmakase's reads about
0.1 EV brighter, so check the constant against the renderer's before changing it.

Requires numpy (and Pillow for `offset`). Run from the repository root.
"""
import argparse
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.dont_write_bytecode = True
HERE = Path(__file__).parent
ROOT = HERE.parents[1]
_spec = importlib.util.spec_from_file_location('charts', HERE / 'camera-raw-charts.py')
charts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(charts)
sys.path.insert(0, str(HERE))
import tiff16  # noqa: E402

SENSOR = list(range(-7, 2))
EXPOSURES = [-4, -2, 0, 2, 4]
AMOUNTS = [25, 50, 100]
LOG_X = np.arange(-16, 0.001, 0.25)
OUT = ROOT / 'crates/rawmakase-engine/src/develop/whites_scene_data.rs'
TONE = np.array([float(x) for x in re.findall(
    r'\d+\.\d+', (ROOT / 'crates/rawmakase-model/src/camera_profiles/dng_tone.rs').read_text().split('= [')[1])])
TONE_X = np.linspace(0, 1, len(TONE))
RGB_TO_PRO = np.array([[0.529345, 0.330072, 0.140583], [0.098374, 0.873462, 0.028164],
                       [0.016883, 0.117673, 0.865444]])


def decode(v):
    v = np.asarray(v, float)
    return np.where(v <= 0.04045, v / 12.92, ((np.maximum(v, 0) + 0.055) / 1.055) ** 2.4)


def untone(y):
    return np.interp(np.clip(y, 0, 1), TONE, TONE_X)


def name(s, e, amount):
    return f's{s:+.1f}-e{e:+.1f}-w{amount}'


def write_charts(out):
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(['cargo', 'test', '--test', 'color', 'write_sensor_charts', '--', '--ignored'],
                   cwd=ROOT, check=True, env={**__import__('os').environ, 'RAWMAKASE_SENSOR_CHARTS': str(out)})


def render(chart_dir, refs, sensors):
    cases_doc = json.loads((charts.CORPUS / 'cases.json').read_text())
    layout = json.loads((charts.CORPUS / 'charts/layout.json').read_text())['patches']
    existing = json.loads(refs.read_text()) if refs.exists() else {}
    work = Path(tempfile.mkdtemp(prefix='whites-scene-'))
    jobs, planned = [], []
    for s in sensors:
        source = chart_dir / f'sensor{s:+.2f}.dng'
        if not source.exists():
            sys.exit(f'{source} does not exist; run `charts` first')
        for e in EXPOSURES:
            for amount in [0] + AMOUNTS:
                key = name(s, e, amount)
                if key in existing:
                    continue
                settings = {'Exposure2012': str(e), 'Whites2012': str(amount)}
                tiff = work / f'{key}.tif'
                planned.append((key, tiff))
                jobs.append({'source': str(source), 'dng': str(work / 'copy.dng'), 'xmp': str(work / 'copy.xmp'),
                             'settings': charts.xmp(cases_doc['base'], {'settings': settings}, {}),
                             'out': str(tiff)})
    if not jobs:
        sys.exit('Nothing to render')
    if not charts.photoshop_idle():
        sys.exit(f'{charts.PHOTOSHOP} is not running or has documents open.')
    script = work / 'render.jsx'
    script.write_text(charts.TEMPLATE % {'jobs': json.dumps(jobs)})
    print(f'Rendering {len(jobs)} cases in {work}', flush=True)
    subprocess.run(['osascript', '-e', f'with timeout of 36000 seconds\ntell application "{charts.PHOTOSHOP}" '
                    f'to do javascript file (POSIX file "{script}")\nend timeout'], check=True, stdout=subprocess.DEVNULL)
    for key, tiff in planned:
        if not tiff.exists():
            sys.exit(f'missing render: {key}')
        existing[key] = charts.patch_values(tiff, layout)
        tiff.unlink()
    refs.write_text(json.dumps(existing))
    print(f'{refs}: {len(existing)} renders')


def neutral_indices():
    layout = json.loads((charts.CORPUS / 'charts/layout.json').read_text())['patches']
    return [i for i, p in enumerate(layout) if p['group'] in ('ramp', 'neutral')]


def scene(refs, key, patches):
    """The neutral patches' scene values (before the profile tone curve)."""
    return untone(decode(np.array(refs[key], float)[patches, 1] / 65535))


def log_curve(refs, s, e, amount, patches):
    """log2 of Whites' output at LOG_X, from one render pair. Below the darkest
    measured patch the gain stays that patch's; above the brightest it continues
    with the last slope (at least 1) up to white."""
    x, y = scene(refs, name(s, e, 0), patches), scene(refs, name(s, e, amount), patches)
    keep = (x > 3e-5) & (x < 0.995) & (y > 3e-5)
    x, y = x[keep], y[keep]
    order = np.argsort(x)
    lx, ly = np.log2(x[order]), np.log2(np.minimum(y[order], 1))
    distinct = np.r_[True, np.diff(lx) > 0.05]
    lx, ly = lx[distinct], np.maximum.accumulate(ly[distinct])
    out = np.interp(LOG_X, lx, ly)
    below = LOG_X < lx[0]
    out[below] = LOG_X[below] + (ly[0] - lx[0])
    above = LOG_X > lx[-1]
    if ly[-1] < 0:
        slope = max((ly[-1] - ly[-2]) / (lx[-1] - lx[-2]), 1.)
        out[above] = ly[-1] + (LOG_X[above] - lx[-1]) * slope
    else:
        out[above] = 0
    return np.minimum(np.maximum.accumulate(out), 0)


def table(refs_path):
    refs = json.loads(refs_path.read_text())
    patches = neutral_indices()
    missing = [name(s, e, a) for s in SENSOR for e in EXPOSURES for a in [0] + AMOUNTS
               if name(s, e, a) not in refs]
    if missing:
        sys.exit(f'{len(missing)} renders missing, e.g. {missing[0]}')
    curves = [[[log_curve(refs, s, e, a, patches) for a in AMOUNTS] for e in EXPOSURES] for s in SENSOR]

    def floats(values):
        return ', '.join(f'{v:.4f}' for v in values)

    lines = [
        '//! Camera Raw 18.7 positive Whites before the profile tone curve, measured on',
        '//! synthetic-d65 shot at sensor exposures -7 to +1 EV with the Exposure slider at',
        '//! -4 to +4. Generated by scripts/corpus/whites-scene.py; do not edit.',
        '// Measured samples, not approximations to mathematical constants.',
        '#![allow(clippy::excessive_precision, clippy::approx_constant)]',
        '',
        '/// The charts\' sensor exposures (EV).',
        f'pub(super) const SENSOR: [f32; {len(SENSOR)}] = [{floats(SENSOR)}];',
        '/// The Exposure slider positions (EV).',
        f'pub(super) const EXPOSURES: [f32; {len(EXPOSURES)}] = [{floats(EXPOSURES)}];',
        '/// log2 of the first input sample; samples are `LOG_STEP` apart.',
        f'pub(super) const LOG_START: f32 = {LOG_X[0]:.1f};',
        f'pub(super) const LOG_STEP: f32 = {LOG_X[1] - LOG_X[0]:.2f};',
        '/// log2 of Whites\' output for the scene value 2^(LOG_START + i LOG_STEP): per sensor',
        '/// exposure, Exposure slider position and Whites +25, +50, +100.',
        f'pub(super) static CURVES: [[[[f32; {len(LOG_X)}]; {len(AMOUNTS)}]; {len(EXPOSURES)}]; {len(SENSOR)}] = [',
    ]
    for per_e in curves:
        lines.append('    [')
        for per_a in per_e:
            lines.append('        [')
            for c in per_a:
                lines.append(f'            [{floats(c)}],')
            lines.append('        ],')
        lines.append('    ],')
    lines.append('];')
    OUT.write_text('\n'.join(lines) + '\n')
    subprocess.run(['rustfmt', '--edition', '2024', str(OUT)], check=True)
    print(f'wrote {OUT.relative_to(ROOT)}')


def evaluate(curves, s, e, amount, x):
    """Whites' output for scene values x, as basic_tone.rs evaluates CURVES."""
    k = AMOUNTS.index(amount)
    e = float(np.clip(e, EXPOSURES[0], EXPOSURES[-1]))
    j = min(int(np.searchsorted(EXPOSURES, e, side='right')) - 1, len(EXPOSURES) - 2)
    we = (e - EXPOSURES[j]) / (EXPOSURES[j + 1] - EXPOSURES[j])

    def member(i, lx):
        return np.interp(lx, LOG_X, curves[i][j][k]) * (1 - we) + np.interp(lx, LOG_X, curves[i][j + 1][k]) * we

    lx = np.log2(np.maximum(x, 2.0 ** -40))
    if s <= SENSOR[0]:
        shifted = lx + SENSOR[0] - s
        ly = member(0, np.maximum(shifted, LOG_X[0])) + np.minimum(shifted - LOG_X[0], 0)
    elif s >= SENSOR[-1]:
        ly = member(len(SENSOR) - 1, np.maximum(lx, LOG_X[0])) + np.minimum(lx - LOG_X[0], 0)
    else:
        i = int(np.floor(s - SENSOR[0]))
        t = s - SENSOR[i]
        a, b = lx - t, lx + 1 - t
        ly = (1 - t) * (member(i, np.maximum(a, LOG_X[0])) + np.minimum(a - LOG_X[0], 0)) \
            + t * (member(i + 1, np.maximum(b, LOG_X[0])) + np.minimum(b - LOG_X[0], 0))
    return np.minimum(2.0 ** ly, 1)


def offset(refs_path, manifests):
    from PIL import Image
    from scipy.optimize import minimize_scalar
    refs = json.loads(refs_path.read_text())
    patches = neutral_indices()
    curves = [[[log_curve(refs, s, e, a, patches) for a in AMOUNTS] for e in EXPOSURES] for s in SENSOR]
    pro_to_rgb = np.linalg.inv(RGB_TO_PRO)

    def load(path):
        a = tiff16.read(path).astype('float32')
        h, w = a.shape[:2]
        size = (round(w * 512 / max(w, h)), round(h * 512 / max(w, h)))
        return np.stack([np.array(Image.fromarray(np.ascontiguousarray(a[..., i])).resize(size, Image.BOX))
                         for i in range(3)], -1).astype(float)

    def rgbtone(p, f):
        lo, hi = p.min(-1, keepdims=True), p.max(-1, keepdims=True)
        a, b = f(lo), f(hi)
        return np.where(hi - lo > 1e-9, a + (b - a) * (p - lo) / np.maximum(hi - lo, 1e-9), a)

    def encode(v):
        return np.where(v <= 0.0031308, 12.92 * v, 1.055 * np.maximum(v, 0) ** (1 / 2.4) - 0.055)

    rows = []
    for manifest in manifests:
        root = manifest.parent
        cases = json.loads(manifest.read_text())['cases']
        for sample in sorted({c['sample'] for c in cases}):
            files = {c['name']: root / c['reference'] for c in cases if c['sample'] == sample}
            if 'whites100' not in files or 'default' not in files:
                continue
            default, target = load(files['default']), load(files['whites100'])
            pro = np.clip(decode(default), 0, 1) @ RGB_TO_PRO.T
            before = rgbtone(pro, untone)
            key = np.log2(np.percentile(np.clip(before @ pro_to_rgb.T, 0, None) @ [0.2126, 0.7152, 0.0722], 98))

            def error(s):
                tone = lambda v: np.interp(evaluate(curves, s, 0, 100, untone(v)), TONE_X, TONE)
                out = encode(np.clip(np.clip(rgbtone(pro, tone), 0, 1) @ pro_to_rgb.T, 0, 1))
                return float(np.abs(out - target)[3:-3, 3:-3].mean())

            best = minimize_scalar(error, bounds=(-9, 2), method='bounded')
            rows.append((key, best.x))
            print(f'{root.name}/{sample}: key {key:+.2f}, best sensor EV {best.x:+.2f} ({best.fun:.4f})', flush=True)
    keys, best = np.array(rows).T
    print(f'SCENE_KEY = {np.median(best - keys):.2f} (spread {np.std(best - keys):.2f} EV on {len(rows)} photos)')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='command', required=True)
    c = sub.add_parser('charts')
    c.add_argument('dir', type=Path)
    r = sub.add_parser('render')
    r.add_argument('--charts', type=Path, required=True)
    r.add_argument('--refs', type=Path, required=True)
    r.add_argument('--sensor', type=int, nargs='*', default=SENSOR)
    t = sub.add_parser('table')
    t.add_argument('--refs', type=Path, required=True)
    o = sub.add_parser('offset')
    o.add_argument('--refs', type=Path, required=True)
    o.add_argument('--photos', type=Path, nargs='+', required=True)
    args = p.parse_args()
    if args.command == 'charts':
        write_charts(args.dir)
    elif args.command == 'render':
        render(args.charts, args.refs, args.sensor)
    elif args.command == 'table':
        table(args.refs)
    else:
        offset(args.refs, args.photos)


if __name__ == '__main__':
    main()
