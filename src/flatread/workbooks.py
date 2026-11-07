"""One Excel reader: a ``.xls``/``.xlsx`` body's sheets as polars frames, through ``polars.read_excel``.

A reader of an Excel workbook reads its sheet through here, into a DataFrame, and its cells from it (with
:func:`~flatread.cells.read_table`). polars reads it with its own calamine engine (``engine="calamine"``,
the ``polars[calamine]`` extra), from bytes, both formats; the failures a caller must tell apart arrive as
:mod:`flatread.errors`'s errors: a body that is no workbook at all, a workbook that is damaged, and a workbook without
the sheet asked for.

A frame is the sheet as polars reads it, every row and column kept (``drop_empty_rows=False``,
``drop_empty_cols=False``), its cells of the types the caller declares. A sheet read with no header row has polars'
own column names, ``column_0``, ``column_1``, ….

The engine reads an Excel error cell (``#N/A``, ``#DIV/0!``, ``#REF!``) as an empty one, so an ``.xlsx`` sheet read
holding one raises :class:`~flatread.errors.UnknownLayoutError` naming the sheet, the cell and the error. An ``.xls``
error cell still reads as an empty cell: the engine gives no sign of one.
"""

import html
import io
import posixpath
import re
import zipfile
from collections.abc import Mapping
from functools import partial
from typing import TYPE_CHECKING, Final, Literal

import polars as pl

from flatread.cells import read_table, wire_names
from flatread.errors import CorruptArchiveError, EmptyBodyError, UnknownLayoutError

if TYPE_CHECKING:
    from collections.abc import Sequence

    import msgspec

__all__ = [
    "ColumnType",
    "read_every_sheet",
    "read_every_sheet_text",
    "read_sheet",
    "read_sheet_rows",
    "read_sheet_table",
    "read_sheet_text",
]

#: The cell types a caller may declare for a column, a subset of the calamine engine's own. Text is not one: the
#: engine writes a number cell's text rounded to nine decimals, so a sheet's text is :func:`read_sheet_text`'s.
type ColumnType = Literal["float", "date", "datetime"]

#: The types the module itself reads a sheet as: a caller's, and the engine's text for :func:`read_sheet_text`.
type _ReadType = Literal["string", "float", "date", "datetime"]

_COLUMN_TYPES: Final = frozenset(("float", "date", "datetime"))

#: Every ZIP's local file header opens with these four bytes. An ``.xlsx`` is a ZIP; a legacy ``.xls`` is not.
_ZIP_SIGNATURE: Final = b"PK" + bytes([3, 4])

#: Every compound file (OLE/CFB) opens with these eight bytes, and a legacy ``.xls`` is one.
_CFB_SIGNATURE: Final = bytes.fromhex("d0cf11e0a1b11ae1")

#: An ``.xlsx`` worksheet's error cell (``t="e"``), its attributes and its contents; any namespace prefix.
_ERROR_CELL: Final = re.compile(
    rb"""<(?:[\w.-]+:)?c\s(?=[^>]*\bt\s*=\s*["']e["'])([^>]*?)(?:/>|>(.*?)</(?:[\w.-]+:)?c>)""", re.DOTALL
)

#: An XML start tag's attribute, its name without a namespace prefix, and its value.
_ATTRIBUTE: Final = re.compile(rb"""(?:[\w.-]+:)?([\w.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


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
    :class:`~flatread.errors.CorruptArchiveError` for a body that opens as a ZIP (an ``.xlsx`` is one) or as a compound
    file (a legacy ``.xls`` is one) and that still cannot be read — cut short, or a part inside it damaged — and
    :class:`~flatread.errors.UnknownLayoutError` for any other body polars cannot open as a workbook (an HTML error page
    served with a 200 lands here), a workbook with no such sheet, or an ``.xlsx`` sheet holding an Excel error cell.

    Cell types are inferred column by column, unless *dtypes* declares them: one type for every column, or a column's
    type by its header name. A cell that does not read as the declared type is then null, and a name the header does
    not carry is ignored, so the caller still checks the header. A type that is not a :data:`ColumnType` raises
    :class:`ValueError`: ``"string"`` among them, whose number cells the engine rounds (:func:`read_sheet_text`).
    """
    return _one(_read(body, sheet, header_row, skip_rows, _declared(dtypes)))


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
    text = _one(_read(body, sheet, header_row, skip_rows, "string"))
    return _exact(text, _one(_read(body, sheet, header_row, skip_rows, "float", scan=False)))


def read_sheet_table(
    body: bytes,
    sheet: str | None,
    row_type: type[msgspec.Struct],
    *,
    header_row: int = 0,
    skip_rows: int = 0,
) -> pl.DataFrame:
    """Read the sheet's text (:func:`read_sheet_text`) into *row_type*'s columns (:func:`~flatread.cells.read_table`).

    A cell that fails the read is named by its row in the sheet, 1-based, as a spreadsheet numbers it: the first data
    row is ``header_row + skip_rows + 2``. A sheet whose header has no rows under it raises
    :class:`~flatread.errors.EmptyBodyError`.
    """
    text = read_sheet_text(body, sheet, header_row=header_row, skip_rows=skip_rows)
    first = header_row + skip_rows + 2
    table = read_table(text, row_type, numbers=pl.int_range(first, first + text.height, eager=True))
    if table.is_empty():
        named = "the workbook's one sheet" if sheet is None else f"the {sheet!r} sheet"
        msg = f"{named} has a header and no rows"
        raise EmptyBodyError(msg)
    return table


def read_sheet_rows(
    grid: pl.DataFrame, rows: Sequence[int], labels: Sequence[str], row_type: type[msgspec.Struct]
) -> pl.DataFrame:
    """Read the *rows* of a header-less sheet's *grid* under *labels* into *row_type*'s columns (``read_table``).

    For a sheet whose table sits among other rows, read with ``header_row=None`` (:func:`read_sheet_text`): *rows* are
    the table's rows, 0-based, and *labels* its header's cells, the table its first ``len(labels)`` columns, a label
    written twice named as ``pl.read_csv`` names it. A cell that fails the read is named by its row in the sheet,
    1-based. A row with a cell past the table's columns raises :class:`~flatread.errors.UnknownLayoutError`, and no
    rows :class:`~flatread.errors.EmptyBodyError`.
    """
    if not rows:
        msg = "the sheet has a header and no rows"
        raise EmptyBodyError(msg)
    width = len(labels)
    cells = grid[list(rows)]
    past = cells.select(grid.columns[width:])
    if past.width and (wide := past.select(pl.any_horizontal(pl.all().fill_null("") != "")).to_series()).any():
        row = rows[int(wide.arg_true()[0])]
        msg = f"row {row + 1} has a cell past the table's {width} columns: {grid.row(row)!r}"
        raise UnknownLayoutError(msg)
    frame = cells.select(grid.columns[:width])
    frame.columns = wire_names(list(labels))
    return read_table(frame, row_type, numbers=pl.Series([row + 1 for row in rows], dtype=pl.Int64))


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
    return _read(body, None, header_row, skip_rows, _declared(dtypes))


def read_every_sheet_text(body: bytes, *, header_row: int | None = 0, skip_rows: int = 0) -> dict[str, pl.DataFrame]:
    """Read every sheet of *body*, by name in the workbook's order, each as :func:`read_sheet_text` reads one.

    Every cell is its exact text. The workbook is read twice, as text and as floats, not twice per sheet. It fails as
    :func:`read_sheet` does.
    """
    text = _read(body, None, header_row, skip_rows, "string")
    floats = _read(body, None, header_row, skip_rows, "float", scan=False)
    return {name: _exact(grid, floats[name]) for name, grid in text.items()}


def _declared(dtypes: ColumnType | Mapping[str, ColumnType] | None) -> ColumnType | Mapping[str, ColumnType] | None:
    """Return a caller's *dtypes*, having checked each is a :data:`ColumnType`."""
    declared = [] if dtypes is None else [dtypes] if isinstance(dtypes, str) else list(dtypes.values())
    if wrong := sorted({kind for kind in declared if kind not in _COLUMN_TYPES}):
        msg = (
            f"a column is read as one of {sorted(_COLUMN_TYPES)!r}, not {wrong!r}: a sheet's text is read_sheet_text's"
        )
        raise ValueError(msg)
    return dtypes


def _one(sheets: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """Return the one sheet read; a workbook read whole that holds more than one raises."""
    if len(sheets) != 1:
        msg = f"the workbook has {len(sheets)} sheets, not one: {list(sheets)}"
        raise UnknownLayoutError(msg)
    return next(iter(sheets.values()))


def _exact(text: pl.DataFrame, floats: pl.DataFrame) -> pl.DataFrame:
    """Return the sheet's *text*, each cell whose text is a number other than its *floats* cell taking that float's."""
    return text.with_columns(
        pl.when((written := text[name].cast(pl.Float64, strict=False)).is_not_null() & (written != floats[name]))
        .then(floats[name].cast(pl.String))
        .otherwise(text[name])
        .alias(name)
        for name in text.columns
    )


def _read(  # noqa: PLR0913  # the body and pl.read_excel's options, then the scan
    body: bytes,
    sheet: str | None,
    header_row: int | None,
    skip_rows: int,
    dtypes: _ReadType | Mapping[str, ColumnType] | None,
    *,
    scan: bool = True,
) -> dict[str, pl.DataFrame]:
    """Read the sheet *sheet* of *body* with ``polars.read_excel``, every row and column kept; ``None`` every sheet.

    A named sheet is read alone, so a workbook without it fails before any other sheet is loaded. *scan* false skips
    the error-cell scan, for a second read of a body already scanned.
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
        sheets = read(sheet_id=0) if sheet is None else read(sheet_name=[sheet])
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
    if scan and body.startswith(_ZIP_SIGNATURE):
        _refuse_error_cells(body, list(sheets))
    return sheets


def _unreadable(body: bytes, exc: BaseException) -> CorruptArchiveError | UnknownLayoutError:
    """Return the error for a body polars failed on: a damaged ZIP or compound file, or a body that is no workbook."""
    if body.startswith((_ZIP_SIGNATURE, _CFB_SIGNATURE)):
        return CorruptArchiveError(f"the workbook cannot be read: {exc}")
    return UnknownLayoutError(f"the body is not a workbook polars can open: {exc}")


def _refuse_error_cells(body: bytes, sheets: list[str]) -> None:
    """Raise for the first Excel error cell in the ``.xlsx`` *body*'s *sheets*, which the engine reads as empty.

    Each sheet's worksheet part is the one ``xl/workbook.xml`` and its relationships name for it. Only the sheets read
    are searched, so an error cell in a sheet nobody reads fails nothing.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            for name, part in _worksheet_parts(archive, sheets).items():
                if (found := _ERROR_CELL.search(archive.read(part))) is None:
                    continue
                attributes, contents = found.groups()
                value = re.search(rb"<(?:[\w.-]+:)?v>([^<]*)</", contents or b"")
                cell = _attributes(attributes).get("r", "?")
                error = html.unescape(value.group(1).decode()) if value is not None else ""
                msg = f"the {name!r} sheet's cell {cell} is the Excel error {error!r}, not a value"
                raise UnknownLayoutError(msg)
    except (zipfile.BadZipFile, KeyError) as exc:
        msg = f"the workbook cannot be read: {exc}"
        raise CorruptArchiveError(msg) from exc


def _worksheet_parts(archive: zipfile.ZipFile, sheets: list[str]) -> dict[str, str]:
    """Return each of *sheets*' worksheet part in *archive*, by the sheet's name."""
    relationships = map(_attributes, _tags(archive.read("xl/_rels/workbook.xml.rels"), b"Relationship"))
    targets = {item.get("Id"): item.get("Target", "") for item in relationships}
    ids = {
        item.get("name"): item.get("id") for item in map(_attributes, _tags(archive.read("xl/workbook.xml"), b"sheet"))
    }
    parts: dict[str, str] = {}
    for name in sheets:
        target = targets[ids[name]]
        parts[name] = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
    return parts


def _tags(xml: bytes, tag: bytes) -> list[bytes]:
    """Return the attributes of each *tag* start tag in *xml*, with or without a namespace prefix."""
    return [found.group(1) for found in re.finditer(rb"<(?:[\w.-]+:)?" + tag + rb"\s([^>]*?)/?>", xml)]


def _attributes(tag: bytes) -> dict[str, str]:
    """Return a start tag's attributes, each by its name without a namespace prefix, its value unescaped."""
    return {
        name.decode(): html.unescape((double or single).decode()) for name, double, single in _ATTRIBUTE.findall(tag)
    }
