# hobart-data

A rolling 90-day feed of geomagnetic measurements from the **Hobart magnetic
observatory**, refreshed every 30 minutes by a scheduled workflow and served as
static JSON for the AusMIS magnetometers site.

The site page that reads it:
<https://ausmis.github.io/magnetometers-site/hobart-explorer.html>

## Source and attribution

The measurements are produced by the **Bureau of Meteorology, Space Weather
Services**, and come from their World Data Centre archive:

<https://downloads.sws.bom.gov.au/wdc/wdc_mag/data/hbt/>

Space Weather Services is the source of this information, and their copyright in
it is acknowledged. It is presented here with permission under the terms
published at <https://www.sws.bom.gov.au/Copyright>, which allow the freely
available information on their site to be downloaded and redistributed provided
the source is identified and a link to their home page is carried.

**Responsibility for the repackaging in this repository is ours, not Space
Weather Services'.** The original daily files are the authoritative record; what
is served here is a reduced, quantised subset of them (see *Precision* below).
Anyone needing the real data should go to the archive above.

Note also what these numbers are: **uncalibrated variometer variations**.
Changes over minutes to days are meaningful; the absolute level is not, and the
baseline is re-set at instrument changes. They are not absolute field values.

## What is in here

```
feed/hbt/index.json      metadata: window, sample interval, data_end (~0.5 kB)
feed/hbt/recent90.json   the rolling window up to the end of yesterday (~1 MB)
feed/hbt/today.json      the current UT day only (~12 kB)
```

The split is deliberate. `today.json` is the only file that changes on a normal
30-minute cycle, so the repository grows by tens of kilobytes a day rather than
a megabyte per update.

The two files abut exactly: concatenate `recent90` then `today` and the minute
grid is continuous. `H`, `D` and `Z` are on a regular one-minute grid with no
timestamps shipped — the reader derives them from `t0` and `dt`. Each series is
int16 with an offset and scale, base64-encoded; `-32768` means no data for that
minute and must be treated as a gap, never interpolated across.

The full schema, including a browser decoder, is documented in
`scripts/build_feed.py`.

### Precision

Quantisation costs about 0.0015 nT, which is far below the instrument's own
resolution but is a real loss: this feed is for display, not for analysis.
Decoded values have been checked against the source parquet at a maximum error
of 0.0074 nT including rounding.

### Gaps

The source feed genuinely drops out — days that are short, or missing outright,
are normal and are shown as breaks. Days that failed quality control in the
fetcher (a stuck component, an implausible range) are written as gaps rather
than plotted.

## How it updates

`.github/workflows/update.yml` runs every 30 minutes:

1. restores the cached raw `.gz` files, so a finalised day costs no request;
2. runs `scripts/wdc_fetch.py`, which asks the Bureau's server only about the
   current and recent days, using `If-None-Match` / `If-Modified-Since` — the
   server answers `304` with an empty body when nothing has changed;
3. runs `scripts/build_feed.py`;
4. commits **only if the files actually changed**.

Nothing in the output is derived from the wall clock, so a cycle that finds no
new data produces byte-identical files and makes no commit. The Bureau writes
its daily file when new data arrives rather than on a fixed schedule, so quiet
periods of an hour or more are ordinary.

The two scripts are vendored copies from the `aurora` analysis repository, kept
here so the workflow has no external dependency.

## Using the feed elsewhere

The files are plain static JSON and `raw.githubusercontent.com` serves them with
permissive CORS, so any page can read them. If you do, please carry the Space
Weather Services attribution above — and consider fetching from the archive
directly instead, which will be more accurate and more complete.
