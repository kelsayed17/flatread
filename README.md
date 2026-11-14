# flatread

Read a publisher's files into typed rows, exactly, or fail loudly.

Exchanges, regulators and statistics agencies publish data as CSV and pipe files, Excel workbooks, fixed-width text,
print reports, XML, HTML pages, PDFs and SDMX messages, often inside a zip or in a legacy encoding. Their files carry
preambles and trailers, titled tables, carried-down cells, thousands separators, tokens like `.` or `ND` for a missing
value, and headers that change without notice. flatread reads each one strictly: every cell is its value, one of its
column's missing-value tokens, or an error naming where it is. Nothing is silently `None`, no short row is padded, no
digit is rounded.

## A reader

A reader is a [msgspec](https://jcristharif.com/msgspec/) row struct, whose fields say where each value comes from,
given to the reader class for the file's shape:

```python
import datetime as dt
from typing import Annotated

import msgspec

from flatread.delimited import CsvFile
from flatread.errors import ReadError
from flatread.fields import Format, Label


class Rate(msgspec.Struct, frozen=True):
    day: Annotated[dt.date, Label("Date"), Format("%m/%d/%Y")]
    rate: Annotated[float | None, Label("Rate")]


RATES = CsvFile(Rate, null_values=["ND"], final_line_ending=True)

assert RATES.rows(b"Date,Rate\n01/02/2026,4.33\n01/05/2026,ND\n") == [
    Rate(dt.date(2026, 1, 2), 4.33),
    Rate(dt.date(2026, 1, 5), None),
]
assert RATES.frame(b"Date,Rate\n01/02/2026,4.33\n").columns == ["day", "rate"]

for cut in (b"Date,Rate\n01/02/2026,4.33", b"Date,Rate\n1/2/2026,4.33\n", b"Date,Rate\n01/02/2026,4.33,x\n"):
    try:
        RATES.rows(cut)
    except ReadError as e:
        print(e)
# the body's end is not the file's, which always ends its last line with a line ending: it was cut
# the 'Date' cell '1/2/2026' is not a date written %m/%d/%Y: row 1
# row 1 has 3 fields, not the header's 2
```

`reader.frame(body)` returns a polars DataFrame of the struct's fields and `reader.rows(body)` a list of the struct.
Both take the body's bytes, so either passes straight to an HTTP client's body reader. Build a reader once, at import,
and share it: it holds no state between reads.

**The markers** (`flatread.fields`) go in each field's `Annotated`: `Label` (the header's text), `Format` (a date or
time's `strptime` format, its zone, its alternatives; `%q` writes a quarter), `Flag` (a boolean's words), `Joined` (a
date written across two columns), `Shape` (a regex every text cell fullmatches), `Span` (a fixed-width column's
place), `Cell` (a sheet cell stating a field for every row) and `Link` (an HTML cell's `href`). A field that admits
`None` may be empty, and a field with a default takes it where its cell is empty; any other empty cell raises. A
`Literal` field holds one of its values, typed as they are.

**A file's quirks are switches**: underscored class attributes a subclass sets, documented on each reader. The readers
compose by inheritance, each piece overriding one step and calling the next, so a quirk is a class:

```python
import re

from flatread.delimited import HeadersCsv, TrailerCsv


class Volume(msgspec.Struct, frozen=True):
    symbol: Annotated[str, Label("Symbol")]
    volume: Annotated[int, Label("Volume")]
    shares: Annotated[int | None, Label("Shares")]


class VolumeFile(TrailerCsv[Volume], HeadersCsv[Volume]):
    """A pipe file under one of two headers, closed by a line counting its rows."""

    _headers_lacking = ((), ("shares",))  # the whole header, and the older one without Shares
    _trailer = re.compile(r"Rows: (?P<count>\d+)")


VOLUMES = VolumeFile(Volume, separator="|")

assert VOLUMES.rows(b"Symbol|Volume\nAAA|10\nRows: 1\n") == [Volume("AAA", 10, None)]
```

[`docs/examples/README.md`](docs/examples/README.md) shows every reader and switch on a short excerpt of a real file
that needs it.

## Readers

A module per format family. Each imports only the modules below it, and only its own family's parsers, so a CSV-only
install loads no HTML, PDF, XML or workbook library (the import-linter contracts in `pyproject.toml`).

| Module | Reads | Extra |
|---|---|---|
| `flatread.fields` | a row field's markers: `Label`, `Format`, `Flag`, `Joined`, `Shape`, `Span`, `Cell`, `Link` | |
| `flatread.errors` | `ReadError` (`EmptyBodyError`, `RaggedRowError`) and `LayoutError` | |
| `flatread.core` | `File`, the base every reader composes on; `TextFile`; `Thousands`, `Carried`, `Reshaped` | |
| `flatread.grids` | a table cut from a grid of text cells: `GridFile`, `TitledGrid`, `FilledHeader`, `BlankEnded`, `BesideGrid` | |
| `flatread.delimited` | CSV and delimited text: `CsvFile`, `HeadersCsv`, `NamedCsv`, `TrailerCsv`, `TitledCsv`, `PreambleCsv`, `SectionedCsv`, `SpilledCsv`, `PaddedCsv`, `ThousandsCsv`, `CsvText` | |
| `flatread.fixed` | fixed-width text: `FixedWidthFile` | |
| `flatread.reports` | a table laid out for print: `ReportFile`, `LongReport` | |
| `flatread.layout` | a wide table read long: `LongFile` | |
| `flatread.found` | a table whose header is found by its labels: `FoundTable`, `FoundCsv` | |
| `flatread.archives` | a file in a zip or a legacy encoding: `ZippedFile`, `ZippedFiles`, `ZippedOrBare`, `StreamedZip`, `Transcoded`, `zip_names`, `zipped_member` | |
| `flatread.sheets` | xlsx and xls workbooks: `SheetFile` and a class per sheet shape, `SheetsFile`, `SheetText`, `BiffSheet`, `FoundSheet`, `sheet_grid` | `excel` |
| `flatread.markup` | XML: `XmlFile`, `NestedXml`, `parse_xml` | `xml` |
| `flatread.sdmx` | SDMX-CSV and SDMX-ML: `SdmxCsv`, `SdmxStructure`, `SdmxSeries`, `SdmxObservations` | `xml` |
| `flatread.pages` | HTML tables and records: `HtmlFile`, `HtmlRecords`, `parse_html` and its cell helpers | `html` |
| `flatread.pdf` | a PDF's text layer: `PdfFile`, `LaidOutPdf`, `text_layer` | `pdf` |

`flatread.pages` reads through libxml2's HTML parser, so a page reads as the libxml2 inside lxml parses it. lxml's
Windows wheels bundle libxml2 2.11, which keeps the text of a CDATA section in a cell and nests one level deeper
before its 256-level limit. Elsewhere lxml bundles 2.14.

## Errors

Every error a read raises is one of two kinds, both `ValueError`s:

- `flatread.errors.ReadError`: the body is not the file its reader describes. Its message names where: the row, the
  cell, the line or the member. `EmptyBodyError` (nothing to read) and `RaggedRowError` (a row wider or narrower than
  its header) are the two kinds worth telling apart.
- `flatread.errors.LayoutError`: the reader is declared wrong (a date field with no `Format`, spans that overlap, a
  switch left unset or naming a field the struct lacks, pieces composed in an order that cannot read). It is raised
  when the reader is built; only a few show at the first read (an inner reader whose columns are not the fields).

Catch `ReadError` to handle bad data, and let a `LayoutError` fail loudly. No other exception escapes a read: a
parser's own error, or its panic (polars' and calamine's are `BaseException`s), is a `ReadError`.

**Hostile bodies.** A read is bounded by its body: a zip member inflates to at most 1 GiB in all, a PDF's streams
decode to at most 128 MiB and its forms nest 16 deep, an SDMX message holds at most 1 MiB outside its series, and an
`.xlsx` or `.xls` sheet spans at most 2^23 cells and 16,384 columns. Past a bound the read raises `ReadError`, before
it holds the rest.

**Strictness costs time, by choice.** A CSV's number columns are read as text and then cast, so a cell padded with
whitespace (`" 5"`) or a float that underflows (`1e-400`) raises; it makes a CSV read 10-30% slower. A sheet's number
columns get a third load, which tells a text cell from a number cell, so text written as a number (`"2"`) or as a
token (`n/a`, `NA`) in a number field raises; and an `.xlsx`'s span is read from its cell references before calamine
builds it. Together these make a sheet read 40-55% slower. An `.xlsb` or `.ods` is not read.

## From 5.0

6.0 makes 5.0's readers one public API. A reader declared for 5.0 reads the same files the same way, but for these:

- Every body error is a `ReadError`, every declaration error a `LayoutError`; `UnknownLayoutError` is gone.
- A CSV's boolean column is `true` or `false` exactly, as in every other reader (polars read `TRUE` and `tRuE` too).
- A CSV's mistyped cell, Flag or date error names the header's label and the row, not polars' message or the field.
- `GridFile`, `TitledGrid`, `FilledHeader`, `BlankEnded` and `BesideGrid` moved from `flatread.core` to
  `flatread.grids`, and `FoundSheet` from `flatread.found` to `flatread.sheets` (so `flatread.found` needs no extra).
- `HeadersCsv._headers`, each header's labels written out again, is `_headers_lacking`: each published header by the
  fields it lacks (`((), ("shares",))`), so a label is written once, in the struct. A lacked field must admit `None`.
- `NamedCsv._carried` is gone: a header must name the column of every field that holds a value on every row, which
  the struct says; `_required_fields` names any other field whose column every header names.
- The pieces compose in any order where they can (`HeadersCsv` before or after `PaddedCsv` or `TitledCsv`), and an
  order that cannot read is a `LayoutError` when built: list `LongFile` before `Carried`, and every piece before the
  CSV or fixed-width reader that ends the read. A field a trailer, title or zip member's name states is stated to
  every piece (`File._stated_fields()`), so `ZippedFiles` composes with `HeadersCsv`, `NamedCsv` and `LongFile`.
- A switch is checked when the reader is built (`File._check_switches()`): one left unset, a regex group naming no
  field, a `Shape` polars cannot compile, an unknown zone, a `Format` that writes what its field cannot hold, `%z`.
  A constructor that sets a switch (`_keys`, `_member`) sets it before calling `super().__init__()`.
- Stricter reads: an HTML number, date or boolean cell is one text run (`5.3<sup>1</sup>` raises, not `5.31`); a cut
  HTML page, a page's bytes that are not its declared charset, a title or trailer that is not UTF-8, and a `FoundCsv`
  line narrower than its header raise; a sheet's text cell in a number or boolean field raises; `%y` reads 69 as
  1969, as `strptime` does; `PaddedCsv` strips a cell before matching its tokens; `SectionedCsv` matches where a
  line begins; an SDMX-CSV's floats are finite but `obs_value`'s (`File._non_finite`).
- An error names the file's own row in every grid reader, and the header's label, not the field.
- `null_values=` is every text reader's constructor argument (XML, HTML, PDF, report, found, fixed-width).
- `SdmxData._member` is `_zipped_member` (it streams the member; `ZippedFile` composes too). `row_one` left
  `File.__init__`. The hooks' column, `flatread.core.Column`, is public.
- Renamed switches: `ReportFile._header` → `_header_lines`,
  `ReportFile._end` → `_last_line`; `LongReport._key` → `_key_field`; `HtmlFile._table` → `_table_selector`;
  `HtmlRecords._record`, `_heading`, `_stated` → `_record_selector`, `_heading_selector`, `_heading_pattern`;
  `FoundTable._section`, `_stated` → `_section_prefix`, `_section_field`; `FoundTable._nulls` → the `null_values=`
  constructor argument.
- `scalar_type`, `check_ending`, `text_sheet` and `SheetFile.frame_on` are private.

## Install

```
uv add "flatread[excel,xml,html,pdf] @ git+https://github.com/kelsayed17/flatread@v6.0.0"
```

Python 3.14; polars and msgspec, plus each extra's parsers: `excel` fastexcel and xlrd, `xml` lxml, `html` lxml and
Scrapling, `pdf` pdfminer.six.
