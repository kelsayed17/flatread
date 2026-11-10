# flatread v1: a declared layout per table, read on one native path per format

Status: draft for approval (2026-11-09). Supersedes flatread 0.4 in full: no shims, a migration list at the end.

## Why

prism-clients began dropping flatread 0.4 this week (ten packages so far; about fifteen still import it) because it was **slow** (PMMS: 5.8 ms against plain polars' 1.6 ms) and
carried **too much machinery**: it tried to answer every provider's quirk inside the library (`strip_start`,
`parens_negative`, quote-inside-a-field detection, one-column blank-line rules, …), so every read paid for all of it.

The plain-polars rewrite then re-grew the same machinery six times. Across the six ported `decoders.py` (9,381 lines)
roughly 46% is copied machinery and about 22% is genuinely provider-specific: five copies of the strict typer, five of the
date-format struct, five of the workbook load, three of the final-line-ending check, six wide-to-long reads, four ways
to wire the ROWS/FRAME constants, four ways to pin errata. And it is still slow where it matters: every CSV goes
`csv` module → Python lists → `pl.DataFrame(orient="row")`; every sheet is loaded twice plus a regex per column; some
readers reopen a workbook per sheet.

The user's goals for v1, in their words: "eliminating duplication, maximizing speed, and providing utility and
configuration".

## Goals

1. **One declaration per table.** A table is a Python class: its labels → fields, types come from the row struct, and
   only what the provider does differently is written down. The reader is derived from it.
2. **Configurable per provider by inheritance.** flatread's defaults → one provider class per package → one class per
   table, each overriding only what differs (the user: "every provider's read settings are configurable"; "a regular
   class / OOP").
3. **Fast: one native path per format.** No per-cell Python, no double loads on the common path. A v1 reader is never
   slower than the hand-written reader it replaces on the same recorded body, and the benchmark proves it per format.
4. **Strict, loudly.** Every cell is its value, one of the column's missing tokens, or an error naming the line, the
   column and the text. No silent nulls, no padding.
5. **A few utilities** that every client needs: a full validation report, a diff of a re-recording, and a scaffold
   that writes a row struct and a layout from a sample body.

## Non-goals

- **Paging.** It stays where it is (see "Paging" below): headgate walks, prism-clients' `pagination.py` names the
  cursor. flatread reads one page.
- **Streaming reads.** The approved standing rule is one buffered get per method (EIA's 256 MiB cap, Z1's buffered
  get). `stream_csv`, `stream_zip` and `stream_unzip` go.
- **Formats only one or two providers need.** PDF text (Congress only), SDMX-ML and other XML (Federal Reserve and
  ECB; Congress and SEC each write their own shape), HTML tables, the OCC print reports and fixed column-block
  surveys stay plain polars in their client, plugged in through the hooks below.
- **Schemas from outside metadata** (Treasury's metadata, the FINRA catalogue, the Unusual Whales spec, SDMX
  `struct.xml`). Each client already plans its own generator script; flatread's scaffold only reads a sample body.
- Anything about how prism-server or any downstream consumer queries the tables.

## Anti-goals (a failure even if it works)

- **A new option for one provider's quirk.** The rule for what enters flatread: **a pattern three or more providers
  share** (the user: "for now, yes"). A quirk one provider has is a method that provider's class overrides, in plain
  polars.
- A read that pays for features its layout does not use.
- A silent coercion: a padded short row, a NaN read as a number, a rounded digit, an erratum that outlives the error.

## The shape of it

```python
import flatread


class FreddieMac(flatread.Csv):  # the provider's defaults, once per package
    final_line_ending = True
    missing = {"", "."}  # an override replaces the parent's set whole


class Fmhpi(FreddieMac):  # one table: only what differs
    row = HpiRow  # a plain msgspec struct, snake_case fields, no annotations
    labels = {
        "Year": "year",
        "Month": "month",
        "GEO_Type": "geo_type",
        "GEO_Name": "geo_name",
        "GEO_Code": "geo_code",
        "Index_NSA": "index_nsa",
        "Index_SA": "index_sa",
    }

    @classmethod
    def check(cls, frame: pl.DataFrame) -> None:  # the one Freddie-only rule, plain polars
        ...


class FmhpiStates(Fmhpi):
    where = pl.col("geo_type") == "State"  # one body, several tables: a filter


# decoders.py, the constants client.py's methods pass as expect=
FMHPI_STATES_FRAME: Final = body_reader(FmhpiStates.frame)
FMHPI_STATES_ROWS: Final = body_reader(FmhpiStates.rows)
```

`client.py` is unchanged: `return await self.get(_PATH, expect=FMHPI_STATES_FRAME, ...)`.

### Layout classes

A **layout** is a subclass of one of four format bases. Settings are class attributes; building the class
(`__init_subclass__`) checks them once (an unknown setting, a label naming no field, a field no label names, a type
flatread cannot read, a regex polars cannot compile) and compiles the read plan, so a read does no set-up work.
`Layout.frame(body) -> pl.DataFrame` and `Layout.rows(body) -> list[Row]` are classmethods, so they pass straight to
`body_reader`. An intermediate class with no `row` is a provider's defaults and is never read.

| Base | Reads | Fast path | Fallback |
|---|---|---|---|
| `Csv` (and `Pipe`, `Tsv`: `Csv` with `separator` set) | delimited text | one `pl.read_csv` of the whole body, every cell text, after appending a sentinel field to every line (below) | stdlib `csv` for a body where a quoted field spans lines or a blank line sits between rows |
| `Sheet` | one sheet of an xls/xlsx | one fastexcel load of the table's rows, each column's type inferred from every cell with `dtype_coercion="strict"` (explicit dtypes are not used: fastexcel turns a text cell in a float column into a silent null; found while building, 2026-11-09) | when the strict load refuses (a column mixes kinds, such as `.` among numbers), one text load plus one typed (`coerce`) load of only the number and date fields' columns: today's cost, never more |
| `Json` | records at a path in a JSON body | `msgspec.json.decode` into a struct flatread derives from `row` and `labels` (`msgspec.defstruct(..., rename=…)`), then columns | text columns through the shared typer, for a provider that writes numbers as strings (Treasury) |
| `FixedWidth` | fixed-column text | one split into lines, `str.slice` per column in polars | — |

A body can be **inside a zip or gzip**: `member = "x.csv"` (or a regex, or a callable for a dated name) on any base
reads that member. Zip member selection is shared by Philadelphia Fed, Treasury TIC,
CFTC, FINRA and SEC, so it is one setting, not a base.

#### Why the CSV fast path is a sentinel

polars pads a short row with nulls and has no switch to refuse it (checked on polars 2.1: `missing_columns` and
`extra_columns` concern the schema, not rows), which is why every hand-written reader splits with the `csv` module.
flatread instead appends `separator + "\x01"` to every line with one `bytes.replace` (C speed) and reads with one
`pl.read_csv(infer_schema=False)`. A row of the right width ends in a sentinel column holding `"\x01"` on every row; a
short row puts it in an earlier column and leaves the last null; a long row makes polars raise. Measured on Freddie
Mac's `fmhpi_master_file.csv` (16.5 MB, 272,619 rows), read and typed: **23 ms, against the hand-written reader's
395 ms** (a throwaway spike, 2026-11-09). The fast path applies only when all of these hold, each checked natively
on the bytes:

- every line is a record: no quote character in the body, or every line holds an even number of quotes;
- line endings are uniform: no `\r` at all, or every line ends `\r\n` (then the sentinel goes before `\r\n`);
- no byte `\x01` in the body (it could fake a sentinel);
- blank lines, which are framing and skipped (Freddie, Chicago and SF Fed all skip them), are removed first with a
  native filter.

A last line without its ending gets its sentinel too (`final_line_ending = False`, IBKR). Any other body takes the
stdlib `csv` fallback, which is correct and as fast as today's readers. **Line numbers are not tracked on the fast
path**: when a read fails, flatread re-reads the body on the fallback, which counts lines, to name the line in the
error. The success path pays nothing for error messages.

### Settings

Every setting has a library default, a provider class may override it, and a table class may override it again.
Settings that are not on the list below do not exist. A provider quirk outside them is a hook.

**Shared by every base**

| Setting | Default | Meaning |
|---|---|---|
| `row` | — | The msgspec struct a row is. Its fields, in order, are the frame's columns, snake_case. Types: `str`, `int`, `float`, `Decimal`, `bool` (text `true`/`false` in any case; any other tokens are a `cells` hook), `date`, `datetime`, `Literal[...]`, any of them `\| None`. A date with no `dates` entry reads ISO 8601. |
| `labels` | — | The provider's label → field, in the file's column order. Labels live only here, never on the struct. A label mapped to `None` is checked present and then dropped (a notes or blank column: SF Fed, Chicago, Atlanta). A file that writes one label twice (Nasdaq's and NYSE's two `Filler` columns) gives `labels` as a sequence of `(label, field)` pairs, matched in order. A label that changed over the file's history is a `variants` entry. |
| `strip` | `False` | Take whitespace off both ends of every text cell before anything else; a cell of only whitespace is then `""` (SF Fed, Atlanta, Chicago's release JSON). Off by default: strict means as written. |
| `variants` | `()` | Other whole label maps tried in order when the header is not `labels`'s: a header that changed on a date (Nasdaq's `Filler`/`Rule 3210`, CFTC's 2004 rename, SEC notes from Dec-2020). The first exact match wins; none matching raises `UnknownLayoutError` listing what was found. |
| `missing` | `{""}` | Texts that stand for no value, in every column. A field that is not `\| None` refuses all of them. |
| `missing_by_field` | `{}` | Extra tokens for one field (`{"rate": {"NA"}, "margin": {"Default"}}`, IBKR). |
| `dates` | `{}` | field → `strftime` format, or a tuple of formats. Checked by writing the value back (polars' `%Y` alone reads `12/31/25` as year 25), so no separate shape regex. Built-in named formats for the shapes three or more providers write: `flatread.QUARTER` (`2024Q1`, `2024:Q1`), `flatread.EXCEL_SERIAL`, `flatread.YEAR_MONTH`. |
| `nonfinite` | `False` | Whether a float column admits NaN and infinities. |
| `long` | `None` | `flatread.Long(index=[…], name="series", value="value", names=r"…")`: the columns not in `index` are melted. Their header labels become the `name` column's values, typed by `row`'s `name` field (a date for dated columns), and each must match the `names` regex whole, so a renamed column raises instead of becoming a new series (SF Fed, Philadelphia, Chicago). A melted cell that is missing yields no row, as every hand-written long read does. |
| `headings` | `None` | For `long`: a row above the header whose cells carry rightwards as a group key (three providers write these). |
| `where` | `None` | A polars expression the read frame is filtered by: one body, several tables (FMHPI by geography). Each table's method reads the whole body (FMHPI: 23 ms each); no cache. |
| `carry` | `{}` | Values read off the body outside the table (a stamp, a trailer, an envelope field) → a column on every row, as `trailer`/`preamble`/`envelope` capture them. |
| `errata` | `()` | `flatread.Erratum(label="…", wrote="…", means="…", why="…", where={label: text, …})`: a known provider mistake, fixed on the text cell before typing. `where` picks the row by other cells' text (its key, as written); without it every cell of `label` that reads `wrote` is fixed. **Raises `ErratumGoneError` if no cell matches**, so a fix never outlives the mistake (SF Fed's model). |
| `refusals` | `{}` | Body text → error: a provider's "no file" answer inside a 2xx (Nasdaq's "Page Not Available", OCC's "File requested does not exist.", a zero-byte body) raises `NotPublishedError` (or another named error) before any parse. |
| `min_rows` | `1` | Fewer rows raises: a header and nothing is not a file. `0` where an empty day is real (NYSE threshold). |
| `key` | `()` | The fields that identify a row; checked unique when set, and used by `flatread.diff`. |

**`Csv`**: `separator` (`,`), `quoted` (`True`), `header` (`True`), `skip_lines` (`0`) or `header_line` (a regex the
header line matches, for a preamble whose length varies: Cboe put/call, Nasdaq phlx, NOAA, OCC),
`trailing_separator` (`False`), `final_line_ending` (`True`), `encoding` (`"utf-8"`, BOM dropped), `trailer` (below).

**`Sheet`**: `sheet` (a name, a regex, or a callable for a sheet named by vintage), `header_row` (`0`), `columns` (a
range, e.g. `"A:G"`), `end` (where the table stops: `"blank_row"`, the default, the first row empty in every table
column, after which only `footer` rows may follow, or `"last_row"`), `footer` (a regex the first cell of a row after
the table must match, for notes, disclaimers and summary rows: Atlanta, SF Fed, Freddie; any other filled row after
the end raises), and `every_sheet = flatread.Sheets(name="vintage")`: read every matching sheet into one long table
with the sheet's name as a column (NY Fed MCT/HLW/LW, Philadelphia RTDSM, Chicago Fed vintages). A body is sniffed by
its signature, not its URL, so an xls served as `.xlsx` reads (NY Fed GSCPI). How a sheet cell meets its field:

- an `int` field over number cells: loaded as float, refused unless every value is whole, then cast;
- a `str` field over number cells: loaded as text; calamine writes a whole number as `1001`, and a decimal is
  re-read from the float load so no digit is lost;
- a `Decimal` field: the float load's shortest round-trip text, then the Decimal rule in the core;
- a `date`/`datetime` field over date cells: loaded natively; over text cells, typed by `dates`; a column of numbers
  styled as dates (Atlanta's serials) declares `dates = {field: flatread.EXCEL_SERIAL}`.

**`Json`**: `records` (a path: `"data"`, `"results[].bars[]"`; `[]` walks a list), `envelope` (paths to scalars
copied onto every row, via `carry`), `next` (the path of the page cursor, read by
`Layout.page(body) -> (frame, cursor | None)`, see "Paging").

Cut by the three-provider rule (each stays a hook in its one or two clients until a third needs it): thousands
separators and percent signs (Farside, one Chicago reader), symbol-keyed maps and parallel arrays in JSON (Alpaca,
prism-client), chart-sheet skipping (NY Fed), CSV footers (Nasdaq phlx), gzip (no client named).

**`FixedWidth`**: `columns` (field → `(start, end)`, 1-based inclusive as the provider's spec writes it), `record`
(a regex the records match; others raise unless `skip` matches them).

**Trailers and stamps** (`trailer` / `preamble`): `flatread.Line(pattern, carry=…, count=…)`. The pattern is a regex
with named groups. `carry` turns groups into columns (`generated_at`), `count` names the group that must equal the row
count. Covers Nasdaq's `File Creation Time:` line, IBKR's `#BOF`/`#EOF|count`, the bare 14-digit stamp (Nasdaq
threshold, Cboe, NYSE) and FINRA's count trailer. A missing trailer raises `TruncatedFileError`.

### Hooks: the escape hatch for one provider's quirk

Three classmethods a layout may override (the readers are classmethods, so the hooks are too), each plain polars,
each run only when overridden:

- `cells(cls, text: pl.DataFrame) -> pl.DataFrame`: the frame of text cells under the provider's labels, before
  typing. For a fix too odd for `errata` (Freddie's comma regex, a three-column FLEX expiration built into one).
- `check(cls, frame: pl.DataFrame) -> None`: the typed frame, before `where`. For a rule one provider has (NOAA's
  Total equals the row sum; Freddie's GEO_Type/GEO_Code rule; IBKR's per-family line ending). Raises
  `flatread.CheckError`.
- `read_text(cls, body: bytes) -> pl.DataFrame` (classmethod, rarely): replace the format front-end entirely and
  return text cells under labels; everything after it (missing, typing, long, errata, checks, rows) still applies. For
  OCC's side-by-side blocks or a PDF table a client parses itself.

A layout that needs none of the settings and none of the hooks is just `row` + `labels`.

### The shared core (what every base hands to)

Each base produces a **text frame**: one `String` column per label, plus a hidden line (or sheet-row, or record)
number. Everything after that is one code path, shared by all bases:

1. header → labels (or variants) checked, renamed to fields;
2. errata applied, each checked present;
3. `cells` hook;
4. missing tokens → null, in one `pl.when` per column;
5. typing: one expression per column (`str.to_integer`, a strict cast, `str.to_date` + write-back), all columns in
   one `select`. `Decimal` never lets polars guess a scale from sampled rows (which would round a later, longer
   cell): the column's largest scale is found in one native pass, the cast is to `pl.Decimal(38, scale)`, and a cell
   that does not write back to its own digits is refused. **Errors are found by one `any_horizontal` over "read null but text
   was not"**; only when it is true does flatread find the first bad cell and build the message (line, label, text).
   The success path never builds a message;
6. `long` melt; `check` hook; `key` uniqueness; `where`; `min_rows`;
7. `carry` columns added.

`rows()` builds structs by zipping columns (`map(Row, *columns)`), measured at 46 ms against `iter_rows`' 78 ms for
FMHPI's 272k rows.

A typed column that is already native (a sheet's float column, a JSON number) skips step 5's cast; it is still
checked for NaN/inf unless `nonfinite`.

### Errors

Every error is a `flatread.ReadError(ValueError)`, so headgate's `body_reader` already raises it as
`ResponseParseError` on a 2xx. The subclasses that clients tell apart stay: `EmptyBodyError`, `UnknownLayoutError`
(header, variant, record), `RaggedRowError`, `TruncatedFileError` (final line ending, trailer), `CellError` (with
`.line`, `.label`, `.text`), `ControlTotalError` (trailer count), `CheckError` (a hook), `ErratumGoneError`,
`NotPublishedError`, `CorruptArchiveError`, `EncodingError`. `or_not_published` goes; a client that wants a marker
catches `NotPublishedError` itself, or headgate's `refusal_check` maps it.

## Paging

Decision: **paging is not flatread's.** headgate already walks (`Client.paginate`, `Cursor(..., stop_on_empty=False)`
raises on a repeating cursor), and Alpaca's unpushed `prism.clients._walk` is 50 lines of glue between headgate and a
table (`Page`, `cursor(param)`, `walk_rows`, `walk_frame`). That glue stays in prism-clients as the standing rule's
`pagination.py`; if a second repo ever needs it, it belongs in headgate, which owns the walk. flatread's part is one
page: `Json.page(body) -> (pl.DataFrame, str | None)` reads the records and the cursor at `next`, so a provider's
`Page` is built from one call. The `rows`/`frame` of the whole walk is `pl.concat`, already in `_walk`.

## Utilities

- **`flatread.report(Layout, body) -> Report`**: the whole body checked, every fault counted rather than the first
  raised: per column, the count of bad cells and the first three (line, text); ragged lines; header drift against
  `labels` and `variants`; errata present or gone. For a re-recording or a new provider; `str(report)` is readable,
  `report.ok` is a bool. No second mode in the core: the typing step already builds a bad-cell mask per column, and
  `report` reads those masks and the header and errata checks; nothing else.
- **`flatread.diff(Layout, old, new) -> Diff`**: two bodies read by one layout and compared by `key`: rows added,
  removed and changed (field by field), and header changes. For reviewing a re-recorded cassette before committing it.
- **`python -m flatread scaffold BODY --format csv|pipe|tsv|sheet|json|fixed [--sheet …] [--records …]`**: prints a
  row struct and a layout class from a sample: snake_case field names from the labels, the narrowest type every cell
  reads as (int, float, date in a detected format, str), `| None` where a cell is missing, and the tokens seen as
  missing. A starting point to edit, never a generated file kept in sync.

## What a prism-clients `decoders.py` becomes

Today `freddie_mac/decoders.py` is 542 lines: `_DateFormat`, eight `_Table`s, `_optional`, `_base`, `_check_ending`,
`_cells`, `_typed`, `_read_table`, `_decode_table`, the FMHPI reader and 26 constants built from partials. On v1:

```python
class FreddieMac(flatread.Csv):
    final_line_ending = True


class PmmsHistory(FreddieMac):
    row = PmmsRow
    labels = {name: name for name in PmmsRow.__struct_fields__}  # the file's labels are already snake_case
    dates = {"date": "%m/%d/%Y"}
    missing = {"", " "}  # pmms30p holds a single space for 32 weeks of 1971


class Fmhpi(FreddieMac):
    row = HpiRow
    labels = {...}
    missing = {"", "."}

    def check(self, frame): ...  # GEO_Type in US/State/CBSA; a CBSA row has a code, others none; month 1..12


class FmhpiStates(Fmhpi):
    where = pl.col("geo_type") == "State"


...
PMMS_HISTORY_FRAME: Final = body_reader(PmmsHistory.frame)
PMMS_HISTORY_ROWS: Final = body_reader(PmmsHistory.rows)
```

Expected size: the declarations and the genuinely Freddie-specific checks, about a quarter of today's file. The same
holds in proportion for the other five: the copied typer, date struct, workbook load, line-ending check, unpivots and
partial factories all go.

Tests keep feeding recorded bodies through `tests/support/cassettes.endpoint_body`. The edge-case tests that today
monkeypatch `decoders._load` or call private layouts instead call `Layout.frame(body)` on a small hand-written body,
or `Layout.from_text(frame)` (a seam that takes an already-loaded text frame and runs the shared core), so a sheet
test needs no workbook file.

## Speed: how it is proven

- `benchmarks/` in this repo: a script that takes a prism-clients checkout path, reads each corpus body with
  `tests.support.cassettes.endpoint_body`, and times the v1 layout against the hand-written reader (imported from that
  checkout) on the same body, best of 7. Cassettes are never copied into this public repo.
- The corpus: one large and one small body per format from the six ported clients (FMHPI and PMMS CSV; Chicago Fed
  CFNAI vintages and ADS for sheets, including the long reads; Atlanta Fed China zip; SF Fed's widest sheet), plus a
  JSON body from a client in flight when one is recorded.
- The bar: no slower than hand-written on any corpus body, and each frame `.equals()` the hand-written one (or the
  difference is listed and explained). The results table goes in the release PR.
- Cassette YAML decode is not a bottleneck: libyaml loads FMHPI's 19 MB cassette in 0.13 s. No change there.

## Configuration beyond layouts

None. The user's configuration is the provider and table classes. No global switches, environment variables or
config files.

## Release and migration

v1.0.0 replaces 0.4 wholesale; prism-clients pins the tag as it pins v0.4.0 today. **The user's decision
(2026-11-09): one big pin-bump PR.** v1 deletes 0.4 outright, and the prism-clients PR that pins v1.0.0 also ports
every package still importing 0.4 (about fifteen: cboe, cftc, eia, federal_reserve, finra, ibkr, nasdaqtrader,
new_york_fed, noaa, occ, sec, treasury, unusual_whales, yfinance, congress, and the shared `_frames.py`,
`_trailers.py`, `pages.py`, `streams.py`) onto v1 layouts, keeping each one's current datasets and outputs. The client
sessions' new datasets build on v1 only after that PR lands. That PR is the real test of the settings: it runs v1
against the Nasdaq variants, IBKR trailers, OCC refusals, FINRA counts and Treasury JSON strings on their recorded
bodies, and adds a JSON body to the benchmark corpus. Removed, and what replaces each:

| 0.4 | v1 |
|---|---|
| `cells.Column` in `Annotated[...]` on struct fields | `labels`, `missing`, `dates`, … on the layout class; the struct is plain |
| `cells.read_table`, `match_header`, `column_specs`, `resolve_type`, `dtype_of` | the shared core, internal |
| `delimited.read_csv`, `read_header` | `Csv` / `Pipe` / `Tsv` layouts; `scaffold` reads a header |
| `delimited.stream_csv`, `archives.stream_zip`, `stream_unzip` | dropped (one buffered get per method) |
| `archives.unzip`, `unzip_member`, `zip_members` | the `member` setting; `flatread.unzip(body) -> dict[str, bytes]` kept as one function for a client's own split |
| `workbooks.read_sheet*`, `read_every_sheet*` | `Sheet` layouts, `every_sheet` |
| `frames.frame_schema`, `frame_rows`, `typed_rows`, `typed_file` | `Layout.frame` / `Layout.rows` |
| `text.read_text`, `decode_text`, `decode_utf8`, `split_lines` | internal; `encoding` setting |
| `pdf.read_text` | dropped: Congress ports its 50 lines (already in its plan) |
| `errors.or_not_published` | `refusals` + `NotPublishedError` |

Package: `msgspec`, `polars` and (extra `excel`) `fastexcel` stay; `stream-unzip` and `pdfminer-six` go.

## Left to the builder

- Module layout inside `flatread/`, private names, and how `__init_subclass__` stores the compiled plan.
- The exact `Report` and `Diff` structures and their text rendering.
- Which corpus bodies beyond those named, and the benchmark script's shape.
- Error message wording, as long as each names line (or sheet row / record), label and text.
- Whether `Pipe`/`Tsv` are subclasses or just `Csv` with `separator` (behavior must be identical).

## Open (to confirm with the user or a client session)

- **ECB (confirmed by the ECB session, 2026-11-09, `flatread-v1-ecb-formats.md`):** SDMX-CSV only, no SDMX-JSON.
  Each body fits `Csv` with no new setting:
  - Layout: long, CRLF line endings, quoted commas, 21 to 49 columns per data structure definition (83 of them).
  - Missing values: `missing = {""}`, and `nonfinite = True` because NaN, INF and -INF are written by name.
  - `TIME_PERIOD` stays a `str` field, with shapes YYYY, YYYY-Qn, YYYY-MM, YYYY-MM-DD and YYYY-Sn.
  - The `ACTION` column, present on `updatedAfter` calls only, is a `variants` entry.
  - The zero-byte empty window is `min_rows = 0` plus a `refusals` entry, or `headgate`'s `empty=`.

  The dataflow list is SDMX-ML. It is not a table, so it stays in the client.
- The three-provider rule is "for now": when a fourth client hand-writes the same XML records reader, XML records
  become a fifth base.

## Review record

**ponytail-review (2026-11-09), verdict "fix 1–4 first, then 5–8"; all eight fixed in this draft:**
1. CSV sentinel on CRLF, a last line without its ending, blank lines and a stray `\x01`: the fast path's conditions
   are now explicit, and errors re-read on the fallback for their line.
2. Hooks were instance methods beside classmethod readers: they are classmethods.
3. `missing` examples disagreed: an override replaces the set whole, and the examples say `{"", "."}`.
4. `Decimal` by `str.to_decimal` could round a later, longer cell: the scale is the column's largest, and a cell
   that does not write back is refused.
5. Seven settings broke the three-provider rule: cut (listed under `Json`).
6. The report's "collect mode": dropped; the report reads the typing step's own masks.
7. The workbook fallback assumed fastexcel names the bad column: it does not, so the fallback is today's two loads,
   over only the number fields' columns.
8. `Erratum(at=…)` was undefined: it is `label` + `wrote` + an optional `where` on other cells' text.

Left: finding 9 (each `where` table reads the whole body) is stated in the `where` row, by design.
Not checked by the review: msgspec `defstruct(rename=…)` against polars' own JSON read, and polars 2.1's `Decimal`;
both are measured in the build's first benchmark.

**Builder check (2026-11-09):** seven questions. Settled as the builder's calls, now in the doc: sheet table ends
(`end`, `footer` on `Sheet`), check-then-drop columns (`labels` → `None`), whitespace (`strip`, default off),
sheet cell semantics per field type, `Long`'s `names` pattern and dropped empty cells, blank lines as framing. One
went to the user: how prism-clients moves from 0.4 to v1 (below).
