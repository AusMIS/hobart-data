#!/usr/bin/env python
"""The analysis the feed carries beyond the raw channels: a local K index, the
planetary Kp it is checked against, and a Morlet scalogram.

Ported from the AusMIS/final-code analysis of the home-built fluxgate, which is
where each of these was worked out and where the figures that justify them
live:

    local K       results_h.py, and build_timeseries_page.py's LOCAL K block
    Sq removal    decompose.py, ditto
    GFZ Kp        compare_kp.py
    scalogram     build_timeseries_page.py's WAVELET block, including the
                  palette-PNG encoding and its bit-exactness check

The code is deliberately a port rather than an import: this repo has to run
unattended in GitHub Actions and cannot depend on a checkout that is not there.
What is shared instead is the *contract* - the encoding below, and the manifest
the site's assets/js/explorer.js reads. verify_roundtrip() is the same assertion
build_timeseries_page.py makes, so an encoding change fails a build here too
rather than silently shifting one page's traces.

Two things differ from the fluxgate analysis, and both are deliberate:

  * The station constant is NOT fitted. Hobart is a real observatory with a
    published K9 lower limit, and fitting L against Kp - which is right for an
    uncalibrated home-built sensor - would quietly make K mean something
    different on the two pages. See K9_DEFAULT.

  * The scalogram reaches only to a 2-minute period, because this is 1-minute
    data. The fluxgate page resolves Pc3 (10-45 s) from its 2 s logger; nothing
    below Pc5 is visible here, and the period axis says so.
"""

from __future__ import annotations

import io

import numpy as np
import pandas as pd

SENTINEL = -32768

# Standard quasi-logarithmic K class boundaries as fractions of K9.
K_FRACTIONS = np.array([0, .01, .02, .04, .08, .14, .24, .40, .66, 1.00])

# The lower limit of K9 for Hobart, in nT: the 3-hour range at which K reaches
# 9. This is a published property of the observatory, not something to fit.
# Printed on every run beside the agreement with Kp, so a wrong value shows up
# as a poor correlation rather than passing silently.
K9_DEFAULT = 500.0

KP_URL = 'https://kp.gfz.de/app/json/'      # GFZ Potsdam, CC BY 4.0


# --------------------------------------------------------------------------
# encoding - the contract with assets/js/explorer.js
# --------------------------------------------------------------------------
def quantise(x: np.ndarray) -> tuple[float, float, np.ndarray]:
    """int16 with a scale/offset; NaN becomes the sentinel."""
    x = np.asarray(x, dtype=float)
    finite = np.isfinite(x)
    if not finite.any():
        return 0.0, 1.0, np.full(len(x), SENTINEL, dtype=np.int16)
    lo, hi = float(np.min(x[finite])), float(np.max(x[finite]))
    offset = (lo + hi) / 2
    scale = max((hi - lo) / 2, 1e-9) / 32000
    q = np.full(len(x), SENTINEL, dtype=np.int16)
    q[finite] = np.round((x[finite] - offset) / scale).astype(np.int16)
    return offset, scale, q


def verify_roundtrip(x: np.ndarray, offset: float, scale: float,
                     q: np.ndarray, name: str = 'series') -> None:
    """Assert the JavaScript decode inverts quantise().

    The encoder here and the decoder in the site's explorer.js were coupled by
    nothing but convention. Reproducing the JS arithmetic and asserting it
    inverts means a change to either side fails this build instead of shifting
    every trace on the page by an unnoticed amount.
    """
    x = np.asarray(x, dtype=float)
    decoded = np.where(q == SENTINEL, np.nan, q * scale + offset)
    assert np.array_equal(np.isnan(decoded), ~np.isfinite(x)), \
        f'{name}: gaps do not survive the round trip'
    finite = np.isfinite(x)
    if finite.any():
        worst = float(np.max(np.abs(decoded[finite] - x[finite])))
        assert worst <= scale, f'{name}: round trip out by {worst} > one step {scale}'


# --------------------------------------------------------------------------
# local K
# --------------------------------------------------------------------------
def sq_removed(h: pd.Series) -> pd.Series:
    """H with the regular daily (Sq) variation taken out.

    Each day is de-meaned first so slow drift cannot leak into the daily curve,
    and the curves are combined with a +/-7 day median so disturbed days do not
    set the shape. Straight from the fluxgate analysis; the only change is that
    NaN is expected here, because the observatory feed genuinely drops out.
    """
    day = h.index.normalize()
    mod = h.index.hour * 60 + h.index.minute
    demeaned = h - h.groupby(day).transform('mean')
    grid = demeaned.groupby([day, mod]).mean().unstack()
    sq = grid.rolling(15, center=True, min_periods=3).median()
    sq = sq.stack().reindex(pd.MultiIndex.from_arrays([day, mod])).to_numpy()
    return pd.Series(demeaned.to_numpy() - sq, index=h.index, name='dH')


def local_k(dh: pd.Series, k9: float = K9_DEFAULT,
            min_minutes: int = 150) -> pd.DataFrame:
    """The 3-hourly K index from the range of dH.

    A bin is only `good` if enough of its 180 minutes were actually observed:
    the range of a bin that is mostly gap is a lower bound, not a measurement,
    and drawing it as though it were one would understate real activity. The
    page greys those bars rather than hiding them.
    """
    grouped = dh.resample('3h')
    span = (grouped.max() - grouped.min()).rename('range')
    observed = grouped.count().rename('minutes')
    out = pd.concat([span, observed], axis=1)
    out['good'] = out.minutes >= min_minutes
    out['K'] = np.searchsorted(K_FRACTIONS * k9, out['range'].to_numpy(),
                               side='right') - 1
    out.loc[out['range'].isna(), 'K'] = np.nan
    return out


def fetch_kp(start, end, timeout: int = 30) -> pd.Series:
    """Planetary Kp from GFZ Potsdam over [start, end].

    Fetched here rather than in the browser: the page would be at the mercy of
    whatever CORS headers kp.gfz.de sends, and a reader with a blocked request
    would see an empty panel with no way to tell why. Requires attribution
    (CC BY 4.0), which the page carries.
    """
    import requests

    params = {'start': pd.Timestamp(start).strftime('%Y-%m-%dT%H:%M:%SZ'),
              'end': pd.Timestamp(end).strftime('%Y-%m-%dT%H:%M:%SZ'),
              'index': 'Kp'}
    r = requests.get(KP_URL, params=params, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    times = pd.to_datetime(payload['datetime'], utc=True)
    values = pd.to_numeric(pd.Series(payload['Kp']), errors='coerce')
    return pd.Series(values.to_numpy(), index=times, name='Kp')


# --------------------------------------------------------------------------
# scalogram
# --------------------------------------------------------------------------
def segments(valid: np.ndarray, max_fill: int = 10) -> list[tuple[int, int]]:
    """Continuous stretches of data, splitting on any gap longer than max_fill.

    A wavelet transform run straight across an outage rings on both sides of it
    and invents power that was never measured - the lesson wavelet_full.py
    records. Short gaps are interpolated instead, since splitting on every
    single missing minute would leave nothing long enough to transform.
    """
    runs, start = [], None
    gap = 0
    for i, ok in enumerate(valid):
        if ok:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap > max_fill:
                runs.append((start, i - gap + 1))
                start = None
    if start is not None:
        runs.append((start, len(valid)))
    return [(a, b) for a, b in runs if b - a > 0]


def scalogram(h: pd.Series, periods: np.ndarray, dt: float = 60.0,
              wavelet: str = 'cmor1.5-1.0', chunk: int = 16) -> np.ndarray:
    """Morlet wavelet power in H, as dB above the record's own quiet level.

    Returned on the same grid as `h`, with NaN wherever there was no data to
    transform. Scales are done in chunks because the full array is complex128
    and would otherwise be hundreds of megabytes.
    """
    import pywt

    values = h.to_numpy(dtype=float)
    valid = np.isfinite(values)
    power = np.full((len(periods), len(values)), np.nan)
    scales = pywt.frequency2scale(wavelet, dt / periods)

    for a, b in segments(valid):
        piece = pd.Series(values[a:b]).interpolate(limit_direction='both').to_numpy()
        if len(piece) < 16 or not np.isfinite(piece).all():
            continue
        piece = piece - piece.mean()
        for i in range(0, len(scales), chunk):
            coef, _ = pywt.cwt(piece, scales[i:i + chunk], wavelet,
                               sampling_period=dt)
            power[i:i + chunk, a:b] = np.abs(coef) ** 2
        power[:, a:b][:, ~valid[a:b]] = np.nan       # never claim filled minutes

    # Quiet reference per scale: the median of the lower half of the record, so
    # a stormy fortnight cannot raise the baseline it is being measured against.
    with np.errstate(invalid='ignore'):
        reference = np.nanmedian(np.where(power <= np.nanmedian(power, axis=1,
                                                               keepdims=True),
                                          power, np.nan), axis=1, keepdims=True)
    reference = np.where(np.isfinite(reference) & (reference > 0), reference, np.nan)
    with np.errstate(divide='ignore', invalid='ignore'):
        return 10 * np.log10(power / reference)


def palette_png(block: np.ndarray, limit: float) -> bytes:
    """A dB block as a palette PNG, flipped so row 0 is the LONGEST period.

    RdBu_r has exactly 256 entries, so writing the colormap INDEX and shipping
    the lookup table as the PNG palette is bit-exact rather than an
    approximation, and halves the bytes. Index 0 is reserved for "no data" here,
    unlike the fluxgate window which had no gaps at all to encode.
    """
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import colormaps
    from PIL import Image

    cmap = colormaps['RdBu_r']
    finite = np.isfinite(block)
    # Fill before scaling: casting NaN to uint8 is undefined, and the filled
    # cells are overwritten with the reserved index immediately below.
    scaled = np.clip((np.where(finite, block, 0.0) + limit) / (2 * limit), 0, 1)
    index = 1 + np.round(scaled * 254).astype(np.uint8)
    index[~finite] = 0

    table = (np.array([cmap(i / 254.0) for i in range(255)])[:, :3] * 255)
    table = np.round(table).astype(np.uint8)
    palette = np.vstack([np.zeros((1, 3), np.uint8), table]).flatten().tolist()

    image = Image.fromarray(np.ascontiguousarray(index[::-1]))
    image = image.convert('P')
    image.putpalette(palette + [0] * (768 - len(palette)))
    image.info['transparency'] = 0
    buffer = io.BytesIO()
    image.save(buffer, format='PNG', optimize=True, transparency=0)
    return buffer.getvalue()


def bin_columns(block: np.ndarray, width: int) -> np.ndarray:
    """Average groups of `width` columns, ignoring NaN, for the overview."""
    cols = (block.shape[1] // width) * width
    trimmed = block[:, :cols].reshape(block.shape[0], -1, width)
    with np.errstate(invalid='ignore'):
        # An all-NaN group is a real gap, not an error: nanmean warns, so the
        # mean of the mask is used to decide emptiness instead.
        counts = np.isfinite(trimmed).sum(axis=2)
        total = np.nansum(np.where(np.isfinite(trimmed), trimmed, 0.0), axis=2)
        return np.where(counts > 0, total / np.maximum(counts, 1), np.nan)
