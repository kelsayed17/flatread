# flatread

Read the flat files publishers serve, exactly, or fail loudly.

Exchanges, regulators and statistics agencies publish data as pipe-delimited text, CSV, Excel workbooks, ZIP archives
and PDFs. These files carry preambles and trailers, record counts and control totals, "not available" pages served in
place of a file, tokens like `.` or `ND` for a missing value, and headers that change without notice. A generic CSV
reader reads most of that as data, or quietly turns what it doesn't understand into `null`.

flatread takes the opposite stance. You declare a file once, as the [msgspec](https://jcristharif.com/msgspec/)
structs its rows (and, where it states values of itself, the file) decode into. flatread reads it into those typed
rows, or into a [polars](https://pola.rs) DataFrame of the same columns. Every cell ends one of three ways: it is its
value, it is a token the column declares stands for none, or the read fails with an error naming the line, the column
and the text. Nothing is silently `None`.

## Example

```python
import datetime as dt
from typing import Annotated, NamedTuple

import msgspec
import polars as pl

from flatread.cells import Column
from flatread.delimited import DelimitedFile, FromTrailer, GenerationStamp


class Quote(msgspec.Struct, frozen=True):
    symbol: Annotated[str, Column("Symbol")]
    price: Annotated[float | None, Column("Price", missing=("",))]
    volume: Annotated[int, Column("Volume")]
    halted: Annotated[bool, Column("Halted", true="Y", false="N")]


class QuoteFile(msgspec.Struct, frozen=True):
    generated_at: Annotated[dt.datetime, FromTrailer(GenerationStamp())]
    quotes: list[Quote]


class QuoteFrame(NamedTuple):
    generated_at: dt.datetime
    quotes: pl.DataFrame


QUOTES = DelimitedFile.of_file(QuoteFile, frame_type=QuoteFrame, separator="|")

body = b"Symbol|Price|Volume|Halted\nAAPL|227.50|1200|N\nXYZ||300|Y\n20261105183000\n"

QUOTES.read(body)
# QuoteFile(generated_at=datetime(2026, 11, 5, 18, 30),
#           quotes=[Quote('AAPL', 227.5, 1200, False), Quote('XYZ', None, 300, True)])

QUOTES.read_frame(body).quotes  # the same rows as a DataFrame: symbol str, price f64, volume i64, halted bool
```

And what it refuses:

| The body | The error |
|---|---|
| `1,200` in the Volume column | `CellError: line 2: the 'Volume' cell '1,200' is not a whole number` |
| the trailer line missing | `TruncatedFileError: no trailer: line 3 is 'XYZ\|\|300\|Y', not the 14-digit time the file was generated, so the file may have been cut off` |
| a fifth field on a row | `RaggedRowError: line 3 should have 4 fields, not 5: 'XYZ\|\|300\|Y\|9'` |

A declaration that cannot hold fails at import, not when a file is read. Examples: a field with no `Column`, a field
that may be `None` though nothing in its column ever is, a check over a column the rows lack, or a frame type whose
fields differ from the file struct's.

## What it reads

| Module | What |
|---|---|
| `flatread.delimited` | A delimited text file declared by its structs, read whole (`DelimitedFile`) or as its chunks arrive (`StreamedFile`). The header parts are `Labels`, `Layouts`, `ByName` and `NoHeader`; the checks are `RowCount` and `ColumnTotal`. Values a file writes about itself are `FromPreamble`, `FromHeader` and `FromTrailer` fields. |
| `flatread.cells` | `Column`, the declaration on a row-struct field, and the strict cell reader. Shapes are derived from `strftime` formats, in range: no 13th month, no 25th hour. |
| `flatread.frames` | The frame half: a file's lines counted, checked for width and quoting, and split into a DataFrame. |
| `flatread.text` | A body's text and its numbered lines, whole or streamed. |
| `flatread.archives` | A ZIP's files, whole or inflated as its chunks arrive, with CRCs and sizes checked. |
| `flatread.workbooks` | An Excel sheet through calamine, with every number cell's exact text (calamine's own text rounds to nine decimals). Needs the `excel` extra. |
| `flatread.pdf` | A PDF's text, line by line in the order its pages draw it. Needs the `pdf` extra. |
| `flatread.errors` | `ReadError`, a `ValueError`, and one subclass for each thing a body can get wrong. |

Every error is a `ReadError`, and a `ReadError` is a `ValueError`. So an HTTP client that treats a decoder's
`ValueError` as a malformed response handles flatread's errors with no glue.

## Install

```
pip install flatread            # delimited text, archives
pip install "flatread[excel]"   # + Excel workbooks
pip install "flatread[pdf]"     # + PDF text
```

Python 3.14 or newer.

## License

MIT
