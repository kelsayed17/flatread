"""One Excel reader: a ``.xls``/``.xlsx`` body's sheets as polars frames, through fastexcel (the ``excel`` extra).

A reader of an Excel workbook reads its sheet through here, into a DataFrame and from it typed rows (with
:func:`~flatread.cells.read_labelled_cells`). fastexcel binds the Rust calamine reader, which
reads both formats from bytes; it is the engine ``polars.read_excel(engine="calamine")`` uses, called directly so that
the failures a caller must tell apart arrive as :mod:`flatread.errors`'s errors: a body that is no workbook at
all, a workbook that is damaged, and a workbook without the sheet asked for.

**Through polars.** fastexcel hands a sheet over only as Arrow, which polars reads without a copy; so a workbook's rows,
like its frame, are read through polars.

A frame is calamine's, with nothing done to it after: unlike ``polars.read_excel``, which drops a row whose every cell
is empty and narrows a whole-number float column to ``Int64`` and a midnight-only datetime column to ``Date``, a reader
here keeps all three as calamine read them. A decoder that wants any of them says so itself.
"""

import math
from collections.abc import Callable, Mapping
from functools import partial
from typing import TYPE_CHECKING, Final, Literal

import fastexcel
import polars as pl

from flatread.errors import CorruptArchiveError, EmptyBodyError, UnknownLayoutError

if TYPE_CHECKING:
    from fastexcel import DType, DTypeMap

__all__ = ["ColumnType", "read_every_sheet", "read_sheet", "read_sheet_text"]

#: The cell types a caller may declare for a column, a subset of fastexcel's own ``DType``.
type ColumnType = Literal["string", "float", "date", "datetime"]

#: Every ZIP's local file header opens with these four bytes. An ``.xlsx`` is a ZIP; a legacy ``.xls`` is not.
_ZIP_SIGNATURE: Final = b"PK" + bytes([3, 4])


def read_sheet(
    body: bytes,
    sheet: str | None,
    *,
    header_row: int | None = 0,
    skip_rows: int = 0,
    dtypes: ColumnType | Mapping[str, ColumnType] | None = None,
) -> pl.DataFrame:
    """Read the sheet named *sheet* from a ``.xls``/``.xlsx`` *body*, with row *header_row* as its header.

    ``sheet=None`` reads the workbook's one sheet, whatever its name, and a workbook of more than one raises
    :class:`~flatread.errors.UnknownLayoutError`: for a publisher that names the sheet after the file's date
    range and pins nothing else to it (Treasury's investor-class allotments), whose header the caller checks instead.

    *header_row* is 0-based and the rows above it are not read; the *skip_rows* rows under it are not data either (a
    second header line, such as EIA's series descriptions). With *header_row* ``None`` there is no header: every row
    is data, from the sheet's first, and fastexcel names the columns ``__UNNAMED__0``, ``__UNNAMED__1``, ….

    Raises :class:`~flatread.errors.EmptyBodyError` for an empty body,
    :class:`~flatread.errors.CorruptArchiveError` for a body that opens as a ZIP (an ``.xlsx`` is one) and that
    calamine still cannot read — cut short, or a part inside it damaged — and
    :class:`~flatread.errors.UnknownLayoutError` for any other body calamine cannot open as a workbook (an HTML
    error page served with a 200 lands here, and so does a damaged ``.xls``) or a workbook with no such sheet.

    Cell types are calamine's, inferred column by column from the first 1,000 rows, unless *dtypes* declares them:
    one type for every column, or a column's type by its header name. A cell that does not read as the declared type
    is then null, and a name the header does not carry is ignored, so the caller still checks the header. Declaring is
    what a sheet whose early rows are blank needs: inference falls back to text for such a column and fastexcel logs a
    warning on every read (EIA's petroleum stocks, where four series start after 1982). The caller types what it reads.
    """
    workbook = _open(body)
    if sheet is None:
        if len(workbook.sheet_names) != 1:
            msg = f"the workbook has {len(workbook.sheet_names)} sheets, not one: {workbook.sheet_names}"
            raise UnknownLayoutError(msg)
        (sheet,) = workbook.sheet_names
    elif sheet not in workbook.sheet_names:
        msg = f"the workbook has no sheet {sheet!r}; it has {workbook.sheet_names}"
        raise UnknownLayoutError(msg)
    return _load(body, _loader(workbook, header_row, skip_rows, dtypes), sheet)


def read_sheet_text(
    body: bytes,
    sheet: str | None,
    *,
    header_row: int | None = 0,
    skip_rows: int = 0,
) -> pl.DataFrame:
    """Read the sheet as :func:`read_sheet` does, every cell as its exact text, for a strict cell reader.

    calamine writes a number cell's text rounded to nine decimals, its trailing zeros dropped
    (``-0.5816963701473981`` reads ``-0.58169637``, ``12.0000000001`` reads ``12``, ``1E-10`` reads ``0``), so
    reading figures from that text would change them. Its float read of the same cell is exact. A cell whose text is
    exactly that rounding of its float, and whose float the rounding changed, is a number cell calamine rounded: it
    takes its float's shortest exact text. Every other cell keeps calamine's text: a number the rounding did not
    change (``12``), a text cell (``"1.50"``, whose float is read from that very text, so nothing was rounded), a date
    as ``YYYY-MM-DD HH:MM:SS``, and an empty cell's null. Measured 2026-11-05 on every workbook fixture: calamine's
    text is that rounding for all 5,986 number cells. The sheet is read twice.
    """
    text = read_sheet(body, sheet, header_row=header_row, skip_rows=skip_rows, dtypes="string")
    floats = read_sheet(body, sheet, header_row=header_row, skip_rows=skip_rows, dtypes="float")
    return text.with_columns(
        pl.Series(name, [_exact(cell, value) for cell, value in zip(text[name], floats[name], strict=True)], pl.String)
        for name in text.columns
    )


def _exact(cell: str | None, value: float | None) -> str | None:
    """Return *cell*'s exact text: its float's, if *cell* is calamine's nine-decimal rounding of it that lost digits."""
    if cell is None or value is None or not math.isfinite(value) or _rounded(value) != cell or float(cell) == value:
        return cell
    return repr(value)


def _rounded(value: float) -> str:
    """Return *value* as calamine writes a number cell: rounded to nine decimals, trailing zeros and point dropped."""
    written = f"{value:.9f}".rstrip("0").rstrip(".")
    return "0" if written in {"", "-0"} else written


def read_every_sheet(
    body: bytes,
    *,
    header_row: int | None = 0,
    skip_rows: int = 0,
    dtypes: ColumnType | Mapping[str, ColumnType] | None = None,
) -> dict[str, pl.DataFrame]:
    """Read every sheet of a ``.xls``/``.xlsx`` *body*, by name in the workbook's order, as :func:`read_sheet` does one.

    It fails as :func:`read_sheet` does, and opens the workbook once.
    """
    workbook = _open(body)
    load = _loader(workbook, header_row, skip_rows, dtypes)
    return {sheet: _load(body, load, sheet) for sheet in workbook.sheet_names}


def _open(body: bytes) -> fastexcel.ExcelReader:
    """Open *body* as a workbook, which reads its directory of sheets but no sheet."""
    if not body:
        msg = "the body is empty"
        raise EmptyBodyError(msg)
    try:
        return fastexcel.read_excel(body)
    except fastexcel.CalamineError as exc:
        raise _unreadable(body, exc) from exc


def _loader(
    workbook: fastexcel.ExcelReader,
    header_row: int | None,
    skip_rows: int,
    dtypes: ColumnType | Mapping[str, ColumnType] | None,
) -> Callable[[str], fastexcel.ExcelSheet]:
    """Return what reads one sheet of the open *workbook* by name, its header and cell types as the caller asked."""
    declared: DType | DTypeMap | None = None
    if isinstance(dtypes, Mapping):
        # Built from the items, whose tuples are covariant, the keys widen to fastexcel's ``str | int``;
        # ``dict(dtypes)`` would not, a mapping's keys being invariant.
        declared = dict(dtypes.items())
    elif dtypes is not None:
        declared = dtypes
    return partial(workbook.load_sheet, header_row=header_row, skip_rows=skip_rows, dtypes=declared)


def _load(body: bytes, load: Callable[[str], fastexcel.ExcelSheet], sheet: str) -> pl.DataFrame:
    """Read the sheet *sheet* into a frame, as calamine reads it."""
    try:
        return load(sheet).to_polars()
    except fastexcel.CalamineError as exc:
        # calamine reads a worksheet's own part only when it is loaded, so a damaged one fails here, not at the open.
        raise _unreadable(body, exc) from exc


def _unreadable(body: bytes, exc: fastexcel.CalamineError) -> CorruptArchiveError | UnknownLayoutError:
    """Return the error for a body calamine failed on: a damaged ZIP, or a body that is no workbook calamine opens."""
    if body.startswith(_ZIP_SIGNATURE):
        return CorruptArchiveError(f"the workbook cannot be read: {exc}")
    return UnknownLayoutError(f"the body is not a workbook calamine can open: {exc}")
