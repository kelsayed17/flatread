"""A delimited file declared once, by its structs and a few parts, and read as typed rows or as a DataFrame.

A delimited text file is declared once, as one :class:`DelimitedFile`, a module-level constant beside its structs. The
structs say what the file holds:

* the row struct's fields, each with its :class:`~flatread.cells.Column`, are the columns in order, and each cell
  is read strictly by its column (:func:`~flatread.cells.read_cells`);
* for a file that states something of itself, the file struct: its one ``list`` field is the rows, and every other
  field says where its value is written, with :class:`FromPreamble` (a line before the header), :class:`FromHeader`
  (which of the file's headers it has) or :class:`FromTrailer` (a line after the rows) in its annotation. So every line
  a file writes is a value or a check, and none is dropped unread.

The parts say how it is framed, each one thing: the body's reading (``text``, a chain of ``bytes`` to ``str`` steps such
as :func:`not_published_body`, an archive's member and the decoding), the header (:class:`Labels`, :class:`Layouts`,
:class:`ByName` or :class:`NoHeader`), and the checks the rows must pass once read (:class:`RowCount`,
:class:`ColumnTotal`). A file too large to hold is a :class:`StreamedFile`: the same header parts and cells, read a
batch at a time as its chunks arrive.

:meth:`DelimitedFile.of_rows` declares a file of rows alone, read as ``list[Row]`` and as a DataFrame;
:meth:`DelimitedFile.of_file` a file with a file struct, read as that struct and as its frame ``NamedTuple``, whose
fields are the file struct's with the rows as a DataFrame. Both forms run one pipeline, so they agree by construction,
and every file is checked in one order:

1. the body's text, by the file's ``text`` steps;
2. its lines, each with its field count (:func:`~flatread.frames.counted_lines`), quote-aware for a quoted file;
3. the preamble lines and the header, each by its part. A body that fails either, but holds the publisher's
   :class:`NotPublishedLine`, is that answer instead;
4. the line ending, when every real file ends with one: a body that is not the file has already failed its header;
5. the trailer lines, each by its part, the last line the last field's;
6. the rows: their widths (:func:`~flatread.frames.first_ragged`), then their cells, read by their columns. The
   error raised is the first faulty line's, whichever check finds it;
7. the file's checks, over its values and its rows.

A file declared wrongly fails when it is built, at import, not when it is read: a row struct whose fields declare no
column, a file struct field that says nothing of where it comes from, a frame type whose fields are not the file
struct's, a check that names a field the file has not.
"""

import csv
import datetime as dt
import itertools
import typing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Final, NamedTuple, Protocol, cast, get_args, get_origin, override

import msgspec
import polars as pl

from flatread.cells import CellError, ColumnSpec, column_specs, read_cells
from flatread.errors import (
    ControlTotalError,
    NotPublishedError,
    RaggedRowError,
    ReadError,
    TruncatedFileError,
    UnknownLayoutError,
)
from flatread.frames import (
    counted_batch,
    counted_lines,
    first_ragged,
    frame_line,
    frame_rows,
    read_quoted_rows,
    split_fields,
)
from flatread.text import Line, batched, check_line_ending, decode_utf8, stream_lines

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence

__all__ = [
    "ByName",
    "Check",
    "ColumnTotal",
    "DelimitedFile",
    "FromHeader",
    "FromPreamble",
    "FromTrailer",
    "GenerationStamp",
    "Header",
    "Labels",
    "Layouts",
    "LinePart",
    "NoHeader",
    "NotPublishedLine",
    "RowCount",
    "StreamedFile",
    "not_published_body",
    "wall_time",
]


# -- Parts for the lines before and after the rows -----------------------------------------------------------------


class LinePart[ValueT](Protocol):
    """A part that reads one line before the header or after the rows: its check, and the value it carries."""

    def read(self, line: Line) -> ValueT:
        """Check that *line* is this part's line and return its value; raise a :class:`~flatread.errors.ReadError`."""
        ...


class _Mark:
    """A file struct field's annotation saying where its value is written, compared by identity.

    ``typing`` caches an ``Annotated`` type by how its metadata compares, so a mark that compared by value would let two
    fields of one type whose parts are equal (two parts with no fields, or the same part before and after the rows)
    share one cached annotation, and with it the wrong part. A mark is equal only to itself.
    """

    __slots__ = ("part",)

    def __init__(self, part: LinePart[Any] | None = None) -> None:
        """Hold the part that reads the field's line; ``None`` for a value the header gives."""
        self.part = part

    @override
    def __repr__(self) -> str:
        """Return the mark as it is written in an annotation."""
        return f"{type(self).__name__}({'' if self.part is None else repr(self.part)})"


class FromPreamble(_Mark):
    """A file struct field's annotation: its value is a line's before the header, read by *part*, in field order."""

    __slots__ = ()

    def __init__(self, part: LinePart[Any]) -> None:
        """Hold the part that reads the line."""
        super().__init__(part)


class FromTrailer(_Mark):
    """A file struct field's annotation: its value is a line's after the rows, read by *part*, in field order."""

    __slots__ = ()

    def __init__(self, part: LinePart[Any]) -> None:
        """Hold the part that reads the line."""
        super().__init__(part)


class FromHeader(_Mark):
    """A file struct field's annotation: its value is the labels of the header the file has, as a tuple.

    With *optional*, only the labels of the ``optional`` columns the header has: which of them the file carries.
    """

    __slots__ = ("optional",)

    def __init__(self, *, optional: bool = False) -> None:
        """Mark the field as the header's, all its labels or, with *optional*, its optional columns'."""
        super().__init__(None)
        self.optional = optional


#: A generation stamp's digits: ``YYYYMMDDHHMMSS``.
_STAMP_DIGITS: Final = 14
#: Where each of a stamp's six numbers sits in it.
_STAMP_PARTS: Final = ((0, 4), (4, 6), (6, 8), (8, 10), (10, 12), (12, 14))
#: The one second past 59 a stamp reads as a time at all: a leap second.
_LEAP_SECOND: Final = 60


@dataclass(frozen=True, slots=True)
class GenerationStamp:
    """A trailer of 14 digits, ``YYYYMMDDHHMMSS``: the wall time the publisher generated the file, naive.

    A last line that is not 14 ASCII digits raises :class:`~flatread.errors.TruncatedFileError`, since a file cut
    off loses it; 14 digits that are no real date and time (a 13th month) raise
    :class:`~flatread.errors.UnknownLayoutError`, since no publisher writes one. A second of 60, a leap second,
    is the next minute's first: the time the stamp names, which a ``datetime`` cannot hold as written.
    """

    def read(self, line: Line) -> dt.datetime:
        """Check that *line* is the stamp; return its date and time."""
        stamp = line.text
        if not (len(stamp) == _STAMP_DIGITS and stamp.isascii() and stamp.isdigit()):
            msg = (
                f"no trailer: line {line.number} is {stamp!r}, not the 14-digit time the file was generated, "
                "so the file may have been cut off"
            )
            raise TruncatedFileError(msg)
        try:
            return wall_time(*(int(stamp[start:end]) for start, end in _STAMP_PARTS))
        except ValueError:
            msg = f"line {line.number}: the generation stamp {stamp!r} is no real date and time"
            raise UnknownLayoutError(msg) from None


def wall_time(year: int, month: int, day: int, hour: int, minute: int, second: int) -> dt.datetime:  # noqa: PLR0913, PLR0917  # a time's six parts
    """Return the naive wall time a file's own stamp names; raise :class:`ValueError` for one that is no real time.

    The one rule every file-written stamp reads by: a second of 60, a leap second, is the next minute's first, the time
    the stamp names, which a ``datetime`` cannot hold as written. Any other part out of its range is no real time.
    """
    leap = int(second == _LEAP_SECOND)
    moment = dt.datetime(year, month, day, hour, minute, second - leap)  # noqa: DTZ001  # the file names no zone
    try:
        return moment + dt.timedelta(seconds=leap)
    except OverflowError:  # the leap second that ends 9999, whose next minute no datetime holds
        msg = f"{moment.isoformat()} and its leap second is past the last time a datetime holds"
        raise ValueError(msg) from None


# -- The publisher's answer for a file it does not have --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NotPublishedLine:
    """A line by which a publisher answers a request for a file it does not have, and what that answer is.

    A body that fails its preamble or its header but holds this line anywhere raises
    :class:`~flatread.errors.NotPublishedError` rather than that error, so a caller walking dates can tell a day
    with no file from drift.
    """

    #: The line, exactly as the answer writes it.
    marker: str
    #: What the answer is, for the error: ``"the answer for a day with no file"``.
    meaning: str


def not_published_body(
    then: Callable[[bytes], str],
    *,
    meaning: str,
    exactly: bytes | None = None,
    containing: bytes | None = None,
) -> Callable[[bytes], str]:
    """Return a ``text`` step: a body that is the publisher's answer raises, any other goes on to *then*.

    The answer is a body that, its surrounding whitespace aside, is *exactly* a text (``b""`` for an empty body), or
    one *containing* a text, checked before anything reads the body, an archive included. It raises
    :class:`~flatread.errors.NotPublishedError` naming *meaning*.
    """

    def text(body: bytes) -> str:
        if (exactly is not None and body.strip() == exactly) or (containing is not None and containing in body):
            msg = f"not published: the body is {meaning}"
            raise NotPublishedError(msg)
        return then(body)

    return text


# -- Headers -----------------------------------------------------------------------------------------------------------


class Layout(NamedTuple):
    """A header the file may have: its labels as written, and the row struct fields they name, in order."""

    labels: tuple[str, ...]
    names: tuple[str, ...]
    #: The fields a header may leave out, for a header part that reads its layout off the line (:class:`ByName`).
    optional: tuple[str, ...] = ()


class Header(Protocol):
    """A file's header part: how many lines it is, the layouts it may name, and which one a line is."""

    @property
    def lines(self) -> int:
        """Return how many lines the header is: 1, or 0 for a file with none."""
        ...

    def layouts(self, specs: Sequence[ColumnSpec]) -> list[Layout]:
        """Return the layouts the header may have, for the row struct's columns *specs*."""
        ...

    def written(self, layout: Layout, separator: str) -> str:
        """Return the header line that gives *layout*, as the file writes it."""
        ...

    def match(self, line: Line | None, layouts: Sequence[Layout], separator: str) -> Layout:
        """Return the layout *line* is; raise :class:`~flatread.errors.UnknownLayoutError` if it is none."""
        ...


@dataclass(frozen=True, slots=True)
class _Style:
    """How a header writes its labels: quoted (but for its *bare* fields), after a prefix mark, ending with a separator.

    A line is its layout's header when it is that written line exactly, or, with *strip*, when its labels and the
    written ones agree with their padding taken off.
    """

    quote: str | None
    prefix: str
    trailing_separator: bool
    strip: bool
    bare: tuple[str, ...]

    def written(self, layout: Layout, separator: str) -> str:
        """Return the header line that gives *layout*."""
        labels = (
            label if self.quote is None or name in self.bare else f"{self.quote}{label}{self.quote}"
            for label, name in zip(layout.labels, layout.names, strict=True)
        )
        return f"{self.prefix}{separator.join(labels)}{separator if self.trailing_separator else ''}"

    def match(self, line: Line | None, layouts: Sequence[Layout], separator: str) -> Layout:
        """Return the layout whose header *line* is; raise for any other line."""
        for layout in layouts:
            if line is not None and self._same(line.text, self.written(layout, separator), separator):
                return layout
        expected = " or ".join(repr(self.written(layout, separator)) for layout in layouts)
        if line is None:
            msg = f"the file ends before its header, {expected}, so it may have been cut off"
            raise TruncatedFileError(msg)
        msg = f"line {line.number} is not the header {expected}: {line.text[:200]!r}"
        raise UnknownLayoutError(msg)

    def _same(self, text: str, written: str, separator: str) -> bool:
        """Return whether the line *text* is the header *written*."""
        if not self.strip:
            return text == written
        return [token.strip() for token in text.split(separator)] == [
            token.strip() for token in written.split(separator)
        ]


@dataclass(frozen=True, slots=True)
class Labels:
    """The header is the columns' labels, in field order: each column's label, or one of its alternatives.

    An ``optional`` column may be left out, so a file of *k* optional columns may have any of their subsets. *quote* is
    the character each label is written between, but for the fields *bare* names, which are written without it;
    *prefix* is a mark the line opens with (``#``), *trailing_separator* that the line ends with one, and *strip* that
    each label is compared with its padding taken off.
    """

    quote: str | None = None
    prefix: str = ""
    trailing_separator: bool = False
    strip: bool = False
    bare: tuple[str, ...] = ()

    @property
    def lines(self) -> int:
        """Return 1: the header is one line."""
        return 1

    def layouts(self, specs: Sequence[ColumnSpec]) -> list[Layout]:
        """Return every layout the labels make: each subset of the optional columns, each alternative label."""
        optional = [spec for spec in specs if spec.column.optional]
        found: list[Layout] = []
        for dropped in itertools.product((False, True), repeat=len(optional)):
            left = {spec.name for spec, drop in zip(optional, dropped, strict=True) if drop}
            kept = [spec for spec in specs if spec.name not in left]
            found.extend(
                Layout(tuple(labels), tuple(spec.name for spec in kept))
                for labels in itertools.product(*(spec.labels for spec in kept))
            )
        return found

    def written(self, layout: Layout, separator: str) -> str:
        """Return the header line that gives *layout*."""
        return self._style().written(layout, separator)

    def match(self, line: Line | None, layouts: Sequence[Layout], separator: str) -> Layout:
        """Return the layout *line* is."""
        return self._style().match(line, layouts, separator)

    def _style(self) -> _Style:
        """Return how the header writes its labels."""
        return _Style(self.quote, self.prefix, self.trailing_separator, self.strip, self.bare)


@dataclass(frozen=True, slots=True)
class Layouts:
    """The header is one of an enumerated set of layouts, each naming the row struct fields it carries, in order.

    For a wire whose headers are a few combinations of its columns, not every subset of them (Treasury's seven yield
    headers; EIA's three balance layouts). A field a layout leaves out reads ``None``, so it must be ``optional``. The
    style keywords are :class:`Labels`'.
    """

    names: tuple[tuple[str, ...], ...]
    quote: str | None = None
    prefix: str = ""
    trailing_separator: bool = False
    strip: bool = False
    bare: tuple[str, ...] = ()

    @property
    def lines(self) -> int:
        """Return 1: the header is one line."""
        return 1

    def layouts(self, specs: Sequence[ColumnSpec]) -> list[Layout]:
        """Return the enumerated layouts, each under every label its columns may have."""
        by_name = {spec.name: spec for spec in specs}
        found = []
        for names in self.names:
            if unknown := [name for name in names if name not in by_name]:
                msg = f"a layout names {unknown!r}, which are not fields"
                raise ValueError(msg)
            if left := [spec.name for spec in specs if spec.name not in names and not spec.column.optional]:
                msg = f"a layout leaves out {left!r}, which are not optional"
                raise ValueError(msg)
            found.extend(
                Layout(tuple(labels), names) for labels in itertools.product(*(by_name[name].labels for name in names))
            )
        return found

    def written(self, layout: Layout, separator: str) -> str:
        """Return the header line that gives *layout*."""
        return self._style().written(layout, separator)

    def match(self, line: Line | None, layouts: Sequence[Layout], separator: str) -> Layout:
        """Return the layout *line* is."""
        return self._style().match(line, layouts, separator)

    def _style(self) -> _Style:
        """Return how the header writes its labels."""
        return _Style(self.quote, self.prefix, self.trailing_separator, self.strip, self.bare)


@dataclass(frozen=True, slots=True)
class NoHeader:
    """The file has no header: every line is a row of the columns in field order.

    With *refuse*, a header part, a first line that is a header that part reads raises
    :class:`~flatread.errors.UnknownLayoutError`: the file was served with a header it never has, so it is not
    this file (CFTC's current-week report, beside its archive's quoted header).
    """

    refuse: Labels | Layouts | None = None

    @property
    def lines(self) -> int:
        """Return 0: the file has no header line."""
        return 0

    def layouts(self, specs: Sequence[ColumnSpec]) -> list[Layout]:
        """Return the one layout, every column, then each layout *refuse* reads, which :meth:`match` refuses."""
        own = Layout(tuple(spec.labels[0] for spec in specs), tuple(spec.name for spec in specs))
        return [own, *(self.refuse.layouts(specs) if self.refuse is not None else ())]

    def written(self, layout: Layout, separator: str) -> str:
        """Return the labels a header would give, which the file never writes."""
        return separator.join(layout.labels)

    def match(self, line: Line | None, layouts: Sequence[Layout], separator: str) -> Layout:
        """Return the one layout, after refusing a first line that is a header *refuse* reads."""
        own, *refused = layouts
        if self.refuse is not None and line is not None:
            try:
                self.refuse.match(line, refused, separator)
            except ReadError:
                return own
            msg = f"line {line.number} is the header {line.text[:200]!r}, which this file never has"
            raise UnknownLayoutError(msg)
        return own


@dataclass(frozen=True, slots=True)
class ByName:
    """The header names the columns in any order: each label once, every column not ``optional`` among them.

    For a wire that adds, drops and reorders columns over time and names each (Unusual Whales' full tape). A label the
    row struct does not declare, a label given twice, and a column left out that is not ``optional`` each raise
    :class:`~flatread.errors.UnknownLayoutError`. *quote* is the character a label may be written between, and
    *strip* that each label is compared with its padding taken off.
    """

    quote: str | None = None
    strip: bool = False

    @property
    def lines(self) -> int:
        """Return 1: the header is one line."""
        return 1

    def layouts(self, specs: Sequence[ColumnSpec]) -> list[Layout]:
        """Return every label a column may have, each beside its field, with the fields a header may leave out."""
        pairs = [(label, spec.name) for spec in specs for label in spec.labels]
        optional = tuple(spec.name for spec in specs if spec.column.optional)
        return [Layout(tuple(label for label, _ in pairs), tuple(name for _, name in pairs), optional)]

    def written(self, layout: Layout, separator: str) -> str:
        """Return the header of every column under its current label, in field order."""
        first = dict(zip(reversed(layout.names), reversed(layout.labels), strict=True))
        return separator.join(first[name] for name in dict.fromkeys(layout.names))

    def match(self, line: Line | None, layouts: Sequence[Layout], separator: str) -> Layout:
        """Return the layout the line names: its labels in its order, each the field it names."""
        (every,) = layouts
        if line is None:
            msg = "the file ends before its header, so it may have been cut off"
            raise TruncatedFileError(msg)
        by_label = dict(zip(every.labels, every.names, strict=True))
        labels = tuple(self._label(token) for token in self._tokens(line.text, separator))
        if unknown := [label for label in labels if label not in by_label]:
            msg = f"line {line.number} names columns {unknown!r} the rows do not have: {line.text[:200]!r}"
            raise UnknownLayoutError(msg)
        names = tuple(by_label[label] for label in labels)
        if repeated := sorted({label for label, name in zip(labels, names, strict=True) if names.count(name) > 1}):
            msg = f"line {line.number} names {repeated!r} more than once: {line.text[:200]!r}"
            raise UnknownLayoutError(msg)
        if left := [name for name in every.names if name not in names and name not in every.optional]:
            msg = f"line {line.number} leaves out the columns {left!r}, which every file has: {line.text[:200]!r}"
            raise UnknownLayoutError(msg)
        return Layout(labels, names)

    def _tokens(self, text: str, separator: str) -> list[str]:
        """Return the header line's labels as written: split on *separator*, but not inside a quoted label."""
        if self.quote is None:
            return text.split(separator)
        return next(csv.reader([text], delimiter=separator, quotechar=self.quote))

    def _label(self, token: str) -> str:
        """Return the label a header token gives: its padding and its quotes taken off, as declared."""
        label = token.strip() if self.strip else token
        if self.quote is not None and len(label) > 1 and label[0] == label[-1] == self.quote:
            label = label[1:-1]
        return label


# -- Checks over the rows --------------------------------------------------------------------------------------------


class Check(Protocol):
    """A check the rows must pass once read, against the values the file states of itself."""

    def validate(self, fields: Collection[str], columns: Mapping[str, ColumnSpec]) -> None:
        """Raise :class:`ValueError` unless the file states *fields* and its rows have *columns* the check can read.

        Called when the file is declared, so a check naming a field or a column the file has not, or a column it cannot
        check, fails at import rather than when a file is read.
        """
        ...

    def check(self, values: Mapping[str, Any], rows: pl.DataFrame) -> None:
        """Raise a :class:`~flatread.errors.ReadError` if *rows* do not agree with *values*."""
        ...


@dataclass(frozen=True, slots=True)
class RowCount:
    """The file states how many rows it has, in the field *field*; a file of any other count raises.

    :class:`~flatread.errors.ControlTotalError`: rows were lost or added.
    """

    field: str

    def validate(self, fields: Collection[str], columns: Mapping[str, ColumnSpec]) -> None:
        """Require the field holding the count."""
        del columns
        _require_field(self.field, fields)

    def check(self, values: Mapping[str, Any], rows: pl.DataFrame) -> None:
        """Check the count."""
        if (declared := values[self.field]) != rows.height:
            msg = f"the file declares {declared} rows, but {rows.height} were read"
            raise ControlTotalError(msg)


@dataclass(frozen=True, slots=True)
class ColumnTotal:
    """The file states the sum of the column *column*, in the field *field*; rows that sum otherwise raise.

    :class:`~flatread.errors.ControlTotalError`: values were lost, added or changed. The column must be a whole
    number that is never ``None``, so the sum is exact and every row is in it.
    """

    column: str
    field: str

    def validate(self, fields: Collection[str], columns: Mapping[str, ColumnSpec]) -> None:
        """Require the field holding the total, and a column of whole numbers that is never ``None``."""
        _require_field(self.field, fields)
        spec = columns.get(self.column)
        if spec is None or spec.kind is not int or spec.nullable:
            msg = f"a total is checked over {self.column!r}, which is not a column of whole numbers that is never None"
            raise ValueError(msg)

    def check(self, values: Mapping[str, Any], rows: pl.DataFrame) -> None:
        """Check the total."""
        total = rows[self.column].sum()
        if (declared := values[self.field]) != total:
            msg = f"the file declares a total {self.column} of {declared}, but the rows sum to {total}"
            raise ControlTotalError(msg)


def _require_field(field: str, fields: Collection[str]) -> None:
    """Raise :class:`ValueError` unless the file struct states *field*."""
    if field not in fields:
        msg = f"a check reads {field!r}, which the file struct does not state"
        raise ValueError(msg)


# -- The file ----------------------------------------------------------------------------------------------------------


class _Head(NamedTuple):
    """A file struct field whose value the file states: its name, where it is written, and the part that reads it."""

    name: str
    place: str  # "preamble", "header" or "trailer"
    part: LinePart[Any] | None
    #: For a header's field: whether it gives only the optional columns' labels.
    optional: bool = False


class DelimitedFile[FileT, FrameT]:
    """A delimited file declared once by its structs: read as typed rows (:meth:`read`) or a frame (:meth:`read_frame`).

    Build one with :meth:`of_rows` or :meth:`of_file`; the constructor is theirs.
    """

    __slots__ = (
        "_checks",
        "_file_type",
        "_frame_type",
        "_header",
        "_heads",
        "_layouts",
        "_line_ending",
        "_not_published",
        "_optional",
        "_quoted",
        "_row",
        "_rows_name",
        "_separator",
        "_text",
        "_trailing_separator",
    )

    def __init__(  # noqa: PLR0913  # private: of_rows and of_file name every part
        self,
        *,
        row: type[msgspec.Struct],
        rows_name: str | None,
        heads: tuple[_Head, ...],
        file_type: Callable[..., Any] | None,
        frame_type: Callable[..., Any] | None,
        separator: str,
        header: Header,
        quoted: bool,
        trailing_separator: bool,
        line_ending: bool,
        not_published: NotPublishedLine | None,
        checks: tuple[Check, ...],
        text: Callable[[bytes], str],
    ) -> None:
        """Hold a declaration :meth:`of_rows` or :meth:`of_file` has checked."""
        known = {head.name for head in heads}
        columns = {spec.name: spec for spec in column_specs(row)}
        for check in checks:
            check.validate(known, columns)
        self._row, self._rows_name, self._heads = row, rows_name, heads
        self._file_type, self._frame_type = file_type, frame_type
        specs = column_specs(row)
        self._separator, self._header, self._layouts = separator, header, header.layouts(specs)
        self._optional = frozenset(spec.name for spec in specs if spec.column.optional)
        self._quoted, self._trailing_separator, self._line_ending = quoted, trailing_separator, line_ending
        self._not_published, self._checks = not_published, checks
        self._text = text  # set last: __setattr__ refuses every change once it is

    @override
    def __setattr__(self, name: str, value: object) -> None:
        """Refuse a change once the declaration is built: a declared file is a constant."""
        if hasattr(self, "_text"):
            msg = "a DelimitedFile cannot be changed once it is declared"
            raise AttributeError(msg)
        object.__setattr__(self, name, value)

    @classmethod
    def of_rows[RowT: msgspec.Struct](  # noqa: PLR0913  # the row, then one keyword per part of the file
        cls,
        row: type[RowT],
        *,
        separator: str,
        header: Header | None = None,
        quoted: bool = False,
        trailing_separator: bool = False,
        line_ending: bool = False,
        not_published: NotPublishedLine | None = None,
        text: Callable[[bytes], str] = decode_utf8,
    ) -> DelimitedFile[list[RowT], pl.DataFrame]:
        """Declare a file of a header and *row*'s rows alone, read as ``list[row]`` and as a DataFrame.

        *separator* splits a line, and *header* is the file's header part, :class:`Labels` unless it says otherwise.
        *quoted* says a field may be quoted, CSV's way; *trailing_separator* that every row ends with a separator,
        which is framing, not a field. *line_ending* says every real file ends with a line ending, so one that does not
        was cut off. *not_published* is the publisher's line for a file it does not have, if it answers with one.
        *text* reads the body's text.
        """
        return cast(
            "DelimitedFile[list[RowT], pl.DataFrame]",
            cls(
                row=row,
                rows_name=None,
                heads=(),
                file_type=None,
                frame_type=None,
                separator=separator,
                header=header or Labels(),
                quoted=quoted,
                trailing_separator=trailing_separator,
                line_ending=line_ending,
                not_published=not_published,
                checks=(),
                text=text,
            ),
        )

    @classmethod
    def of_file[F: msgspec.Struct, R](  # noqa: PLR0913  # the file, its frame, then one keyword per part
        cls,
        file: type[F],
        *,
        frame_type: type[R],
        separator: str,
        header: Header | None = None,
        quoted: bool = False,
        trailing_separator: bool = False,
        line_ending: bool = False,
        not_published: NotPublishedLine | None = None,
        checks: tuple[Check, ...] = (),
        text: Callable[[bytes], str] = decode_utf8,
    ) -> DelimitedFile[F, R]:
        """Declare a file whose struct *file* says where each of its values is written, read as it and *frame_type*.

        *file*'s one ``list`` field is the rows; each other field is annotated :class:`FromPreamble`,
        :class:`FromHeader` or :class:`FromTrailer`. *frame_type* is a ``NamedTuple`` of *file*'s fields in order, the
        rows as a DataFrame. *checks* are run over the rows once read. The other arguments are :meth:`of_rows`'. A
        declaration that does not hold raises :class:`TypeError` or :class:`ValueError` here.
        """
        rows_name, row, heads = _file_layout(file)
        _check_frame_type(frame_type, file, rows_name)
        return cast(
            "DelimitedFile[F, R]",
            cls(
                row=row,
                rows_name=rows_name,
                heads=heads,
                file_type=file,
                frame_type=frame_type,
                separator=separator,
                header=header or Labels(),
                quoted=quoted,
                trailing_separator=trailing_separator,
                line_ending=line_ending,
                not_published=not_published,
                checks=checks,
                text=text,
            ),
        )

    @property
    def headers(self) -> tuple[str, ...]:
        """Return the header lines the file may have, as written; empty for a file with none."""
        if not self._header.lines:
            return ()
        return tuple(self._header.written(layout, self._separator) for layout in self._layouts)

    def read(self, body: bytes) -> FileT:
        """Read *body* into its typed rows, and the values the file states of itself into its file struct."""
        values, rows = self._read(body)
        typed = frame_rows(rows, self._row)
        if self._rows_name is None:
            return cast("FileT", typed)
        assert self._file_type is not None  # noqa: S101  # of_file sets both
        return cast("FileT", self._file_type(**values, **{self._rows_name: typed}))

    def read_frame(self, body: bytes) -> FrameT:
        """Read *body* into a DataFrame of its rows, and the values the file states of itself into its frame type."""
        values, rows = self._read(body)
        if self._rows_name is None:
            return cast("FrameT", rows)
        assert self._frame_type is not None  # noqa: S101  # of_file sets both
        return cast("FrameT", self._frame_type(**values, **{self._rows_name: rows}))

    def _read(self, body: bytes) -> tuple[dict[str, Any], pl.DataFrame]:
        """Run the pipeline, in the module's order."""
        text = self._text(body)
        found = counted_lines(text, self._separator, quoted=self._quoted)
        values: dict[str, Any] = {}
        try:
            start, layout = self._head(found, values)
        except ReadError:
            self._raise_if_not_published(found)
            raise
        if self._line_ending:
            check_line_ending(text, frame_line(found, -1))
        trailers = [head for head in self._heads if head.place == "trailer"]
        end = found.height - len(trailers)
        if trailers and end < start:
            last = frame_line(found, -1)
            msg = f"no trailer: the file ends with its header, on line {last.number}, so it may have been cut off"
            raise TruncatedFileError(msg)
        for at, head in enumerate(trailers):
            values[head.name] = cast("LinePart[Any]", head.part).read(frame_line(found, end + at))
        typed, error = _read_rows(
            found.slice(start, end - start),
            self._row,
            layout,
            self._separator,
            quoted=self._quoted,
            trailing_separator=self._trailing_separator,
        )
        if error is not None:
            raise error
        for check in self._checks:
            check.check(values, typed)
        return values, typed

    def _head(self, found: pl.DataFrame, values: dict[str, Any]) -> tuple[int, Layout]:
        """Read the preamble lines and the header; return where the rows start and the header's layout."""
        preamble = [head for head in self._heads if head.place == "preamble"]
        for at, head in enumerate(preamble):
            values[head.name] = cast("LinePart[Any]", head.part).read(_line(found, at, "its preamble"))
        at = len(preamble)
        line = frame_line(found, at) if at < found.height else None
        layout = self._header.match(line, self._layouts, self._separator)
        for head in self._heads:
            if head.place == "header":
                values[head.name] = tuple(
                    label
                    for label, name in zip(layout.labels, layout.names, strict=True)
                    if not head.optional or name in self._optional
                )
        return at + self._header.lines, layout

    def _raise_if_not_published(self, found: pl.DataFrame) -> None:
        """Raise the publisher's answer, for a body that failed its framing but holds the publisher's line."""
        if self._not_published is not None and (found["line"] == self._not_published.marker).any():
            msg = f"not published: the body holds {self._not_published.marker!r}, {self._not_published.meaning}"
            raise NotPublishedError(msg) from None


class StreamedFile[RowT: msgspec.Struct]:
    """A delimited file of a header and *row*'s rows, read as its chunks arrive: in batches of rows or of frames.

    For a file too large to hold (EIA's grid monitor, the Board's ``Z1.csv``, Unusual Whales' full tape). The header
    and the rows are read by the same parts and the same strict cells as a :class:`DelimitedFile`, a batch at a time,
    one batch held. *lines* turns the chunks into the file's lines: :func:`~flatread.text.stream_lines` (UTF-8,
    ending with a line ending) unless the file says otherwise, an archive's file through
    :func:`~flatread.archives.stream_unzip` first. A line that fails is raised when it is met, after every row
    before it has been yielded: a caller that needs the whole file or nothing keeps what it read only once the
    iteration ends without an error.
    """

    __slots__ = ("_header", "_layouts", "_lines", "_quoted", "_row", "_separator", "_trailing_separator")

    def __init__(  # noqa: PLR0913  # the row, then one keyword per part of the file
        self,
        row: type[RowT],
        *,
        separator: str,
        header: Header | None = None,
        quoted: bool = False,
        trailing_separator: bool = False,
        lines: Callable[[AsyncIterator[bytes]], AsyncIterator[Line]] = stream_lines,
    ) -> None:
        """Declare the file: the arguments are :meth:`DelimitedFile.of_rows`', and *lines* its chunks' lines."""
        self._header = header or Labels()
        self._row, self._layouts = row, self._header.layouts(column_specs(row))
        self._separator, self._quoted, self._trailing_separator = separator, quoted, trailing_separator
        self._lines = lines  # set last: __setattr__ refuses every change once it is

    @override
    def __setattr__(self, name: str, value: object) -> None:
        """Refuse a change once the declaration is built: a declared file is a constant."""
        if hasattr(self, "_lines"):
            msg = "a StreamedFile cannot be changed once it is declared"
            raise AttributeError(msg)
        object.__setattr__(self, name, value)

    async def stream(self, chunks: AsyncIterator[bytes], *, batch_rows: int) -> AsyncIterator[RowT]:
        """Yield the file's rows, one *row* each, in order, reading *batch_rows* lines at a time."""
        async for frame in self.stream_frames(chunks, batch_rows=batch_rows):
            for row in frame_rows(frame, self._row):
                yield row

    async def stream_frames(self, chunks: AsyncIterator[bytes], *, batch_rows: int) -> AsyncIterator[pl.DataFrame]:
        """Yield the file's rows as DataFrames of *row*'s columns, a batch of up to *batch_rows* rows each."""
        found = self._lines(chunks)
        first = await anext(found, None)
        layout = self._header.match(first, self._layouts, self._separator)
        async for batch in batched(_lines_from(first, found, keep_first=not self._header.lines), batch_rows):
            typed, error = _read_rows(
                counted_batch(batch, self._separator, quoted=self._quoted),
                self._row,
                layout,
                self._separator,
                quoted=self._quoted,
                trailing_separator=self._trailing_separator,
            )
            if not typed.is_empty():
                yield typed
            if error is not None:
                raise error


async def _lines_from(first: Line | None, rest: AsyncIterator[Line], *, keep_first: bool) -> AsyncIterator[Line]:
    """Yield a stream's lines after its header: the first one too, for a file with none."""
    if keep_first and first is not None:
        yield first
    async for line in rest:
        yield line


def _read_rows(  # noqa: PLR0913  # the rows, their struct and layout, then how the file writes them
    rows: pl.DataFrame,
    row: type[msgspec.Struct],
    layout: Layout,
    separator: str,
    *,
    quoted: bool,
    trailing_separator: bool,
) -> tuple[pl.DataFrame, ReadError | None]:
    """Read checked *rows* into typed rows: those before the first line at fault, and that line's error, if one is.

    Each check keeps only the rows before its first fault, so a later check sees only earlier lines, and the error
    returned is always the first faulty line's: a row without its trailing separator, of the wrong width, or with a
    cell that is not its column's value. A whole file raises it; a stream yields the rows first.
    """
    error: ReadError | None = None
    if trailing_separator:
        rows, error = _drop_trailing_separator(rows, separator)
    if (ragged := first_ragged(rows, len(layout.names))) is not None:
        rows, error = rows.slice(0, ragged[0]), ragged[1]
    cells = _cells(rows, layout, separator, quoted=quoted)
    labels = dict(zip(layout.names, layout.labels, strict=True))
    try:
        return read_cells(cells, row, rows["number"], labels), error
    except CellError as exc:
        kept = cells.slice(0, exc.index)
        return read_cells(kept, row, rows["number"].slice(0, exc.index), labels), exc


def _drop_trailing_separator(rows: pl.DataFrame, separator: str) -> tuple[pl.DataFrame, ReadError | None]:
    """Take each row's trailing separator off, which is framing; the rows before one without it, and its error."""
    line = pl.col("line")
    error: ReadError | None = None
    unended = rows.with_row_index("row index").filter(~line.str.ends_with(separator))
    if not unended.is_empty():
        first = frame_line(unended, 0)
        msg = f"line {first.number} does not end with {separator!r}, as every row does: {first.text[:200]!r}"
        rows, error = rows.slice(0, unended["row index"][0]), RaggedRowError(msg)
    return rows.with_columns(line.str.strip_suffix(separator), pl.col("fields") - 1), error


def _cells(rows: pl.DataFrame, layout: Layout, separator: str, *, quoted: bool) -> pl.DataFrame:
    """Split the checked rows into one text column per field the layout names."""
    names = list(layout.names)
    if quoted:
        schema = dict.fromkeys(names, pl.String())
        return read_quoted_rows(rows["line"], schema, separator=separator, empty_string_is_null=False)
    return split_fields(rows["line"], separator, names)


def _line(found: pl.DataFrame, at: int, before: str) -> Line:
    """Return the line at *at*; a file with fewer lines raises :class:`~flatread.errors.TruncatedFileError`."""
    if at >= found.height:
        msg = f"the file ends after {found.height} lines, inside {before}, so it may have been cut off"
        raise TruncatedFileError(msg)
    return frame_line(found, at)


def _file_layout(file: type[msgspec.Struct]) -> tuple[str, type[msgspec.Struct], tuple[_Head, ...]]:
    """Return *file*'s rows field, its row struct, and the fields the file states; raise for anything else."""
    rows: list[tuple[str, type[msgspec.Struct]]] = []
    heads: list[_Head] = []
    for field in msgspec.structs.fields(file):
        annotation = field.type
        if get_origin(annotation) is list and _is_struct(row := get_args(annotation)[0]):
            rows.append((field.name, row))
            continue
        marks = (
            [item for item in get_args(annotation)[1:] if isinstance(item, FromPreamble | FromHeader | FromTrailer)]
            if get_origin(annotation) is Annotated
            else []
        )
        if len(marks) != 1:
            where = "FromPreamble, FromHeader or FromTrailer"
            msg = f"{file.__name__}.{field.name} says nothing of where it is written: {where}"
            raise TypeError(msg)
        (mark,) = marks
        if isinstance(mark, FromHeader):
            heads.append(_Head(field.name, "header", None, optional=mark.optional))
        else:
            heads.append(_Head(field.name, "preamble" if isinstance(mark, FromPreamble) else "trailer", mark.part))
    if len(rows) != 1:
        msg = f"{file.__name__} has {len(rows)} list fields of rows, not one"
        raise TypeError(msg)
    ((rows_name, row),) = rows
    column_specs(row)  # the row struct's declarations are checked here, at import
    return rows_name, row, tuple(heads)


def _check_frame_type(frame_type: type, file: type[msgspec.Struct], rows_name: str) -> None:
    """Raise :class:`ValueError` unless *frame_type* is *file*'s fields in order and type, its rows a DataFrame."""
    fields = getattr(frame_type, "_fields", None)
    expected = tuple(field.name for field in msgspec.structs.fields(file))
    if fields != expected:
        msg = f"{frame_type.__name__}'s fields {fields!r} are not {file.__name__}'s, {expected!r}"
        raise ValueError(msg)
    hints = typing.get_type_hints(frame_type)
    for field in msgspec.structs.fields(file):
        wanted = pl.DataFrame if field.name == rows_name else _unannotated(field.type)
        if hints[field.name] != wanted:
            msg = f"{frame_type.__name__}.{field.name} is {hints[field.name]!r}, not {wanted!r} as {file.__name__}'s is"
            raise ValueError(msg)


def _unannotated(annotation: object) -> object:
    """Return *annotation* without its ``Annotated`` metadata."""
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def _is_struct(annotation: object) -> bool:
    """Return whether *annotation* is a msgspec struct type."""
    return isinstance(annotation, type) and issubclass(annotation, msgspec.Struct)
