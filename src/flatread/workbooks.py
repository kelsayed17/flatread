"""One Excel reader: a ``.xls``/``.xlsx`` body's sheets as polars frames, through ``polars.read_excel``.

A reader of an Excel workbook reads its sheet through here, into a DataFrame, and its cells from it (with
:func:`~flatread.cells.read_table`). polars reads it with its own calamine engine (``engine="calamine"``,
the ``polars[calamine]`` extra), from bytes, both formats; the failures a caller must tell apart arrive as
:mod:`flatread.errors`'s errors: a body that is no workbook at all, a workbook that is damaged, and a workbook without
the sheet asked for.

A frame is the sheet as polars reads it, every row and column kept (``drop_empty_rows=False``,
``drop_empty_cols=False``), its cells of the types the caller declares. A sheet read with no header row has polars'
own column names, ``column_0``, ``column_1``, ….
"""

import io
from collections.abc import Mapping
from functools import partial
from typing import Final, Literal

import polars as pl

from flatread.errors import CorruptArchiveError, EmptyBodyError, UnknownLayoutError

__all__ = ["ColumnType", "read_every_sheet", "read_sheet", "read_sheet_text"]

#: The cell types a caller may declare for a column, a subset of the calamine engine's own.
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
    is data, from the sheet's first, its columns ``column_0``, ``column_1``, ….

    Raises :class:`~flatread.errors.EmptyBodyError` for an empty body,
    :class:`~flatread.errors.CorruptArchiveError` for a body that opens as a ZIP (an ``.xlsx`` is one) and that
    still cannot be read — cut short, or a part inside it damaged — and :class:`~flatread.errors.UnknownLayoutError`
    for any other body polars cannot open as a workbook (an HTML error page served with a 200 lands here) or a workbook
    with no such sheet.

    Cell types are inferred column by column, unless *dtypes* declares them: one type for every column, or a column's
    type by its header name. A cell that does not read as the declared type is then null, and a name the header does
    not carry is ignored, so the caller still checks the header.
    """
    sheets = _read(body, sheet, header_row, skip_rows, dtypes)
    if len(sheets) != 1:
        msg = f"the workbook has {len(sheets)} sheets, not one: {list(sheets)}"
        raise UnknownLayoutError(msg)
    return next(iter(sheets.values()))


def read_sheet_text(
    body: bytes,
    sheet: str | None,
    *,
    header_row: int | None = 0,
    skip_rows: int = 0,
) -> pl.DataFrame:
    """Read the sheet as :func:`read_sheet` does, every cell as its exact text, for :func:`~flatread.cells.read_table`.

    The calamine engine writes a number cell's text rounded to nine decimals (``-0.5816963701473981`` reads
    ``-0.58169637``, ``1E-10`` reads ``0``), while its float read of the same cell is exact. So a cell whose text is a
    number other than its float takes its float's text; every other cell keeps its own. The sheet is read twice.
    """
    text = read_sheet(body, sheet, header_row=header_row, skip_rows=skip_rows, dtypes="string")
    floats = read_sheet(body, sheet, header_row=header_row, skip_rows=skip_rows, dtypes="float")
    return text.with_columns(
        pl.when((written := text[name].cast(pl.Float64, strict=False)).is_not_null() & (written != floats[name]))
        .then(floats[name].cast(pl.String))
        .otherwise(text[name])
        .alias(name)
        for name in text.columns
    )


def read_every_sheet(
    body: bytes,
    *,
    header_row: int | None = 0,
    skip_rows: int = 0,
    dtypes: ColumnType | Mapping[str, ColumnType] | None = None,
) -> dict[str, pl.DataFrame]:
    """Read every sheet of a ``.xls``/``.xlsx`` *body*, by name in the workbook's order, as :func:`read_sheet` does one.

    It fails as :func:`read_sheet` does.
    """
    return _read(body, None, header_row, skip_rows, dtypes)


def _read(
    body: bytes,
    sheet: str | None,
    header_row: int | None,
    skip_rows: int,
    dtypes: ColumnType | Mapping[str, ColumnType] | None,
) -> dict[str, pl.DataFrame]:
    """Read the sheet *sheet* of *body* with ``polars.read_excel``, every row and column kept; ``None`` every sheet.

    A named sheet is read alone, so a workbook without it fails before any other sheet is loaded.
    """
    if not body:
        msg = "the body is empty"
        raise EmptyBodyError(msg)
    options: dict[str, object] = {"skip_rows": skip_rows}
    if header_row is not None:
        options["header_row"] = header_row
    if dtypes is not None:
        options["dtypes"] = dict(dtypes.items()) if isinstance(dtypes, Mapping) else dtypes
    read = partial(
        pl.read_excel,
        io.BytesIO(body),
        engine="calamine",
        has_header=header_row is not None,
        read_options=options,
        drop_empty_rows=False,
        drop_empty_cols=False,
        raise_if_empty=False,
    )
    try:
        return read(sheet_id=0) if sheet is None else read(sheet_name=[sheet])
    except ValueError as exc:
        if sheet is not None and "no matching sheet" in str(exc):
            msg = f"the workbook has no sheet {sheet!r}"
            raise UnknownLayoutError(msg) from exc
        raise _unreadable(body, exc) from exc
    except Exception as exc:  # the engine's own error, for a body it cannot open or a damaged part it loads
        raise _unreadable(body, exc) from exc
    except BaseException as exc:  # a panic in the engine, which is no Exception; any other passes on
        if type(exc).__name__ != "PanicException":
            raise
        raise _unreadable(body, exc) from exc


def _unreadable(body: bytes, exc: BaseException) -> CorruptArchiveError | UnknownLayoutError:
    """Return the error for a body polars failed on: a damaged ZIP, or a body that is no workbook it opens."""
    if body.startswith(_ZIP_SIGNATURE):
        return CorruptArchiveError(f"the workbook cannot be read: {exc}")
    return UnknownLayoutError(f"the body is not a workbook polars can open: {exc}")
