# flatread

Declare a provider's table once, as a class, and read its body on one native polars path, exactly, or fail loudly.

Exchanges, regulators and statistics agencies publish data as CSV and pipe files, Excel workbooks, JSON APIs, zips
and fixed-width text. Their files carry preambles and trailers, row counts, "no file" pages served with a 200, tokens
like `.` or `ND` for a missing value, and headers that change without notice. flatread reads each one strictly: every
cell is its value, one of its column's missing tokens, or an error naming its line, its label and its text. Nothing is
silently `None`, no short row is padded, no digit is rounded.

A table is a **layout**: a class with a plain msgspec row struct and only the settings where the provider differs.
A provider's defaults are a class too, and its tables inherit them. `Layout.frame(body)` returns a polars DataFrame of
the struct's snake_case fields, and `Layout.rows(body)` a list of the struct. Both are classmethods, so they pass
straight to an HTTP client's body reader.

## Example

```python
import datetime as dt

import msgspec
import polars as pl

import flatread


class HpiRow(msgspec.Struct, frozen=True):
    year: int
    month: int
    geo_type: str
    geo_name: str
    geo_code: int | None
    index_nsa: float
    index_sa: float


class FreddieMac(flatread.Csv):  # the provider's defaults
    missing = frozenset({"", "."})


class Fmhpi(FreddieMac):  # one table
    row = HpiRow
    labels = {
        "Year": "year", "Month": "month", "GEO_Type": "geo_type", "GEO_Name": "geo_name",
        "GEO_Code": "geo_code", "Index_NSA": "index_nsa", "Index_SA": "index_sa",
    }  # fmt: skip


class FmhpiStates(Fmhpi):  # one body, several tables
    where = pl.col("geo_type") == "State"


frame = FmhpiStates.frame(body)  # 16.5 MB, 272,619 rows: one pl.read_csv, one typing pass
rows = FmhpiStates.rows(body)  # list[HpiRow]
```

In prism-clients each dataset's constants are then `body_reader(FmhpiStates.frame)` and
`body_reader(FmhpiStates.rows)`.

## Formats

| Base | Reads | How |
|---|---|---|
| `Csv`, `Pipe`, `Tsv` | delimited text | one `pl.read_csv` of the whole body, every cell text. A sentinel field on every line makes a short row show instead of being padded. A body where a quoted field spans lines (or that holds a `\x01` byte) reads through the stdlib `csv` module instead. |
| `Sheet` | a sheet of an xlsx or xls (the `excel` extra) | one fastexcel load, each column's type inferred from every cell, strictly. A column mixing kinds falls back to a text load plus a typed load; an erratum or a `cells` hook sees the typed cells as text. Floats keep every digit. An xlsx error cell (`#N/A`) in the table raises; in an xls it reads as empty, since fastexcel does not report it. A filled cell right of the header's last label raises. |
| `Json` | records at a path in a JSON body | one `msgspec.json.decode` into structs built from the layout. Numbers decode natively, and a text field must be a JSON string; a provider writing every value as a string with its own `"null"` reads in text mode, where any other JSON value raises. The records path must be present and a list. `Json.page(body)` also returns the cursor at `next`. |
| `FixedWidth` | fixed-width records | `str.slice` per column at the provider's 1-based positions; a line shorter than the last column raises. |

Any layout can read a zip member: `member = "x.csv"`, a regex, or a callable.

## Settings

Every setting is a class attribute. A subclass overrides it whole: a `missing` set replaces its parent's, so include
`""` when you mean it. Building a class checks every setting against the row struct (an unknown setting, a label
naming no field, a field nothing fills, a regex polars cannot compile) and compiles the read. A class without `row` is
a provider's defaults and is never read.

| Setting | Default | Meaning |
|---|---|---|
| `row` | — | the msgspec struct; its fields, in order, are the frame's columns. `str`, `int`, `float`, `Decimal`, `bool` (`true`/`false`), `date`, `datetime`, `time`, `Literal[...]`, any `\| None` |
| `labels` | — | provider label → field, in the file's order; `None` checks a column present and drops it; ordered `(label, field)` pairs when a label repeats |
| `variants` | `()` | other whole label maps, tried in order (a header that changed on a date); any label set may leave a `\| None` field out, which then reads as null |
| `missing` | `{""}` | tokens for no value; a field that is not `\| None` refuses them |
| `missing_by_field` | `{}` | extra tokens for one field |
| `strip` | `False` | take whitespace off every text cell first |
| `dates` | `{}` | field → `strftime` format, a tuple of them, or `flatread.QUARTER` / `YEAR_MONTH` / `EXCEL_SERIAL`; checked by writing the value back. A date, datetime or time with none reads ISO |
| `nonfinite` | `False` | admit NaN and infinities in float fields |
| `long` | `None` | `Long(value=, names=, name=None)`: melt every column `labels` does not name into `value`; each label must match the regex `names` whole, its named groups fill those fields, and `name` takes the whole label |
| `headings` | `None` | the field a group heading above the header fills, for a long table |
| `errata` | `()` | `Erratum(label, wrote, means, why, where={...})`: a provider mistake fixed before typing; raises `ErratumGoneError` once the provider fixes it |
| `refusals` | `{}` | body text → error, for a "no file" answer in a 200 (`b""` is an empty body) |
| `min_rows` | `1` | fewer rows raise `EmptyBodyError`; `0` lets a file hold no rows. An empty body always raises: a file with no rows still has its header |
| `key` | `()` | fields that identify a row: checked unique, and compared by `diff` |
| `where` | `None` | a polars filter of the typed frame |
| `member` | `None` | the zip member this table is |

`Csv`: `separator`, `quoted`, `header`, `skip_lines`, `header_line` (a regex the header line matches), `trailing_separator`,
`final_line_ending` (`True` required, `False` refused, `None` either), `encoding`, `preamble` and `trailer` (each a
`Line(pattern, carry={group: field}, count=group)`; a carried value is typed even when the file has no rows).
`Sheet`: `sheet`, `header_row`, `columns` (`"A:G"`), `end` (`"blank_row"` or `"last_row"`), `footer` (a regex for
notes after the table), `every_sheet` (`Sheets(name)`: every matching sheet into one table). `Json`: `records`
(`"data"`, `"results[].bars"`, `""` for a list body), `envelope` (`{path: field}`), `next`. `FixedWidth`: `columns`
(`{field: (first, last)}`), `record`, `skip`, `final_line_ending`.

A provider quirk outside these settings is a hook, written in plain polars, run only where a class overrides it:

```python
class Hooked(FreddieMac):
    @classmethod
    @override
    def cells(cls, frame: pl.DataFrame) -> pl.DataFrame:  # text cells under the labels, before typing
        return frame.with_columns(pl.col("Rate").str.replace(",", "."))

    @classmethod
    @override
    def check(cls, frame: pl.DataFrame) -> None:  # the typed frame, before `where`; raise CheckError
        ...
```

A format of its own (a PDF table, side-by-side blocks) overrides `read_text(body) -> flatread.core.Text`, and the
shared core (labels, errata, typing, long, checks) still runs on what it returns. `Layout.from_text(frame)` reads a
frame of text cells as a body would be: a seam for tests.

ruff's `RUF012` flags every `labels = {...}` as a mutable class attribute; flatread only reads settings, so a
package of layouts ignores it (`per-file-ignores = {"*/decoders.py" = ["RUF012"]}`).

## Utilities

- `flatread.report(Layout, body)`: every fault in a body, rather than the first: bad cells per column with the
  first three, the errata gone, or the structural fault that stops the read.
- `flatread.diff(Layout, old, new)`: two recordings compared by `key`: rows added, removed, and each changed value.
- `python -m flatread scaffold PATH --format csv|pipe|tsv|sheet|json [--sheet ...] [--records ...]`: print a row
  struct and a layout read off a sample, to edit.

## Errors

Every error is a `flatread.errors.ReadError`, a `ValueError`: `EmptyBodyError`, `UnknownLayoutError` (and
`CellError` for one cell, with `.where`, `.label`, `.text`), `RaggedRowError`, `TruncatedFileError`,
`ControlTotalError`, `CheckError`, `ErratumGoneError`, `NotPublishedError`, `CorruptArchiveError`, `EncodingError`.

## From 0.4

v1 replaces 0.4 outright, with no aliases.

| 0.4 | v1 |
|---|---|
| `Column(...)` in `Annotated` on struct fields | `labels`, `missing`, `dates`, ... on the layout; the struct is plain |
| `delimited.read_csv`, `read_header` | `Csv` / `Pipe` / `Tsv` layouts |
| `delimited.stream_csv`, `archives.stream_zip`, `stream_unzip` | gone: one buffered read per body |
| `archives.unzip`, `unzip_member` | `flatread.unzip`, and the `member` setting |
| `workbooks.read_sheet*`, `read_every_sheet*` | `Sheet` layouts, `every_sheet` |
| `frames.frame_schema`, `frame_rows`, `typed_rows`, `typed_file` | `Layout.frame`, `Layout.rows` |
| `text.*` | internal; the `encoding` setting |
| `pdf.read_text` | gone |
| `errors.or_not_published` | `refusals` and `NotPublishedError` |

The design and its reasons are in `docs/superpowers/specs/2026-11-09-flatread-v1-design.md`.

## Install

```
uv add "flatread[excel] @ git+https://github.com/kelsayed17/flatread@v1.0.0"
```

Python 3.14; polars, msgspec, and fastexcel for sheets.
