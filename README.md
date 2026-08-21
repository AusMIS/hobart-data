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
feed/hbt/index.json      the manifest: window, sample interval, data_end, and
                         the panels the page draws (~1 kB)
feed/hbt/recent90.json   the rolling window up to the end of yesterday (~1 MB)
feed/hbt/today.json      the current UT day only (~12 kB)
feed/hbt/k.json          3-hourly local K and planetary Kp (~25 kB)
feed/hbt/overview.png    Morlet scalogram of H (~500 kB)
feed/hbt/wavelet.json    what the scalogram covers
```

The split is deliberate, and it is about git history rather than download size.
`today.json` is the only file that changes on a normal 30-minute cycle, so the
repository grows by tens of kilobytes a day rather than a megabyte per update.
`k.json` moves once every three hours, and the scalogram only when the rolling
window does — once a UT day.

**The manifest decides what the page draws.** `index.json` carries a `panels`
list, and the site's explorer renders exactly that and nothing else — it does
not know what an observatory measures. Adding a panel is a change here, not a
change to the website.

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

### The derived indices

`k.json` carries a **local K index** — the standard quasi-logarithmic classes
applied to the 3-hour range of H with the regular daily (Sq) variation removed —
alongside the **planetary Kp** from [GFZ Potsdam](https://kp.gfz.de/), which is
entirely independent of this observatory and so is a fair check on it. Kp is
licensed CC BY 4.0 and the page carries the attribution.

The station constant is Hobart's **published K9 lower limit of 500 nT**, not a
value fitted to make the two indices agree. That distinction matters: the
home-built fluxgate on the same site *does* fit its constant, because it is
uncalibrated and has no published one, and using a fitted value here would
quietly make K mean two different things on two pages that invite comparison.
The choice is checkable — every run prints the agreement, currently
ρ = 0.72 and mean |K − Kp| = 0.64 over 719 bins, and 500 nT is also the best of
the candidates tried, so the published value and the data agree.

A bin whose 180 minutes are mostly missing is marked `good: false` and drawn
greyed: the range of a mostly-absent bin is a lower bound, not a measurement.

`overview.png` is a Morlet scalogram of H, in dB above the record's own quiet
level, encoded as a palette PNG — RdBu_r has exactly 256 entries, so shipping
the colormap index with the table as the PNG palette is bit-exact rather than an
approximation. It reaches down only to a **two-minute period**, because this is
1-minute data: Pc3 and most of Pc4 are simply not in this record. Unlike the
fluxgate page there are no fine tiles, since every rebuild of those would add
several megabytes to this repository's history for good.

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

The scalogram is the expensive step, about 100 seconds, so it is computed only
when the rolling window actually moves rather than on all 48 daily cycles.

`scripts/wdc_fetch.py` and `scripts/build_feed.py` are vendored copies from the
`aurora` analysis repository, and `scripts/science.py` is a port of the analysis
worked out for the home-built fluxgate in `AusMIS/final-code` — kept here rather
than imported so the workflow has no external dependency. What is shared with
that repository is the *contract*, not the code: the int16 encoding, and the
manifest the site's `assets/js/explorer.js` reads. Both sides assert that the
browser's decode inverts the encoding exactly, so a change to either fails a
build instead of silently shifting a page's traces.

## Using the feed elsewhere

The files are plain static JSON and `raw.githubusercontent.com` serves them with
permissive CORS, so any page can read them. If you do, please carry the Space
Weather Services attribution above — and consider fetching from the archive
directly instead, which will be more accurate and more complete.
