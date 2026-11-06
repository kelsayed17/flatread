"""A delimited text file read by ``pl.read_csv`` into its row struct's typed columns, under the file's own labels.

:func:`read_csv` checks the header (:func:`~flatread.cells.match_header`) and each line's field count, since polars
pads a short row with empty cells; one ``pl.read_csv`` then reads every cell as text and
:func:`~flatread.cells.read_table` reads each strictly. Blank lines are framing. :func:`stream_csv` reads a file too
large to hold the same way, a batch of lines at a time.
"""

from typing import TYPE_CHECKING, Final

import polars as pl

from flatread.cells import column_specs, match_header, read_table, wire_names
from flatread.errors import EmptyBodyError, RaggedRowError, TruncatedFileError, UnknownLayoutError
from flatread.text import decode_utf8

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import msgspec

__all__ = ["read_csv", "stream_csv"]

_QUOTED: Final = r'"(?:[^"]|"")*"'  # a quoted field, its doubled quotes included: no separator inside it counts


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
    lines = [line.removesuffix("\r") for line in text.split("\n")][skip_lines:]
    first_line += skip_lines
    trailer = [""] if trailing_separator else []
    if header:
        blank = next(at for at, line in enumerate(lines) if line)  # the text is not blank, so a line is filled
        lines, first_line = lines[blank:], first_line + blank
        columns = _read((lines[0] + "\n").encode(), separator, quoted, None).columns
        lines, first_line = lines[1:], first_line + 1
        if trailer and columns[-1:] != trailer:
            msg = f"the header {columns!r} does not end with the separator {separator!r}"
            raise UnknownLayoutError(msg)
        columns = columns[: len(columns) - len(trailer)]
    else:
        columns = wire_names([spec.labels[0] for spec in column_specs(row_type)])
    match_header(columns, row_type, ordered=ordered)
    numbers = [first_line + at for at, line in enumerate(lines) if line]
    rows = pl.Series([line for line in lines if line], dtype=pl.String)
    ragged = _ragged(rows, numbers, len(columns) + len(trailer), separator, quoted)
    if ragged is not None:  # a bad cell before a ragged row is named first
        rows, numbers = rows.head(ragged[0]), numbers[: ragged[0]]
    schema: dict[str, type[pl.DataType]] = dict.fromkeys([*columns, *trailer], pl.String)
    data = ("\n".join(rows) + "\n").encode()
    frame = _read(data, separator, quoted, schema) if rows.len() else pl.DataFrame(schema=schema)
    if trailer and (filled := frame[""].is_not_null()).any():
        at = int(filled.arg_true()[0])
        ragged = at, RaggedRowError(f"line {numbers[at]} does not end with the separator {separator!r}")
        frame, numbers = frame.head(at), numbers[:at]
    table = read_table(frame.drop(trailer), row_type, numbers=pl.Series(numbers, dtype=pl.Int64), ordered=ordered)
    if ragged is not None:
        raise ragged[1]
    return table


def _ragged(
    rows: pl.Series,
    numbers: list[int],
    width: int,
    separator: str,
    quoted: bool,  # noqa: FBT001
) -> tuple[int, RaggedRowError] | None:
    """Return the first row of other than *width* fields, and its error.

    In a quoted file a line whose quotes do not pair is part of a field that holds a line ending, and is not counted.
    """
    fields = (rows.str.replace_all(_QUOTED, "") if quoted else rows).str.count_matches(separator, literal=True) + 1
    bad = fields != width
    if quoted:
        bad &= rows.str.count_matches('"', literal=True) % 2 == 0
    if not bad.any():
        return None
    at = int(bad.arg_true()[0])
    msg = f"line {numbers[at]} has {fields[at]} fields, not the header's {width}: {rows[at][:200]!r}"
    return at, RaggedRowError(msg)


def _read(data: bytes, separator: str, quoted: bool, schema: dict[str, type[pl.DataType]] | None) -> pl.DataFrame:  # noqa: FBT001
    try:
        return pl.read_csv(
            data,
            separator=separator,
            quote_char='"' if quoted else None,
            has_header=schema is None,
            schema=schema,
            infer_schema=False,
        )
    except pl.exceptions.PolarsError as exc:
        error = RaggedRowError if "fields" in str(exc) else UnknownLayoutError
        msg = f"polars cannot read the file: {exc}"
        raise error(msg) from exc


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

    The file is UTF-8 and ends with a line ending; a quoted field holding one must not straddle a batch. A batch with a
    fault raises; the batches before it have been yielded.
    """
    if batch_rows < 1:
        msg = f"a batch holds at least one line, not {batch_rows}"
        raise ValueError(msg)
    head, line = "", 1
    async for batch in _batches(chunks, batch_rows, first=batch_rows + header):
        text = decode_utf8(batch)
        if header and not head:
            head, text = text.split("\n", 1)
            head, line = head + "\n", line + 1
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
    if line == 1:
        msg = "the file is empty"
        raise EmptyBodyError(msg)


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
