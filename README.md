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

Each release's changes, and how to move a reader from the one before, are in [`CHANGELOG.md`](CHANGELOG.md).

## flatread.next (preview)

`flatread.next` builds a reader from values rather than classes: a row struct, a format, and a stage for each quirk
the file has, each a frozen value configured by keyword. It reads the formats and quirks it has stages for, CSV,
sheets, fixed-width text, PDF and SDMX, and refuses at build any field it cannot yet read as declared. Its names may
change in any 0.x release.

```python
import re

from flatread.errors import TruncatedFileError
from flatread.next import Csv, Reader, Trailer

PIPED = Reader(Rate, Csv(separator="|"), table=Trailer(re.compile(r"\d+ rows")), null_values=["ND"])

assert PIPED.rows(b"Date|Rate\n01/02/2026|4.33\n01/05/2026|ND\n2 rows\n") == [
    Rate(dt.date(2026, 1, 2), 4.33),
    Rate(dt.date(2026, 1, 5), None),
]

for cut in (b"Date|Rate\n01/02/2026|4.33\n", b"Date|Rate\n1/2/2026|4.33\n1 rows\n"):
    try:
        PIPED.rows(cut)
    except TruncatedFileError as e:
        print("cut:", e)
    except ReadError as e:
        print(e)
# cut: the last line '01/02/2026|4.33' is not the file's trailer: the body was cut
# the 'Date' cell '1/2/2026' is not a date written %m/%d/%Y: row 2
```

A row number is the row as the file numbers it: its line in a text file (the header is line 1), its row in a sheet.

## Install

```
uv add "flatread[excel,xml,html,pdf] @ git+https://github.com/kelsayed17/flatread@v0.5.0"
```

Python 3.14; polars and msgspec, plus each extra's parsers: `excel` fastexcel and xlrd, `xml` lxml, `html` lxml and
Scrapling, `pdf` pdfminer.six.
