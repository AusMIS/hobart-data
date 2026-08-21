#!/usr/bin/env python
"""Download and convert BOM World Data Centre magnetometer data.

Replaces the brute-force loop in ObservatoryDataDownload.ipynb, which guessed
filenames: 6 possible data-type letters x 1130 numeric values per year, i.e.
6780 requests to retrieve about 365 real files.  The server publishes an Apache
directory index for each year, so one request per year tells us exactly which
files exist and the search disappears.  That also fixes three silent losses in
the old loop: np.arange(101, 1231) never asked for 31 December, the year list
stopped at 25 so 1998 and 2026 were never fetched, and nothing recorded which
instrument a day came from.

    conda run -n pygmt17 python wdc_fetch.py --station hbt \
        --start 2025-07-01 --end 2025-12-31 --out ./WDCData

Products written per station (see --minute-only / --full-rate-only):

    raw/<yy>/<name>.gz        verbatim server copy, so a parser fix never
                              means downloading everything again
    fullrate/<STN>_<year>.parquet   time (UTC), h, d, z at the native rate
    minute/<STN>_<year>_1min.parquet   per-minute mean/min/max plus a count
    manifest.json             provenance and QC, one record per day
    stn.log                   the station history file, fetched once

The minute product keeps min and max, not just the mean, so daily ranges and
3-hourly disturbance statistics stay correct despite the decimation - the same
quantities asp_daily_stats.py computes from the Alice Springs 1-minute data.

A note on the file headers, which vary by era:

    1998 'b'  85401 -42.88 147.35   hdz  1.000  0.0092 b (i2,x,i2,x,f5.2,...f8.2)
    2006 'e'  ...  h,d,z 1.000000000000 0.0099000, 0.0088000, 0.0091000 e (...f8.3)
    2026 'K'  ...  H,D,Z 5.000000000000 1.0000000, 1.0000000, 1.0000000 K (...f14.6)

Field count, component spelling and the number of trailing floats all change,
so the header is tokenised rather than read by position.  The float after the
components is the sample rate in samples per second (1 Hz in 1998, 4 Hz in
2016, 5 Hz now).  The trailing floats are NOT applied: in the b/e era they are
the instrument resolution in nT/count while the data are already in nT, so
multiplying by 0.0099 would shrink the values a hundredfold.  In later eras
they are unit scale factors, and the 2016 'S' headers carry -1.0 for Z.  All of
it is recorded in the manifest and left to the user to act on.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests

BASE = 'https://downloads.sws.bom.gov.au/wdc/wdc_mag/data'
COLUMNS = ['hour', 'minute', 'second', 'h', 'd', 'z']
STUCK_MAX_UNIQUE = 16       # a healthy day has thousands of distinct values
IMPLAUSIBLE_RANGE = 1000.0  # nT; mid-latitude H does not do this honestly

FULL_SCHEMA = pa.schema([('time', pa.timestamp('ns', tz='UTC')),
                         ('h', pa.float64()), ('d', pa.float64()),
                         ('z', pa.float64())])
MINUTE_SCHEMA = pa.schema(
    [('time', pa.timestamp('ns', tz='UTC'))]
    + [(f'{c}_{s}', pa.float64()) for c in 'hdz' for s in ('mean', 'min', 'max')]
    + [('n', pa.int32())])

HEADER_RE = re.compile(r"""
    ^\s*(?P<station_no>\d+)\s+
    (?P<lat>-?\d+(?:\.\d+)?)\s+
    (?P<lon>-?\d+(?:\.\d+)?)\s+
    (?P<components>[A-Za-z]+(?:\s*,\s*[A-Za-z]+)*)\s+
    (?P<rate>\d+(?:\.\d+)?)\s+
    (?P<scales>-?\d+(?:\.\d+)?(?:\s*,\s*-?\d+(?:\.\d+)?)*)\s+
    (?P<dtype>[A-Za-z])\s*
    \((?P<fmt>[^)]*)\)
""", re.VERBOSE)


# --------------------------------------------------------------------------
# server

def year_dir(year: int) -> str:
    return f'{year % 100:02d}'


def year_of(yy: str) -> int:
    return 1900 + int(yy) if int(yy) >= 90 else 2000 + int(yy)


def _size_to_bytes(text: str) -> int:
    m = re.match(r'([\d.]+)([KMG]?)', text.strip())
    if not m:
        return 0
    return int(float(m.group(1)) * {'': 1, 'K': 1024, 'M': 1024**2,
                                    'G': 1024**3}[m.group(2)])


def list_remote_days(session, station: str, year: int, cache_dir: Path,
                     refresh: bool = False) -> dict:
    """{date: {name, bytes, mtime}} for one year, from the directory index.

    The parsed index is cached, except for the current year which grows daily.
    """
    cache = cache_dir / f'{station}_{year}.json'
    this_year = datetime.now(timezone.utc).year
    if cache.exists() and not refresh and year != this_year:
        raw = json.loads(cache.read_text())
        return {Date.fromisoformat(k): v for k, v in raw.items()}

    url = f'{BASE}/{station}/raw/{year_dir(year)}/'
    resp = session.get(url, timeout=90)
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()

    name_re = re.compile(rf'href="(m[A-Za-z]\d{{6}}\.{re.escape(station)}\.gz)"')
    out = {}
    for row in resp.text.split('<tr'):
        found = name_re.search(row)
        if not found:
            continue
        name = found.group(1)
        cells = re.findall(r'align="right">\s*([^<]*?)\s*</td>', row)
        mtime = cells[0] if cells else ''
        size = _size_to_bytes(cells[1]) if len(cells) > 1 else 0
        yy, mmdd = name[2:4], name[4:8]
        try:
            day = Date(year_of(yy), int(mmdd[:2]), int(mmdd[2:]))
        except ValueError:
            print(f'  ! {name}: not a real date, skipped')
            continue
        if day in out:
            print(f'  ! {day} has more than one file, keeping {out[day]["name"]}')
            continue
        out[day] = {'name': name, 'bytes': size, 'mtime': mtime}

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({k.isoformat(): v for k, v in sorted(out.items())}))
    return out


def cached_ok(dest: Path) -> bool:
    """True if a plausible (non-truncated, gzip) copy is already on disk."""
    if not (dest.exists() and dest.stat().st_size > 1000):
        return False
    with dest.open('rb') as fh:
        return fh.read(2) == b'\x1f\x8b'            # gzip magic


def download_day(session, url: str, dest: Path, validators: dict | None = None,
                 revalidate: bool = False, retries: int = 4) -> str:
    """Fetch one daily file.  Returns 'new', 'updated', 'unchanged' or 'failed'.

    A finalised day never changes, so a good local copy is enough and no request
    is made at all.  The current day is a different matter: BOM rewrites it
    every half hour as data arrives, so a cached copy is stale by definition and
    `revalidate` asks the server whether it has moved on.  That is done with
    If-None-Match / If-Modified-Since, to which this server replies 304 with an
    empty body, making a frequent poll almost free.
    """
    have = cached_ok(dest)
    if have and not revalidate:
        return 'unchanged'

    headers = {}
    known = (validators or {}).get(dest.name, {}) if have else {}
    if known.get('etag'):
        headers['If-None-Match'] = known['etag']
    if known.get('last_modified'):
        headers['If-Modified-Since'] = known['last_modified']

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + '.part')

    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=120, headers=headers)
            if resp.status_code == 304:
                return 'unchanged'
            resp.raise_for_status()
            if resp.content[:2] != b'\x1f\x8b':
                # the old notebook's bare except hid exactly this case
                raise ValueError(f'not gzip ({len(resp.content)} bytes, '
                                 f'looks like an error page)')
            part.write_bytes(resp.content)
            part.rename(dest)
            if validators is not None:              # one key per thread, no lock
                validators[dest.name] = {
                    'etag': resp.headers.get('ETag'),
                    'last_modified': resp.headers.get('Last-Modified'),
                    'bytes': len(resp.content)}
            return 'updated' if have else 'new'
        except Exception as exc:                    # noqa: BLE001 - report and retry
            if attempt == retries - 1:
                print(f'  ! {url}: {exc}')
                return 'failed'
            time.sleep(2 ** attempt)
    return 'failed'


# --------------------------------------------------------------------------
# parsing

def parse_header(line: str) -> dict:
    """Station metadata from the first line of a daily file."""
    m = HEADER_RE.match(line)
    if not m:
        return {'raw': line.strip(), 'parsed': False, 'rate_hz': None,
                'dtype': None, 'scales': None, 'components': None}
    scales = [float(v) for v in m.group('scales').split(',')]
    return {
        'raw': line.strip(), 'parsed': True,
        'station_no': m.group('station_no'),
        'lat': float(m.group('lat')), 'lon': float(m.group('lon')),
        'components': re.sub(r'\s', '', m.group('components')),
        'rate_hz': float(m.group('rate')),
        'scales': scales,          # metadata only - deliberately not applied
        'dtype': m.group('dtype'),
        'fmt': m.group('fmt'),
    }


def parse_day(path: Path, day: Date):
    """Read one daily .gz into a DataFrame of UTC-stamped h, d, z."""
    blob = path.read_bytes()
    meta = {'file': path.name, 'bytes': len(blob),
            'sha256': hashlib.sha256(blob).hexdigest()}
    text = gzip.decompress(blob).decode('utf-8', errors='replace')
    lines = text.splitlines()
    if not lines:
        meta['error'] = 'empty file'
        return None, meta

    meta.update({k: v for k, v in parse_header(lines[0]).items() if k != 'raw'})
    meta['header'] = lines[0].strip()

    body = [ln for ln in lines[1:] if ln.strip()]
    df = pd.read_csv(io.StringIO('\n'.join(body)), sep=r'\s+', names=COLUMNS,
                     index_col=False, on_bad_lines='skip')
    meta['bad_lines'] = len(body) - len(df)
    df = df.dropna()
    if df.empty:
        meta['error'] = 'no usable data lines'
        return None, meta

    # the date lives only in the filename; the file itself has time of day only
    stamp = (pd.Timestamp(day, tz='UTC')
             + pd.to_timedelta(df['hour'].to_numpy(), unit='h')
             + pd.to_timedelta(df['minute'].to_numpy(), unit='m')
             + pd.to_timedelta(df['second'].to_numpy(), unit='s'))
    out = pd.DataFrame({'time': stamp, 'h': df['h'].to_numpy(),
                        'd': df['d'].to_numpy(), 'z': df['z'].to_numpy()})

    h = out['h'].to_numpy()
    lo, hi = np.percentile(h, [0.5, 99.5])
    n_unique = int(np.unique(h).size)
    meta.update({
        'n_samples': len(out),
        'first': out['time'].iloc[0].isoformat(),
        'last': out['time'].iloc[-1].isoformat(),
        'out_of_order': int((out['time'].diff() < pd.Timedelta(0)).sum()),
        'missing_minutes': int(1440 - out['time'].dt.floor('min').nunique()),
        'h_unique': n_unique,
        **{f'{c}_{s}': float(getattr(out[c], s)()) for c in 'hdz'
           for s in ('min', 'max')},
        # QC flags: record the failure, never drop the day silently
        'h_stuck': bool(n_unique <= STUCK_MAX_UNIQUE),
        'range_implausible': bool(hi - lo > IMPLAUSIBLE_RANGE),
    })
    return out, meta


def to_minute(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby(df['time'].dt.floor('min'))
    out = g.agg(h_mean=('h', 'mean'), h_min=('h', 'min'), h_max=('h', 'max'),
                d_mean=('d', 'mean'), d_min=('d', 'min'), d_max=('d', 'max'),
                z_mean=('z', 'mean'), z_min=('z', 'min'), z_max=('z', 'max'),
                n=('h', 'size'))
    out.index.name = 'time'
    out = out.reset_index()
    out['n'] = out['n'].astype('int32')
    return out


# --------------------------------------------------------------------------
# driver

def convert_year(station: str, year: int, days: dict, out_root: Path,
                 manifest: dict, args) -> int:
    """Parse every cached day of one year and write the parquet products.

    `days` is the whole year's listing, not just the requested range: the year
    parquet mirrors every daily file present in the raw cache, so fetching
    March after January does not silently drop January from the output.
    """
    stn = station.upper()
    full_path = out_root / 'fullrate' / f'{stn}_{year}.parquet'
    min_path = out_root / 'minute' / f'{stn}_{year}_1min.parquet'
    writer = None
    minutes, n_days = [], 0

    if not args.minute_only:
        full_path.parent.mkdir(parents=True, exist_ok=True)
    if not args.full_rate_only:
        min_path.parent.mkdir(parents=True, exist_ok=True)

    for day in sorted(days):
        gz = out_root / 'raw' / year_dir(year) / days[day]['name']
        if not gz.exists():
            continue
        df, meta = parse_day(gz, day)
        meta['url'] = f'{BASE}/{station}/raw/{year_dir(year)}/{days[day]["name"]}'
        meta['server_mtime'] = days[day].get('mtime', '')
        manifest[day.isoformat()] = meta
        if df is None:
            print(f'  ! {day}: {meta.get("error")}')
            continue

        if not args.minute_only:
            table = pa.Table.from_pandas(df, schema=FULL_SCHEMA,
                                         preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(full_path, FULL_SCHEMA,
                                          compression='zstd')
            writer.write_table(table)
        if not args.full_rate_only:
            minutes.append(to_minute(df))
        n_days += 1

        if args.no_keep_gz:
            gz.unlink()

    if writer is not None:
        writer.close()
    if minutes and not args.full_rate_only:
        mdf = pd.concat(minutes, ignore_index=True)
        pq.write_table(pa.Table.from_pandas(mdf, schema=MINUTE_SCHEMA,
                                            preserve_index=False),
                       min_path, compression='zstd')
    return n_days


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--station', default='hbt', help='WDC station code (hbt, dav, ...)')
    p.add_argument('--start', help='first day, YYYY-MM-DD (default: earliest available)')
    p.add_argument('--end', help='last day, YYYY-MM-DD (default: today)')
    p.add_argument('--out', default='./WDCData', type=Path)
    p.add_argument('--workers', type=int, default=6)
    p.add_argument('--list-only', action='store_true',
                   help='report what would be fetched and stop')
    p.add_argument('--refresh', action='store_true',
                   help='re-read directory listings and rebuild the parquet files')
    p.add_argument('--no-keep-gz', action='store_true',
                   help='delete each raw file once it has been converted')
    p.add_argument('--revalidate-days', type=int, default=2, metavar='N',
                   help='re-ask the server about the last N days, which BOM is '
                        'still rewriting (default 2; 0 trusts every cached file)')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--minute-only', action='store_true')
    g.add_argument('--full-rate-only', action='store_true')
    args = p.parse_args()

    station = args.station.lower()
    out_root = args.out / station
    start = Date.fromisoformat(args.start) if args.start else Date(1900, 1, 1)
    end = Date.fromisoformat(args.end) if args.end else Date.today()
    if start > end:
        p.error('--start is after --end')

    session = requests.Session()
    session.headers['User-Agent'] = 'wdc_fetch.py (research use)'
    listings = out_root / 'listings'

    years = range(max(start.year, 1990), end.year + 1)
    wanted, all_days = {}, {}
    for year in years:
        days = list_remote_days(session, station, year, listings, args.refresh)
        keep = {d: v for d, v in days.items() if start <= d <= end}
        if keep:
            wanted[year] = keep
            all_days[year] = days

    if not wanted:
        print(f'no data for {station} between {start} and {end}')
        print(f'(is {BASE}/{station}/raw/ a real path?)')
        return 1

    total = sum(len(v) for v in wanted.values())
    n_bytes = sum(d['bytes'] for v in wanted.values() for d in v.values())
    print(f'{station}: {total} daily files, {n_bytes / 1024**3:.2f} GB (listed sizes), '
          f'{min(min(v) for v in wanted.values())} to '
          f'{max(max(v) for v in wanted.values())}')
    for year, days in wanted.items():
        letters = sorted({d['name'][1] for d in days.values()})
        print(f'  {year}: {len(days):3d} files, type {"".join(letters)}, '
              f'{min(days)} to {max(days)}')
    if args.list_only:
        return 0

    # station history: small, and it documents the instrument changes behind
    # the baseline jumps in the data
    log = out_root / 'stn.log'
    if not log.exists():
        try:
            r = session.get(f'{BASE}/{station}/stn.log', timeout=60)
            if r.ok:
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_bytes(r.content)
        except Exception as exc:                    # noqa: BLE001
            print(f'  ! stn.log: {exc}')

    manifest_path = out_root / 'manifest.json'
    manifest = {}
    if manifest_path.exists() and not args.refresh:
        manifest = json.loads(manifest_path.read_text()).get('days', {})

    # HTTP validators live apart from the manifest on purpose: the manifest
    # records what the data is, this records what the server last told us.
    http_cache_path = out_root / 'http_cache.json'
    validators = (json.loads(http_cache_path.read_text())
                  if http_cache_path.exists() else {})
    # UT, because that is what the day files are keyed on
    fresh_from = (datetime.now(timezone.utc).date()
                  - timedelta(days=args.revalidate_days))
    # A day captured while it was still the current day stays truncated in the
    # cache forever, because we stop revalidating once it is old. Re-ask about
    # any cached day that looks badly incomplete; if it really is incomplete at
    # the source the server just answers 304 and it costs nothing.
    incomplete = {d for d, m in manifest.items()
                  if (m.get('missing_minutes') or 0) > 30}

    for year, days in wanted.items():
        year_raw = out_root / 'raw' / year_dir(year)
        jobs = [(f'{BASE}/{station}/raw/{year_dir(year)}/{d["name"]}',
                 year_raw / d['name'],
                 day >= fresh_from or day.isoformat() in incomplete)
                for day, d in days.items()]
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            status = list(pool.map(
                lambda j: download_day(session, j[0], j[1], validators, j[2]), jobs))
        tally = {k: status.count(k) for k in
                 ('new', 'updated', 'unchanged', 'failed') if status.count(k)}
        print(f'{year}: ' + ', '.join(f'{v} {k}' for k, v in tally.items())
              + f' ({time.time() - t0:.0f}s)', flush=True)
        http_cache_path.parent.mkdir(parents=True, exist_ok=True)
        http_cache_path.write_text(json.dumps(validators, indent=1, sort_keys=True))

        # rebuild from every cached day of the year, not just this run's range
        n = convert_year(station, year, all_days[year], out_root, manifest, args)
        print(f'{year}: converted {n} days', flush=True)
        if args.no_keep_gz:
            print(f'  ! --no-keep-gz: {year} products cover only the days fetched '
                  'this run; a wider range later means downloading again')

        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(
            {'station': station, 'base_url': f'{BASE}/{station}/raw/',
             'updated': datetime.now(timezone.utc).isoformat(),
             'days': dict(sorted(manifest.items()))}, indent=1))

    flagged = [d for d, m in manifest.items() if m.get('h_stuck')
               or m.get('range_implausible')]
    if flagged:
        print(f'\n{len(flagged)} days flagged in the manifest (kept, not dropped): '
              f'{flagged[0]} ... {flagged[-1]}')
    print(f'manifest: {manifest_path}')

    # A day the listing offers but that is not in the raw cache is a hole in the
    # record.  The next run over the same range picks it up on its own, because
    # download_day fetches anything it has no good local copy of; the danger is
    # not that it stays unfixed but that nobody notices.  2026-08-18 failed once
    # and sat missing for days behind a run that otherwise looked healthy, so an
    # unattended run must not report success while the record has a gap.
    missing = []
    for year, days in wanted.items():
        year_raw = out_root / 'raw' / year_dir(year)
        missing += [(day, days[day]['name']) for day in sorted(days)
                    if not cached_ok(year_raw / days[day]['name'])]
    if missing:
        print(f'\n! {len(missing)} listed day(s) still missing after this run:')
        for day, name in missing[:10]:
            print(f'    {day}  {name}')
        if len(missing) > 10:
            print(f'    ... and {len(missing) - 10} more')
        print('  Re-run the same command to retry them.')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
