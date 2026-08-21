#!/usr/bin/env python
"""Turn the minute parquet from wdc_fetch.py into a compact web feed.

Written for the live Hobart page on the AusMIS magnetometers site.  The feed is
split in two so that frequent updates stay cheap in git history:

    today.json      the current UT day, rewritten every time the updater runs
                    (~9 kB), because BOM keeps appending to it all day
    recent90.json   the days before today, rebuilt once per UT day (~1 MB)
    k.json          3-hourly local K and planetary Kp, moving every 3 h (~25 kB)
    overview.png    Morlet scalogram of H, rebuilt once per UT day (~500 kB)
    index.json      the manifest: what exists, and the panels the page draws

Rewriting the 1 MB window every half hour would add ~50 MB a day to the data
repo forever; rewriting only the current day is ~400 kB a day.

    conda run -n pygmt17 python build_feed.py --data ./WDCData --out ./feed

Format.  Each series is a regular minute grid - no timestamps are shipped, the
reader derives them from t0 and dt - quantised to int16 with an offset and
scale, and base64'd.  That is the encoding the site's explorer uses for every
feed (see science.quantise, which asserts the browser's decode inverts it), and
it costs about 2 bytes per sample instead of ~12 as JSON text.

index.json is a manifest: as well as the grid it carries the `panels` list that
decides what the page draws and in what order.  The explorer knows nothing about
observatories or fluxgates, so a new panel is a change here rather than a change
to the website.

    value[i] = o + s * int16[i]          i-th sample is at t0 + i*dt seconds
    int16[i] == -32768                   no data for that minute

Days that failed QC in the fetcher's manifest (h_stuck, range_implausible) are
written as gaps rather than plotted: the 2016 dead-channel period showed what a
single stuck day does to a shared colour scale, and one 14000 nT artefact would
wreck the quantiser's precision for the whole window.
"""

from __future__ import annotations

import argparse
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import science

SENTINEL = science.SENTINEL
SERIES = {'h': 'H', 'd': 'D', 'z': 'Z'}
SOURCE = ('Bureau of Meteorology Space Weather Services, World Data Centre, '
          'Hobart observatory (station 85401)')
UNITS = 'nT, uncalibrated variometer variation (changes are meaningful, the level is not)'


# The encoding lives in science.py alongside the assertion that the site's
# JavaScript decode inverts it, so the two cannot drift apart unnoticed.
quantise = science.quantise

# What the page draws, in the order it draws it. The explorer reads this and
# nothing else - it has no idea what an observatory measures - so a new panel is
# a change here rather than a change to the site.
PANELS = [
    {'id': 'h', 'kind': 'line', 'label': 'H  (horizontal)',
     'short': 'H', 'unit': 'nT', 'height': 150},
    {'id': 'd', 'kind': 'line', 'label': 'D  (declination channel)',
     'short': 'D', 'unit': 'nT', 'height': 150},
    {'id': 'z', 'kind': 'line', 'label': 'Z  (vertical)',
     'short': 'Z', 'unit': 'nT', 'height': 150},
    {'id': 'kp', 'kind': 'k', 'field': 'kp', 'height': 86, 'short': 'Kp (GFZ)',
     'label': 'Planetary Kp — GFZ Potsdam (independent)'},
    {'id': 'klocal', 'kind': 'k', 'field': 'local', 'height': 86,
     'short': 'Local K',
     'label': 'Local K — from the 3 h range of H with Sq removed'},
    {'id': 'wavelet', 'kind': 'wavelet', 'height': 230, 'short': 'Wavelet',
     'label': 'Morlet wavelet power in H, dB above the quiet level '
              '(red = enhanced)'},
]

# Periods the scalogram covers. The floor is two minutes because this is
# 1-minute data: Pc3 and most of Pc4 are simply not in this record, unlike the
# fluxgate page which resolves them from a 2 s logger.
PERIODS = np.geomspace(120, 24 * 3600, 110)
OVERVIEW_BIN = 20        # minutes per column, matching the fluxgate page


def source_modified(root: Path, day: pd.Timestamp) -> str | None:
    """The Last-Modified the BOM server gave for that day's file, if known."""
    path = root / 'http_cache.json'
    if not path.exists():
        return None
    cache = json.loads(path.read_text())
    stamp = day.strftime('%y%m%d')
    for name, meta in cache.items():
        if name[2:8] == stamp:
            return meta.get('last_modified')
    return None


def flagged_days(root: Path) -> set:
    """Dates the fetcher marked as instrument faults."""
    path = root / 'manifest.json'
    if not path.exists():
        return set()
    days = json.loads(path.read_text()).get('days', {})
    return {d for d, m in days.items()
            if m.get('h_stuck') or m.get('range_implausible')}


def load_minutes(root: Path, station: str, start, end) -> pd.DataFrame:
    """Minute means on a complete grid over [start, end), gaps as NaN."""
    stn = station.upper()
    frames = []
    for year in range(start.year, end.year + 1):
        path = root / station / 'minute' / f'{stn}_{year}_1min.parquet'
        if path.exists():
            frames.append(pd.read_parquet(path,
                                          columns=['time'] + [f'{c}_mean' for c in SERIES]))
    if not frames:
        raise SystemExit(f'no minute parquet under {root / station} - run wdc_fetch.py first')

    df = pd.concat(frames).drop_duplicates('time').set_index('time').sort_index()
    df = df.rename(columns={f'{c}_mean': c for c in SERIES})

    bad = flagged_days(root / station)
    if bad:
        mask = df.index.strftime('%Y-%m-%d').isin(bad)
        if mask.any():
            df.loc[mask, list(SERIES)] = np.nan
            print(f'  {mask.sum()} minutes blanked from {len(bad)} QC-flagged days')

    grid = pd.date_range(start, end, freq='min', inclusive='left', tz='UTC')
    return df.reindex(grid)


def encode(df: pd.DataFrame, station: str, source_modified: str | None) -> dict:
    """Pack a minute grid into the feed payload.

    Every field is derived from the data, never from the wall clock, so a poll
    that finds nothing new produces a byte-identical file and therefore no
    commit.  Staleness is judged from data_end, which is the honest measure: if
    the updater dies, data_end stops advancing and the page says so.  When the
    poller last ran is already recorded by git as the commit time.
    """
    valid = df[list(SERIES)].notna().any(axis=1)
    data_end = df.index[valid][-1] if valid.any() else None
    out = {
        'station': station, 'name': 'Hobart', 'source': SOURCE, 'units': UNITS,
        't0': df.index[0].strftime('%Y-%m-%dT%H:%M:%SZ'),
        'dt': 60, 'n': len(df), 'sentinel': SENTINEL,
        'data_end': data_end.strftime('%Y-%m-%dT%H:%M:%SZ') if data_end is not None else None,
        'source_modified': source_modified,
        'series': {},
    }
    for key, label in SERIES.items():
        o, s, q = quantise(df[key].to_numpy(dtype=float))
        out['series'][key] = {
            'label': label, 'o': o, 's': s,
            'n_valid': int((q != SENTINEL).sum()),
            'b': base64.b64encode(q.tobytes()).decode(),
        }
    return out


def write_analysis(out_dir: Path, h: pd.Series, k9: float,
                   wavelet_bin: int, rebuild_wavelet: bool) -> dict:
    """The K index, the Kp it is checked against, and the scalogram.

    Returns the fragments index.json needs. Everything here is best-effort: a
    GFZ outage or a wavelet failure must not cost the feed its actual data, so
    each part is attempted separately and what already exists is kept if the
    attempt fails.
    """
    extra: dict = {}

    # --- local K, and Kp to check it against ------------------------------
    try:
        extra.update(_write_k(out_dir, h, k9))
    except Exception as exc:                                   # noqa: BLE001
        # Nothing derived is worth the feed itself: this runs unattended every
        # half hour, and a page with three channels and no K beats a page that
        # stopped updating three days ago.
        print(f'  K unavailable ({exc}); channels written without it')
        if (out_dir / 'k.json').exists():
            extra['k'] = 'k.json'

    # --- scalogram --------------------------------------------------------
    meta_path = out_dir / 'wavelet.json'
    if not rebuild_wavelet and meta_path.exists():
        print('  scalogram: unchanged')
        extra['wavelet'] = json.loads(meta_path.read_text())
        return extra
    try:
        block = science.scalogram(h, PERIODS)
        with np.errstate(invalid='ignore'):
            limit = float(np.nanpercentile(np.abs(block), 99))
        # A window with nothing in it yields a NaN colour limit, and json.dumps
        # would write a bare NaN - which is not JSON, and which the page can
        # only report as a broken feed. Better to ship no panel.
        if not np.isfinite(limit):
            raise ValueError('no finite power in the scalogram')
        overview = science.bin_columns(block, wavelet_bin)
        png = science.palette_png(overview, limit)
        (out_dir / 'overview.png').write_bytes(png)
        meta = {
            'periods': [float(PERIODS[0]), float(PERIODS[-1])],
            'limit': limit,
            'rows': len(PERIODS),
            'overview': {'src': 'overview.png', 'cols': int(overview.shape[1]),
                         'bin': wavelet_bin},
            # No fine tiles. On the fluxgate page they are seven static files
            # committed once; here every rebuild would be a fresh 5 MB in the
            # history of a repo that already grows by a megabyte a day.
            'tiles': [],
        }
        meta_path.write_text(json.dumps(meta, separators=(',', ':'), allow_nan=False))
        extra['wavelet'] = meta
        print(f'  scalogram: {overview.shape[1]} columns at {wavelet_bin} min, '
              f'{len(png) / 1024:.0f} kB, +/-{limit:.1f} dB')
    except Exception as exc:                                   # noqa: BLE001
        print(f'  scalogram failed ({exc}); panel omitted')
        if meta_path.exists():
            extra['wavelet'] = json.loads(meta_path.read_text())
    return extra


def _write_k(out_dir: Path, h: pd.Series, k9: float) -> dict:
    dh = science.sq_removed(h)
    k = science.local_k(dh, k9=k9)
    payload = {
        'k9': k9,
        't': [t.strftime('%Y-%m-%dT%H:%M:%SZ') for t in k.index],
        'local': [None if not np.isfinite(v) else int(v) for v in k.K],
        'good': [bool(v) for v in k.good],
        'kp': [None] * len(k),
    }
    try:
        kp = science.fetch_kp(h.index[0], h.index[-1]).reindex(k.index)
        payload['kp'] = [None if not np.isfinite(v) else round(float(v), 3)
                         for v in kp]
        pair = pd.concat([k[k.good].K, kp.rename('Kp')], axis=1).dropna()
        if len(pair) > 10:
            print(f'  K vs Kp over {len(pair)} bins: '
                  f'rho = {pair.K.corr(pair.Kp, method="spearman"):.2f}, '
                  f'mean |K-Kp| = {(pair.K - pair.Kp).abs().mean():.2f} '
                  f'(K9 = {k9:.0f} nT)')
    except Exception as exc:                                   # noqa: BLE001
        # A missing Kp panel is a smaller loss than a missing feed.
        print(f'  Kp unavailable ({exc}); local K written without it')

    write_json(out_dir / 'k.json', payload)
    # A URL, not the arrays: index.json is rewritten every run and these are not.
    return {'k': 'k.json'}


def write_json(path: Path, payload: dict) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    # allow_nan=False on purpose: Python writes a bare NaN, which is not valid
    # JSON and which a browser can only report as a broken feed. Raising here
    # means the caller drops that part of the payload instead of shipping it.
    text = json.dumps(payload, separators=(',', ':'), allow_nan=False)
    # only rewrite when the content actually changes, so an unchanged poll
    # leaves the file (and therefore git) alone
    if path.exists() and path.read_text() == text:
        print(f'  {path.name}: unchanged')
        return 0
    path.write_text(text)
    # Channel payloads describe themselves by their grid; k.json and index.json
    # have no minutes to report.
    detail = (f', {payload["n"]} minutes from {payload["t0"]}'
              if 'n' in payload and 't0' in payload else '')
    print(f'  {path.name}: {len(text) / 1024:.1f} kB{detail}')
    return len(text)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--station', default='hbt')
    p.add_argument('--data', default='./WDCData', type=Path,
                   help='the --out directory used by wdc_fetch.py')
    p.add_argument('--out', default='./feed', type=Path)
    p.add_argument('--days', type=int, default=90, help='length of the rolling window')
    p.add_argument('--today-only', action='store_true',
                   help='skip the rolling window (the every-30-min case)')
    p.add_argument('--k9', type=float, default=science.K9_DEFAULT,
                   help='lower limit of K9 for the station, nT')
    p.add_argument('--wavelet-bin', type=int, default=OVERVIEW_BIN,
                   help='minutes per scalogram column')
    p.add_argument('--no-analysis', action='store_true',
                   help='channels only: no K, Kp or scalogram')
    args = p.parse_args()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    today = pd.Timestamp(now.date(), tz='UTC')
    window_start = today - pd.Timedelta(days=args.days)
    station_root = args.data / args.station
    out_dir = args.out / args.station

    print(f'today   {today:%Y-%m-%d}')
    day = load_minutes(args.data, args.station, today, today + pd.Timedelta(days=1))
    payload = encode(day, args.station, source_modified(station_root, today))
    written = write_json(out_dir / 'today.json', payload)

    recent = None
    recent_changed = False
    if not args.today_only:
        print(f'window  {window_start:%Y-%m-%d} to {today:%Y-%m-%d}')
        recent = load_minutes(args.data, args.station, window_start, today)
        size = write_json(out_dir / f'recent{args.days}.json',
                          encode(recent, args.station,
                                 source_modified(station_root, today - pd.Timedelta(days=1))))
        written += size
        recent_changed = size > 0

    # The analysis spans the whole window, so it needs both files' minutes on
    # one grid - the same splice the page does when it draws them.
    extra = {}
    if not args.no_analysis and recent is not None:
        print('analysis')
        h = pd.concat([recent['h'], day['h']])
        h = h[~h.index.duplicated()].sort_index()
        # The scalogram is the expensive part and only moves when the rolling
        # window does, so it rides on whether recent{days}.json actually
        # changed rather than being recomputed 48 times a day.
        extra = write_analysis(out_dir, h, args.k9, args.wavelet_bin,
                               rebuild_wavelet=recent_changed)

    write_json(out_dir / 'index.json', {
        'station': args.station, 'name': 'Hobart', 'source': SOURCE, 'units': UNITS,
        'window_days': args.days, 'dt': 60, 'n': 0, 'sentinel': SENTINEL,
        't0': window_start.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'data_end': payload['data_end'],
        'overview': 'h',
        'panels': [p for p in PANELS
                   if p['kind'] == 'line' or p['id'] in extra
                   or (p['kind'] == 'k' and 'k' in extra)],
        'files': {'recent': f'recent{args.days}.json', 'today': 'today.json'},
        'coverage': {'start': window_start.strftime('%Y-%m-%dT%H:%M:%SZ'),
                     'end': (today + pd.Timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')},
        **extra,
    })
    if payload['data_end']:
        age = (now - datetime.strptime(payload['data_end'], '%Y-%m-%dT%H:%M:%SZ')
               .replace(tzinfo=timezone.utc)).total_seconds() / 60
        print(f'data ends {payload["data_end"]} ({age:.0f} min old)')
    print(f'{written / 1024:.1f} kB written to {out_dir}')


if __name__ == '__main__':
    main()
