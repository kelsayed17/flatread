"""Cells read into their fields' types, strictly, in one polars ``select``.

:func:`fields_of` compiles a row struct's fields once, when a layout class is built. :func:`type_frame` then reads a
frame whose columns are those fields, each as text or already typed (a sheet's floats, a JSON number), and returns
the typed frame and a frame of the same columns that is true where a cell failed: text that is neither one of the
field's missing tokens nor its type, or a missing cell in a field that is not ``| None``. The success path builds no
message; :func:`first_bad` builds one only when a read is about to raise.

A date is read by ``strftime`` format and checked by writing it back in that format, since polars' ``%Y`` alone
reads ``12/31/25`` as the year 25. A ``Decimal`` never lets polars guess its scale from sampled rows: the column's
largest scale is found first, and a cell that does not write back to its own digits is refused.
"""

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal, get_args, get_origin

import msgspec
import polars as pl

from flatread.errors import CellError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

__all__ = [
    "EPOCH_MS",
    "EPOCH_S",
    "EXCEL_SERIAL",
    "QUARTER",
    "YEAR_MONTH",
    "DateFormat",
    "Field",
    "as_text",
    "fields_of",
    "first_bad",
    "type_frame",
]


@dataclass(frozen=True, slots=True)
class DateFormat:
    """A date shape that no ``strftime`` format reads: a name, the regex a cell matches whole, and the read."""

    name: str
    pattern: str
    #: The cell's text (matching *pattern*) to its date; null where it is no real date.
    to_date: Callable[[pl.Expr], pl.Expr]
    #: Whether *to_date* returns an instant, a naive UTC datetime, read in the layout's ``time_zone``: a date field
    #: takes the day it is midnight of, a datetime field the time.
    instant: bool = False


def _quarter(text: pl.Expr) -> pl.Expr:
    parts = text.str.extract_groups(r"^(\d{4}):?Q([1-4])$")
    quarter = parts.struct[1].cast(pl.Int8)
    return pl.date(parts.struct[0].cast(pl.Int32), (quarter - 1) * 3 + 1, 1)


def _year_month(text: pl.Expr) -> pl.Expr:
    parts = text.str.extract_groups(r"^(\d{4})-(\d{2})$")
    month = parts.struct[1].cast(pl.Int8)
    month = pl.when(month.is_between(1, 12)).then(month)  # polars raises on a 13th month even where unselected
    return pl.date(parts.struct[0].cast(pl.Int32), month, 1)


def _excel_serial(text: pl.Expr) -> pl.Expr:
    days = text.str.extract(r"^(\d+)$").cast(pl.Int64)
    return pl.lit(dt.date(1899, 12, 30)) + pl.duration(days=days)


#: ``2024Q1`` or ``2024:Q1``: the quarter's first day.
QUARTER: Final = DateFormat("a quarter written 2024Q1", r"^\d{4}:?Q[1-4]$", _quarter)
#: ``2026-09``: the month's first day.
YEAR_MONTH: Final = DateFormat("a month written 2026-09", r"^\d{4}-\d{2}$", _year_month)
#: Excel's day number, whole days after 1899-12-30, as a sheet's number cell styled as a date holds it.
EXCEL_SERIAL: Final = DateFormat("an Excel day number", r"^\d+$", _excel_serial)


def _epoch(unit: Literal["s", "ms"]) -> Callable[[pl.Expr], pl.Expr]:
    return lambda text: pl.from_epoch(text.str.to_integer(strict=False), time_unit=unit).cast(pl.Datetime("us"))


#: Whole milliseconds since 1970-01-01 UTC, as a JSON number or text (OCC, Yahoo).
EPOCH_MS: Final = DateFormat("milliseconds since 1970", r"^-?\d+$", _epoch("ms"), instant=True)
#: Whole seconds since 1970-01-01 UTC.
EPOCH_S: Final = DateFormat("seconds since 1970", r"^-?\d+$", _epoch("s"), instant=True)

#: The texts a bool field reads by default, compared in lower case.
TRUTHS: Final = (("true", True), ("false", False))

#: The default of a mapping setting: read-only, so no call can change another's.
_EMPTY: Final[Mapping[str, Any]] = MappingProxyType({})

#: The prefix of a fault column while it shares the typing pass's frame with the values.
_BAD: Final = "\x00bad:"


type Kind = type | tuple[str, ...]
type Format = str | DateFormat

_SCALARS: Final[Mapping[type, type]] = {
    msgspec.inspect.StrType: str,
    msgspec.inspect.IntType: int,
    msgspec.inspect.FloatType: float,
    msgspec.inspect.DecimalType: Decimal,
    msgspec.inspect.BoolType: bool,
    msgspec.inspect.DateType: dt.date,
    msgspec.inspect.DateTimeType: dt.datetime,
    msgspec.inspect.TimeType: dt.time,
}
#: The kinds a ``dates`` format reads, each with its ISO formats and its name in an error.
_WHEN: Final[Mapping[type, tuple[tuple[str, ...], str]]] = {
    dt.date: (("%Y-%m-%d",), "date"),
    dt.datetime: (("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"), "datetime"),
    dt.time: (("%H:%M:%S",), "time"),
}

_EXPECTED: Final[Mapping[type, str]] = {
    str: "text",
    int: "a whole number",
    float: "a number",
    Decimal: "a decimal number",
    bool: "true or false",
}


@dataclass(frozen=True, slots=True)
class Field:
    """One row-struct field, compiled: its name, cell type, whether it may be missing, and how its cells read."""

    name: str
    #: ``str``, ``int``, ``float``, ``Decimal``, ``bool``, ``date`` or ``datetime``; or a ``Literal``'s values.
    kind: Kind
    nullable: bool
    missing: frozenset[str]
    formats: tuple[Format, ...]
    nonfinite: bool
    strip: bool
    #: A bool field's texts, each with its value; a cell matches one in any case.
    truths: tuple[tuple[str, bool], ...] = TRUTHS
    #: A regex the cell's text matches whole, before it is read.
    pattern: str | None = None
    #: The zone a datetime field is read in and carries; ``None`` for naive datetimes (an offset read as UTC).
    zone: str | None = None

    @property
    def expected(self) -> str:
        """What a cell of the field must be, for an error."""
        return self._expected + (f", matching the pattern {self.pattern!r}" if self.pattern is not None else "")

    @property
    def _expected(self) -> str:
        if isinstance(self.kind, tuple):
            return f"one of {list(self.kind)!r}"
        if self.kind in _WHEN:
            shapes = [f.name if isinstance(f, DateFormat) else f for f in self.formats]
            zone = f" in {self.zone}" if self.zone is not None else ""
            return f"a {_WHEN[self.kind][1]} written {' or '.join(shapes)}{zone}"
        if self.kind is bool and self.truths != TRUTHS:
            return "one of " + " or ".join(repr(text) for text, _ in self.truths)
        return _EXPECTED[self.kind]


def _literal_values(annotation: object) -> tuple[str, ...] | None:
    """Return a ``Literal``'s values in their written order (msgspec's inspection sorts them), or ``None``."""
    if get_origin(annotation) is Literal:
        return tuple(str(value) for value in get_args(annotation))
    for arg in get_args(annotation):
        if (values := _literal_values(arg)) is not None:
            return values
    return None


def _kind(info: msgspec.inspect.Type, annotation: object, where: str) -> tuple[Kind, bool]:
    """Return the cell type of a field typed *info* (annotated *annotation*) and whether it may be ``None``."""
    nullable = False
    if isinstance(info, msgspec.inspect.UnionType):
        others = [t for t in info.types if not isinstance(t, msgspec.inspect.NoneType)]
        nullable = len(others) < len(info.types)
        if len(others) == 1:
            info = others[0]
    if isinstance(info, msgspec.inspect.LiteralType) and all(isinstance(v, str) for v in info.values):
        return _literal_values(annotation) or tuple(str(value) for value in info.values), nullable
    for type_class, kind in _SCALARS.items():
        if isinstance(info, type_class):
            return kind, nullable
    msg = f"{where} has a type flatread does not read a cell into: {info!r}"
    raise TypeError(msg)


def fields_of(  # noqa: PLR0913  # the struct, then each setting a field's reading takes
    row: type[msgspec.Struct],
    *,
    missing: frozenset[str] = frozenset({""}),
    missing_by_field: Mapping[str, frozenset[str]] = _EMPTY,
    dates: Mapping[str, Format | tuple[Format, ...]] = _EMPTY,
    nonfinite: bool = False,
    strip: bool | frozenset[str] = False,
    truths: tuple[tuple[str, bool], ...] = TRUTHS,
    patterns: Mapping[str, str] = _EMPTY,
    zone: str | None = None,
) -> tuple[Field, ...]:
    """Compile *row*'s fields in order; a type no cell reads into raises :class:`TypeError`."""
    by_field, formats_of = missing_by_field, dates
    for name in (*by_field, *formats_of, *patterns, *(() if isinstance(strip, bool) else strip)):
        if name not in row.__struct_fields__:
            msg = f"{row.__name__} has no field {name!r}"
            raise TypeError(msg)
    info = msgspec.inspect.type_info(row)
    if not isinstance(info, msgspec.inspect.StructType):  # pragma: no cover  # row is a Struct by its annotation
        raise TypeError(row)
    annotations = {field.name: field.type for field in msgspec.structs.fields(row)}
    fields = []
    for field in info.fields:
        kind, nullable = _kind(field.type, annotations[field.name], f"{row.__name__}.{field.name}")
        given = formats_of.get(field.name)
        formats: tuple[Format, ...] = ()
        if isinstance(given, tuple):
            formats = given
        elif given is not None:
            formats = (given,)
        if given is not None and kind not in _WHEN:
            msg = f"{row.__name__}.{field.name} has a date format but is not a date"
            raise TypeError(msg)
        if kind is dt.time and any(isinstance(f, DateFormat) and f.instant for f in formats):
            msg = f"{row.__name__}.{field.name} is a time of day, which no instant is"
            raise TypeError(msg)
        if not formats and isinstance(kind, type) and kind in _WHEN:
            formats = _WHEN[kind][0]
        fields.append(
            Field(
                field.name,
                kind,
                nullable,
                missing | by_field.get(field.name, frozenset()),
                formats,
                nonfinite,
                strip if isinstance(strip, bool) else field.name in strip,
                truths,
                patterns.get(field.name),
                zone,
            )
        )
    return tuple(fields)


def _decimal_scale(column: pl.Series) -> int:
    longest = column.str.extract(r"\.(\d+)$").str.len_chars().max()
    return min(int(longest), 38) if isinstance(longest, int) else 0  # polars' widest; a longer fraction is no cell


def _no_trailing_zeros(text: pl.Expr) -> pl.Expr:
    return text.str.replace(r"(\.\d*?)0*$", "${1}").str.replace(r"\.$", "")


def _instant(utc: pl.Expr, field: Field) -> pl.Expr:
    """Return a naive UTC datetime as *field* reads it: in its zone, or the day it is midnight of there."""
    local = utc.dt.replace_time_zone("UTC").dt.convert_time_zone(field.zone or "UTC")
    if field.kind is dt.date:
        return pl.when(local.dt.time() == dt.time()).then(local.dt.date())
    return local if field.zone is not None else utc


def _local(parsed: pl.Expr, field: Field) -> pl.Expr:
    """Return a wall-clock datetime in *field*'s zone; null where the zone skips or repeats that time."""
    if field.zone is None or field.kind is not dt.datetime:
        return parsed
    return parsed.dt.replace_time_zone(field.zone, ambiguous="null", non_existent="null")


def _date_read(text: pl.Expr, field: Field) -> pl.Expr:
    reads = []
    for fmt in field.formats:
        if isinstance(fmt, DateFormat):
            read = _instant(fmt.to_date(text), field) if fmt.instant else _local(fmt.to_date(text), field)
            reads.append(pl.when(text.str.contains(fmt.pattern)).then(read))
            continue
        if field.kind is dt.date:
            parsed = text.str.to_date(fmt, strict=False)
        elif field.kind is dt.time:
            parsed = text.str.to_time(fmt, strict=False)
        else:
            parsed = text.str.to_datetime(fmt, strict=False, time_unit="us")
        if "%z" in fmt or "%:z" in fmt:  # an offset reads as its UTC time; the written offset cannot be written back
            reads.append(_instant(parsed.dt.convert_time_zone("UTC").dt.replace_time_zone(None), field))
            continue
        written, cell = parsed.dt.strftime(fmt), text
        if any(name in fmt for name in ("%b", "%B", "%a", "%A", "%p")):  # SEP, Sep and sep are one month
            written, cell = written.str.to_lowercase(), cell.str.to_lowercase()
        if "%.f" in fmt:  # a fraction reads with however many digits it has: 00.0, 00.000000 and 00 are one time
            written, cell = _no_trailing_zeros(written), _no_trailing_zeros(cell)
        reads.append(pl.when(written == cell).then(_local(parsed, field)))
    return pl.coalesce(reads) if len(reads) > 1 else reads[0]


def _text_read(text: pl.Expr, field: Field, scale: int) -> pl.Expr:  # noqa: PLR0911  # one return per kind
    """Return *field*'s read of its text column *text*: its value, or null where the text is not its type."""
    kind = field.kind
    if isinstance(kind, tuple):
        return text.cast(pl.Enum(list(kind)), strict=False)
    if kind is str:
        return text
    if kind is int:
        return text.str.to_integer(strict=False)
    if kind is float:
        number = text.cast(pl.Float64, strict=False)
        return number if field.nonfinite else pl.when(number.is_finite()).then(number)
    if kind is Decimal:
        number = text.cast(pl.Decimal(38, scale), strict=False)
        whole = text.str.extract(r"^(-?\d+)", 1)
        digits = text.str.extract(r"\.(\d+)$", 1).fill_null("")
        written = whole + "." + digits.str.pad_end(scale, "0") if scale else whole
        exact = text.str.contains(r"^-?\d+(\.\d+)?$") & (number.cast(pl.String) == written)
        return pl.when(exact).then(number)
    if kind is bool:
        return text.str.to_lowercase().replace_strict(
            {t.lower(): v for t, v in field.truths}, default=None, return_dtype=pl.Boolean
        )
    return _date_read(text, field)


def _native_read(  # noqa: PLR0911  # one return per native kind
    column: pl.Expr, dtype: pl.DataType, field: Field
) -> tuple[pl.Expr, pl.Expr] | None:
    """Return (value, bad) for a column already typed *dtype*, or ``None`` to read it as text instead."""
    if field.kind is float and dtype.is_numeric():
        number = column.cast(pl.Float64)
        bad = pl.lit(value=False) if field.nonfinite else column.is_not_null() & ~number.is_finite()
        return number, bad
    if field.kind is int and dtype.is_float():
        whole = column.is_finite() & (column == column.floor()) & (column.abs() < 2**63)
        return pl.when(whole).then(column.cast(pl.Int64, strict=False)), column.is_not_null() & ~whole
    if field.kind is int and dtype.is_integer():
        return column.cast(pl.Int64), pl.lit(value=False)
    if field.kind is dt.date and isinstance(dtype, pl.Datetime):  # a sheet's date cell: a midnight datetime
        midnight = column.dt.time() == dt.time()
        return pl.when(midnight).then(column.dt.date()), column.is_not_null() & ~midnight
    natives: dict[object, pl.DataType] = {bool: pl.Boolean(), dt.date: pl.Date()}  # a sheet's TRUE is a bool
    native = natives.get(field.kind)
    if native is not None and dtype == native:
        return column, pl.lit(value=False)
    if field.kind is dt.datetime and isinstance(dtype, pl.Datetime):
        value = _local(column.cast(pl.Datetime("us")), field)
        return value, column.is_not_null() & value.is_null()
    return None  # read as its text: a number into a str, Decimal or serial date, or a type it is not


def as_text(column: pl.Expr, dtype: pl.DataType) -> pl.Expr:
    """Return a native column as text: a whole float as its integer, every other float its shortest round trip."""
    if dtype.is_float():
        whole = column.is_finite() & (column == column.floor()) & (column.abs() < 2**53)
        return (
            pl.when(whole).then(column.cast(pl.Int64, strict=False).cast(pl.String)).otherwise(column.cast(pl.String))
        )
    return column.cast(pl.String)


def type_frame(text: pl.DataFrame, fields: Sequence[Field]) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Return *text*'s columns read into *fields*' types, and where a cell failed: both one column per field."""
    values: list[pl.Expr] = []
    bads: list[pl.Expr] = []
    for field in fields:
        column, dtype = pl.col(field.name), text.schema[field.name]
        native = None if dtype == pl.String or field.pattern is not None else _native_read(column, dtype, field)
        if native is not None:
            value, bad = native
            missing = column.is_null()
        else:
            cell = column if dtype == pl.String else as_text(column, dtype)
            if field.strip:
                cell = cell.str.strip_chars()
            scale = 0
            if field.kind is Decimal:
                scale = _decimal_scale(text.select(cell).to_series())
            missing = cell.is_null() | cell.is_in(list(field.missing))
            value = _text_read(cell, field, scale)
            if field.pattern is not None:
                value = pl.when(cell.str.contains(rf"\A(?:{field.pattern})\z")).then(value)
            bad = ~missing & value.is_null()
            value = pl.when(~missing).then(value)
        if not field.nullable:
            bad |= missing
        values.append(value.alias(field.name))
        bads.append(bad.fill_null(value=True).alias(_BAD + field.name))
    both = text.select(*values, *bads)  # one pass: each cell is read once for its value and its fault
    names = [field.name for field in fields]
    # Indexing and building from Series run no query: each eager select is a polars collect, a fixed cost per read.
    return both[names], pl.DataFrame([both[_BAD + name].alias(name) for name in names])


def first_bad(
    text: pl.DataFrame,
    bad: pl.DataFrame,
    fields: Sequence[Field],
    labels: Mapping[str, str],
    locate: Callable[[int], str],
) -> CellError:
    """Return the error for *bad*'s first failing cell: the first row with one, its leftmost, named by its label."""
    row = int(bad.select(pl.any_horizontal(pl.all())).to_series().arg_true()[0])
    field = next(f for f in fields if bad[f.name][row])
    cell = text[field.name][row]
    written = "" if cell is None else str(cell)
    expected = field.expected
    if cell is None or written in field.missing:
        expected += ": the field always holds a value"
    return CellError(locate(row), labels.get(field.name, field.name), written, expected)
