"""A delimited text file read by ``pl.read_csv`` into its row struct's typed columns, under the file's own labels.

:func:`read_csv` checks the header (:func:`~flatread.cells.match_header`) and each record's field count, since polars
pads a short row with empty cells; one ``pl.read_csv`` then reads every cell as text and
:func:`~flatread.cells.read_table` reads each strictly. A quoted field may hold a line ending, so a record may span
lines; it is named by its first. Blank lines outside a quoted field are framing. :func:`stream_csv` reads a file too
large to hold the same way, a batch of lines at a time. :func:`read_header` reads a header alone, for a file whose row
struct is built from it.
"""

import codecs
import re
from typing import TYPE_CHECKING, Final, NamedTuple

import polars as pl

from flatread.cells import column_specs, match_header, read_table, wire_names
from flatread.errors import (
    EmptyBodyError,
    EncodingError,
    RaggedRowError,
    ReadError,
    TruncatedFileError,
    UnknownLayoutError,
)
from flatread.text import decode_utf8

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import msgspec

__all__ = ["Header", "read_csv", "read_header", "stream_csv"]

_QUOTED: Final = r'"(?:[^"]|"")*"'  # a quoted field, its doubled quotes included: no separator inside it counts


class Header(NamedTuple):
    """A file's header, as :func:`read_header` reads it: its line in the file and its labels as written."""

    #: The header's line, 1-based, blank lines before it counted.
    line: int
    #: The labels, in the header's order, each as written: a repeated label is not renamed, an empty one is ``""``.
    labels: tuple[str, ...]


def read_header(body: bytes | str, *, separator: str = ",", quoted: bool = False) -> Header:
    """Read the header :func:`read_csv` would: the first line of *body* that is not blank, its fields its labels.

    The line is read by the same ``pl.read_csv`` call as the file, as a row rather than a header, so a label written
    twice stays as written (:func:`read_csv` names the second as polars does, ``KEY_duplicated_0``). Raises
    :class:`~flatread.errors.EmptyBodyError` for a body with no line that is not blank,
    :class:`~flatread.errors.EncodingError` for bytes that are not UTF-8, and
    :class:`~flatread.errors.UnknownLayoutError` for a line polars cannot read (a quote that does not close).
    """
    lines = _lines(body if isinstance(body, str) else decode_utf8(body))
    if (at := _first_filled(lines)) is None:
        msg = "the file is empty"
        raise EmptyBodyError(msg)
    row = _read((lines[at] + "\n").encode(), separator, quoted, None, first=at + 1, has_header=False).row(0)
    return Header(at + 1, tuple("" if label is None else str(label) for label in row))


def read_csv(  # noqa: PLR0913  # the body and row, then the file's options
    body: bytes | str,
    row_type: type[msgspec.Struct],
    *,
    separator: str = ",",
    quoted: bool = False,
    header: bool = True,
    skip_lines: int = 0,
    trailing_separator: bool = False,
    line_ending: bool = False,
    ordered: bool = True,
    first_line: int = 1,
) -> pl.DataFrame:
    """Read *body* (UTF-8, or text already read) into *row_type*'s columns, typed, under the file's labels.

    *quoted* reads CSV's quoting; *header* false names the columns by *row_type*'s labels; *skip_lines* lines come
    before the header. *trailing_separator* says every line ends with the separator, so the last field must be empty.
    *line_ending* says the file ends with a line ending, so one without was cut off. *ordered* false lets the header
    give the columns in any order. *first_line* is the body's first line's number in the file, for an error.
    """
    text = body if isinstance(body, str) else decode_utf8(body)
    if not text.strip():
        msg = "the file is empty"
        raise EmptyBodyError(msg)
    if line_ending and not text.endswith("\n"):
        msg = f"the file ends mid-line, with {text.rsplit(chr(10), 1)[-1][:200]!r}, so it may have been cut off"
        raise TruncatedFileError(msg)
    lines = _lines(text).slice(skip_lines)
    first_line += skip_lines
    trailer = [""] if trailing_separator else []
    if header:
        if (at := _first_filled(lines)) is None:
            msg = f"the file has no header after its {skip_lines} skipped lines"
            raise EmptyBodyError(msg)
        first_line += at
        columns = _read((lines[at] + "\n").encode(), separator, quoted, None, first=first_line, has_header=True).columns
        lines, first_line = lines.slice(at + 1), first_line + 1
        if trailer and columns[-1:] != trailer:
            msg = f"the header {columns!r} does not end with the separator {separator!r}"
            raise UnknownLayoutError(msg)
        columns = columns[: len(columns) - len(trailer)]
    else:
        columns = wire_names([spec.labels[0] for spec in column_specs(row_type)])
    match_header(columns, row_type, ordered=ordered)
    rows, numbers, unclosed = _records(lines, first_line, quoted)
    whole = rows if unclosed is None else rows.head(unclosed)
    ragged = _ragged(whole, numbers, len(columns) + len(trailer), separator, quoted)
    if ragged is None and unclosed is not None:  # a ragged row before the open quote is named first
        ragged = unclosed, _unclosed(rows[unclosed].split("\n", 1)[0], numbers[unclosed], separator)
    if ragged is not None:  # a bad cell before a ragged row is named first
        rows, numbers = rows.head(ragged[0]), numbers.head(ragged[0])
    schema: dict[str, type[pl.DataType]] = dict.fromkeys([*columns, *trailer], pl.String)
    frame = pl.DataFrame(schema=schema)
    if rows.len():
        data = (rows.str.join("\n").item() + "\n").encode()
        frame = _read(data, separator, quoted, schema, first=numbers[0], last=numbers[-1], has_header=False)
    if trailer and (filled := frame[""].is_not_null()).any():
        at = int(filled.arg_true()[0])
        ragged = at, RaggedRowError(f"line {numbers[at]} does not end with the separator {separator!r}")
        frame, numbers = frame.head(at), numbers.head(at)
    table = read_table(frame.drop(trailer), row_type, numbers=numbers, ordered=ordered)
    if ragged is not None:
        raise ragged[1]
    return table


def _lines(text: str) -> pl.Series:
    """Return *text*'s lines, each without its line ending (LF, or CRLF)."""
    return pl.Series([text]).str.split("\n").explode().str.strip_suffix("\r")


def _first_filled(lines: pl.Series) -> int | None:
    """Return the index of the first of *lines* that is not blank, or ``None`` when every one is."""
    filled = (lines != "").arg_true()
    return int(filled[0]) if filled.len() else None


def _records(lines: pl.Series, first_line: int, quoted: bool) -> tuple[pl.Series, pl.Series, int | None]:  # noqa: FBT001
    """Return the file's records, each one's first line, and the one whose quoted field does not close, if any.

    A record is a line, or in a quoted file the lines a quoted field holding a line ending spans, joined by LF. Only a
    blank line outside a quoted field is framing.
    """
    frame = pl.DataFrame({"row": lines}).with_row_index("line", offset=first_line)
    quotes = frame["row"].str.count_matches('"', literal=True) if quoted else None
    unclosed = False
    if quotes is not None and (quotes % 2 == 1).any():  # else each line is its own record
        within = (quotes.cum_sum() - quotes) % 2 == 1  # the line opens inside a quoted field
        spanned = within | within.shift(-1, fill_value=False)  # a line of a record of several
        frame = frame.with_columns(record=(~within).cum_sum())
        joined = (  # grouped apart from the lines that are records alone, which are most
            frame.filter(spanned)
            .group_by("record", maintain_order=True)
            .agg(pl.col("line").first(), pl.col("row").str.join("\n"))
        )
        frame = pl.concat([frame.filter(~spanned), joined.select(frame.columns)]).sort("record")
        unclosed = quotes.sum() % 2 == 1
    frame = frame.filter(pl.col("row") != "")
    return frame["row"], frame["line"].cast(pl.Int64), frame.height - 1 if unclosed else None


def _unclosed(opened: str, line: int, separator: str) -> UnknownLayoutError:
    """Return the error for the record of *line*, whose first line *opened* holds a quote nothing pairs.

    Its fields are read as CSV reads them, left to right: a field that does not open with a quote but holds one
    (``5" pipe``) has a quote inside it, which opens nothing; otherwise a quoted field does not close.
    """
    at = 0
    while at <= len(opened):
        if opened.startswith('"', at):  # a quoted field ends at a quote before the separator or the line's end
            close = re.compile(rf'"(?:{re.escape(separator)}|$)').search(opened, at + 1)
            if close is None:
                break
            at = close.end()
            continue
        end = opened.find(separator, at)
        if '"' in opened[at : len(opened) if end < 0 else end]:
            return UnknownLayoutError(f"line {line} has a quote inside a field, not around it: {opened[:200]!r}")
        if end < 0:
            break
        at = end + 1
    return UnknownLayoutError(f"line {line} opens a quoted field that does not close: {opened[:200]!r}")


def _ragged(
    rows: pl.Series,
    numbers: pl.Series,
    width: int,
    separator: str,
    quoted: bool,  # noqa: FBT001
) -> tuple[int, ReadError] | None:
    """Return the first row of other than *width* fields, and its error; a quoted field is taken whole.

    In a quoted file a field is either quoted whole or holds no quote: text beside a quoted field (``"x"y``) is not
    one field polars and the count agree on, so it raises before polars reads it.
    """
    fields = (rows.str.replace_all(_QUOTED, "") if quoted else rows).str.count_matches(separator, literal=True) + 1
    bad = fields != width
    malformed = ~rows.str.contains(_fields(separator)) if quoted else None
    if malformed is not None:
        bad |= malformed
    if not bad.any():
        return None
    at = int(bad.arg_true()[0])
    if malformed is not None and malformed[at]:
        msg = f"line {numbers[at]} has a quote inside a field, not around it: {rows[at][:200]!r}"
        return at, UnknownLayoutError(msg)
    msg = f"line {numbers[at]} has {fields[at]} fields, not the header's {width}: {rows[at][:200]!r}"
    return at, RaggedRowError(msg)


def _fields(separator: str) -> str:
    """Return the pattern of a quoted record: its fields apart by *separator*, each quoted whole or holding no quote."""
    sep = f"\\{separator}" if separator in "]\\^-[" else separator
    field = f'(?:{_QUOTED}|[^"{sep}]*)'
    return f"^{field}(?:[{sep}]{field})*$"


def _read(  # noqa: PLR0913  # the data and its options, then the lines it spans
    data: bytes,
    separator: str,
    quoted: bool,  # noqa: FBT001
    schema: dict[str, type[pl.DataType]] | None,
    *,
    first: int,
    last: int | None = None,
    has_header: bool,
) -> pl.DataFrame:
    """Read *data* with one ``pl.read_csv``, every cell text: under *schema*, or its first line's labels (*has_header*).

    An error names the lines *first* to *last* the data spans.
    """
    opened = data.startswith(codecs.BOM_UTF8)  # polars drops a mark that opens its bytes, so a line goes first
    try:
        return pl.read_csv(
            b"-\n" + data if opened else data,
            separator=separator,
            quote_char='"' if quoted else None,
            has_header=has_header,
            schema=schema,
            infer_schema=False,
            skip_rows=int(opened),
        )
    except pl.exceptions.PolarsError as exc:  # the checks before it leave polars nothing it should refuse
        where = f"line {first}" if last in {None, first} else f"lines {first} to {last}"
        msg = f"polars cannot read {where}: {exc}"
        raise UnknownLayoutError(msg) from exc


async def stream_csv(  # noqa: PLR0913  # the stream and row, its batch, then read_csv's options
    chunks: AsyncIterator[bytes],
    row_type: type[msgspec.Struct],
    *,
    batch_rows: int,
    separator: str = ",",
    quoted: bool = False,
    header: bool = True,
    ordered: bool = True,
) -> AsyncIterator[pl.DataFrame]:
    """Yield the file's rows as :func:`read_csv` reads them, a frame of *batch_rows* lines at a time.

    The file is UTF-8 and ends with a line ending; a quoted field holding one must not straddle a batch, and one that
    does reads as a quote that does not close. A batch with a fault raises; the batches before it have been yielded.
    """
    if batch_rows < 1:
        msg = f"a batch holds at least one line, not {batch_rows}"
        raise ValueError(msg)
    head, line, first, filled = "", 1, True, False
    async for batch in _batches(chunks, batch_rows, first=batch_rows + header):
        text, first = _decode(batch, first=first), False
        if (at := _first_filled(_lines(text))) is None:  # blank lines are framing, before the header as after it
            line += text.count("\n")
            continue
        if header and not head:
            lines = text.split("\n")
            head, text, line = lines[at] + "\n", "\n".join(lines[at + 1 :]), line + at + 1
        filled = True
        frame = read_csv(
            head + text,
            row_type,
            separator=separator,
            quoted=quoted,
            header=header,
            ordered=ordered,
            first_line=line - header,
        )
        line += text.count("\n")
        if frame.height:
            yield frame
    if not filled:
        msg = "the file is empty"
        raise EmptyBodyError(msg)


def _decode(batch: bytes, *, first: bool) -> str:
    """Decode a batch of the stream; a byte-order mark is the file's only when it opens the first."""
    if first:
        return decode_utf8(batch)
    try:
        return batch.decode()
    except UnicodeDecodeError as exc:
        msg = f"the file is not UTF-8: {exc}"
        raise EncodingError(msg) from exc


async def _batches(chunks: AsyncIterator[bytes], lines: int, *, first: int) -> AsyncIterator[bytes]:
    """Yield the stream's bytes *first*, then *lines*, lines at a time; a last line without its ending raises."""
    pending, need = b"", first
    async for chunk in chunks:
        pending += chunk
        while pending.count(b"\n") >= need:
            *batch, pending = pending.split(b"\n", need)
            yield b"\n".join(batch) + b"\n"
            need = lines
    if b"\n" in pending:
        batch, pending = pending.rsplit(b"\n", 1)
        yield batch + b"\n"
    if pending:
        msg = f"the file ends mid-line, with {decode_utf8(pending)[:200]!r}, so it may have been cut off"
        raise TruncatedFileError(msg)
