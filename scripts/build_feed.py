#!/usr/bin/env python
"""Turn the minute parquet from wdc_fetch.py into a compact web feed.

Written for the live Hobart page on the AusMIS magnetometers site.  The feed is
split in two so that frequent updates stay cheap in git history:

    today.json      the current UT day, rewritten every time the updater runs
                    (~9 kB), because BOM keeps appending to it all day
    recent90.json   the days before today, rebuilt once per UT day (~1 MB)
    index.json      what exists and when it was last touched

Rewriting the 1 MB window every half hour would add ~50 MB a day to the data
repo forever; rewriting only the current day is ~400 kB a day.

    conda run -n pygmt17 python build_feed.py --data ./WDCData --out ./feed

Format.  Each series is a regular minute grid - no timestamps are shipped, the
reader derives them from t0 and dt - quantised to int16 with an offset and
scale, and base64'd.  That is the encoding the site's existing explorer already
uses (quantise() in AusMIS/final-code/build_timeseries_page.py), and it costs
about 2 bytes per sample instead of ~12 as JSON text.

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

SENTINEL = -32768
SERIES = {'h': 'H', 'd': 'D', 'z': 'Z'}
SOURCE = ('Bureau of Meteorology Space Weather Services, World Data Centre, '
          'Hobart observatory (station 85401)')
UNITS = 'nT, uncalibrated variometer variation (changes are meaningful, the level is not)'


def quantise(x: np.ndarray) -> tuple[float, float, np.ndarray]:
    """int16 with a scale/offset; NaN becomes the sentinel.

    Same encoding as quantise() in the site's build_timeseries_page.py, so both
    pages decode series the same way.
    """
    finite = np.isfinite(x)
    if not finite.any():
        return 0.0, 1.0, np.full(len(x), SENTINEL, dtype=np.int16)
    lo, hi = float(np.min(x[finite])), float(np.max(x[finite]))
    offset = (lo + hi) / 2
    scale = max((hi - lo) / 2, 1e-9) / 32000
    q = np.full(len(x), SENTINEL, dtype=np.int16)
    q[finite] = np.round((x[finite] - offset) / scale).astype(np.int16)
    return offset, scale, q


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


def write_json(path: Path, payload: dict) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, separators=(',', ':'))
    # only rewrite when the content actually changes, so an unchanged poll
    # leaves the file (and therefore git) alone
    if path.exists() and path.read_text() == text:
        print(f'  {path.name}: unchanged')
        return 0
    path.write_text(text)
    print(f'  {path.name}: {len(text) / 1024:.1f} kB, {payload["n"]} minutes '
          f'from {payload["t0"]}')
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

    if not args.today_only:
        print(f'window  {window_start:%Y-%m-%d} to {today:%Y-%m-%d}')
        recent = load_minutes(args.data, args.station, window_start, today)
        written += write_json(out_dir / f'recent{args.days}.json',
                              encode(recent, args.station,
                                     source_modified(station_root, today - pd.Timedelta(days=1))))

    write_json(out_dir / 'index.json', {
        'station': args.station, 'name': 'Hobart', 'source': SOURCE, 'units': UNITS,
        'window_days': args.days, 'dt': 60, 'n': 0, 'sentinel': SENTINEL,
        't0': window_start.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'data_end': payload['data_end'],
        'files': {'recent': f'recent{args.days}.json', 'today': 'today.json'},
        'coverage': {'start': window_start.strftime('%Y-%m-%dT%H:%M:%SZ'),
                     'end': (today + pd.Timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')},
    })
    if payload['data_end']:
        age = (now - datetime.strptime(payload['data_end'], '%Y-%m-%dT%H:%M:%SZ')
               .replace(tzinfo=timezone.utc)).total_seconds() / 60
        print(f'data ends {payload["data_end"]} ({age:.0f} min old)')
    print(f'{written / 1024:.1f} kB written to {out_dir}')


if __name__ == '__main__':
    main()
