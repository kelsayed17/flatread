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
| `flatread.errors` | `ReadError` (`EmptyBodyError`, `RaggedRowError`, `TruncatedFileError`, `ControlTotalError`, `UnknownLayoutError`, `CorruptArchiveError`, `EncodingError`) and `LayoutError` | |
| `flatread.core` | `File`, the base every reader composes on; `TextFile`; `Thousands`, `Carried`, `Reshaped` | |
| `flatread.grids` | a table cut from a grid of text cells: `GridFile`, `TitledGrid`, `FilledHeader`, `BlankEnded`, `BesideGrid` | |
| `flatread.delimited` | CSV and delimited text: `CsvFile`, `HeadersCsv`, `NamedCsv`, `TrailerCsv`, `TitledCsv`, `PreambleCsv`, `SectionedCsv`, `SpilledCsv`, `PaddedCsv`, `ThousandsCsv`, `CsvText` | |
| `flatread.fixed` | fixed-width text: `FixedWidthFile` | |
| `flatread.reports` | a table laid out for print: `ReportFile`, `KeyedReport` | |
| `flatread.layout` | a wide table read long: `LongFile` | |
| `flatread.found` | a table whose header is found by its labels: `FoundTable`, `FoundCsv` | |
| `flatread.archives` | a file in a zip or a legacy encoding: `ZippedFile`, `ZippedFiles`, `ZippedOrBare`, `StreamedZip`, `Transcoded`, `zip_names`, `zipped_member` | |
| `flatread.sheets` | xlsx and xls workbooks: `SheetFile` and a class per sheet shape, `EverySheet`, `SheetText`, `BiffSheet`, `FoundSheet`, `sheet_grid` | `excel` |
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
  cell, the line or the member. Its subclasses are the kinds worth telling apart:

  | class | the body |
  |---|---|
  | `EmptyBodyError` | holds nothing to read |
  | `RaggedRowError` | has a row wider or narrower than its header, or a fixed-width line of another width |
  | `TruncatedFileError` | was cut: it ends inside a zip member or an XML element, before a title or trailer, or not as the file ends |
  | `ControlTotalError` | states a count, in a trailer or a title, that is not the rows it holds |
  | `UnknownLayoutError` | is laid out as another file: a header, root, sheet, title or zip member not the reader's |
  | `CorruptArchiveError` | is a zip that will not open, or holds a member that will not inflate or fails its CRC-32 |
  | `EncodingError` | is not the text encoding it is written in (an XML body's too), or declares a charset no codec reads |

  A cell that is not its type, or a body past a bound (below), is a plain `ReadError`.
- `flatread.errors.LayoutError`: the reader is declared wrong (a date field with no `Format`, spans that overlap, a
  switch left unset, misspelled, set to a value its piece does not take or naming a field the struct lacks, a
  separator of more than one character, a msgspec `Meta` constraint, which no reader checks, pieces composed in an
  order that cannot read). It is raised
  when the reader is built; only a few show at the first read (an inner reader whose columns are not the fields).

Catch `ReadError` to handle bad data, and let a `LayoutError` fail loudly. No other exception escapes a read: a
parser's own error, or its panic (polars' and calamine's are `BaseException`s), is a `ReadError`.

**Hostile bodies.** A read is bounded by its body: a zip member inflates to at most 1 GiB in all, a PDF's streams
decode to at most 128 MiB and its forms nest 16 deep, an SDMX message holds at most 1 MiB outside its series, and an
`.xlsx` or `.xls` sheet spans at most 2^23 cells and 16,384 columns. Past a bound the read raises `ReadError`, before
it holds the rest.

**What strictness costs.** polars' CSV parser takes a number after a space or a tab (`" 5"`) and a float that
underflows (`1e-400` as `0.0`), so a body where a number may be either (a space or tab outside its text cells, a
`0.0` beside an `e-`) has its number columns read again, as text, and cast; any other is read once. A sheet's number
cell is told from text written as a number (`"2"`) or a token (`n/a`, `NA`) by its kind, which an `.xlsx` sheet's
part writes and a third load reads elsewhere; and an `.xlsx` sheet's span is read from its cell references before
calamine builds it, a pass over the sheet's XML. In CodSpeed's simulation (`benchmarks/test_speed.py`) a CSV read
costs about 1.1 times what it did in 5.0 and a sheet read about 1.25 times; a wide or row-by-row CSV read costs
less. An `.xlsb` or `.ods` is not read.

## From 7.1

7.2 refuses, when a reader is built, declarations 7.1 read without a word, each reading a file otherwise than it
says. A reader 7.1 built may raise `LayoutError` now; none of these read its file as declared:

- A switch within two edits of one its pieces read (`_quotechar` for `_quote_char`), which 7.1 ignored. A format that
  keeps a value of its own under such a name annotates it in its class (`_header_alone: ClassVar[bool] = True`).
- A switch annotated as a boolean, a number, text or `Literal` values, set to none of them (`_closing_separator =
  "never"`, read as `None`; `_header = "no"`, a `TypeError` once read).
- A `CsvFile` separator or `_quote_char` that is not one character of one byte, ends a line, or is the other; a
  `CsvText` separator of more than one character.
- A field whose type names a msgspec `Meta` constraint (`ge`, `max_length`, `pattern`, `tz` and the rest), which
  `rows()` never checked: state a pattern as a `Shape`, values as a `Literal`. A `Meta` of descriptions alone is kept.
- A keyword-only struct (`kw_only=True`), whose `rows()` raised `TypeError`; a `Flag` whose two words are one, every
  cell read as false; a `_given` value not of its field's type (`3.7` for an `int`, read as `3`).

Three faults now raise the kind they are, each still a `ReadError`: an XML or SDMX body cut short is a
`TruncatedFileError`, XML bytes its encoding does not read an `EncodingError`, and a fixed-width line of another width
a `RaggedRowError`. Their messages are 7.1's.

## From 7.0

7.1 adds five kinds of `ReadError`: `TruncatedFileError`, `ControlTotalError`, `UnknownLayoutError`,
`CorruptArchiveError` and `EncodingError`. Each is a `ReadError`, so code that catches one catches them, and the
messages are 7.0's. A `FoundCsv` cell past `csv`'s field size limit is a `ReadError`, where 7.0 let `csv.Error` out.

## From 6.0

A reader declared for 6.0 reads the same files the same way, but for these:

- `HeadersCsv`, `NamedCsv`, `TrailerCsv`, `SpilledCsv` and `TitledCsv` have no constructor of their own: each takes
  `CsvFile`'s keywords, as before, and checks its switches in `File._check_switches()`. A misdeclared one raises the
  same `LayoutError` when built; where it has two faults, which one is named first may differ.
- A `NamedCsv`'s empty or mistyped cell is named by the header's label, as every other reader names it, not by its
  field.
- Blank handling is one switch per reader, in one vocabulary: `SheetFile._gaps = True` → `_blank_rows = "drop"`,
  `SheetFile._blank_rows = True` → `_blank_rows = "keep"` (the default is `"refuse"`); `ReportFile._skip_blank =
  False` → `_blank_lines = "read"` (the default is `"drop"`); `LongFile._empty_rows = False` → `_empty_cells = "drop"`
  (the default is `"keep"`); `LongFile._spacers` → `_spacer_columns`. An old name, or a value none of these, is a
  `LayoutError` when built, so a format not yet moved is not read otherwise.
- A `TitledSheet` title with a group (`count`, or one named for a field) is a `LayoutError` when built: a sheet's title
  states no field and counts no rows, which it ignored before. A `TitledSheet` with no `_titles` is one too.
- Seven readers and switches have plain names: `SheetsFile` → `EverySheet`, `CellHeaderSheet` →
  `TypedHeaderSheet`, `NumberTextSheet` → `TextNumbersSheet`, `OnlySheet` → `OneSheet`, `LongReport` → `KeyedReport`
  (it is no `LongFile`), `FoundTable._row_count` → `_numbered_rows`, `LongFile._bare_header_refused` →
  `_header_needs_rows`. The old names are gone.
- A name is a method on every piece that has it, or a value on every piece that has it, since pieces that compose
  share one class (`tests/test_switch_names.py` checks): `FixedWidthFile._stated` → `_outside_fields` (the fields
  the lines above or below its table state), `SdmxObservations._value` → `_value_label` (the observation's value
  attribute). The other six such names were internal. A switch set under its 6.0 name is a `LayoutError` when built.

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
uv add "flatread[excel,xml,html,pdf] @ git+https://github.com/kelsayed17/flatread@v7.1.0"
```

Python 3.14; polars and msgspec, plus each extra's parsers: `excel` fastexcel and xlrd, `xml` lxml, `html` lxml and
Scrapling, `pdf` pdfminer.six.
