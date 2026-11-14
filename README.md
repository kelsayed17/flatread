# flatread

Read a provider's published files into typed rows, exactly, or fail loudly.

Exchanges, regulators and statistics agencies publish data as CSV and pipe files, Excel workbooks, fixed-width text,
print reports, XML, HTML pages, PDFs and SDMX messages, often inside a zip or in a legacy encoding. Their files carry
preambles and trailers, titled tables, carried-down cells, thousands separators, tokens like `.` or `ND` for a missing
value, and headers that change without notice. flatread reads each one strictly: every cell is its value, one of its
column's missing tokens, or an error naming where it is. Nothing is silently `None`, no short row is padded, no digit is
rounded.

A file is a **reader**: a msgspec row struct whose fields name their columns (`Label`, and `Format` for a date or
time), given to the reader class for the file's shape. A shape's switches are class attributes, so a provider's quirk
is a subclass that sets one. `reader.frame(body)` returns a polars DataFrame of the struct's fields, and
`reader.rows(body)` a list of the struct, so either passes straight to an HTTP client's body reader.

## Example

```python
import datetime as dt
from typing import Annotated

import msgspec

from flatread.delimited import CsvFile
from flatread.fields import Format, Label


class Rate(msgspec.Struct):
    day: Annotated[dt.date, Label("Date"), Format("%m/%d/%Y")]
    rate: Annotated[float, Label("Rate")]


RATES = CsvFile(Rate, final_line_ending=True)

RATES.rows(b"Date,Rate\n01/02/2026,4.33\n")  # [Rate(day=datetime.date(2026, 1, 2), rate=4.33)]
RATES.rows(b"Date,Rate\n01/02/2026,4.33")  # ReadError: ... always ends its last line with a line ending: it was cut
```

## Readers

A module per format family. Each imports only the modules below it, and only its own family's parsers, so a CSV-only
consumer installs no extra and loads no HTML, PDF or workbook library (the import-linter contracts in
`pyproject.toml`).

| Module | Reads | Extra |
|---|---|---|
| `flatread.fields` | a row field's markers: `Label`, `Format`, `Flag`, `Cell`, `Joined`, `Span`, `Shape`, `Link` | |
| `flatread.core` | `File`, the base every reader composes on; `TextFile`, `Reshaped`, `Carried`, `Thousands` | |
| `flatread.grids` | a table cut from a grid of text cells: `GridFile`, `TitledGrid`, `FilledHeader`, `BlankEnded`, `BesideGrid` | |
| `flatread.delimited` | CSV and delimited text: `CsvFile`, `CsvText`, `HeadersCsv`, `NamedCsv`, `PaddedCsv`, `PreambleCsv`, `SectionedCsv`, `SpilledCsv`, `ThousandsCsv`, `TitledCsv`, `TrailerCsv` | |
| `flatread.fixed` | fixed-width text: `FixedWidthFile` | |
| `flatread.layout` | a wide table read long: `LongFile` | |
| `flatread.archives` | a file in a zip or a legacy encoding: `ZippedFile`, `ZippedFiles`, `ZippedOrBare`, `StreamedZip`, `Transcoded` | |
| `flatread.reports` | a table laid out for print: `ReportFile`, `LongReport` | |
| `flatread.sheets` | xlsx and xls workbooks: `SheetFile`, `SheetsFile`, `SheetText`, `FoundSheet`, and a class per sheet shape | `excel` |
| `flatread.found` | a table whose header is found by its labels: `FoundTable`, `FoundCsv` (`FoundSheet` is in `flatread.sheets`) | |
| `flatread.markup` | XML: `XmlFile`, `NestedXml` | `xml` |
| `flatread.sdmx` | SDMX-CSV and SDMX-ML: `SdmxCsv`, `SdmxStructure`, `SdmxSeries`, `SdmxObservations` | `xml` |
| `flatread.pages` | HTML tables and records: `HtmlFile`, `HtmlRecords` | `html` |
| `flatread.pdf` | a PDF's text layer: `PdfFile`, `LaidOutPdf` | `pdf` |

`flatread.pages` reads through libxml2's HTML parser, so a page reads as the libxml2 inside lxml parses it. lxml's
Windows wheels bundle libxml2 2.11, which keeps the text of a CDATA section in a cell and nests one level deeper
before its 256-level limit. Elsewhere lxml bundles 2.14.

[`docs/examples/README.md`](docs/examples/README.md) shows every reader and switch on a short excerpt of a real file
that needs it.

## Errors

A read raises `flatread.errors.ReadError` for a body that is not its file, naming the row, cell, line or member, and
`LayoutError` for a reader declared wrong (most when it is built). `EmptyBodyError` and `RaggedRowError` are the
`ReadError`s worth telling apart. Both roots are `ValueError`s.

## From 4.0

5.0 replaces flatread's code with the readers prism-clients built in `prism.clients._readers`, which read every
provider prism-clients serves. 4.0's `Layout` classes, `flatread.cells`, `flatread.records`, `flatread.check`,
`flatread.scaffold` and the `python -m flatread` scaffolder are gone, as are the error classes no reader raises
(`CellError`, `CheckError`, `ControlTotalError`, `CorruptArchiveError`, `EncodingError`, `ErratumGoneError`,
`NotPublishedError`, `TruncatedFileError`). A reader that imported `prism.clients._readers.X` imports `flatread.X`.

## Install

```
uv add "flatread[excel,xml,html,pdf] @ git+https://github.com/kelsayed17/flatread@v5.0.0"
```

Python 3.14; polars and msgspec, plus each extra's parsers: `excel` fastexcel and xlrd, `xml` lxml, `html` lxml and
Scrapling, `pdf` pdfminer.six.
