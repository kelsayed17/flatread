r"""A table's columns, declared on its row struct, and its text cells read into them strictly.

A row struct declares each field's column with :class:`Column` in its annotation:
``price: Annotated[float | None, Column("PRICE", missing=("", "."))]``. :func:`read_table` takes a frame whose columns
are the source's own labels (as ``pl.read_csv`` or ``pl.read_excel`` names them) and reads every cell by polars' own
conversions. Each cell is its field's value, ``None`` for one of the column's ``missing`` tokens, or an
:class:`~flatread.errors.UnknownLayoutError` naming the row, the column and the text: never silently ``None``.

``int`` reads by ``str.to_integer``, ``float`` by a cast to ``Float64`` (a number too large for one is refused, not
read as an infinity; a NaN or an infinity is refused unless the column's ``pattern`` admits it, as :data:`NONFINITE`
does), ``bool`` by ``replace_strict`` on the column's two tokens, a ``Literal`` as a ``pl.Enum``, and a
date or datetime by ``str.to_datetime`` in one of the column's formats, checked by writing the value back in that
format (polars' ``%Y`` alone reads ``12/31/25`` as the year 25). A format with an offset (``%z`` or ``%#z``) ends with
it, and the cell's text before its offset is the text checked so; a leap second (``:60``) is read only where it rolls
over to midnight after 30 June or 31 December, in UTC for a cell with an offset. A field's ``Meta`` constraints are
checked too, and a cell that reads as its type but fails them is named so. A column's ``pattern`` and a ``Meta``
pattern are run by polars' regex, so one it cannot compile (a look-around, a backreference) is refused when the row
struct is.
"""

import datetime as dt
import re
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final, NamedTuple, get_args, get_origin

import msgspec
import polars as pl

from flatread.errors import UnknownLayoutError

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "DECIMAL",
    "NONFINITE",
    "CellError",
    "Column",
    "ColumnSpec",
    "UtcDatetime",
    "column_specs",
    "dtype_of",
    "match_header",
    "read_table",
    "resolve_type",
    "wire_names",
]

#: A moment the source states in UTC: a field so typed decodes aware, and its frame column is ``Datetime`` in UTC.
type UtcDatetime = Annotated[dt.datetime, msgspec.Meta(tz=True)]

#: A float written by name: an infinity or a NaN, any case, signed or not. A float column refuses one unless its
#: ``pattern`` admits it: ``pattern=f"{DECIMAL}|{NONFINITE}"``.
NONFINITE: Final = r"[+-]?(?i:inf|infinity|nan)"

#: A decimal number: a sign, digits with an optional point, or a point and digits, and an optional exponent.
DECIMAL: Final = r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"

_AT: Final = "row index"

#: A format's offset directive, ``%z`` or ``%#z``.
_OFFSET_DIRECTIVE: Final = re.compile(r"%#?z")

#: A cell's offset at its end, as polars' ``%#z`` reads one: ``Z``, ``+HH``, ``+HHMM`` or ``+HH:MM``, either sign.
_OFFSET: Final = r"(?:Z|[+-][0-9]{2}(?::?[0-9]{2})?)$"

#: What a cell of each type is, in an error.
_WHAT: Final[dict[type, str]] = {
    str: "text",
    int: "a whole number",
    float: "a number",
    bool: "a true or false token",
}  # no field is named so: a field name is an identifier


class CellError(UnknownLayoutError):
    """A cell that is neither its column's value nor a missing token; *index* is its row's place in the frame."""

    def __init__(self, message: str, index: int) -> None:
        """Hold the message and the row's place."""
        super().__init__(message)
        self.index = index


@dataclass(frozen=True, slots=True)
class Column:
    """One column of a table, declared in its row struct field's annotation.

    *label* is the source's name for the column, or the names it has gone by. *format* is a date's or datetime's
    ``strftime`` format, or the formats its cells are written in. *pattern* is a shape every cell must match whole.
    *missing* is the texts that stand for no value. *true* and *false* are a ``bool``'s tokens. *strip* and
    *strip_start* are characters taken off a cell first, *prefix* text every cell begins with, *grouping* a number's
    thousands separator, and *parens_negative* that ``(95.1)`` is negative. *optional* says the source may leave the
    column out.
    """

    label: str | tuple[str, ...]
    format: str | tuple[str, ...] | None = None
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
    """A row struct field's column, checked: its name, declaration, labels, cell type and polars type."""

    name: str
    column: Column
    labels: tuple[str, ...]
    kind: type
    nullable: bool
    dtype: pl.DataType
    formats: tuple[str, ...]
    #: The field's msgspec type, ``Meta`` constraints included.
    info: msgspec.inspect.Type


def resolve_type(info: msgspec.inspect.Type) -> tuple[msgspec.inspect.Type, bool]:
    """Return *info* without its ``Meta`` wrapper and its ``| None``, and whether it may be ``None``."""
    nullable = False
    while True:
        if isinstance(info, msgspec.inspect.Metadata):
            info = info.type
        elif isinstance(info, msgspec.inspect.UnionType):
            arms = [arm for arm in info.types if not isinstance(arm, msgspec.inspect.NoneType)]
            if len(arms) != 1:
                msg = f"a union of {arms!r}, which no cell reads as"
                raise TypeError(msg)
            nullable = nullable or len(arms) < len(info.types)
            info = arms[0]
        else:
            return info, nullable


#: The cell type and the column type of each msgspec type a cell reads as.
_KINDS: Final[dict[type, tuple[type, pl.DataType]]] = {
    msgspec.inspect.StrType: (str, pl.String()),
    msgspec.inspect.IntType: (int, pl.Int64()),
    msgspec.inspect.FloatType: (float, pl.Float64()),
    msgspec.inspect.BoolType: (bool, pl.Boolean()),
    msgspec.inspect.DateType: (dt.date, pl.Date()),
    msgspec.inspect.DateTimeType: (dt.datetime, pl.Datetime("us")),
}


def dtype_of(info: msgspec.inspect.Type) -> pl.DataType | None:
    """Return the column type of a field of msgspec type *info*, resolved (:func:`resolve_type`); ``None`` for none.

    A ``Literal`` of texts is an ``Enum`` of them, sorted, and a ``Meta(tz=True)`` datetime (a :data:`UtcDatetime`) is
    a ``Datetime`` in UTC.
    """
    if isinstance(info, msgspec.inspect.LiteralType):
        texts = all(isinstance(value, str) for value in info.values)
        return pl.Enum(sorted(str(value) for value in info.values)) if texts else None
    if isinstance(info, msgspec.inspect.DateTimeType) and info.tz is True:
        return pl.Datetime("us", "UTC")
    found = _KINDS.get(type(info))
    return found[1] if found is not None else None


#: Each row struct's checked columns, held only while the struct lives: a row struct built per header (a file whose
#: columns are its dates) is freed with its builder's cache entry, not kept for the life of the process.
_SPECS: Final[weakref.WeakKeyDictionary[type[msgspec.Struct], tuple[ColumnSpec, ...]]] = weakref.WeakKeyDictionary()


def column_specs(row_type: type[msgspec.Struct]) -> tuple[ColumnSpec, ...]:
    """Return *row_type*'s columns in field order; a declaration that cannot be read raises.

    Checked once per row struct and kept while the struct lives; the cache holds the struct weakly.
    """
    if (specs := _SPECS.get(row_type)) is None:
        infos = {field.name: field.type for field in msgspec.inspect.type_info(row_type).fields}  # ty: ignore[unresolved-attribute]  # a Struct's info is a StructType
        specs = _SPECS[row_type] = tuple(
            _spec(row_type, field, infos[field.name]) for field in msgspec.structs.fields(row_type)
        )
    return specs


def _spec(row_type: type, field: msgspec.structs.FieldInfo, info: msgspec.inspect.Type) -> ColumnSpec:
    where = f"{row_type.__name__}.{field.name}"
    _, *metadata = get_args(field.type) if get_origin(field.type) is Annotated else (None,)
    columns = [item for item in metadata if isinstance(item, Column)]
    if len(columns) != 1:
        msg = f"{where} declares {len(columns)} Columns, not one"
        raise TypeError(msg)
    (column,) = columns
    try:
        resolved, nullable = resolve_type(info)
    except TypeError as exc:
        msg = f"{where} is {exc}"
        raise TypeError(msg) from None
    if (dtype := dtype_of(resolved)) is None:
        if isinstance(resolved, msgspec.inspect.LiteralType):
            msg = f"{where} is a Literal of {resolved.values!r}: only text values read from a cell"
        else:
            msg = f"{where} is a {resolved!r}, which no cell reads as"
        raise TypeError(msg)
    kind = str if isinstance(dtype, pl.Enum) else _KINDS[type(resolved)][0]
    if (column.missing or column.optional) and not nullable:
        msg = f"{where} declares missing tokens or an optional column but cannot be None"
        raise ValueError(msg)
    if nullable and not (column.missing or column.optional):
        msg = f"{where} can be None, but its column declares no missing token and is not optional"
        raise ValueError(msg)
    formats = (column.format,) if isinstance(column.format, str) else column.format or ()
    if (kind in {dt.date, dt.datetime}) != bool(formats):
        msg = f"{where}: a date or datetime gives its format, and only it does"
        raise ValueError(msg)
    if any(_OFFSET_DIRECTIVE.search(fmt) for fmt in formats) and not (
        isinstance(dtype, pl.Datetime) and dtype.time_zone is not None
    ):
        msg = f"{where} reads an offset, so it is a UtcDatetime"
        raise ValueError(msg)
    if any(_OFFSET_DIRECTIVE.search(_before_offset(fmt)) for fmt in formats):
        msg = f"{where}: an offset (%z or %#z) ends its format"
        raise ValueError(msg)
    if kind is bool and (column.true is None or column.false is None):
        msg = f"{where} is a bool without both its true and false tokens"
        raise ValueError(msg)
    labels = (column.label,) if isinstance(column.label, str) else column.label
    _check_patterns(where, column, resolved)
    return ColumnSpec(field.name, column, labels, kind, nullable, dtype, formats, resolved)


def _check_patterns(where: str, column: Column, info: msgspec.inspect.Type) -> None:
    """Check that polars' regex, which runs them, compiles the column's pattern and the field's ``Meta`` pattern.

    msgspec checks a ``Meta`` pattern with :mod:`re`, which reads what polars' Rust regex does not (a look-around, a
    backreference), so one would otherwise pass here and fail at the first read, as polars' own error.
    """
    patterns = [] if column.pattern is None else [_whole(column.pattern)]
    if isinstance(info, msgspec.inspect.StrType) and info.pattern is not None:
        patterns.append(info.pattern)
    for pattern in patterns:
        try:
            pl.select(pl.lit("").str.contains(pattern))
        except pl.exceptions.ComputeError as exc:  # the regex engine's reason is the line that opens "error: "
            lines = str(exc).splitlines()
            reason = next((line.removeprefix("error: ") for line in lines if line.startswith("error: ")), lines[0])
            msg = f"{where}'s pattern {pattern!r} is not one polars' regex reads: {reason}"
            raise TypeError(msg) from None


def _whole(pattern: str) -> str:
    """Return *pattern* matched against a whole cell."""
    return f"^(?:{pattern})$"


def wire_names(labels: Sequence[str]) -> list[str]:
    """Return *labels* named as ``pl.read_csv`` names a header's: a repeat ``x`` is ``x_duplicated_0``, then ``_1``."""
    seen: dict[str, int] = {}
    names: list[str] = []
    for label in labels:
        names.append(label if label not in seen else f"{label}_duplicated_{seen[label]}")
        seen[label] = seen.get(label, -1) + 1
    return names


def _match(columns: Sequence[str], specs: Sequence[ColumnSpec], *, ordered: bool) -> dict[str, str]:
    """Return each frame column's field, by its label; a header that is not the declared columns raises."""
    seen: dict[str, int] = {}
    found: dict[str, str] = {}
    remaining = list(columns)
    for spec in specs:
        name = None
        for label in spec.labels:
            repeat = seen.get(label, 0)
            names = (label,) if not repeat else (f"{label}_duplicated_{repeat - 1}", f"{label}_{repeat}")
            candidates = remaining[:1] if ordered else remaining
            if (name := next((n for n in names if n in candidates), None)) is not None:
                seen[label] = repeat + 1
                break
        if name is not None:
            found[name] = spec.name
            remaining.remove(name)
        elif not spec.column.optional:
            break
    if remaining or any(spec.name not in found.values() and not spec.column.optional for spec in specs):
        msg = f"the header {list(columns)!r} is not the columns {[spec.labels[0] for spec in specs]!r}"
        raise UnknownLayoutError(msg)
    return found


def match_header(columns: Sequence[str], row_type: type[msgspec.Struct], *, ordered: bool = True) -> dict[str, str]:
    """Return each of *columns*' field in *row_type*; a header that is not its declared columns raises.

    The labels must be *row_type*'s, in field order (any order with *ordered* false), an ``optional`` column left out or
    not, a repeated label named as polars names it; anything else raises :class:`~flatread.errors.UnknownLayoutError`.
    """
    return _match(columns, column_specs(row_type), ordered=ordered)


def read_table(
    frame: pl.DataFrame, row_type: type[msgspec.Struct], *, numbers: pl.Series | None = None, ordered: bool = True
) -> pl.DataFrame:
    """Read *frame*, its columns under the source's labels, into *row_type*'s typed columns, under the same labels.

    The labels must be *row_type*'s, in field order (any order with *ordered* false), an ``optional`` column left out
    or not; any other header raises :class:`~flatread.errors.UnknownLayoutError`. A null cell is an empty one (``""``).
    *numbers* is each row's line or row in the source, for an error; ``1, 2, …`` by default. The first cell that is not
    its column's value raises :class:`CellError`.
    """
    specs = {spec.name: spec for spec in column_specs(row_type)}
    found = _match(frame.columns, list(specs.values()), ordered=ordered)
    names = {wire: found[wire] for wire in frame.columns}  # the frame's own order
    texts = frame.select(pl.col(wire).cast(pl.String).fill_null("").alias(name) for wire, name in names.items())
    values = texts.select(_value(specs[name]) for name in names.values())
    numbers = numbers if numbers is not None else pl.int_range(1, frame.height + 1, eager=True)
    _check(texts, values, [specs[name] for name in names.values()], numbers)
    return values.select(pl.col(name).cast(specs[name].dtype).alias(wire) for wire, name in names.items())


def _stripped(spec: ColumnSpec) -> pl.Expr:
    """Return the cell's text with the column's declared padding taken off."""
    text = pl.col(spec.name)
    if spec.column.strip is not None:
        text = text.str.strip_chars(spec.column.strip)
    if spec.column.strip_start is not None:
        text = text.str.strip_chars_start(spec.column.strip_start)
    return text


def _missing(spec: ColumnSpec) -> pl.Expr:
    return _stripped(spec).is_in(list(spec.column.missing))


def _parsed(spec: ColumnSpec) -> pl.Expr:
    """Return the cell read as its field's type, its ``Meta`` constraints aside: null for a cell that is not one."""
    column, text = spec.column, _stripped(spec)
    if column.prefix is not None:
        text = pl.when(text.str.starts_with(column.prefix)).then(text.str.strip_prefix(column.prefix))
    if column.pattern is not None:
        text = pl.when(text.str.contains(_whole(column.pattern))).then(text)
    return _read(text, spec)


def _value(spec: ColumnSpec) -> pl.Expr:
    """Return the cell read as its field's type: null for a missing token, and for a cell that is not a value."""
    column, value = spec.column, _parsed(spec)
    if (within := _within(value, spec.info)) is not None:
        value = pl.when(within).then(value)
    if column.missing:
        value = pl.when(_missing(spec)).then(None).otherwise(value)
    return value.alias(spec.name)


def _read(text: pl.Expr, spec: ColumnSpec) -> pl.Expr:
    """Return *text* read as the field's type by polars, non-strictly: null where polars reads no value."""
    column = spec.column
    if isinstance(spec.dtype, pl.Enum):
        return pl.when(text.is_in(spec.dtype.categories.to_list())).then(text)
    if spec.kind is bool:
        return text.replace_strict({column.true: True, column.false: False}, default=None, return_dtype=pl.Boolean)
    if spec.kind in {dt.date, dt.datetime}:
        return pl.coalesce(_moment(text, fmt, spec) for fmt in spec.formats)
    if spec.kind is str:
        return text
    if column.grouping is not None:
        group = re.escape(column.grouping)
        grouped = text.str.contains(rf"^\(?[+-]?[0-9]{{1,3}}(?:{group}[0-9]{{3}})*(?:\.[0-9]+)?\)?$")
        text = pl.when(grouped).then(text.str.replace_all(column.grouping, "", literal=True))
    if column.parens_negative:
        bracketed = text.str.starts_with("(") & text.str.ends_with(")")
        text = pl.when(bracketed).then("-" + text.str.strip_prefix("(").str.strip_suffix(")")).otherwise(text)
    if spec.kind is int:
        return text.str.to_integer(strict=False)
    value = text.cast(pl.Float64, strict=False)
    # polars casts a number too large for a float to an infinity; only a NaN or infinity written by name, in a
    # column whose pattern admits it (the text reaching here matched it whole), is one.
    finite = value.is_finite()
    if column.pattern is not None:
        finite |= text.str.contains(f"^(?:{NONFINITE})$")
    return pl.when(finite).then(value)


def _before_offset(fmt: str) -> str:
    """Return *fmt* without the offset directive (``%z`` or ``%#z``) that ends it; *fmt* itself without one."""
    return re.sub(r"%#?z$", "", fmt)


def _moment(text: pl.Expr, fmt: str, spec: ColumnSpec) -> pl.Expr:
    """Return *text* read in *fmt*, kept only where writing it back in *fmt* gives the cell again.

    A format with an offset ends with it (:func:`_spec` checks so). The cell is read whole, into UTC, and the text
    before its offset is the one written back, in the format before the offset: polars does not write an offset as
    the cell may (``Z``, or ``+00`` for ``%#z``).
    """
    if (head := _before_offset(fmt)) == fmt:
        parsed = _written_back(text, fmt)
        if isinstance(spec.dtype, pl.Datetime) and spec.dtype.time_zone == "UTC":
            parsed = parsed.dt.replace_time_zone("UTC")
    else:
        whole = text.str.to_datetime(fmt, time_unit="us", time_zone="UTC", strict=False, exact=True)
        local = _written_back(text.str.replace(_OFFSET, ""), head, leap_on=whole)  # a leap second is one in UTC
        parsed = pl.when(local.is_not_null() & text.str.contains(_OFFSET)).then(whole)
    return parsed.dt.date() if spec.kind is dt.date else parsed


def _written_back(text: pl.Expr, fmt: str, *, leap_on: pl.Expr | None = None) -> pl.Expr:
    """Return *text* read naive in *fmt*, kept only where writing it back in *fmt* gives the text again.

    A leap second is checked on *leap_on*, the moment the cell names in UTC, when its text is local time before an
    offset; on the value read otherwise.
    """
    parsed = text.str.to_datetime(fmt, time_unit="us", strict=False, exact=True)
    moment = parsed if leap_on is None else leap_on
    # Case aside (a month's name); a fraction (%.f) compared without it; a leap second written back as :60.
    base, cell = fmt.replace("%.f", ""), text.str.to_lowercase()
    if "%.f" in fmt:
        cell = cell.str.replace(r"(:[0-9]{2})\.[0-9]{1,6}", "${1}")
    written = parsed.dt.strftime(base).str.to_lowercase()
    before = parsed - pl.duration(seconds=1)
    leap = before.dt.strftime(base).str.to_lowercase() == cell.str.replace(r":60(\D|$)", ":59${1}")
    # Only the last second of 30 June or 31 December, which rolls over to midnight, has ever been a leap second.
    midnight = (moment.dt.hour() == 0) & (moment.dt.minute() == 0) & (moment.dt.second() == 0)
    halves = (moment - pl.duration(seconds=1)).dt.strftime("%m-%d").is_in(["06-30", "12-31"])
    same = (written == cell) | (leap & midnight & halves)
    return pl.when(same & parsed.dt.year().is_between(dt.MINYEAR, dt.MAXYEAR)).then(parsed)


def _within(value: pl.Expr, info: msgspec.inspect.Type) -> pl.Expr | None:
    """Return whether *value* holds to its field's ``Meta`` constraints; ``None`` for a field with none."""
    tests: list[pl.Expr] = []
    if isinstance(info, msgspec.inspect.IntType | msgspec.inspect.FloatType):
        bounds = {"gt": value.__gt__, "ge": value.__ge__, "lt": value.__lt__, "le": value.__le__}
        tests.extend(test(limit) for name, test in bounds.items() if (limit := getattr(info, name)) is not None)
        if info.multiple_of is not None:
            tests.append((value % info.multiple_of) == 0)
    elif isinstance(info, msgspec.inspect.StrType):
        if info.min_length is not None:
            tests.append(value.str.len_chars() >= info.min_length)
        if info.max_length is not None:
            tests.append(value.str.len_chars() <= info.max_length)
        if info.pattern is not None:  # msgspec's pattern is a search, not a whole match
            tests.append(value.str.contains(info.pattern))
    return pl.all_horizontal(tests) if tests else None


def _check(texts: pl.DataFrame, values: pl.DataFrame, specs: Sequence[ColumnSpec], numbers: pl.Series) -> None:
    """Raise for the first cell, in row and then column order, that is neither a value nor a missing token."""
    if not specs:
        return
    unread = texts.select((values[spec.name].is_null() & ~_missing(spec)).alias(spec.name) for spec in specs)
    found = unread.with_row_index(_AT).filter(pl.any_horizontal(spec.name for spec in specs))
    if found.is_empty():
        return
    first = found.row(0, named=True)
    spec = next(spec for spec in specs if first[spec.name])
    text = texts[spec.name][first[_AT]]
    if texts.slice(first[_AT], 1).select(_parsed(spec)).item() is not None:  # its type, outside its Meta's limits
        limits = ("gt", "ge", "lt", "le", "multiple_of", "min_length", "max_length", "pattern")
        held = ", ".join(
            f"{name}={limit!r}" for name in limits if (limit := getattr(spec.info, name, None)) is not None
        )
        msg = f"row {numbers[first[_AT]]}: the {spec.labels[0]!r} cell {text!r} is not within its constraints ({held})"
        raise CellError(msg, first[_AT])
    if isinstance(spec.dtype, pl.Enum):
        what = f"one of {spec.dtype.categories.to_list()!r}"
    elif spec.formats:
        what = f"a {spec.kind.__name__} in {' or '.join(spec.formats)}"
    else:
        what = _WHAT[spec.kind] + (f" matching {spec.column.pattern!r}" if spec.column.pattern is not None else "")
    allowed = f", nor one of {list(spec.column.missing)!r}" if spec.column.missing else ""
    msg = f"row {numbers[first[_AT]]}: the {spec.labels[0]!r} cell {text!r} is not {what}{allowed}"
    raise CellError(msg, first[_AT])
