"""A workbook sheet's table read into its row struct's fields, by fastexcel (an Excel 3 or 4 workbook by xlrd).

:class:`SheetFile` is a clean table on one named sheet: its header in one row (``header_row``, Excel's numbering),
then a row per record to the sheet's end, nothing outside the header's columns. Errors name the sheet's own rows.

**How a sheet is read.** fastexcel's typed load coerces without a word (a text cell is the number its text parses as,
or null, ``1.5`` truncates to ``1`` as a whole number, a plain number reads as a date), so a table is loaded twice: as
text, which says what each cell holds, and typed by the struct's fields. A number field holds number cells: a text
cell raises, ``1e3`` or ``nan`` by its text and one written as a number (``2``) by a third load, of the number columns
as booleans, which reads only a number cell as a value (:class:`NumberTextSheet` reads a sheet that writes its
numbers as text); a whole number is loaded as a float and checked whole and of 64 bits. A boolean field holds boolean
cells; a date field without a ``Format`` date cells (Excel's day 60, the 1900-02-29 of its 1900 date system, raises),
and with one, text in that format; a text field's number cell is its every digit (the text load rounds it to nine
decimals, a float load keeps them). A cell that is not a value of its column raises, naming it, unless it is one of
the sheet's ``null_values`` (``#N/A``). A plain table, every column of one kind, its field's, is read from one strict
load instead, which refuses a column mixing number cells and number text, and a second, of the columns it reads empty
cells in as text, where the strict load reads a word for none (``NA``) as none too (``tests/test_sheet_loads.py`` pins
the load counts). fastexcel's failure on a body, and a panic of calamine's (pyo3's ``PanicException``, no
``Exception``), are a ReadError, as is a sheet spanning more cells than calamine is let build as one dense grid
(:data:`_SPAN_MAX`), refused before it loads: an ``.xlsx`` sheet's span is read from its cell references (a few ms a
MB), an ``.xls`` sheet's from its records.

**The switches** of :class:`SheetFile`: ``_header`` (false: no header row, fields by position), ``_gaps`` (an empty
row inside the table is dropped) or ``_blank_rows`` (kept, a row of no values), ``_trim`` (a cell of only whitespace
is empty), ``_strip`` (header cells stripped), ``_above`` (rows over the header that head its columns too). A header
cell may be empty over a real column: its field's ``Label`` is ``""``.

**The sheet shapes**, each composed before the reader it changes:

* :class:`PaddedSheet`: text cells padded with spaces, stripped before they are typed;
* :class:`NumberTextSheet`: numbers written as text cells as well as number cells;
* :class:`NotedSheet`: notes under the table, which ends at its first row with nothing past column A;
* :class:`DatedSheet`: a table that runs while its first column holds date cells, notes under it;
* :class:`TitledSheet`: title rows over the header, each matching its regex;
* :class:`PreambleSheet`: a preamble of any length, the header the first row opening with its first label;
* :class:`BesideSheet`: more beside the table, only its own columns loaded;
* :class:`OnlySheet`: a workbook of one sheet whose name varies;
* :class:`CellHeaderSheet` and :class:`GroupedSheet`: headers read long (date cells as keys; keys under group
  headings).

:class:`SheetsFile` reads every sheet of a workbook, each by a reader of its own (a sheet a year). :class:`SheetText`
cuts a table from a sheet's grid of text cells (:func:`sheet_grid`, each number with every digit) for a sheet too
irregular for :class:`SheetFile`, and :class:`BiffSheet` from an Excel 3 or 4 workbook's. :class:`FoundSheet` is a
:class:`~flatread.found.FoundTable` on a workbook's first sheet.
"""

import contextlib
import datetime as dt
import functools
import html
import io
import itertools
import re
import struct
import typing
import zipfile
from collections.abc import Callable, Collection, Generator, Iterator, Mapping, Sequence
from typing import Final, Literal

import fastexcel
import msgspec
import polars as pl
import xlrd
from xlrd import compdoc

from flatread.archives import _UNZIP_ERRORS, _inflated
from flatread.core import (
    _UNIT,
    Column,
    File,
    _check_above,
    _first,
    _flagged,
    _moment,
    _none_dtype,
    _repeated,
    _stacked,
    _stated,
    _temporal,
)
from flatread.errors import LayoutError, ReadError
from flatread.found import FoundTable
from flatread.grids import GridFile
from flatread.layout import LongFile

__all__ = [
    "BesideSheet",
    "BiffSheet",
    "CellHeaderSheet",
    "DatedSheet",
    "FoundSheet",
    "GroupedSheet",
    "NotedSheet",
    "NumberTextSheet",
    "OnlySheet",
    "PaddedSheet",
    "PreambleSheet",
    "SheetFile",
    "SheetText",
    "SheetsFile",
    "TitledSheet",
    "sheet_grid",
]

#: The fastexcel type each field scalar's column is loaded as by the typed load: a whole number as a float, which is
#: then checked whole (fastexcel would truncate ``1.5`` to ``1``), a date or time written as text as text (its
#: ``Format`` parses it), and text as a float, whose number cells hold every digit (the text load rounds them to nine
#: decimals: :func:`_with_digits`).
_SHEET_DTYPES: Final[Mapping[object, fastexcel.DType]] = {
    str: "float",
    int: "float",
    float: "float",
    bool: "boolean",
    dt.date: "date",
    dt.datetime: "datetime",
    dt.time: "datetime",
}


#: A date or time cell as fastexcel writes it as text: a date field's at midnight, a time of day's on Excel's day 0.
_DATE_CELL: Final = r"^\d{4}-\d{2}-\d{2} 00:00:00$"


_TIME_CELL: Final = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?$"


_CLOCK_CELL: Final = r"^1899-12-31 \d{2}:\d{2}:\d{2}(?:\.\d+)?$"


#: Each date or time scalar's cell, as its text is shaped, and what an error calls it.
_MOMENT_CELLS: Final[Mapping[object, tuple[str, str]]] = {
    dt.date: (_DATE_CELL, "date cell"),
    dt.datetime: (_TIME_CELL, "date or time cell"),
    dt.time: (_CLOCK_CELL, "time cell"),
}


#: The type a loaded column is cast to, where fastexcel's is not its field's: a whole number is loaded as a float.
_SHEET_CASTS: Final[Mapping[object, pl.DataType]] = {
    int: pl.Int64(),
    dt.datetime: pl.Datetime("us"),
    dt.time: pl.Time(),
}


#: A boolean cell as fastexcel writes it as text.
_BOOLEAN_CELLS: Final = ("true", "false")


#: A number cell as fastexcel writes it as text: its digits rounded to nine decimals, no trailing zero, no ``+`` and no
#: exponent (``1e20`` is ``100000000000000000000``). A text cell its float load reads as a number (``1e3``, ``1.0``,
#: ``+5``, ``inf``) is written otherwise.
_NUMBER_CELL: Final = r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]{0,8}[1-9])?$"


#: A whole number's text and a decimal's, as :class:`NumberTextSheet` reads a text cell (``-12``; ``-1.03``, ``.5``).
_WHOLE_TEXT: Final = r"^-?[0-9]+$"


_DECIMAL_TEXT: Final = r"^-?(?:[0-9]+\.?[0-9]*|\.[0-9]+)$"


#: The whole numbers an ``int`` field holds, 64 bits' (a float load reads ``1e20`` as one), as floats: low, high.
_INT_RANGE: Final = (-(2.0**63), 2.0**63)


#: The date fastexcel reads Excel's serial 60 as, 1900-02-29 in Excel's 1900 date system, a day no calendar has: the
#: same as serial 59's.
_LEAP_DAY: Final = dt.date(1900, 2, 28)


def _whole(numbers: pl.Series) -> pl.Series:
    """Return whether each of *numbers*, floats, is a whole number an ``int`` field holds: 64 bits' (none is NaN)."""
    return (numbers == numbers.floor()) & numbers.is_between(*_INT_RANGE, closed="left")


def _refuse(text: pl.Series, wrong: pl.Series, what: str, row_one: int) -> None:
    """Raise ReadError for the first of *text*'s cells *wrong* marks (an empty one is none): it is not a *what*."""
    if (at := _first((text.is_not_null() & wrong).fill_null(value=False), row_one)) is not None:
        msg = f"the {text.name!r} cell {text[at - row_one]!r} is not a {what}: row {at}"
        raise ReadError(msg)


def _with_digits(text: pl.Series, numbers: pl.Series) -> pl.Series:
    """Return text cells *text* with each number cell's every digit, from *numbers*, a float load of them.

    fastexcel's text of a number cell rounds it to nine decimals; where that text is not the cell's number, the
    number's own text replaces it. A text cell its float load reads as a number (``1e3``) is that number: kept.
    """
    rounded = numbers.is_not_null() & (text.cast(pl.Float64, strict=False) != numbers)
    return pl.select(pl.when(rounded).then(numbers.cast(pl.String)).otherwise(text)).to_series().alias(text.name)


#: The methods a format reads its sheet otherwise by: a format that overrides any reads its sheet by two loads.
_ONE_LOAD_HOOKS: Final = (
    "_read", "_column", "_layout", "_label", "_value", "_end", "_outside", "_span", "_header_cells", "_headings",
    "_used_columns", "_typed", "_load", "_text_grid", "_completed", "_gapless", "_joined_header", "_stated",
    "_sheet_in", "_header_in", "_one_cell",
)  # fmt: skip


#: The methods a format finds its table's sheet or header row in each body by: one that overrides either places each.
_PLACING: Final = ("_sheet_in", "_header_in")


def _failed(e: BaseException) -> bool:
    """Return whether *e* is fastexcel's failure to read a body: its own error, or a panic of calamine's Rust code.

    A panic is pyo3's ``PanicException``, a ``BaseException`` (``except Exception`` misses it) that no module exports,
    so it is known by its name.
    """
    return isinstance(e, fastexcel.FastExcelError) or type(e).__name__ == "PanicException"


@contextlib.contextmanager
def _loading(what: str) -> Generator[None]:
    """Run fastexcel's calls on a body, its failure (:func:`_failed`) raised as a ReadError: *what*, then the error."""
    try:
        yield
    except BaseException as e:
        if not _failed(e):
            raise
        msg = f"{what}: {e}"
        raise ReadError(msg) from e


#: The most cells a sheet may span, its rows by its columns, and the most columns: calamine builds a sheet's whole span
#: as one dense grid, about 33 bytes a cell and more a column (a 200 KB ``.xls`` with one cell moved to row 65,536 and
#: column 256 took 860 MB, a 1.4 KB ``.xlsx`` with a cell at J1000000 567 MB), so a span past them is refused before a
#: load. 2^23 cells is 1,048,576 rows by 8 columns, 4,000 times the largest real sheet among the tests' (2,006 cells);
#: 16,384 columns is Excel's own limit. An ``.xls`` sheet's span runs from its first filled cell to its last, an
#: ``.xlsx`` sheet's from A1 to its last cell reference (a cell written empty, ``<c r="Z9"/>``, counts too).
_SPAN_MAX: Final = 2**23
_COLUMNS_MAX: Final = 16_384


#: An Excel 97 or 95 workbook's opening, a compound document's; the BIFF records of a cell calamine reads (a BLANK's or
#: MULBLANK's it does not), each opening with its row and column; MULRK, whose last column ends it; a sheet's BOF.
_COMPOUND: Final = bytes.fromhex("d0cf11e0a1b11ae1")
_BIFF_CELLS: Final = frozenset({0x0006, 0x00D6, 0x00FD, 0x0203, 0x0204, 0x0205, 0x027E})
_MULRK: Final = 0x00BD
_BOF: Final = 0x0809


def _refuse_span(rows: int, columns: int) -> None:
    """Raise ReadError for a sheet of *rows* by *columns*, past :data:`_SPAN_MAX` cells or :data:`_COLUMNS_MAX`."""
    if rows > 0 and (columns > _COLUMNS_MAX or rows * columns > _SPAN_MAX):  # a sheet of no cell spans none
        msg = (
            f"a sheet of the workbook spans {rows} rows by {columns} columns, past the {_SPAN_MAX} cells and"
            f" {_COLUMNS_MAX} columns a sheet is read to"
        )
        raise ReadError(msg)


def _check_span(body: bytes) -> None:
    """Refuse a workbook *body* one of whose sheets spans past :data:`_SPAN_MAX` cells or its columns, before a load.

    An ``.xlsx``'s sheets are bounded by :func:`_check_xlsx_span`; an ``.xls``'s by its BIFF records, walked (about
    0.3 µs each, a quarter of a second for a million cells). A body that is neither is fastexcel's to read or refuse.
    """
    if body.startswith(b"PK"):
        _check_xlsx_span(body)
    elif body.startswith(_COMPOUND):
        _check_xls_span(body)


def _check_xls_span(body: bytes) -> None:
    """Refuse an Excel 97 or 95 workbook *body* one of whose sheets spans past :data:`_SPAN_MAX` or its columns.

    A sheet's span runs from its first filled cell to its last, as calamine builds it (a BLANK cell is none).
    """
    try:
        document = compdoc.CompDoc(body, logfile=io.StringIO())
        stream = document.get_named_stream("Workbook") or document.get_named_stream("Book")
    # xlrd's checks of a corrupt document are its own error, struct's, a lookup's or an assert.
    except (compdoc.CompDocError, struct.error, LookupError, ValueError, AssertionError) as e:
        msg = f"the body is not a readable Excel workbook: {e!r}"
        raise ReadError(msg) from e
    if stream is None:  # no workbook in it, which fastexcel refuses
        return
    unpack, at, end = struct.Struct("<HHHH").unpack_from, 0, len(stream)
    spans: list[tuple[int, int]] = []
    low_row = low_column = 1 << 16
    high_row = high_column = -1
    while at + 8 <= end:
        kind, size, row, column = unpack(stream, at)
        at += 4 + size
        if kind == _BOF:  # a sheet's records begin
            spans.append((high_row - low_row + 1, high_column - low_column + 1))
            low_row = low_column = 1 << 16
            high_row = high_column = -1
        elif kind in _BIFF_CELLS or (kind == _MULRK and at <= end):
            last = int.from_bytes(stream[at - 2 : at], "little") if kind == _MULRK else column
            low_row, high_row = min(low_row, row), max(high_row, row)
            low_column, high_column = min(low_column, column), max(high_column, last)
    spans.append((high_row - low_row + 1, high_column - low_column + 1))
    for rows, columns in spans:
        _refuse_span(rows, columns)


#: An ``.xlsx`` part's tags that place a sheet's cells, ``<row>`` and ``<c>`` (with a namespace prefix or not), each
#: with its attributes, quoted values whole; an attribute; a cell reference (calamine reads ``j9`` as ``J9``).
_PLACING_TAG: Final = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?(row|c)(?![\w.:-])((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>")
_ATTRIBUTE: Final = re.compile(rb"([\w.:-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_CELL_REFERENCE: Final = re.compile(rb"([A-Za-z]+)([0-9]+)")
#: A workbook's ``<sheet>`` tag and a relationships part's ``<Relationship>``, each with its attributes.
_SHEET_TAG: Final = re.compile(rb"<(?:[\w.-]+:)?sheet(?![\w.:-])((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>")
_RELATIONSHIP_TAG: Final = re.compile(rb"<(?:[\w.-]+:)?Relationship(?![\w.:-])((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>")
#: What the scan of a sheet part counts, in one pass of polars' regex engine over each chunk of it: its ``<c>`` tags;
#: those that open on a reference of one letter and five digits at most, and of any; its ``r`` attributes, however
#: spaced; its ``<row>`` tags that open on a row number; and whether it holds a namespace-prefixed ``<c>`` or ``<row>``,
#: or an attribute after a quote with no space between (``"r=``), which only a tag-by-tag read places.
_SPAN_COUNTS: Final = {
    "cells": r"<c[\s/>]",
    "near": r'<c r="[A-Za-z][0-9]{1,5}"',
    "referenced": r'<c r="[A-Za-z]+[0-9]+"',
    "r": r"\sr\s*=",
    "rows": r'<row r="[0-9]+"',
    "odd": r"(?::(?:c|row)[\s/>]|[\"']r\s*=)",
}
_REFERENCE: Final = r'<c r="([A-Za-z]+)([0-9]+)"'
#: How much of a part the scan inflates at once, which a tag cannot span: a chunk ends before its last ``<``.
_CHUNK: Final = 1 << 22


def _column_number(letters: bytes) -> int:
    """Return the column *letters* name, 1-based (``A`` is 1, ``AA`` 27), as calamine reads them, in either case."""
    number = 0
    for letter in letters.upper():
        number = number * 26 + letter - ord("A") + 1
    return number


def _check_xlsx_span(body: bytes) -> None:
    """Refuse an ``.xlsx`` *body* one of whose sheets spans past :data:`_SPAN_MAX` cells or its columns.

    Each sheet the workbook lists is found as calamine finds it: its ``r:id`` in ``xl/workbook.xml``, that
    relationship's ``Target`` in ``xl/_rels/workbook.xml.rels`` (under ``xl/``, or from the root where it opens with
    ``/``), in any case, its entities read or not.
    Its span runs from A1 to its last cell's row and column: a cell's own reference, or, written without one, its
    place (the next column in its row, its row's ``r`` or the row after the last). A part is counted in one pass
    (:data:`_SPAN_COUNTS`, about 2 ms a MB with its inflating), and its references read where one passes column Z or row
    99,999; one written otherwise is read tag by tag. A part in another encoding than UTF-8 calamine refuses to load.
    A zip that will not open, or that holds no ``xl/workbook.xml`` and its rels (an ``.xlsb``, an ``.ods``), raises
    ReadError.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            for name in _xlsx_sheets(archive):
                _refuse_span(*_xlsx_span(archive, name))
    except ReadError:  # a ValueError, as zipfile's own errors are
        raise
    except _UNZIP_ERRORS as e:
        msg = f"the body is a workbook's zip that will not open: {e}"
        raise ReadError(msg) from e


def _xlsx_sheets(archive: zipfile.ZipFile) -> list[str]:
    """Return the members of *archive* that hold the sheets its workbook lists, as calamine finds them (any case)."""
    names = {name.lower(): name for name in archive.namelist()}
    book, links = names.get("xl/workbook.xml"), names.get("xl/_rels/workbook.xml.rels")
    if book is None or links is None:
        # calamine also reads an .xlsb and an .ods from a zip, whose sheets this bounds not: neither is read
        msg = "the body is a zip but no .xlsx workbook (an .xlsb or .ods is not read): it has no xl/workbook.xml"
        raise ReadError(msg)
    ids = {
        value
        for tag in _SHEET_TAG.finditer(_inflated(archive, book))
        for name, value in _attributes(tag[1])
        if name.rpartition(b":")[2] == b"id"
    }
    sheets = []
    for tag in _RELATIONSHIP_TAG.finditer(_inflated(archive, links)):
        attributes = dict(_attributes(tag[1]))
        if attributes.get(b"Id") in ids and (target := attributes.get(b"Target")) is not None:
            written = target.decode("utf-8", "replace")
            paths = [path[1:] if path.startswith("/") else f"xl/{path}" for path in {written, html.unescape(written)}]
            sheets.extend(names[path.lower()] for path in paths if path.lower() in names)
    return sheets


def _attributes(tag: bytes) -> list[tuple[bytes, bytes]]:
    """Return the attributes in a tag's *tag* text, in order: each name and value, as written (no entity read)."""
    return [(match[1], match[2] if match[2] is not None else match[3]) for match in _ATTRIBUTE.finditer(tag)]


def _xlsx_span(archive: zipfile.ZipFile, name: str) -> tuple[int, int]:
    """Return the rows and columns sheet part *name* spans, from A1 to its last cell (:func:`_check_xlsx_span`).

    Where every ``<c>`` opens on its reference, and no ``r`` attribute but those and the rows' is written, each cell is
    its reference: one letter and five digits at most bound the span by 99,999 rows and 26 columns, and any other is
    found from the references. Any other part is placed tag by tag (:func:`_placed_span`).
    """
    counts = dict.fromkeys(_SPAN_COUNTS, 0)
    with archive.open(name) as part:
        for chunk in _chunks(part):
            text = pl.DataFrame({"text": [chunk.decode("utf-8", "replace")]})
            found = text.select(
                pl.col("text").str.count_matches(pattern).alias(key) for key, pattern in _SPAN_COUNTS.items()
            )
            for key, count in found.row(0, named=True).items():
                counts[key] += count
    if counts["odd"] or counts["cells"] != counts["referenced"] or counts["r"] != counts["cells"] + counts["rows"]:
        return _placed_span(archive, name)
    if counts["near"] == counts["cells"]:
        return 99_999, 26
    rows = columns = 0
    with archive.open(name) as part:
        for chunk in _chunks(part):
            text = pl.Series([chunk.decode("utf-8", "replace")])
            parts = text.str.extract_all(_REFERENCE).explode().str.extract_groups(_REFERENCE)
            # The largest of each, as text: a longer number (its leading zeros dropped), or name, is the larger.
            widest = (
                parts.struct.field("2").str.strip_chars_start("0").str.pad_start(24, "0").max(),
                parts.struct.field("1").str.to_uppercase().str.pad_start(24, "@").max(),
            )
            rows = max(rows, int(str(widest[0] or 0)))
            columns = max(columns, _column_number(str(widest[1] or "").lstrip("@").encode()))
    return rows, columns


def _chunks(part: typing.IO[bytes]) -> Iterator[bytes]:
    """Yield *part* inflated, each chunk ending before the last ``<`` it holds, so that no tag spans two."""
    rest = b""
    while chunk := part.read(_CHUNK):
        chunk = rest + chunk
        cut = max(chunk.rfind(b"<"), 0)  # no "<" past the first: the chunk waits for the next
        if cut:
            yield chunk[:cut]
        rest = chunk[cut:]
    yield rest


def _placed_span(archive: zipfile.ZipFile, name: str) -> tuple[int, int]:
    """Return sheet part *name*'s span, each ``<row>`` and ``<c>`` placed as calamine places it, the last ``r`` winning.

    A row is its ``r``, or the one after the last; a cell is its reference, or the next column in its row; a prefixed
    attribute (``x:r``) places nothing, and a reference calamine cannot read raises in its load.
    """
    rows = columns = row = column = 0
    with archive.open(name) as part:
        for chunk in _chunks(part):
            for tag in _PLACING_TAG.finditer(chunk):
                place = [value for key, value in _attributes(tag[2]) if key == b"r"]
                if tag[1] == b"row":
                    row = int(place[-1]) if place and place[-1].isdigit() else row + 1
                    column = 0
                    rows = max(rows, row)
                    continue
                reference = _CELL_REFERENCE.fullmatch(place[-1]) if place else None
                if reference is None:
                    column += 1
                    rows = max(rows, row)
                else:
                    column = _column_number(reference[1])
                    rows = max(rows, int(reference[2]))
                columns = max(columns, column)
    return rows, columns


def _workbook(body: bytes) -> fastexcel.ExcelReader:
    """Return the workbook *body*, open, or raise ReadError for a body that is no workbook or spans too far.

    An Excel 97 or 95 workbook's sheet whose span calamine would build past :data:`_SPAN_MAX` cells is refused first.
    """
    _check_span(body)
    with _loading("the body is not a workbook"):
        return fastexcel.read_excel(body)


def _of_kind(column: Column, cells: pl.Series) -> bool:
    """Return whether each of a strict load's *cells* is one *column* holds, as the two loads read its cells."""
    dtype, scalar = cells.dtype, column.scalar
    if column.written is not None or column.flag is not None or scalar is str:
        return dtype == pl.String
    if scalar in {int, float}:
        # fastexcel's strict load reads a text cell ``nan`` as NaN among number cells, which no number cell holds.
        return dtype == pl.Float64 and bool(_whole(cells).all() if scalar is int else not cells.is_nan().any())
    if scalar in _MOMENT_CELLS:
        shape = _MOMENT_CELLS[scalar][0]
        return isinstance(dtype, pl.Datetime) and bool(
            cells.dt.strftime("%Y-%m-%d %H:%M:%S").drop_nulls().str.contains(shape).all()
        )
    return scalar is bool and dtype == pl.Boolean


def _reads_numbers(column: Column) -> bool:
    """Return whether *column* reads number cells: an ``int`` or ``float`` field's, not a ``Flag``'s."""
    return column.scalar in {int, float} and column.flag is None and column.written is None


def _near_leap_day(column: Column, values: pl.Series) -> bool:
    """Return whether *column* reads date cells and *values*, its date or datetime values, fall on :data:`_LEAP_DAY`."""
    if column.scalar not in {dt.date, dt.datetime} or column.written is not None or column.flag is not None:
        return False
    return bool((values.cast(pl.Date) == _LEAP_DAY).any())


def _cell_at(frame: pl.DataFrame, row: int, at: int) -> pl.Series:
    """Return *frame*'s cell in its row *row* (0-based) and sheet column *at*: a series of one, null where none."""
    name = str(at)
    return frame[name].slice(row, 1) if name in frame.columns and 0 <= row < frame.height else pl.Series([None])


class SheetFile[RowT: msgspec.Struct](File[RowT]):
    """A clean table on one sheet of a workbook, read by fastexcel with the types its row struct's fields lay out."""

    _by_cell = True

    #: Whether the table opens with a header row: a format whose sheet writes none (GDPNow's ticker map) sets it false,
    #: and its fields are read by position, their names their labels, from the row under ``header_row`` to the sheet's
    #: end: the rows down to ``header_row`` are skipped, unread, and ``header_row=0`` reads from row 1.
    _header: typing.ClassVar[bool] = True
    #: Whether an empty row inside the table is a gap, dropped: a format whose sheet leaves one (Cboe's VXO archive,
    #: between two days) sets it; otherwise an empty row raises.
    _gaps: typing.ClassVar[bool] = False
    #: Whether an empty row inside the table is a row of no values, kept: a format numbering its rows by place, blank
    #: ones too (CFTC's Swaps Report tables of 2012 to 2014), sets it.
    _blank_rows: typing.ClassVar[bool] = False
    #: Whether a cell of only whitespace is empty (EIA leaves blank-looking cells), as fastexcel's
    #: ``whitespace_as_null`` reads it. The header is then read as the sheet writes it, so a heading written twice is
    #: twice (fastexcel would name the second ``East_1``).
    _trim: typing.ClassVar[bool] = False
    #: Whether a header cell is read stripped of its whitespace (EIA pads some headings). The header is then read from
    #: the sheet's own row, as :attr:`_trim`'s is: a date, time or boolean header cell is its text (``2026-01-02
    #: 00:00:00``, ``true``), and a label written twice is twice, not numbered.
    _strip: typing.ClassVar[bool] = False
    #: How many rows right over the header row head its columns too (EIA's ``Sourcekey`` row over its descriptions): a
    #: cell heads its own column and every column to its right with no cell of its own. Each column's header is then
    #: its cells top to bottom, the unit separator (U+001F) between, read as written (stripped; a blank cell is empty),
    #: and a reader that lays out the key columns (:class:`LongFile`) takes them from it.
    _above: typing.ClassVar[int] = 0
    #: The number fields whose cells are read from their text, not checked as number cells: ``True``, every one
    #: (:class:`NumberTextSheet`), or the fields a format's own :meth:`_value` reads from text on purpose (a figure
    #: written ``-1,249,056*``, a number cell under a date style). Any other number field's cell that is no number cell
    #: (text written as one, ``2``) raises, a load more telling it from a number cell.
    _number_text: typing.ClassVar[bool | Collection[str]] = False

    def __init__(self, row: type[RowT], *, sheet: str, header_row: int = 1, null_values: Sequence[str] = ()) -> None:
        """Lay out a table of *row* on the sheet named *sheet*, under its header in sheet row *header_row* (Excel's).

        Each field names its column with a :class:`~flatread.fields.Label`. A date or time field without a
        :class:`~flatread.fields.Format` holds the sheet's date cells; with one, text written in that
        format. *null_values* are the sheet's text for no value (``#N/A``, ``NA``). Errors name the sheet's own rows.
        Raises ``LayoutError`` where the :attr:`_above` rows do not fit over *header_row*.
        """
        super().__init__(row)
        self._row_one = header_row + 1
        _check_above(self._above, header_row)
        self._sheet = sheet
        self._header_row = header_row
        self._null_values = list(null_values)

    @typing.override
    def _joined_header(self) -> bool:
        return bool(self._above)

    #: The workbook open for one body and its sheet's text grid, on a reader made for that body (:meth:`_placed`);
    #: ``None`` on a shared reader, whose :meth:`_read` opens the body itself.
    _opened: fastexcel.ExcelReader | None = None
    _opened_grid: pl.DataFrame | None = None
    #: Whether this reader is one read's own copy (:meth:`frame`'s), which takes the rows of the body it reads.
    _reading: bool = False

    @typing.override
    def frame(self, body: bytes) -> pl.DataFrame:
        # A copy of this reader reads the body, so the header row a format finds in it (:meth:`_header_in`), which its
        # errors' rows count from, is this read's alone: the reader is shared, by every thread.
        reading = object.__new__(type(self))
        vars(reading).update(vars(self), _reading=True)
        return super(SheetFile, reading).frame(body)

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        reader = self._opened or _workbook(body)
        if (frame := self._read_first(reader, body)) is not None:
            return frame
        # Two loads: the sheet's every cell as text (the grid), which says what a cell holds and serves all above and
        # around the table, and the table's cells typed, which fastexcel coerces without a word (a text cell is null as
        # a number, or the number its text parses as). A text field's column is loaded as floats: its number cells'
        # every digit.
        grid = self._opened_grid if self._opened_grid is not None else self._text_grid(reader, self._sheet)
        if self._header and grid.height < self._header_row:
            msg = f"the {self._sheet!r} sheet ends at row {grid.height}, before its header row {self._header_row}"
            raise ReadError(msg)
        span = self._span(grid)
        used = self._used_columns()
        table = span if used is None else [str(at) for at in used]
        if not set(table) <= set(span):  # sets: a sheet may be 16,384 columns wide
            msg = f"the {self._sheet!r} sheet has no column {used!r} of its table"
            raise ReadError(msg)
        written = bool(self._above) or self._trim or self._strip  # the header as the sheet's own row writes it
        if written and used is None and span and span[0] != grid.columns[0]:
            # Headings read from the sheet's first column: a table that starts after cells of its top rows is refused.
            msg = (
                f"the {self._sheet!r} sheet's header row holds {grid.columns.index(span[-1]) + 1} columns,"
                f" not the {len(span)} loaded"
            )
            raise ReadError(msg)
        cells = grid.select(table)
        if self._trim:  # as fastexcel's whitespace_as_null: a cell of only whitespace is empty
            cells = cells.with_columns(pl.when(pl.col(pl.String).str.strip_chars() != "").then(pl.col(pl.String)))
        # A sheet without a header: no row is one, and its rows begin under header_row.
        named = (
            (self._headings(cells) if written else self._header_cells(cells)) if self._header else [""] * cells.width
        )
        kept = [at for at, label in enumerate(named) if label not in self._unread]
        header = [named[at] for at in kept]
        columns = self._columns if (layout := self._layout(header)) is None else layout
        # The numbered field holds the sheet's row, under no column.
        columns = [column for column in columns if column.field != self._numbered and column.cell is None]
        # The header's names number a label's repeats by their place (``Rate``, ``Rate_1``), as fastexcel names them.
        labels = (
            [column.label for column in columns]
            if written
            else _repeated([column.label for column in columns], "{label}_{n}", 1)
        )
        if not self._header and len(header) > len(columns):
            msg = f"the {self._sheet!r} sheet's table is {len(header)} columns wide, not {len(columns)}"
            raise ReadError(msg)
        # A header-less sheet's last columns, empty all down, are no columns of its (calamine trims them): no values.
        columns, absent = columns[: len(header)], columns[len(header) :]
        if self._header and header != labels:
            msg = f"the {self._sheet!r} sheet's header (row {self._header_row}) {header} is not {labels}"
            raise ReadError(msg)
        text = cells.slice(self._header_row).select(pl.nth(kept))
        text.columns = [column.field for column in columns]
        # By position, not label: an empty header cell is no name. A position is the sheet's column, which fastexcel
        # keys a dtype by: a table may open past column A.
        placed = dict(zip(kept, columns, strict=True))
        typed, stated = self._typed(reader, grid, {int(name): placed.get(at) for at, name in enumerate(table)}, used)
        typed = typed.select(table).select(pl.nth(kept))
        typed.columns = text.columns
        # A cell holds no value when it is empty, an empty string or one of the sheet's words for none.
        text = text.with_columns(pl.when(~pl.col(pl.String).is_in(["", *self._null_values])).then(pl.col(pl.String)))
        end = self._end(text)
        self._outside(grid, end)
        text, typed = text.head(end), typed.head(end)
        empty = text.select(pl.all_horizontal(pl.all().is_null())).to_series()
        if not (self._gaps or self._blank_rows) and (at := _first(empty, self._row_one)) is not None:
            msg = f"the {self._sheet!r} sheet's row {at} is empty, and a table's rows run to the sheet's end"
            raise ReadError(msg)
        # Each column's cells under its label, which errors name, or its field's name where its header cell is empty.
        frame = pl.DataFrame(
            [
                self._value(column, text[column.field].alias(column.label or column.field), typed[column.field])
                for column in columns
            ]
        )
        self._refuse_misread(reader, grid, frame, text, [(column, int(table[at])) for at, column in placed.items()])
        frame = self._completed(frame, absent, stated)
        # Dropped once typed and numbered, so an error and a numbered row still name the sheet's own row.
        return self._gapless(frame, empty) if self._gaps else frame

    def _read_first(self, reader: fastexcel.ExcelReader, body: bytes) -> pl.DataFrame | None:
        """Return the table of the workbook open in *reader* read but by this reader's two loads, or ``None``.

        A format that finds its sheet or its header in each body (:meth:`_sheet_in`, :meth:`_header_in`) has a reader
        of this body's read it (:meth:`_placed`), here, so a zip's member (``ZippedFile``) is placed as a bare workbook
        is; a plain table is read from one load (:meth:`_one_load`).
        """
        if self._opened is None and any(getattr(type(self), hook) is not getattr(SheetFile, hook) for hook in _PLACING):
            placed = self._placed(reader)
            if self._reading:  # frame()'s copy: its checks name the body's rows
                self._row_one = placed._row_one  # noqa: SLF001 (a reader of this class's)
            # This class's read, not the format's: the pieces before it (a zip's) read the body down to the workbook.
            return SheetFile._read(placed, body)
        return self._one_load(reader) if self._opened_grid is None else None

    def _one_loadable(self) -> bool:
        """Return whether a table may be read from one strict load: a plain table, by none of the hooks or switches."""
        labels = [column.label for column in self._columns if column.field != self._numbered]
        return (
            self._header
            and not (self._above or self._trim or self._strip or self._gaps or self._blank_rows)
            and not (self._null_values or self._unread or self._blank_columns)
            and all(column.cell is None for column in self._columns)
            and "" not in labels
            and len(set(labels)) == len(labels)
            and all(getattr(type(self), hook) is getattr(SheetFile, hook) for hook in _ONE_LOAD_HOOKS)
        )

    def _one_load(self, reader: fastexcel.ExcelReader) -> pl.DataFrame | None:
        """Return the table read from one load, or ``None`` where the two loads must read it: a read's fast path.

        fastexcel's strict load types each column by every one of its cells and raises for a column that mixes kinds
        (a ``.`` among numbers), so a column of one kind, its field's, needs no text load to say what each cell holds.
        Anything else (a mixed column, a header or a cell the two loads refuse) is ``None``, and the two loads raise
        their own error. A sheet that holds tokens for none (``null_values``) mixes kinds and goes to them directly.
        """
        if not self._one_loadable() or self._sheet not in reader.sheet_names:
            return None
        columns = [column for column in self._columns if column.field != self._numbered]
        try:
            loaded = reader.load_sheet(
                self._sheet, header_row=self._header_row - 1, dtype_coercion="strict", schema_sample_rows=None
            )
            table = loaded.to_polars()
        except BaseException as e:  # a mixed column, or a cell calamine panics on typed: the text load may read it
            if not _failed(e):
                raise
            return None
        # fastexcel names a column by its header cell, a repeat by its place (Rate_1) and an empty or date cell
        # __UNNAMED__n, so a stray cell beside the table or a header that is not the labels is a mismatch.
        if table.columns != [column.label for column in columns]:
            return None
        values = [self._one_kind(column, table[column.label]) for column in columns]
        if any(value is None for value in values):
            return None
        frame = pl.DataFrame([value for value in values if value is not None])
        if frame.select(pl.all_horizontal(pl.all().is_null()).any()).item():  # an empty row, which the loads refuse
            return None
        # The strict load reads a text cell of a word for none (``NA``, ``null``, ``#N/A``) as none, as an empty cell:
        # a column holding none is loaded as text too, which tells them apart (one load more, only then).
        held = {
            name: loaded.selected_columns[at].absolute_index
            for at, name in enumerate(table.columns)
            if table[name].null_count()
        }
        return self._completed(frame, [], []) if not held or self._empty_where_none(reader, table, held) else None

    def _empty_where_none(self, reader: fastexcel.ExcelReader, table: pl.DataFrame, held: Mapping[str, int]) -> bool:
        """Return whether each cell the strict load *table* reads as none in its columns *held* is empty.

        *held* maps each column's name to its sheet column (``A`` is 0). An empty cell's text is none or ``""``; any
        other (``NA``) is a text cell, which the two loads name.
        """
        texts = self._load(
            reader, self._sheet, skip_rows=self._header_row, dtypes="string", use_columns=sorted(held.values())
        )
        # A row the text load ends before holds no text: an empty cell in each column.
        rows = min(texts.height, table.height)
        found = texts.head(rows).select(
            pl.any_horizontal(
                table[name].head(rows).is_null() & pl.col(str(at)).is_not_null() & (pl.col(str(at)) != "")
                for name, at in held.items()
            ).any()
        )
        return not found.item()

    def _one_kind(self, column: Column, cells: pl.Series) -> pl.Series | None:
        """Return *column*'s values from its strictly loaded *cells*, or ``None`` where they are not all its kind."""
        if cells.null_count() == cells.len():
            return pl.Series(column.field, [None] * cells.len(), _none_dtype(column.scalar))
        if not _of_kind(column, cells) or _near_leap_day(column, cells):
            return None
        if column.written is not None or column.flag is not None or column.scalar is str:
            text = pl.select(pl.when(cells != "").then(cells)).to_series().alias(column.label)
            try:
                return self._value(column, text, text)
            except ValueError:  # a cell its Format or Flag refuses: the two loads name it
                return None
        dtype = pl.Date() if column.scalar is dt.date else _SHEET_CASTS.get(column.scalar, cells.dtype)
        return cells.cast(dtype).alias(column.field)

    def _span(self, grid: pl.DataFrame) -> list[str]:
        """Return the *grid*'s columns the table spans, as fastexcel loads a table under its header.

        They run from the first to the last column holding a cell from the header row down (a title's column left of
        the table is none of them); a sheet without a header spans every column.
        """
        held = grid.slice(self._header_row - 1).select(pl.all().is_not_null().any()).row(0) if grid.width else ()
        if not self._header or not (filled := [at for at, cell in enumerate(held) if cell]):
            return grid.columns
        return grid.columns[filled[0] : filled[-1] + 1]

    def _typed(
        self,
        reader: fastexcel.ExcelReader,
        grid: pl.DataFrame,
        read: Mapping[int, Column | None],
        used: list[int] | None,
    ) -> tuple[pl.DataFrame, list[pl.Expr]]:
        """Load the table's cells typed, the rows under its header, and read its ``Cell`` fields: a read's second load.

        *read* maps each of the table's sheet columns to the column it reads there (``None``: none); the frame holds
        every column loaded, named by place. A Cell typed by its cell (a date's) is loaded with the table, from its row,
        where the table types its column alike or reads none there. Those over columns the table types otherwise (a
        date over numbers) share one load more, each of its column, and a second of them over one column of another
        type a load of its own. Any other Cell is its text, the *grid*'s.
        """
        dtypes: fastexcel.DTypeMap = {
            at: "string" if column is None or column.written is not None else _SHEET_DTYPES[column.scalar]
            for at, column in read.items()
        }
        claimed: dict[int, fastexcel.DType] = {}
        aside: dict[int, fastexcel.DType] = {}
        where: dict[str, Literal["table", "aside", "own"]] = {}  # the load each typed Cell's field is read from
        for column in self._columns:
            if column.cell is None or column.written is not None or column.scalar is str:
                continue
            at, dtype = column.cell[1], _SHEET_DTYPES[column.scalar]
            if str(at) not in grid.columns:  # a cell past the sheet's columns is empty: its text says so
                continue
            held = dtypes[at] if read.get(at) is not None else claimed.setdefault(at, dtype)
            where[column.field] = (
                "table" if held == dtype else "aside" if aside.setdefault(at, dtype) == dtype else "own"
            )

        def first(load: str) -> int:
            return min(
                column.cell[0] - 1 for column in self._columns if column.cell and where.get(column.field) == load
            )

        start = min(self._header_row, first("table")) if "table" in where.values() else self._header_row
        loaded = None if used is None else [*used, *sorted(claimed.keys() - set(used))]
        typed = self._load(reader, self._sheet, skip_rows=start, dtypes=dtypes | claimed, use_columns=loaded)
        beside, top = pl.DataFrame(), first("aside") if aside else 0
        if aside:
            beside = self._load(
                reader, self._sheet, skip_rows=top, dtypes=dict(aside.items()), use_columns=sorted(aside)
            )
        stated = []
        for column in self._columns:
            if column.cell is None:
                continue
            row, at = column.cell
            text = _cell_at(grid, row - 1, at)
            match where.get(column.field):
                case "table":
                    value = _cell_at(typed, row - 1 - start, at)
                case "aside":
                    value = _cell_at(beside, row - 1 - top, at)
                case "own":
                    value = self._one_cell(reader, row, at, {at: _SHEET_DTYPES[column.scalar]})
                case _:
                    value = text
            stated.append(self._stated(column, text, value))
        return typed.slice(self._header_row - start), stated

    def _frame_on(self, reader: fastexcel.ExcelReader, body: bytes) -> pl.DataFrame:
        """Read *body*'s table as :meth:`frame` does, from *reader*, the workbook open for it: one open, many sheets."""
        return self._placed(reader).frame(body)

    def _placed(self, reader: fastexcel.ExcelReader) -> typing.Self:
        """Return a reader of the body open in *reader*: its sheet :meth:`_sheet_in`'s, its header :meth:`_header_in`'s.

        The shared reader is left as it is, read by any thread; the one returned reads the open workbook and its grid.
        """
        sheet = self._sheet_in(reader)
        grid = self._text_grid(reader, sheet)
        row = self._header_in(grid)
        placed = object.__new__(type(self))
        vars(placed).update(
            vars(self), _opened=reader, _opened_grid=grid, _sheet=sheet, _header_row=row, _row_one=row + 1
        )
        return placed

    def _sheet_in(self, reader: fastexcel.ExcelReader) -> str:
        """Return the name of the table's sheet in the workbook *reader*: the format's."""
        del reader
        return self._sheet

    def _header_in(self, grid: pl.DataFrame) -> int:
        """Return the sheet row of the table's header in the sheet's *grid*: the format's."""
        del grid
        return self._header_row

    def _text_grid(self, reader: fastexcel.ExcelReader, sheet: str) -> pl.DataFrame:
        """Return *sheet*'s every cell as text, a row per sheet row from row 1, a column per sheet column by place."""
        if sheet not in reader.sheet_names:
            msg = f"the workbook has no sheet {sheet!r}; its sheets are {reader.sheet_names!r}"
            raise ReadError(msg)
        return self._load(reader, sheet, skip_rows=0, dtypes="string", use_columns=None)

    def _completed(self, frame: pl.DataFrame, absent: list[Column], stated: list[pl.Expr]) -> pl.DataFrame:
        """Return the table's typed *frame* with the fields no column holds: its row, the *absent*, the *stated*."""
        if self._numbered is not None:
            numbers = pl.int_range(self._row_one, self._row_one + frame.height, dtype=pl.Int64, eager=True)
            frame = frame.insert_column(0, numbers.alias(self._numbered))
        frame = frame.with_columns(pl.lit(None, _none_dtype(column.scalar)).alias(column.field) for column in absent)
        frame = frame.with_columns(stated)
        # A wide table: its fields in the struct's order.
        return frame.select(self._fields) if set(frame.columns) == set(self._fields) else frame

    def _gapless(self, frame: pl.DataFrame, empty: pl.Series) -> pl.DataFrame:
        """Return *frame* without the rows *empty* marks: a format that needs the sheet's row numbers keeps them."""
        return frame.filter(~empty)

    def _load(
        self,
        reader: fastexcel.ExcelReader,
        sheet: str,
        *,
        skip_rows: int,
        dtypes: fastexcel.DType | fastexcel.DTypeMap,
        use_columns: list[int] | None,
    ) -> pl.DataFrame:
        """Load *sheet* by fastexcel from sheet row *skip_rows* + 1, its columns named by place (``"0"`` is ``A``).

        fastexcel's own failure, or calamine's panic, is a ReadError.
        """
        with _loading(f"the {sheet!r} sheet cannot be loaded as the table"):  # a cut sheet, a cell calamine panics on
            loaded = reader.load_sheet(
                sheet, header_row=None, skip_rows=skip_rows, dtypes=dtypes, use_columns=use_columns
            )
            frame = loaded.to_polars()  # fastexcel loads lazily
            frame.columns = [str(column.absolute_index) for column in loaded.selected_columns]
        return frame

    def _refuse_misread(
        self,
        reader: fastexcel.ExcelReader,
        grid: pl.DataFrame,
        frame: pl.DataFrame,
        text: pl.DataFrame,
        columns: Sequence[tuple[Column, int]],
    ) -> None:
        """Refuse a cell the two loads read as another (:meth:`_refuse_number_text`, :meth:`_refuse_leap_day`)."""
        self._refuse_number_text(reader, grid, frame, text, columns)
        self._refuse_leap_day(reader, frame, columns)

    def _number_cells(self, column: Column) -> bool:
        """Return whether *column*'s cells must be number cells: a number field's that :attr:`_number_text` omits."""
        text = self._number_text
        return _reads_numbers(column) and text is not True and column.field not in (text or ())

    def _refuse_number_text(
        self,
        reader: fastexcel.ExcelReader,
        grid: pl.DataFrame,
        frame: pl.DataFrame,
        text: pl.DataFrame,
        columns: Sequence[tuple[Column, int]],
    ) -> None:
        """Refuse a number field's text cell that holds a value: one written as a number (``2``) reads as it typed.

        No text tells such a cell from a number cell, so the table's number columns, and the cells its number ``Cell``
        fields state, are loaded once more, as booleans: fastexcel reads a number cell so (``0`` is false) and a text
        cell as none. *frame* holds the table's values and *text* its cells (both a row per sheet row under the header),
        *grid* the sheet's text; *columns*, each column and its sheet column (``A`` is 0). A format whose numbers are
        written as text (:attr:`_number_text`) loads nothing.
        """
        numbers = [(column, at) for column, at in columns if self._number_cells(column)]
        cells = [
            (column, column.cell)
            for column in self._columns
            if column.cell is not None and self._number_cells(column) and str(column.cell[1]) in grid.columns
        ]
        if not (numbers or cells):
            return
        top = min([self._header_row, *(row - 1 for _, (row, _) in cells)])
        ats = sorted({*(at for _, at in numbers), *(at for _, (_, at) in cells)})
        kinds = self._load(reader, self._sheet, skip_rows=top, dtypes=dict.fromkeys(ats, "boolean"), use_columns=ats)
        for column, at in numbers:
            kind = kinds[str(at)].slice(self._header_row - top, frame.height)
            kind = kind.extend_constant(None, frame.height - kind.len())  # the sheet's last rows, empty here
            held = frame[column.field].is_not_null() & kind.is_null()
            _refuse(text[column.field].alias(column.label or column.field), held, "number cell", self._row_one)
        for column, (row, at) in cells:
            cell = _cell_at(grid, row - 1, at).cast(pl.String).str.strip_chars(" ")[0]
            if cell not in {None, "", *self._null_values} and _cell_at(kinds, row - 1 - top, at)[0] is None:
                msg = f"the {self._sheet!r} sheet's cell {column.label!r} {cell!r} is not a number cell"
                raise ReadError(msg)

    def _refuse_leap_day(
        self, reader: fastexcel.ExcelReader, frame: pl.DataFrame, columns: Sequence[tuple[Column, int]]
    ) -> None:
        """Refuse a date cell of the table's on Excel's serial 60, 1900-02-29, which its 1900 date system counts.

        No calendar has that day, and fastexcel reads it as serial 59's, 1900-02-28. *frame* holds the table's
        columns, a row per sheet row under the header; *columns*, each column and its sheet column (``A`` is 0). A
        column holding 1900-02-28 is loaded once more, as durations, which are its cells' serials; any other costs
        nothing. A 1904 workbook's serial 60 is 1904-03-01.
        """
        for column, at in columns:
            if not _near_leap_day(column, frame[column.field]):
                continue
            durations = self._load(
                reader, self._sheet, skip_rows=self._header_row, dtypes={at: "duration"}, use_columns=[at]
            )
            serials = durations.to_series().head(frame.height).dt.total_days()
            if (row := _first(serials == 60, self._row_one)) is not None:  # noqa: PLR2004 (the day itself)
                msg = (
                    f"the {column.label!r} cell on row {row} is Excel's day 60, 1900-02-29, which its 1900 date system"
                    " counts and no calendar has"
                )
                raise ReadError(msg)

    @typing.override
    def _label(self, name: str) -> str | None:
        return name if not self._header or name == self._numbered else super()._label(name)

    def _header_cells(self, cells: pl.DataFrame) -> list[str]:
        """Return the header's names, from the table's *cells*: its row's cells as fastexcel names its columns.

        A date, time or boolean cell names no column, as an empty one does (``""``), and a label's repeats are numbered
        by their place (``Rate``, ``Rate_1``). A date or time cell's text is its shape (``2026-10-29 00:00:00``), which
        text written so shares: it names nothing too.
        """
        return _repeated(
            [
                "" if cell is None or cell in _BOOLEAN_CELLS or re.fullmatch(_TIME_CELL, cell) else cell
                for cell in cells.row(self._header_row - 1)
            ],
            "{label}_{n}",
            1,
        )

    def _stated(self, column: Column, text: pl.Series, typed: pl.Series) -> pl.Expr:
        """Return the value *column*'s cell states, its *text* and its *typed* cell, read as a cell of its column is."""
        text = text.cast(pl.String).replace(["", *self._null_values], None).alias(column.label)
        try:
            value = self._value(column, text, typed)
        except ValueError as e:
            msg = f"the {self._sheet!r} sheet's cell {column.label!r} is not its {column.field}: {e}"
            raise ReadError(msg) from e
        # An empty cell (none past the sheet's columns) loads untyped: no value of the field's type.
        dtype = _none_dtype(column.scalar) if value.dtype == pl.Null else value.dtype
        return pl.lit(value[0], dtype).alias(column.field)

    def _one_cell(
        self, reader: fastexcel.ExcelReader, row: int, at: int, dtypes: fastexcel.DType | fastexcel.DTypeMap
    ) -> pl.Series:
        """Return the cell at sheet *row* and column *at*, loaded with *dtypes*: a series of one, null where empty."""
        with _loading(f"the {self._sheet!r} sheet's cell cannot be loaded"):
            loaded = reader.load_sheet(
                self._sheet, header_row=None, skip_rows=row - 1, n_rows=1, use_columns=[at], dtypes=dtypes
            ).to_polars()
        return loaded.to_series(0) if loaded.height else pl.Series([None], dtype=pl.String)

    def _used_columns(self) -> list[int] | None:
        """Return the sheet's columns the table is read from, 0-based (``A`` is 0): ``None``, all."""
        return None

    def _end(self, text: pl.DataFrame) -> int:
        """Return how many of *text*'s rows, the sheet's under its header, are the table's: all of them."""
        return text.height

    def _outside(self, grid: pl.DataFrame, end: int) -> None:
        """Refuse what the sheet's *grid* holds outside the table, above its header or past its first *end* rows: none.

        The grid is every cell as text, a row per sheet row from row 1, its columns named by place (``"0"`` is ``A``).
        """

    def _headings(self, cells: pl.DataFrame) -> list[str]:
        """Return each column's header from the table's *cells* over it and in its header row, U+001F between them."""
        # A cell is as written, or stripped (always where rows head the columns, which join as a stack of cells).
        strip = self._strip or bool(self._above)
        return _stacked(
            [
                [None if cell is None else (cell.strip() if strip else cell) or None for cell in cells.row(row - 1)]
                for row in range(self._header_row - self._above, self._header_row + 1)
            ]
        )

    def _value(self, column: Column, text: pl.Series, typed: pl.Series) -> pl.Series:
        """Return *column*'s values, refusing a cell that is not one of its type, its row the sheet's own."""
        if column.written is not None:
            return _moment(text, _temporal(column.scalar, column.written), column.written, self._row_one).alias(
                column.field
            )
        if column.scalar is str:
            # The two loads' typed cells are a float load of the column (a number cell's every digit); one load's and a
            # Cell's are its text.
            return (_with_digits(text, typed) if typed.dtype == pl.Float64 else text).alias(column.field)
        if column.flag is not None:
            return _flagged(text, column.flag, self._row_one).alias(column.field)
        cell, number = pl.col("text"), pl.col("typed")
        if column.scalar in _MOMENT_CELLS:
            # A text cell written like a date cell has the shape, and fastexcel reads it as no date.
            shape, what = _MOMENT_CELLS[column.scalar]
            checks = [(~cell.str.contains(shape) | number.is_null(), what)]
        elif column.scalar is bool:
            # fastexcel reads a number cell as a boolean (1 is true), and a text cell as none; a boolean cell's text is
            # its word.
            checks = [(~cell.is_in(_BOOLEAN_CELLS) | number.is_null(), "boolean cell")]
        else:
            # ... a boolean cell as a number (true is 1.0), and a text cell as the number its text is (``1e3``). A
            # number cell's text is fastexcel's of a number; a text cell written so (``2``) is refused by its kind
            # (:meth:`_refuse_number_text`).
            wrong = number.is_null() | ~cell.str.contains(_NUMBER_CELL)
            checks = (
                [
                    (wrong | (number != number.floor()), "whole number"),
                    (~number.is_between(*_INT_RANGE, closed="left"), "whole number of 64 bits"),
                ]
                if column.scalar is int
                else [(wrong, "number")]
            )
        # One pass over the cells: each check's mask (named by place: no field's name), and the values (none where the
        # cell is empty).
        held = cell.is_not_null()
        found = pl.DataFrame([text.alias("text"), typed.alias("typed")]).select(
            *((held & wrong).fill_null(value=False).alias(f"\0{at}") for at, (wrong, _) in enumerate(checks)),
            pl.when(held).then(number).alias(column.field),
        )
        for at, (_, what) in enumerate(checks):
            if (wrong := found[f"\0{at}"]).any():
                _refuse(text, wrong, what, self._row_one)
        values = found[column.field]
        return values if (cast := _SHEET_CASTS.get(column.scalar)) is None else values.cast(cast)


class PaddedSheet[RowT: msgspec.Struct](SheetFile[RowT]):
    """A sheet whose text cells are padded with spaces (SF Fed's Stata months, ``' 1988m1'``), stripped before typed.

    A cell stripped to one of the sheet's words for none (``'  NA'``) holds no value.
    """

    @typing.override
    def _value(self, column: Column, text: pl.Series, typed: pl.Series) -> pl.Series:
        stripped = text.str.strip_chars(" ")
        return super()._value(column, stripped.replace(["", *self._null_values], None), typed)


class BesideSheet[RowT: msgspec.Struct](SheetFile[RowT]):
    """A sheet table with more beside it (a second list, a note, a work column): only the table's columns are read.

    They are the sheet's first columns (``A`` on), as many as the struct's fields, unless :attr:`_columns_at` names
    them: the table's own columns, 0-based (``A`` is 0), so a table may start right of others (a column offset) or skip
    a spacer. Nothing else on the sheet is loaded. Compose it with :class:`NotedSheet` for a table that
    also ends above the sheet's last row (``class _Updates(BesideSheet[R], NotedSheet[R])``).
    """

    #: The table's columns on the sheet, in its fields' order; ``None`` takes the first, one a field.
    _columns_at: typing.ClassVar[Sequence[int] | None] = None

    @typing.override
    def _used_columns(self) -> list[int] | None:
        # As many as the header's check needs: a wide table's columns are its fields, a long table's its keys too.
        read = [column for column in self._columns if column.cell is None and column.field != self._numbered]
        return list(range(len(read)) if self._columns_at is None else self._columns_at)


class CellHeaderSheet[RowT: msgspec.Struct](SheetFile[RowT]):
    """A sheet whose header writes date cells (a column a day), each read as its text (``2026-10-29 00:00:00``).

    :class:`SheetFile` names a header's columns as fastexcel does, a date, time or boolean cell naming none; here
    every header cell is its text, as written. Read long, a date header is its key
    (``class _Dated(LongFile[R], CellHeaderSheet[R])``, ``_keys`` matching the date cell's text).
    """

    @typing.override
    def _header_cells(self, cells: pl.DataFrame) -> list[str]:
        return ["" if cell is None else cell for cell in cells.row(self._header_row - 1)]


class NumberTextSheet[RowT: msgspec.Struct](SheetFile[RowT]):
    """A sheet whose numbers are written as text as well as in number cells (SF Fed's ``' 1.03'`` beside ``0.60``).

    A number field's text cell is read as its text's number of the field's own type, strictly: an int field's is
    digits after an optional ``-`` (no ``+5``, ``1e3``, ``1.0`` or ``inf``), a whole number of 64 bits, read digit for
    digit; a float field's a decimal (``-1.03``, ``.5``; no exponent, ``nan`` or ``inf``). A number cell, whose text is
    a decimal too, keeps every digit it holds. A text cell that is no such number, or a boolean, date or time cell,
    raises. Composed after a piece that cleans the text (``class _Sheet(PaddedSheet[RowT], NumberTextSheet[RowT])``).
    Reading text as numbers, it pays no load to tell a text cell from a number cell (:attr:`SheetFile._number_text`).
    """

    _number_text = True

    @typing.override
    def _value(self, column: Column, text: pl.Series, typed: pl.Series) -> pl.Series:
        if column.scalar not in {int, float} or column.flag is not None or column.written is not None:
            return super()._value(column, text, typed)
        whole = column.scalar is int
        # fastexcel's float load reads a decimal's text as its number, but not one padded (PaddedSheet strips it).
        numbers = typed.fill_null(text.cast(pl.Float64, strict=False))
        wrong = ~text.str.contains(_WHOLE_TEXT if whole else _DECIMAL_TEXT) | numbers.is_null()
        if not whole:
            _refuse(text, wrong, "number", self._row_one)
            return pl.select(pl.when(text.is_not_null()).then(numbers)).to_series().alias(column.field)
        # The text's own digits (a float holds 2^53 exactly, no more), where its number is whole: a number cell's text
        # is rounded to nine decimals (``3.0000000004`` is ``3``).
        digits = text.cast(pl.Int64, strict=False)
        _refuse(text, wrong | (numbers != numbers.floor()), "whole number", self._row_one)
        _refuse(text, digits.is_null(), "whole number of 64 bits", self._row_one)
        return pl.select(pl.when(text.is_not_null()).then(digits)).to_series().alias(column.field)


class NotedSheet[RowT: msgspec.Struct](SheetFile[RowT]):
    """A sheet table with notes under it: it ends at its first row with nothing past its first column.

    That row and those below it are the notes (a ``Source:`` line, a ``Notes:`` heading, a blank row and disclosures),
    dropped; a cell past the first column among them is a row the table lost, and raises, naming the sheet's row.
    """

    @typing.override
    def _end(self, text: pl.DataFrame) -> int:
        bare = text.select(pl.all_horizontal(pl.col(text.columns[1:]).is_null())).to_series()
        end = int(bare.arg_true()[0]) if bare.any() else text.height
        if (at := _first(~bare.slice(end), self._row_one + end)) is not None:
            msg = (
                f"the {self._sheet!r} sheet's row {at} holds a cell past its first column under its table, which ends"
                f" at row {self._row_one + end}: only notes go there"
            )
            raise ReadError(msg)
        return end


class DatedSheet[RowT: msgspec.Struct](SheetFile[RowT]):
    """A sheet table of dated rows, with notes under it: it runs while its first column holds date cells.

    A row whose values are all empty is still a row. Below the table only the first column holds anything, the notes,
    which are dropped; a cell past it among them is a row the table lost, and raises, naming the sheet's row.
    """

    @typing.override
    def _end(self, text: pl.DataFrame) -> int:
        dated = text.select(pl.col(text.columns[0]).str.contains(_DATE_CELL).fill_null(value=False)).to_series()
        end = text.height if dated.all() else int(dated.arg_min() or 0)
        below = text.slice(end).select(pl.any_horizontal(pl.col(text.columns[1:]).is_not_null())).to_series()
        if (at := _first(below, self._row_one + end)) is not None:
            msg = (
                f"the {self._sheet!r} sheet's row {at} holds a cell under its table,"
                " where only notes in its first column go"
            )
            raise ReadError(msg)
        return end


class SheetsFile[RowT: msgspec.Struct](File[RowT]):
    r"""Every sheet of a workbook, each read as its table by a reader of its own, one after another: a sheet a year.

    A format gives the regex every sheet's name matches (a sheet that does not raises, and a workbook needs one) and
    *each*, which builds the reader of one sheet from its name. That reader reads the row struct's fields but the ones
    the regex's groups name: a group named for a field states it on that sheet's rows (``ngsstats (?P<year>\d{4}) .*``),
    a field no column holds, which takes no ``Label``.
    """

    def __init__(
        self,
        row: type[RowT],
        *,
        sheets: re.Pattern[str],
        each: Callable[[str], SheetFile[typing.Any] | SheetText[typing.Any]],
    ) -> None:
        """Lay out the tables of *row*, one sheet each (see the class)."""
        self._sheets = sheets
        self._each = each
        super().__init__(row)
        self._stated = [column for column in self._columns if column.field in sheets.groupindex]

    @typing.override
    def _label(self, name: str) -> str | None:
        return name

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        reader = _workbook(body)
        names = reader.sheet_names
        groups = []
        for name in names:
            if (match := self._sheets.fullmatch(name)) is None:
                msg = f"the workbook's sheet {name!r} is not one of {self._sheets.pattern!r}"
                raise ReadError(msg)
            groups.append(match.groupdict())
        frames = []
        for name, group in zip(names, groups, strict=True):
            try:
                frame = self._each(name)._frame_on(reader, body)  # noqa: SLF001 (a sheet reader of its own)
            except ValueError as e:
                msg = f"the {name!r} sheet: {e}"
                # A sheet's reader is built here too: a misdeclared one stays a LayoutError, an empty sheet empty.
                raise (type(e) if isinstance(e, ReadError | LayoutError) else ReadError)(msg) from e
            states = [
                _stated(column, group[column.field], frame.height, f"{name!r} sheet's name") for column in self._stated
            ]
            frames.append(frame.with_columns(states).select(self._fields))
        return pl.concat(frames)


class TitledSheet[RowT: msgspec.Struct](SheetFile[RowT]):
    """A sheet table below title rows (a title, a note, a blank row): each row above its header is one of its titles.

    A format subclasses it and names them: :attr:`_titles`, a regex per row above the header, in order, each matching
    the row's text, its cells joined by tabs up to its last filled one (an empty row is ``re.compile("")``).
    """

    #: The regexes the rows above the header match, one a row; each format's subclass (or its constructor) sets them.
    _titles: Sequence[re.Pattern[str]]

    @typing.override
    def _outside(self, grid: pl.DataFrame, end: int) -> None:
        super()._outside(grid, end)
        rows = [
            "\t".join(cell or "" for cell in row).rstrip("\t") for row in grid.slice(0, self._header_row - 1).rows()
        ]
        if len(rows) != len(self._titles):
            msg = f"the {self._sheet!r} sheet has {len(rows)} rows above its header, not its {len(self._titles)} titles"
            raise ReadError(msg)
        for number, (row, title) in enumerate(zip(rows, self._titles, strict=True), 1):
            if not title.fullmatch(row):
                msg = f"the {self._sheet!r} sheet's row {number} {row[:200]!r} is not its title {title.pattern!r}"
                raise ReadError(msg)


class PreambleSheet[RowT: msgspec.Struct](SheetFile[RowT]):
    """A sheet table below a preamble of any length: its header is the first row whose first cell is its first label.

    The rows above it (a banner, a link, prose) are not read. The header row is found in each body (two workbooks of
    one layout open it on different rows), and errors name the sheet's own rows.
    """

    @typing.override
    def _header_in(self, grid: pl.DataFrame) -> int:
        """Return the sheet row of the header: the first whose first cell is the first column's label."""
        label = self._columns[0].label
        # The sheet's rows from its first, and its first filled column: a table may open past column A.
        if not grid.width or (at := _first(grid.to_series(0) == label)) is None:
            msg = f"the {self._sheet!r} sheet has no row whose first cell is {label!r}, its header's"
            raise ReadError(msg)
        return at


class OnlySheet[RowT: msgspec.Struct](SheetFile[RowT]):
    """A workbook of one sheet whose name varies (a workbook per variable, the sheet named for it): that sheet."""

    def __init__(self, row: type[RowT], *, header_row: int = 1, null_values: Sequence[str] = ()) -> None:
        """Lay out a table of *row* on the workbook's one sheet, under its header in sheet row *header_row*."""
        super().__init__(row, sheet="", header_row=header_row, null_values=null_values)

    @typing.override
    def _sheet_in(self, reader: fastexcel.ExcelReader) -> str:
        if len(names := reader.sheet_names) != 1:
            msg = f"the workbook holds the sheets {names!r}, not one"
            raise ReadError(msg)
        return names[0]


def sheet_grid(body: bytes, sheet: str | None = None, *, null_values: Sequence[str] = ()) -> pl.DataFrame:
    """Return sheet *sheet* of the workbook *body* (``None``: its one sheet, whatever its name) as text cells.

    The grid :class:`SheetText` cuts: a row per sheet row from row 1, a column per sheet column from the first filled
    one, named ``"0"``, ``"1"``, ...; a number with every digit it holds (fastexcel's text load rounds to nine
    decimals), a blank cell or one of *null_values* no value. A body that is no workbook, a workbook without *sheet*,
    or one of several sheets when *sheet* is ``None``, raises ReadError.

    Example: ``docs/examples/sheet_text.txt``.
    """
    reader = _workbook(body)
    return _text_grid(reader, _sheet_named(reader, sheet), null_values)[1]


def _sheet_named(reader: fastexcel.ExcelReader, sheet: str | None) -> str:
    """Return *sheet*, or for ``None`` the name of *reader*'s one sheet; raise ReadError for a workbook of several."""
    if sheet is None:
        if len(reader.sheet_names) != 1:
            msg = f"the workbook holds the sheets {reader.sheet_names!r}, not one"
            raise ReadError(msg)
        return reader.sheet_names[0]
    return sheet


def _text_grid(reader: fastexcel.ExcelReader, sheet: str, null_values: Sequence[str]) -> tuple[int, pl.DataFrame]:
    """Return *sheet* of *reader* as :func:`sheet_grid` lays it out, and the sheet column (``A`` is 0) of its first."""
    if sheet not in reader.sheet_names:
        msg = f"the workbook has no sheet {sheet!r}; its sheets are {reader.sheet_names!r}"
        raise ReadError(msg)
    with _loading(f"the workbook's {sheet!r} sheet will not load"):
        text = _text_sheet(reader, sheet)
    first = int(text.columns[0].removeprefix("__UNNAMED__")) if text.width else 0
    text.columns = [str(at) for at in range(text.width)]
    cell = pl.col(pl.String)
    return first, text.with_columns(pl.when((cell.str.strip_chars() != "") & ~cell.is_in(null_values)).then(cell))


class SheetText[RowT: msgspec.Struct](GridFile[RowT]):
    """A sheet too irregular for :class:`SheetFile` (blocks, summary rows, spacer columns): its cells as text, cut.

    Its grid is :func:`sheet_grid`'s: every cell of the sheet as text, a number with every digit it holds, from sheet
    row 1 and the sheet's first filled column, a column per sheet column; :class:`GridFile` cuts it. *sheet* ``None``
    reads the workbook's one sheet, whatever its name.
    """

    #: The workbook :meth:`_frame_on` was handed, open: the grid is read from it, not from the body.
    _opened: fastexcel.ExcelReader | None = None

    def __init__(
        self, row: type[RowT], *, sheet: str | None = None, header_row: int = 1, null_values: Sequence[str] = ()
    ) -> None:
        """Lay out a table of *row* on *sheet* (``None``: the workbook's only sheet), under its header *header_row*."""
        super().__init__(row, header_row=header_row, null_values=null_values)
        self._sheet = sheet

    def _frame_on(self, reader: fastexcel.ExcelReader, body: bytes) -> pl.DataFrame:
        """Read *body*'s table as :meth:`frame` does, from *reader*, the workbook open for it: one open, many sheets."""
        placed = object.__new__(type(self))
        vars(placed).update(vars(self), _opened=reader)
        return placed.frame(body)

    @typing.override
    def _body_grid(self, body: bytes) -> pl.DataFrame:
        reader = self._opened or _workbook(body)
        return self._grid(reader, _sheet_named(reader, self._sheet))

    _workbook = staticmethod(_workbook)

    def _grid(self, reader: fastexcel.ExcelReader, sheet: str) -> pl.DataFrame:
        """Return *sheet*'s every cell as text, a row per sheet row from row 1, a column per sheet column."""
        return self._sheet_grid(reader, sheet)[1]

    def _sheet_grid(self, reader: fastexcel.ExcelReader, sheet: str) -> tuple[int, pl.DataFrame]:
        """Return *sheet*'s grid (:meth:`_grid`'s) and the sheet column (``A`` is 0) of its column ``0``, its first."""
        return _text_grid(reader, sheet, self._null_values)


#: The BOF record opening a BIFF3 and a BIFF4 worksheet (Excel 3 and 4), which calamine cannot open.
_BIFF_BOFS: Final = (0x0209, 0x0409)


class BiffSheet[RowT: msgspec.Struct](SheetText[RowT]):
    """A :class:`SheetText` on a BIFF3 or BIFF4 workbook, which calamine cannot open: xlrd reads its grid.

    The grid is the one :class:`SheetText` cuts, each cell as fastexcel writes it as text: a number its every digit,
    a date ``YYYY-MM-DD HH:MM:SS``, a boolean ``true`` or ``false``, an error its code (``#N/A``, one of the sheet's
    *null_values* where it means none). A body that is neither, or one xlrd cannot read (cut, corrupt), raises.
    """

    @typing.override
    def _body_grid(self, body: bytes) -> pl.DataFrame:
        return self._biff_grid(body)

    def _biff_grid(self, body: bytes) -> pl.DataFrame:
        """Return the sheet's grid as :meth:`SheetText._grid` does: a column per sheet column from its first filled one.

        Raises ReadError for a body that is no BIFF3 or BIFF4 workbook, one xlrd cannot read, or no such sheet.
        """
        if int.from_bytes(body[:2], "little") not in _BIFF_BOFS:
            msg = f"the body is not a BIFF3 or BIFF4 workbook: it opens {body[:4]!r}"
            raise ReadError(msg)
        try:
            # A workbook with no CODEPAGE record reads its text as latin-1, as Excel did; xlrd's note of it is dropped.
            book = xlrd.open_workbook(file_contents=body, logfile=io.StringIO())
        # xlrd's checks of a corrupt record are its own error, struct's, a lookup's, a decode's or an assert.
        except (xlrd.XLRDError, struct.error, LookupError, ValueError, AssertionError) as e:
            msg = f"the body is not a readable BIFF3 or BIFF4 workbook: {e!r}"
            raise ReadError(msg) from e
        names = book.sheet_names()
        name = self._sheet if self._sheet is not None else names[0] if len(names) == 1 else None
        if name not in names:
            msg = f"the workbook holds the sheets {names!r}, not {'one' if self._sheet is None else repr(self._sheet)}"
            raise ReadError(msg)
        sheet = book.sheet_by_name(name)
        empty = {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}
        first = next((at for at in range(sheet.ncols) if set(sheet.col_types(at)) - empty), sheet.ncols)
        text = functools.partial(_biff_text, book.datemode)
        rows = range(sheet.nrows)
        columns = {
            str(at - first): [
                None if cell is None or not cell.strip() or cell in self._null_values else cell
                for cell in map(
                    text,
                    sheet.col_types(at),
                    sheet.col_values(at),
                    rows,
                    itertools.repeat(at, sheet.nrows),
                    strict=True,
                )
            ]
            for at in range(first, sheet.ncols)
        }
        return pl.DataFrame(columns, schema=dict.fromkeys(columns, pl.String))


def _biff_text(datemode: int, kind: int, value: object, row: int, at: int) -> str | None:
    """Return the text of an xlrd cell of *kind* holding *value*, as fastexcel writes it; an empty cell none.

    A date's serial counts from the workbook's *datemode* (0: 1900, 1: 1904). The cell is at *row* and column *at*
    (0-based), which a ReadError names: a date no calendar has (past year 9999, not a number, Excel's 1900-02-29) or an
    error code none of Excel's.
    """
    match kind, value:
        case xlrd.XL_CELL_TEXT, str():
            return value
        case xlrd.XL_CELL_NUMBER, float():
            return str(int(value)) if value.is_integer() else repr(value)
        case xlrd.XL_CELL_DATE, float():
            # Serial 60 is 1900-02-29 in the 1900 system, which xlrd reads as serial 59's day, as fastexcel does.
            if not datemode and 60 <= value < 61:  # noqa: PLR2004
                msg = f"the cell {xlrd.cellname(row, at)} is Excel's day 60, 1900-02-29, which no calendar has"
                raise ReadError(msg)
            try:
                return f"{xlrd.xldate_as_datetime(value, datemode):%Y-%m-%d %H:%M:%S}"
            except (OverflowError, ValueError) as e:
                msg = f"the cell {xlrd.cellname(row, at)} is a date cell of no date: its serial is {value!r}"
                raise ReadError(msg) from e
        case xlrd.XL_CELL_BOOLEAN, int():
            return _BOOLEAN_CELLS[not value]
        case xlrd.XL_CELL_ERROR, int():
            if (error := xlrd.error_text_from_code.get(value)) is None:
                msg = f"the cell {xlrd.cellname(row, at)} is an error cell of a code none of Excel's: {value:#x}"
                raise ReadError(msg)
            return error
        case _:
            return None


class GroupedSheet[RowT: msgspec.Struct](LongFile[RowT], CellHeaderSheet[RowT]):
    """A sheet read long whose key columns sit under group headings, in the row :attr:`_groups_row` above its header.

    The row struct's last three fields are the group, the label and the value, after the record's (a question, an
    answer and its share). A heading names its column and each to its right until the next; a key column with an empty
    header cell is a spacer, not read, and one left of every heading raises, as a key that is no group's.
    """

    #: The sheet row of the group headings, 1-based (Excel's).
    _groups_row: typing.ClassVar[int]
    _key_count = 2
    _unread = (_UNIT,)
    _split_keys = True

    def __init__(self, row: type[RowT], *, sheet: str, header_row: int = 1, null_values: Sequence[str] = ()) -> None:
        """Lay out a grouped table of *row* (see :class:`SheetFile`): its group and label fields before its value."""
        group, label = row.__struct_fields__[-3:-1]
        # Set before File's constructor, which refuses a long table with no _keys.
        self._keys = re.compile(f"(?P<{group}>[^{_UNIT}]+){_UNIT}(?P<{label}>[^{_UNIT}]+)")
        super().__init__(row, sheet=sheet, header_row=header_row, null_values=null_values)

    @typing.override
    def _header_cells(self, cells: pl.DataFrame) -> list[str]:
        labels = super()._header_cells(cells)
        headings = cells.row(self._groups_row - 1) if self._groups_row <= cells.height else (None,) * cells.width
        # The headings over the record's columns are not groups (a link, a title), and are not read.
        lead = len(self._record())
        named, group = labels[:lead], None
        for heading, label in zip(headings[lead:], labels[lead:], strict=True):
            group = heading or group
            named.append(_UNIT if not label else label if group is None else f"{group}{_UNIT}{label}")
        return named


def _text_sheet(reader: fastexcel.ExcelReader, name: str) -> pl.DataFrame:
    """Load sheet *name* of *reader* with no header row, every cell as text, each decimal number to its last digit.

    Row *i* of the frame is the sheet's row *i* + 1, as Excel numbers it, leading empty rows kept. Columns start at
    the first filled one, as calamine trims them, each named ``__UNNAMED__<j>`` for its sheet column *j* (``A`` is 0).
    """
    sheet = reader.load_sheet(name, header_row=None, skip_rows=0, dtypes="string")
    names = {f"__UNNAMED__{c.index}": f"__UNNAMED__{c.absolute_index}" for c in sheet.available_columns()}
    text = sheet.to_polars()
    # fastexcel keys a dtype by the sheet's column (``A`` is 0), so a sheet that opens past column A keys it there.
    floats: fastexcel.DTypeMap = {column.absolute_index: "float" for column in sheet.available_columns()}
    numbers = reader.load_sheet(name, header_row=None, skip_rows=0, dtypes=floats).to_polars()
    # fastexcel may load a column of text as String. Side by side, by name: half the time of Series in the expressions.
    numbers = numbers.cast(pl.Float64, strict=False).rename(lambda column: f"{column}:float")
    return pl.concat([text, numbers], how="horizontal").select(
        pl.when(
            pl.col(f"{column}:float").is_not_null()
            & (pl.col(column).cast(pl.Float64, strict=False) != pl.col(f"{column}:float"))
        )
        .then(pl.col(f"{column}:float").cast(pl.String))
        .otherwise(pl.col(column))
        .alias(names[column])
        for column in text.columns
    )


class FoundSheet[RowT: msgspec.Struct](FoundTable[RowT]):
    """A :class:`FoundTable` on the first sheet of a workbook (``.xlsx``, ``.xls``), its cells as text."""

    @typing.override
    def _lines(self, body: bytes) -> list[list[str]]:
        reader = _workbook(body)
        if not reader.sheet_names:
            msg = "the workbook holds no sheet"
            raise ReadError(msg)
        with _loading("the workbook's first sheet will not load"):
            sheet = _text_sheet(reader, reader.sheet_names[0])
        return [[cell or "" for cell in line] for line in sheet.iter_rows()]
