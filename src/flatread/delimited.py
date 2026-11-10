r"""Delimited text: a CSV, pipe or tab file read by one ``pl.read_csv`` of the whole body, every cell text.

polars pads a short row with empty cells and has no switch to refuse it, so the fast path appends
``separator + "\x01"`` to every line before the read: a row of the right width ends in a sentinel column holding
``"\x01"``, a short row leaves that column empty, and a long row makes polars raise. The fast path applies only when
every line is a record (no quote, or an even number on every line), line endings are uniform (no CR, or every one
before an LF), and no byte ``\x01`` is in the body; blank lines are framing and go first. Any other body, and any body
the fast path refuses, is read by the stdlib ``csv`` module, which names the line of a fault. The success path tracks
no line numbers: an error's line is found when the error is built.

Around the table a file may write a ``preamble`` (its first line that is not blank) and a ``trailer`` (its last), each
a :class:`Line` whose named groups become columns or a row count to check.
"""

import codecs
import csv
import io
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar, Final, override

import polars as pl

from flatread.core import Plan, Text, column_names
from flatread.errors import (
    ControlTotalError,
    EncodingError,
    RaggedRowError,
    TruncatedFileError,
    UnknownLayoutError,
)
from flatread.layout import Layout

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = ["Csv", "Line", "Pipe", "Tsv"]

_SENTINEL: Final = "\x01"
_TRAIL: Final = "\x00trail"


@dataclass(frozen=True, slots=True)
class Line:
    """A line a file writes about itself around its rows: its pattern, and what its named groups are."""

    #: A regex the whole line matches; its named groups are read by *carry* and *count*.
    pattern: str
    #: Group to field: a value put on every row (a stamp as ``generated_at``).
    carry: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    #: The group that must equal the number of rows read.
    count: str | None = None


@dataclass(slots=True)
class _Framed:
    """A body split into what surrounds its rows and the rows' own bytes."""

    header: list[str] | None
    headings: list[str] | None
    rows: bytes
    #: The 1-based line of the first byte of *rows*.
    first_line: int
    carried: dict[str, str | None]
    count: int | None


class Csv(Layout):
    """A delimited text file: CSV by default; :class:`Pipe` and :class:`Tsv` set the separator."""

    separator: ClassVar[str] = ","
    quoted: ClassVar[bool] = True
    header: ClassVar[bool] = True
    skip_lines: ClassVar[int] = 0
    header_line: ClassVar[str | None] = None
    trailing_separator: ClassVar[bool] = False
    final_line_ending: ClassVar[bool] = True
    encoding: ClassVar[str] = "utf-8"
    preamble: ClassVar[Line | None] = None
    trailer: ClassVar[Line | None] = None

    _SETTINGS: ClassVar[frozenset[str]] = Layout._SETTINGS | {  # noqa: SLF001  # a base extends its parent's
        "separator", "quoted", "header", "skip_lines", "header_line", "trailing_separator", "final_line_ending",
        "encoding", "preamble", "trailer",
    }  # fmt: skip

    @classmethod
    @override
    def _compile(cls, row: type) -> Plan:
        if len(cls.separator.encode()) != 1:
            msg = f"{cls.__name__}: the separator {cls.separator!r} is not one byte"
            raise TypeError(msg)
        codecs.lookup(cls.encoding)
        for line in (cls.preamble, cls.trailer):
            if line is not None:
                groups = re.compile(line.pattern).groupindex
                for group in (*line.carry, *((line.count,) if line.count is not None else ())):
                    if group not in groups:
                        msg = f"{cls.__name__}: the pattern {line.pattern!r} has no group {group!r}"
                        raise TypeError(msg)
        if cls.header_line is not None:
            re.compile(cls.header_line)
        return super()._compile(row)  # ty: ignore[invalid-argument-type]

    @classmethod
    @override
    def _carried_fields(cls) -> frozenset[str]:
        lines = [line for line in (cls.preamble, cls.trailer) if line is not None]
        return frozenset(f for line in lines for f in line.carry.values())

    @classmethod
    def _decode(cls, body: bytes) -> bytes:
        """Return *body* as UTF-8 without a byte-order mark; text not in the layout's encoding raises."""
        if cls.encoding.replace("_", "-").lower() in {"utf-8", "utf8"}:
            body = body.removeprefix(codecs.BOM_UTF8)
            try:
                body.isascii() or body.decode()  # ASCII is UTF-8, checked without a copy
            except UnicodeDecodeError as exc:
                msg = f"the body is not UTF-8: {exc}"
                raise EncodingError(msg) from exc
            return body
        try:
            return body.decode(cls.encoding).encode()
        except UnicodeDecodeError as exc:
            msg = f"the body is not {cls.encoding}: {exc}"
            raise EncodingError(msg) from exc

    @classmethod
    def _fields(cls, line: bytes) -> list[str]:
        """Return one line's fields, as the file's quoting reads them."""
        text = line.decode().removesuffix("\r")
        if not cls.quoted:
            return text.split(cls.separator)
        try:
            return next(csv.reader([text], delimiter=cls.separator, strict=True), [""])
        except csv.Error as exc:
            msg = f"the header (or headings) line {text[:200]!r} does not parse: {exc}"
            raise UnknownLayoutError(msg) from exc

    @classmethod
    def _trailer(cls, body: bytes) -> tuple[dict[str, str | None], int | None, int]:
        """Return the trailer's carried values, its row count, and where the rows end: the trailer's start."""
        if cls.trailer is None:
            return {}, None, len(body)
        end = len(body.rstrip(b"\r\n"))
        start = body.rfind(b"\n", 0, end) + 1
        line = body[start:end].decode().removesuffix("\r")
        found = re.fullmatch(cls.trailer.pattern, line)
        if found is None:
            msg = f"the file's last line {line[:200]!r} is not its trailer: it may have been cut off"
            raise TruncatedFileError(msg)
        count = int(found[cls.trailer.count]) if cls.trailer.count is not None else None
        return {f: found[g] for g, f in cls.trailer.carry.items()}, count, start

    @classmethod
    def _frame(cls, body: bytes) -> _Framed:  # one pass over the lines around the rows
        """Split *body* into its preamble, header, headings, rows and trailer, the top and bottom read line by line."""
        if cls.final_line_ending and not body.endswith(b"\n"):
            tail = body.rsplit(b"\n", 1)[-1][:200]
            msg = f"the file ends mid-line, with {tail!r}, so it may have been cut off"
            raise TruncatedFileError(msg)
        carried, count, end = cls._trailer(body)
        pos, number = 0, 1
        wanted = ["preamble"] * (cls.preamble is not None) + ["skip"] * cls.skip_lines
        if cls.header_line is not None:
            wanted.append("find")
        wanted += ["headings"] * (cls.headings is not None) + ["header"] * cls.header
        header = headings = None
        while wanted:
            if pos >= end:
                if wanted[0] == "find":
                    msg = f"no line matches the header pattern {cls.header_line!r}"
                    raise UnknownLayoutError(msg)
                what = {"preamble": "preamble", "skip": "skipped lines", "headings": "headings", "header": "header"}
                msg = f"the file ends before its {what[wanted[0]]}"
                raise UnknownLayoutError(msg)
            stop = body.find(b"\n", pos, end)
            stop = end if stop < 0 else stop
            line, pos, number = body[pos:stop], stop + 1, number + 1
            if not line.strip(b"\r") and wanted[0] != "skip":
                continue
            step = wanted[0]
            if step == "preamble" and cls.preamble is not None:
                text = line.decode().removesuffix("\r")
                if (found := re.fullmatch(cls.preamble.pattern, text)) is None:
                    msg = f"line {number - 1} {text[:200]!r} is not the file's preamble"
                    raise UnknownLayoutError(msg)
                carried |= {f: found[g] for g, f in cls.preamble.carry.items()}
                count = int(found[cls.preamble.count]) if cls.preamble.count is not None else count
            elif step == "find" and cls.header_line is not None:
                if not re.fullmatch(cls.header_line, line.decode().removesuffix("\r")):
                    continue
                pos, number = pos - len(line) - 1, number - 1  # the matching line is the header (or headings)
            elif step == "headings":
                headings = cls._fields(line)
            elif step == "header":
                header = cls._fields(line)
            wanted.pop(0)
        return _Framed(header, headings, body[pos:end], number, carried, count)

    @staticmethod
    def _fast(rows: bytes) -> bool:
        """Return whether *rows* reads on the fast path: every line a record, uniform endings, no sentinel byte."""
        if _SENTINEL.encode() in rows:
            return False
        if b"\r" in rows and rows.count(b"\r\n") != rows.count(b"\r"):
            return False
        if b'"' in rows:
            lines = pl.Series([rows.decode()]).str.split("\n").explode()
            return bool((lines.str.count_matches('"', literal=True) % 2 == 0).all())
        return True

    @classmethod
    def _width(cls, labels: list[str]) -> list[str]:
        return [*column_names(labels), *([_TRAIL] if cls.trailing_separator else [])]

    @classmethod
    def _read_fast(cls, rows: bytes, names: list[str]) -> pl.DataFrame | None:
        """Return *rows* read by one ``pl.read_csv``, or ``None`` where a row is not *names*' width."""
        data = rows.replace(b"\r\n", b"\n") if b"\r" in rows else rows
        while b"\n\n" in data:
            data = data.replace(b"\n\n", b"\n")
        data = data.removeprefix(b"\n")
        if not data:
            return pl.DataFrame(schema=dict.fromkeys(names, pl.String))
        if not data.endswith(b"\n"):
            data += b"\n"
        data = data.replace(b"\n", f"{cls.separator}{_SENTINEL}\n".encode())
        try:
            frame = pl.read_csv(
                data,
                has_header=False,
                separator=cls.separator,
                quote_char='"' if cls.quoted else None,
                schema=dict.fromkeys([*names, _SENTINEL], pl.String),
                infer_schema=False,
                empty_string_is_null=False,
            )
        except pl.exceptions.PolarsError:
            return None
        if not frame[_SENTINEL].eq_missing(_SENTINEL).all():
            return None
        return frame.drop(_SENTINEL)

    @classmethod
    def _read_slow(cls, rows: bytes, names: list[str], first_line: int) -> tuple[pl.DataFrame, list[int]]:
        """Return *rows* read by the ``csv`` module, and each record's first line; a fault raises, naming its line."""
        text = rows.decode()
        source = io.StringIO(text, newline="")
        if cls.quoted:
            reader = csv.reader(source, delimiter=cls.separator, quotechar='"', strict=True)
        else:
            reader = csv.reader(source, delimiter=cls.separator, quoting=csv.QUOTE_NONE, strict=True)
        records: list[list[str]] = []
        lines: list[int] = []
        last = 0
        try:
            for record in reader:
                start, last = first_line + last, reader.line_num
                if not record or (record == [""] and len(names) > 1):
                    continue
                if len(record) != len(names):
                    wide, want = len(record) - cls.trailing_separator, len(names) - cls.trailing_separator
                    msg = f"line {start} has {wide} fields, not the header's {want}"
                    raise RaggedRowError(msg)
                records.append(record)
                lines.append(start)
        except csv.Error as exc:
            msg = f"line {first_line + reader.line_num - 1}: {exc}"
            raise UnknownLayoutError(msg) from exc
        frame = pl.DataFrame(records, schema=dict.fromkeys(names, pl.String), orient="row")
        return frame, lines

    @classmethod
    def _lines_of(cls, rows: bytes, first_line: int) -> Callable[[int], str]:
        """Return the locate of the fast path's rows: each a line that is not blank, counted when an error is built."""

        def locate(i: int) -> str:
            filled = [n for n, line in enumerate(rows.split(b"\n")) if line.strip(b"\r")]
            return f"line {first_line + filled[i]}"

        return locate

    @classmethod
    @override
    def read_text(cls, body: bytes) -> Text:
        framed = cls._frame(cls._decode(body))
        if framed.header is None:
            labels = [label for label, _ in cls._plan.label_sets[0]]
        else:
            labels = framed.header
            if cls.trailing_separator:
                if labels[-1:] != [""]:
                    msg = f"the header {labels!r} does not end with the separator {cls.separator!r}"
                    raise UnknownLayoutError(msg)
                labels = labels[:-1]
        names = cls._width(labels)
        frame = cls._read_fast(framed.rows, names) if cls._fast(framed.rows) else None
        if frame is not None:
            locate = cls._lines_of(framed.rows, framed.first_line)
        else:
            frame, lines = cls._read_slow(framed.rows, names, framed.first_line)
            locate = lambda i: f"line {lines[i]}"  # noqa: E731  # the slow path counted every record's line
        if cls.trailing_separator:
            if (filled := frame[_TRAIL] != "").any():
                msg = f"{locate(int(filled.arg_true()[0]))} does not end with the separator {cls.separator!r}"
                raise RaggedRowError(msg)
            frame = frame.drop(_TRAIL)
        if framed.count is not None and framed.count != frame.height:
            msg = f"the file has {frame.height} rows, but its trailer says {framed.count}"
            raise ControlTotalError(msg)
        headings = None if framed.headings is None else tuple(h or None for h in framed.headings)
        return Text(frame, locate, framed.carried, headings)


class Pipe(Csv):
    """A pipe-separated file with no quoting: Nasdaq Trader, IBKR, FINRA, Cboe, OCC."""

    separator = "|"
    quoted = False


class Tsv(Csv):
    """A tab-separated file."""

    separator = "\t"
