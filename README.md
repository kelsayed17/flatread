# flatread

Read the flat files publishers serve, exactly, or fail loudly.

Exchanges, regulators and statistics agencies publish data as pipe-delimited text, CSV, Excel workbooks, ZIP archives
and PDFs. These files carry preambles and trailers, record counts and control totals, "not available" pages served in
place of a file, tokens like `.` or `ND` for a missing value, and headers that change without notice. A generic CSV
reader reads most of that as data, or quietly turns what it doesn't understand into `null`.

flatread takes the opposite stance, with as little code of its own as it can. You declare a file's rows once, as a
[msgspec](https://jcristharif.com/msgspec/) struct whose fields declare the columns. [polars](https://pola.rs) reads
the file (`pl.read_csv`, `pl.read_excel`), and flatread reads every cell strictly into a DataFrame whose columns are
the file's own, under its labels, in its order, each typed as its field declares. Every cell ends one of three ways:
it is its value, it is a token the column declares stands for none, or the read fails with an error naming the row,
the column and the text. Nothing is silently `None`.

## Example

```python
from typing import Annotated

import msgspec

from flatread.cells import Column
from flatread.delimited import read_csv
from flatread.frames import typed_rows


class Quote(msgspec.Struct, frozen=True):
    symbol: Annotated[str, Column("Symbol")]
    price: Annotated[float | None, Column("Price", missing=("",))]
    volume: Annotated[int, Column("Volume")]
    halted: Annotated[bool, Column("Halted", true="Y", false="N")]


body = b"Symbol|Price|Volume|Halted\nAAPL|227.50|1200|N\nXYZ||300|Y\n"

frame = read_csv(body, Quote, separator="|")
frame.rows()  # [('AAPL', 227.5, 1200, False), ('XYZ', None, 300, True)]
frame.schema  # Symbol str, Price f64, Volume i64, Halted bool: the file's labels, the fields' types
typed_rows(frame, Quote)  # [Quote(symbol='AAPL', ...), Quote(symbol='XYZ', price=None, ...)]
```

A line a file writes about itself (a generation stamp, a record count) is the caller's to read:
`flatread.text.split_lines(text, head=1, tail=1)` takes those lines off and leaves the rows for `read_csv`.

And what it refuses:

| The body | The error |
|---|---|
| `1,200` in the Volume column | `CellError: row 2: the 'Volume' cell '1,200' is not a whole number` |
| a fifth field on a row | `RaggedRowError: line 3 has 5 fields, not the header's 4: 'XYZ\|\|300\|Y\|9'` |
| a header naming another column | `UnknownLayoutError: the header [...] is not the columns ['Symbol', 'Price', 'Volume', 'Halted']` |
| `12/31/25` in a `%m/%d/%Y` date column | `CellError: ... is not a date in %m/%d/%Y` (polars alone reads it as the year 25) |

A declaration that cannot hold fails when the row struct is first read. Examples: a field with no `Column`, or a field
that may be `None` though nothing in its column ever is.

## What it reads

| Module | What |
|---|---|
| `flatread.delimited` | `read_csv`: one `pl.read_csv`, the header and every row's width checked, the cells read by `read_table`. `stream_csv` reads a file too large to hold, a batch of lines at a time. |
| `flatread.cells` | `Column`, the declaration on a row-struct field, and `read_table`: a frame under the source's labels read strictly by polars' own conversions (`str.to_integer`, a `Float64` cast, `str.to_datetime` written back in the column's format). |
| `flatread.frames` | A row struct's frame schema, and a frame read back into typed rows (`typed_rows`, `typed_file`). |
| `flatread.text` | A body's text, and the lines a file writes about itself around its rows (`split_lines`). |
| `flatread.archives` | A ZIP's files, whole or inflated as its chunks arrive, with CRCs and sizes checked. |
| `flatread.workbooks` | An Excel sheet through polars' calamine engine, with every number cell's exact text (calamine's own text rounds to nine decimals). Needs the `excel` extra. |
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
