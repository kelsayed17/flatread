r"""A wire's columns declared on its row struct, and a frame's text cells read into them strictly.

A row struct whose rows come from a file, a sheet or a table declares each field's column with :class:`Column` in its
annotation: ``price: Annotated[float | None, Column("PRICE", missing=("", "."))]``. The declaration is the one
statement of that column: the label the wire's header gives it, how its text is read as the field's type, and which
cells stand for no value. :func:`read_cells` reads a frame of text cells by those declarations, and every cell ends one
of three ways:

* it is the field's value, read from the cell's text, which must be that type's whole shape;
* it is ``None``, because its text is one of the column's declared ``missing`` tokens, on a field that may be ``None``;
* the read fails with :class:`~flatread.errors.UnknownLayoutError`, naming the line, the column and the text.

So no value is ever lost without a word: a cell is never silently ``None``, and never read as other than it is. A
``str`` field keeps its cell exactly as written, unless the column declares what to ``strip`` or a ``prefix`` it bears.

**The shapes.** Each type reads only its whole shape, matched before the value is read:

* ``int`` — a sign and digits; one too large for a 64-bit integer is refused, never read as ``None``;
* ``float`` — a decimal, unless the column gives its own ``pattern`` (one that admits ``inf`` or ``nan``, say);
* ``bool`` — exactly the column's ``true`` or ``false`` token;
* ``date`` and ``datetime`` — one of the column's ``format``\ s, ``strftime`` formats from which each shape is derived
  (``%m`` is two digits, ``%Y`` a year from 0001 to 9999, ``%z`` an offset, ``%.f`` an optional fraction); a column
  whose dates write a month or day in one digit says so with its own ``pattern``. A cell of the shape that is no real
  day or time (February 30) fails the read. A datetime with an offset is a UTC field, converted to UTC;
* ``str`` — any text, or the column's ``pattern`` when the wire's values have one.

A declaration that cannot be read fails when the row struct is first read, before any cell: a field with no
:class:`Column`, ``missing`` tokens on a field that cannot be ``None``, a field that can be ``None`` though nothing in
its column ever is, a date with no ``format``, a ``bool`` with no tokens, a type with no shape here. So a field's type
says exactly what its column holds.
"""

import datetime as dt
import re
import types
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Annotated, Final, NamedTuple, Union, get_args, get_origin

import msgspec
import polars as pl

from flatread.errors import UnknownLayoutError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "DECIMAL",
    "NONFINITE",
    "WHOLE",
    "CellError",
    "Column",
    "ColumnSpec",
    "Format",
    "UtcDatetime",
    "column_specs",
    "is_utc_datetime",
    "read_cells",
    "read_labelled_cells",
]

#: A moment the file states in UTC: a field so typed decodes aware (msgspec refuses a naive value), and its frame column
#: is ``Datetime`` in UTC. A moment a file writes with no zone stays a plain, naive ``datetime``.
type UtcDatetime = Annotated[dt.datetime, msgspec.Meta(tz=True)]


def is_utc_datetime(annotation: object) -> bool:
    """Whether a field annotated *annotation* is a :data:`UtcDatetime`, alone or ``| None``."""
    return annotation is UtcDatetime or set(get_args(annotation)) == {UtcDatetime, type(None)}


#: A whole number as the files write one: an optional sign and digits. One too large for an ``Int64`` does not read, so
#: the read refuses it (a cell whose text is there but whose value is not), and every one that fits reads.
WHOLE: Final = r"[+-]?[0-9]+"

#: A float a file writes by name, not by digits: an infinity or a NaN, any case, signed or not. A column whose wire
#: writes them says so in its shape: ``Column(..., pattern=f"{DECIMAL}|{NONFINITE}")``.
NONFINITE: Final = r"[+-]?(?i:inf|infinity|nan)"

#: A decimal number: a sign, digits with an optional point, or a point and digits, and an optional exponent.
DECIMAL: Final = r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"

#: An offset from UTC, as ``%z`` reads one: ``+00``, ``+0000`` or ``+00:00``.
_OFFSET: Final = r"[+-][0-9]{2}(?::?[0-9]{2})?"

#: The shape each ``strftime`` directive a format may use reads, for the shape derived from a format: each the range a
#: real date and time has, so polars never rolls an impossible one over (a minute of 60 into the next hour). A second
#: of 60 is a leap second, read as the next minute's first, as :func:`~flatread.delimited.wall_time` reads a
#: stamp's: polars rolls it over. A year is 0001 to 9999: polars reads ``0000``, which a Python ``date`` cannot hold.
#: A fraction of a second is up to six digits, the microseconds a ``datetime`` keeps, so no digit is dropped.
_DIRECTIVES: Final = {
    "Y": "(?:[1-9][0-9]{3}|0[1-9][0-9]{2}|00[1-9][0-9]|000[1-9])",
    "y": "[0-9]{2}",
    "m": "(?:0[1-9]|1[0-2])",
    "d": "(?:0[1-9]|[12][0-9]|3[01])",
    "H": "(?:[01][0-9]|2[0-3])",
    "I": "(?:0[1-9]|1[0-2])",
    "M": "[0-5][0-9]",
    "S": "(?:[0-5][0-9]|60)",
    "f": "[0-9]{1,6}",
    "b": "[A-Za-z]{3}",
    "p": "(?:AM|PM)",
    "z": _OFFSET,
    "%": "%",
}
#: The years a Python ``date`` holds: a value moved past them reads as no real one.
_FIRST_YEAR: Final = dt.MINYEAR
_LAST_YEAR: Final = dt.MAXYEAR
#: The row-index column a check adds, named so no field can be: a field name is an identifier.
_AT: Final = "row index"

#: The two-character directives: ``%.f``, an optional fraction of a second, and ``%#z``, an offset of hours alone too.
_LONG_DIRECTIVES: Final = {".f": r"(?:\.[0-9]{1,6})?", "#z": _OFFSET}


class CellError(UnknownLayoutError):
    """A cell that is neither its column's value nor a missing token; *index* is its row's position in the frame.

    An :class:`~flatread.errors.UnknownLayoutError`, so a caller handles it as any drift; a reader of a stream
    reads *index* to yield the rows before it.
    """

    def __init__(self, message: str, index: int) -> None:
        """Name the cell in *message*, and hold its row's position."""
        super().__init__(message)
        self.index = index


@dataclass(frozen=True, slots=True)
class Format:
    """A date or datetime format with the shape its cells have, when the shape derived from it is not the wire's.

    For one of a column's several formats that writes a month or day in one digit (``3/26/2004``) beside one that does
    not: ``Column(format=("%Y-%m-%d", Format("%m/%d/%Y", r"[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}")))``.
    """

    format: str
    pattern: str


@dataclass(frozen=True, slots=True)
class Column:
    """One column of a wire, declared in its row struct field's annotation.

    *label* is the header's name for the column, or the names it has gone by, any of which the header may give.
    *format* is the ``strftime`` format of a date or datetime field, or the formats its cells are written in, each read
    where its shape matches; a :class:`Format` gives one format its own shape. *pattern* overrides the shape a cell
    must have, when the type's own is not the wire's. *missing* is the texts that stand for no value; the field must
    then be able to be ``None``. *true* and *false* are a ``bool`` field's two tokens. *strip* is the characters taken
    off both ends of every cell before it is read, and *strip_start* those taken off its start alone: a declared
    normalization, such as a wire's padding. *prefix* is text every cell begins with, which is checked and taken off.
    *grouping* is the character a number's thousands are grouped by (``1,234.5``), and *parens_negative* that a
    negative number is written in parentheses (``(95.1)``), as accounting writes it: an ``int`` or ``float`` field's
    shape then requires the grouping where the number has thousands, and allows the parentheses. *optional* says the
    header may leave the column out, in which case every row reads ``None`` for it.
    """

    label: str | tuple[str, ...]
    format: str | Format | tuple[str | Format, ...] | None = None
    pattern: str | None = None
    missing: tuple[str, ...] = ()
    true: str | None = None
    false: str | None = None
    strip: str | None = None
    strip_start: str | None = None
    prefix: str | None = None
    grouping: str | None = None
    parens_negative: bool = False
    optional: bool = False


class ColumnSpec(NamedTuple):
    """A row struct field's column, checked and resolved: what :func:`read_cells` reads it by."""

    #: The field's name, which is its frame column's name.
    name: str
    #: The column as declared.
    column: Column
    #: The labels the header may give the column, the first its current one.
    labels: tuple[str, ...]
    #: The field's type without ``None``: ``str``, ``int``, ``float``, ``bool``, ``date`` or ``datetime``.
    kind: type
    #: Whether the field may be ``None``.
    nullable: bool
    #: Whether the field is a UTC datetime (:data:`~flatread.cells.UtcDatetime`).
    utc: bool
    #: Each format with its anchored shape; for any other type, one entry whose format is ``None``. Empty for a ``str``
    #: field that takes any text.
    shapes: tuple[tuple[str | None, str], ...]
    #: What the shape is, for an error: ``"a number"``, ``"a date as %m/%d/%Y"``.
    described: str


@cache
def column_specs(row_type: type[msgspec.Struct]) -> tuple[ColumnSpec, ...]:
    """Return *row_type*'s columns in field order, each checked; a declaration that cannot be read raises.

    Raises :class:`TypeError` for a field with no :class:`Column` or of a type with no shape, and :class:`ValueError`
    for ``missing`` tokens on a field that cannot be ``None``, a field that can be ``None`` though its column declares
    no missing token and is not optional, a date or datetime with no ``format``, a ``bool`` without both tokens, an
    offset on a field that is not a UTC datetime, or an ``optional`` column on a field that cannot be ``None``.
    """
    return tuple(_spec(row_type, field) for field in msgspec.structs.fields(row_type))


def _spec(row_type: type, field: msgspec.structs.FieldInfo) -> ColumnSpec:
    """Resolve one field's declaration."""
    where = f"{row_type.__name__}.{field.name}"
    if get_origin(field.type) is not Annotated:
        msg = f"{where} declares no Column"
        raise TypeError(msg)
    annotation, *metadata = get_args(field.type)
    columns = [item for item in metadata if isinstance(item, Column)]
    if len(columns) != 1:
        msg = f"{where} declares {len(columns)} Columns, not one"
        raise TypeError(msg)
    (column,) = columns
    utc = is_utc_datetime(annotation)
    kind, nullable = _kind(annotation, where)
    if column.missing and not nullable:
        msg = f"{where} declares missing tokens {column.missing!r} but cannot be None"
        raise ValueError(msg)
    if column.optional and not nullable:
        msg = f"{where} is an optional column but cannot be None"
        raise ValueError(msg)
    if nullable and not (column.missing or column.optional):
        msg = f"{where} can be None, but its column declares no missing token and is not optional, so it never is"
        raise ValueError(msg)
    labels = (column.label,) if isinstance(column.label, str) else column.label
    shapes, described = _shapes(kind, column, where, utc=utc)
    return ColumnSpec(field.name, column, labels, kind, nullable, utc, shapes, described)


def _kind(annotation: object, where: str) -> tuple[type, bool]:
    """Return the field's type without ``None`` and whether it may be ``None``."""
    nullable = False
    if get_origin(annotation) in {Union, types.UnionType}:
        arms = [arm for arm in get_args(annotation) if arm is not type(None)]
        nullable = len(arms) < len(get_args(annotation))
        if len(arms) != 1:
            msg = f"{where} is a union of {arms!r}, which no cell reads as"
            raise TypeError(msg)
        annotation = arms[0]
    if annotation is UtcDatetime:
        return dt.datetime, nullable
    while (supertype := getattr(annotation, "__supertype__", None)) is not None:  # a NewType reads as its supertype
        annotation = supertype
    if annotation in {str, int, float, bool, dt.date, dt.datetime}:
        return annotation, nullable  # ty: ignore[invalid-return-type]  # narrowed to a type by membership
    msg = f"{where} is a {annotation!r}, which no cell reads as"
    raise TypeError(msg)


def _shapes(kind: type, column: Column, where: str, *, utc: bool) -> tuple[tuple[tuple[str | None, str], ...], str]:
    """Return each of the column's formats with its anchored shape, and what the shapes are in words."""
    if kind in {dt.date, dt.datetime}:
        if column.format is None:
            msg = f"{where} is a {kind.__name__} with no format"
            raise ValueError(msg)
        entries = (column.format,) if isinstance(column.format, str | Format) else column.format
        formats = [entry.format if isinstance(entry, Format) else entry for entry in entries]
        if any("%z" in fmt or "%#z" in fmt for fmt in formats) and not utc:
            msg = f"{where} reads an offset, so it is a UtcDatetime"
            raise ValueError(msg)
        shapes = tuple(
            (entry.format, _anchored(entry.pattern))
            if isinstance(entry, Format)
            else (entry, _anchored(column.pattern or _format_pattern(entry)))
            for entry in entries
        )
        return shapes, f"a {kind.__name__} as {' or '.join(formats)}"
    if column.format is not None:
        msg = f"{where} gives a format, which only a date or a datetime reads by"
        raise ValueError(msg)
    if kind is bool:
        if column.true is None or column.false is None:
            msg = f"{where} is a bool without both its true and false tokens"
            raise ValueError(msg)
        tokens = f"{re.escape(column.true)}|{re.escape(column.false)}"
        return ((None, _anchored(tokens)),), f"{column.true!r} or {column.false!r}"
    if kind in {int, float}:
        return ((None, _anchored(_number_shape(kind, column))),), "a whole number" if kind is int else "a number"
    if column.grouping is not None or column.parens_negative:
        msg = f"{where} groups or parenthesizes a number, but is a {kind.__name__}"
        raise ValueError(msg)
    if column.pattern is not None:
        return ((None, _anchored(column.pattern)),), f"text of the shape {column.pattern}"
    return (), "text"


def _number_shape(kind: type, column: Column) -> str:
    """Return the shape of an ``int`` or ``float`` cell: the column's own, an accounting one, or the type's."""
    if column.pattern is not None:
        return column.pattern
    if column.grouping is not None or column.parens_negative:
        return _accounting(kind, column)
    return WHOLE if kind is int else DECIMAL


def _accounting(kind: type, column: Column) -> str:
    """Return the shape of a number written with its thousands grouped and, if declared, a negative in parentheses."""
    if column.grouping is None:
        digits = "[0-9]+"
    else:
        group = re.escape(column.grouping)
        digits = f"(?:[0-9]{{1,3}}(?:{group}[0-9]{{3}})+|[0-9]{{1,3}})"
    number = digits if kind is int else rf"{digits}(?:\.[0-9]+)?"
    signed = f"[+-]?{number}"
    return f"{signed}|\\({number}\\)" if column.parens_negative else signed


def _anchored(pattern: str) -> str:
    """Return *pattern* matching a cell whole."""
    return f"^(?:{pattern})$"


def _format_pattern(format_: str) -> str:
    """Derive the shape a ``strftime`` *format* reads: each directive its shape, every other character itself."""
    parts: list[str] = []
    at = 0
    while at < len(format_):
        if format_[at] != "%" or at + 1 == len(format_):
            parts.append(re.escape(format_[at]))
            at += 1
        elif (long := format_[at + 1 : at + 3]) in _LONG_DIRECTIVES:
            parts.append(_LONG_DIRECTIVES[long])
            at += 3
        elif (directive := format_[at + 1]) in _DIRECTIVES:
            parts.append(_DIRECTIVES[directive])
            at += 2
        else:
            msg = f"the format {format_!r} uses %{directive}, which has no shape here"
            raise ValueError(msg)
    return "".join(parts)


def read_cells(
    cells: pl.DataFrame,
    row_type: type[msgspec.Struct],
    numbers: pl.Series,
    labels: Mapping[str, str] | None = None,
) -> pl.DataFrame:
    """Read *cells*, one text column per field of *row_type*, into the fields' types, strictly.

    *numbers* is each row's line in the file, and *labels* the label each column's header gave it, both for an error. A
    column the header left out (an ``optional`` one) is absent from *cells* and reads ``None`` on every row. Raises
    :class:`CellError`, an :class:`~flatread.errors.UnknownLayoutError`, for the first cell, in the file's order
    and then the columns', that is not its field's value, nor a ``missing`` token.
    """
    specs = column_specs(row_type)
    present = [spec for spec in specs if spec.name in cells.columns]
    texts = cells.select(_text(spec).alias(spec.name) for spec in present)
    values = texts.select(_value(spec) for spec in present)
    _check(texts, values, present, numbers, labels or {})
    absent = [pl.lit(None).alias(spec.name) for spec in specs if spec.name not in cells.columns]
    schema = _schema(specs)
    return values.with_columns(absent).select(schema.names()).cast(schema)


def read_labelled_cells(frame: pl.DataFrame, row_type: type[msgspec.Struct], numbers: pl.Series) -> pl.DataFrame:
    """Read a table whose columns are named by its header's labels, as written and in its order, into *row_type*.

    For a sheet or an HTML table, whose header arrives as its columns' names rather than as a line. The labels must be
    *row_type*'s columns' labels in field order, each its column's label or one of its alternatives, an ``optional``
    column left out or not; any other header raises :class:`~flatread.errors.UnknownLayoutError`. The cells are
    then read by :func:`read_cells`, *numbers* each row's place in the sheet or table, for an error.
    """
    specs = column_specs(row_type)
    labels = frame.columns
    names: list[str] = []
    for spec in specs:
        at = len(names)
        if at < len(labels) and labels[at] in spec.labels:
            names.append(spec.name)
        elif not spec.column.optional:
            break
    if len(names) != len(labels) or any(spec.name not in names and not spec.column.optional for spec in specs):
        expected = [spec.labels[0] for spec in specs]
        msg = f"the header {labels!r} is not the columns {expected!r}"
        raise UnknownLayoutError(msg)
    # A sheet's or a table's empty cell arrives as null: it is an empty cell, whose text is "".
    texts = frame.rename(dict(zip(labels, names, strict=True))).cast(pl.String).fill_null("")
    return read_cells(texts, row_type, numbers, dict(zip(names, labels, strict=True)))


def _text(spec: ColumnSpec) -> pl.Expr:
    """Return the cell's text as it is read: stripped of the column's declared characters, its prefix kept.

    A null cell stays null, which the read refuses: a cell a file or a sheet holds always has text, ``""`` when it is
    empty, so a null is a value the reader's own framing failed to find (:func:`read_labelled_cells` gives a sheet's
    empty cell its ``""``).
    """
    cell = pl.col(spec.name)
    if spec.column.strip is not None:
        cell = cell.str.strip_chars(spec.column.strip)
    if spec.column.strip_start is not None:
        cell = cell.str.strip_chars_start(spec.column.strip_start)
    return cell


def _missing(spec: ColumnSpec) -> pl.Expr:
    """Return whether the cell is one of the column's missing tokens."""
    if not spec.column.missing:
        return pl.lit(value=False)
    return pl.col(spec.name).is_in(list(spec.column.missing))


def _value(spec: ColumnSpec) -> pl.Expr:
    """Return the cell read as its field's type: ``None`` for a missing token, and for a cell that is not a value."""
    text = pl.col(spec.name)
    if spec.column.prefix is not None:
        text = pl.when(text.str.starts_with(spec.column.prefix)).then(text.str.strip_prefix(spec.column.prefix))
    if not spec.shapes:
        value = text
    elif spec.kind in {dt.date, dt.datetime}:
        value = pl.coalesce(
            pl.when(text.str.contains(shape)).then(_temporal(text, spec, fmt)) for fmt, shape in spec.shapes
        )
    else:
        ((_, shape),) = spec.shapes
        value = pl.when(text.str.contains(shape)).then(_scalar(text, spec))
    if spec.column.missing:
        value = pl.when(_missing(spec)).then(None).otherwise(value)
    return value.alias(spec.name)


def _temporal(text: pl.Expr, spec: ColumnSpec, fmt: str | None) -> pl.Expr:
    """Return *text* read as a date or datetime in *fmt*: ``None`` where it is no real one.

    A date is its datetime's day, so a leap second at midnight is the next day's, as its datetime is. A value a
    leap second or an offset moves out of the years a Python ``date`` holds is ``None``.
    """
    if fmt is not None and ("%z" in fmt or "%#z" in fmt):
        value = text.str.to_datetime(fmt, time_unit="us", time_zone="UTC", strict=False, exact=True)
    else:
        value = text.str.to_datetime(fmt, time_unit="us", strict=False, exact=True)
        if spec.utc:
            value = value.dt.replace_time_zone("UTC")
    if spec.kind is dt.date:
        value = value.dt.date()
    return pl.when(value.dt.year().is_between(_FIRST_YEAR, _LAST_YEAR)).then(value)


def _scalar(text: pl.Expr, spec: ColumnSpec) -> pl.Expr:
    """Return *text*, of its column's shape, read as the field's type."""
    if spec.kind is bool:
        return text == spec.column.true
    if spec.column.grouping is not None:
        text = text.str.replace_all(spec.column.grouping, "", literal=True)
    if spec.column.parens_negative:
        text = pl.when(text.str.starts_with("(")).then(pl.lit("-") + text.str.strip_chars("()")).otherwise(text)
    if spec.kind is int:
        return text.cast(pl.Int64, strict=False)
    if spec.kind is float:
        return _float(text)
    return text


def _float(text: pl.Expr) -> pl.Expr:
    """Return *text* read as a float: ``None`` where the float cannot hold the number it writes.

    A finite number too large for a float (``1e400``) would read as infinity, and a non-zero one too small (``1e-400``)
    as zero; both are refused. An infinity or a NaN the column's own shape admits (``inf``) is its value.
    """
    value = text.cast(pl.Float64, strict=False)
    written = text.str.to_lowercase().str.strip_chars_start("+-")
    named = written.is_in(["inf", "infinity", "nan"])
    vanished = (value == 0) & text.str.replace(r"[eE].*$", "").str.contains("[1-9]")
    return pl.when((value.is_finite() & ~vanished) | named).then(value)


def _check(
    texts: pl.DataFrame,
    values: pl.DataFrame,
    specs: Sequence[ColumnSpec],
    numbers: pl.Series,
    labels: Mapping[str, str],
) -> None:
    """Raise for the first cell that is neither a value nor a missing token: no text, or text but no value."""
    unread = [
        (values[spec.name].is_null() & ~texts.select(_missing(spec)).to_series().fill_null(value=False))
        .alias(spec.name)
        .to_frame()
        for spec in specs
    ]
    if not unread:
        return
    flags = pl.concat(unread, how="horizontal")
    found = flags.with_row_index(_AT).filter(pl.any_horizontal(spec.name for spec in specs))
    if found.is_empty():
        return
    first = found.row(0, named=True)
    spec = next(spec for spec in specs if first[spec.name])
    label = labels.get(spec.name, spec.labels[0])
    text = texts[spec.name][first[_AT]]
    if text is None:
        msg = f"line {numbers[first[_AT]]}: the {label!r} cell is absent: the row's framing did not find it"
        raise CellError(msg, first[_AT])
    allowed = f", nor one of {list(spec.column.missing)!r}" if spec.column.missing else ""
    prefixed = f" beginning {spec.column.prefix!r}" if spec.column.prefix is not None else ""
    msg = f"line {numbers[first[_AT]]}: the {label!r} cell {text!r} is not {spec.described}{prefixed}{allowed}"
    raise CellError(msg, first[_AT])


def _schema(specs: Sequence[ColumnSpec]) -> pl.Schema:
    """Return the frame schema the read columns are cast to."""
    dtypes: dict[type, pl.DataType] = {
        str: pl.String(),
        int: pl.Int64(),
        float: pl.Float64(),
        bool: pl.Boolean(),
        dt.date: pl.Date(),
        dt.datetime: pl.Datetime("us"),
    }
    return pl.Schema({spec.name: pl.Datetime("us", "UTC") if spec.utc else dtypes[spec.kind] for spec in specs})
