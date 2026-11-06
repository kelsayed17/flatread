"""The frame half of the text readers: a row struct's DataFrame schema, a file's lines, a frame's typed rows.

:mod:`flatread.text` gives a file's numbered lines without polars; this module counts, checks and splits them into a
DataFrame. :mod:`flatread.delimited` runs its steps for every declared file, and a reader of a file that is not
delimited (a matrix, a prose table, a data set read by name) uses them itself. :func:`frame_rows`
turns a frame into typed rows, and :func:`stream_rows` a streamed file's frames.

**A delimited file's steps.** Most text wires are a header line and rows of fields split on one separator. What makes
the wire its own (the header it expects, a trailer, each column's type) is its declaration; the steps are here:

* :func:`counted_lines` reads the file's lines with each one's field count, and with ``quoted=True`` its quote count;
* :func:`frame_line` names one of them as a :class:`~flatread.text.Line`, for a header or trailer check;
* :func:`check_widths` refuses the first row whose fields are not the header's, or whose quotes do not close;
* :func:`split_fields` splits checked rows on a separator that no field quotes, and :func:`read_quoted_rows` reads
  checked rows whose fields may be quoted;
"""

import datetime as dt
import re
import types
from typing import TYPE_CHECKING, Annotated, Final, Union, get_args, get_origin

import msgspec
import polars as pl

from flatread.cells import is_utc_datetime
from flatread.errors import EmptyBodyError, RaggedRowError, UnknownLayoutError
from flatread.text import Line

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

__all__ = [
    "check_widths",
    "counted_batch",
    "counted_lines",
    "first_ragged",
    "frame_line",
    "frame_lines",
    "frame_rows",
    "frame_schema",
    "read_quoted_rows",
    "split_fields",
    "stream_rows",
]

#: A quoted CSV field, a doubled quote inside it included: what a field count removes before counting separators.
_QUOTED: Final = r'"(?:[^"]|"")*"'

#: How much of a line an error quotes: enough to recognize it, never a whole runaway line.
_SHOWN: Final = 200

#: The row-index column a check adds, named so no field can be: a field name is an identifier.
_AT: Final = "row index"

#: The column type of each field type a row struct may declare. Exact types: a ``datetime`` field is not a date column.
_SCALARS: Final[dict[type, type[pl.DataType]]] = {
    str: pl.String,
    int: pl.Int64,
    float: pl.Float64,
    bool: pl.Boolean,
    dt.date: pl.Date,
    dt.datetime: pl.Datetime,
}


#: The separator tried first: absent from every text file read, so found absent by one scan.
_NUL: Final = chr(0)


def frame_schema(row_type: type[msgspec.Struct]) -> pl.Schema:
    """Return the DataFrame schema of *row_type*'s rows: one column per field, named and typed as the field is.

    A frame reader reads its file straight into this schema, so the frame's columns are the row struct's fields and the
    struct stays the one statement of the wire's shape. A NewType is its supertype's column (a ``NewType("Cusip",
    str)`` is a string), and ``X | None`` is ``X``'s.
    """
    return pl.Schema({field.name: _dtype(field.type) for field in msgspec.structs.fields(row_type)})


def frame_lines(text: str) -> pl.DataFrame:
    """Return the file's non-empty lines as a DataFrame: each one's ``number`` in the file, and its text, ``line``.

    polars reads the file once, each line whole, with :func:`~flatread.text.lines`' rules: a line ends at an
    LF, a CR just before it or ending the file is dropped, and an empty line is framing. *text* is
    :func:`~flatread.text.decode_text`'s, so the encoding and the byte-order mark are already settled. The same
    errors as :func:`~flatread.text.lines`, with one more: a file holding every ASCII control character raises
    :class:`~flatread.errors.UnknownLayoutError`, since polars needs one it lacks to read lines whole.
    """
    if not text:
        msg = "the file is empty"
        raise EmptyBodyError(msg)
    read = pl.read_csv(
        text.encode(),
        separator=_separator_absent_from(text),
        quote_char=None,
        has_header=False,
        schema={"line": pl.String},
        empty_string_is_null=False,
        row_index_name="number",
        row_index_offset=1,
    )
    found = read.filter(pl.col("line").str.len_bytes() > 0)
    if found.is_empty():
        msg = "the file holds only blank lines"
        raise EmptyBodyError(msg)
    return found


async def stream_rows[RowT: msgspec.Struct](
    frames: AsyncIterator[pl.DataFrame], row_type: type[RowT]
) -> AsyncIterator[RowT]:
    """Yield the rows of a streamed file's *frames*, one *row_type* per row, in order, one frame held at a time.

    Each frame's columns are *row_type*'s fields in order (:func:`frame_schema`), so a row's values build the struct
    positionally. An error *frames* raises is raised after the rows of the frames before it.
    """
    async for frame in frames:
        for row in frame_rows(frame, row_type):
            yield row


def frame_rows[RowT: msgspec.Struct](frame: pl.DataFrame, row_type: type[RowT]) -> list[RowT]:
    """Return *frame*'s rows, one *row_type* per row, in order.

    The frame's columns are *row_type*'s fields in order (:func:`frame_schema`), so a row's values build the struct
    positionally. Each column becomes Python values in one call and the rows are zipped from them, which measured 1.6
    times as fast as ``iter_rows`` over a million rows (2026-11).
    """
    return list(map(row_type, *(column.to_list() for column in frame.iter_columns()), strict=True))


def counted_lines(text: str, separator: str, *, quoted: bool = False) -> pl.DataFrame:
    """Return :func:`frame_lines` with each line's ``fields``: its *separator*-split field count.

    With *quoted*, a field that is quoted whole (:data:`_QUOTED`, between separators or the line's ends) is removed
    before the separators are counted, so a separator inside one does not count, as polars reads it. The quotes left
    after that are counted as ``quotes``: any is a quote that does not close or that sits inside a field, which polars
    would read otherwise than the count, and :func:`check_widths` refuses it. The columns are ``number``, ``line``,
    ``fields`` and, with *quoted*, ``quotes``.
    """
    return _counted(frame_lines(text), separator, quoted=quoted)


def counted_batch(batch: Sequence[Line], separator: str, *, quoted: bool = False) -> pl.DataFrame:
    """Return a streamed file's *batch* of lines as :func:`counted_lines` returns a whole file's: the same columns."""
    found = pl.DataFrame(
        {"number": [line.number for line in batch], "line": [line.text for line in batch]},
        schema={"number": pl.Int64, "line": pl.String},
    )
    return _counted(found, separator, quoted=quoted)


def _counted(found: pl.DataFrame, separator: str, *, quoted: bool) -> pl.DataFrame:
    """Add each line's field count, and with *quoted* its quotes left once its fields quoted whole are removed."""
    line = pl.col("line")
    if not quoted:
        return found.with_columns((line.str.count_matches(separator, literal=True) + 1).alias("fields"))
    sep = re.escape(separator)
    whole = f"(^|{sep}){_QUOTED}({sep}|$)"
    # Two passes: a match takes the separator after its field, so the next field quoted whole waits for the second.
    bare = line.str.replace_all(whole, "${1}${2}").str.replace_all(whole, "${1}${2}")
    return found.with_columns(
        (bare.str.count_matches(separator, literal=True) + 1).alias("fields"),
        bare.str.count_matches('"', literal=True).alias("quotes"),
    )


def frame_line(found: pl.DataFrame, index: int) -> Line:
    """Return the line at *index* (``-1`` the last) of :func:`frame_lines`' or :func:`counted_lines`' frame."""
    number, text = found.select("number", "line").row(index)
    return Line(number, text)


def check_widths(rows: pl.DataFrame, width: int) -> None:
    """Check that every row of :func:`counted_lines`' frame has *width* fields; the first that does not raises.

    A frame counted with ``quoted=True`` is checked for its quotes first: a row with a quote that does not close, or
    that sits inside a field, raises before any row of the wrong width, since its fields cannot be counted. Both raise
    :class:`~flatread.errors.RaggedRowError`.
    """
    if (found := first_ragged(rows, width)) is not None:
        raise found[1]


def first_ragged(rows: pl.DataFrame, width: int) -> tuple[int, RaggedRowError] | None:
    """Return the position of the first row of :func:`counted_lines`' frame that is not *width* fields, and its error.

    A row whose quotes cannot be counted is ragged too. ``None`` when every row is *width* fields: what
    :func:`check_widths` raises, for a reader that keeps the rows before it.
    """
    quoted = "quotes" in rows.columns
    bad = pl.col("fields") != width
    if quoted:
        bad |= pl.col("quotes") > 0
    suspect = rows.with_row_index(_AT).filter(bad)
    if suspect.is_empty():
        return None
    at = int(suspect[_AT][0])
    line = frame_line(suspect, 0)
    if quoted and suspect["quotes"][0] > 0:
        msg = (
            f"line {line.number} has a quote that does not close or sits inside a field, so its fields cannot be "
            f"counted: {line.text[:_SHOWN]!r}"
        )
    else:
        msg = f"line {line.number} should have {width} fields, not {suspect['fields'][0]}: {line.text[:_SHOWN]!r}"
    return at, RaggedRowError(msg)


def split_fields(lines: pl.Series, separator: str, names: list[str]) -> pl.DataFrame:
    """Split each line on *separator* into the string columns *names*, every cell as written.

    For lines :func:`check_widths` has passed, of a wire that never quotes a field: each has ``len(names)`` fields.
    """
    return lines.str.splitn(separator, len(names)).struct.rename_fields(names).struct.unnest()


def read_quoted_rows(
    lines: pl.Series,
    schema: Mapping[str, pl.DataType | type[pl.DataType]],
    *,
    separator: str = ",",
    empty_string_is_null: bool = True,
) -> pl.DataFrame:
    """Read the checked rows *lines*, their fields quoted CSV's way or not, into *schema*'s text columns, in order.

    For the rows of :func:`counted_lines` counted with ``quoted=True`` that :func:`check_widths` passed: only those
    lines are read, so framing lines around them (a header, a count line, a trailer) of any width never reach polars,
    and a quoted field never spans a line, since a quote that does not close has been refused. A row polars still reads
    otherwise than counted raises :class:`~flatread.errors.RaggedRowError`.
    """
    if lines.is_empty():
        return pl.DataFrame(schema=schema)
    try:
        return pl.read_csv(
            "\n".join(lines).encode(),
            separator=separator,
            quote_char='"',
            has_header=False,
            new_columns=list(schema),
            schema_overrides=schema,
            empty_string_is_null=empty_string_is_null,
        )
    except pl.exceptions.PolarsError as exc:
        # The checks agree with polars on every row they pass; this is the guard should a row they cannot see differ.
        msg = f"the rows cannot be read as CSV: {exc}"
        raise RaggedRowError(msg) from exc


def _separator_absent_from(text: str) -> str:
    """Return an ASCII control character the file does not contain, so that polars reads each line whole.

    polars has no line reader of its own; a separator that never occurs is how ``read_csv`` becomes one. NUL, absent
    from every text file read, is found absent by one scan; only a file holding it has its characters gathered, once.
    """
    if _NUL not in text:
        return _NUL
    present = set(text)
    for code in range(1, 32):
        if (character := chr(code)) not in "\n\r" and character not in present:
            return character
    msg = "the file holds every ASCII control character, so it is not a text file"
    raise UnknownLayoutError(msg)


def _dtype(annotation: object) -> pl.DataType:
    """Return the polars type of a field annotated *annotation*: a scalar :data:`_SCALARS` names, or one ``| None``.

    A :data:`~flatread.cells.UtcDatetime` is a ``Datetime`` column in UTC.
    """
    if get_origin(annotation) is Annotated:  # a declared Column, or other metadata: the type it annotates
        annotation = get_args(annotation)[0]
    if is_utc_datetime(annotation):
        return pl.Datetime("us", "UTC")
    while (supertype := getattr(annotation, "__supertype__", None)) is not None:
        annotation = supertype
    if get_origin(annotation) in {Union, types.UnionType}:
        arms = [arm for arm in get_args(annotation) if arm is not type(None)]
        if len(arms) == 1:
            return _dtype(arms[0])
    if isinstance(annotation, type) and (dtype := _SCALARS.get(annotation)) is not None:
        return dtype()
    msg = f"no frame column type for a field annotated {annotation!r}"
    raise TypeError(msg)
