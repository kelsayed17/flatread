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
    "EXCEL_SERIAL",
    "QUARTER",
    "YEAR_MONTH",
    "DateFormat",
    "Field",
    "dtype_of",
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

#: The default of a mapping setting: read-only, so no call can change another's.
_EMPTY: Final[Mapping[str, Any]] = MappingProxyType({})

#: The prefix of a fault column while it shares the typing pass's frame with the values.
_BAD: Final = "\x00bad:"

_ISO_DATE: Final = ("%Y-%m-%d",)
_ISO_DATETIME: Final = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")

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

    @property
    def expected(self) -> str:
        """What a cell of the field must be, for an error."""
        if isinstance(self.kind, tuple):
            return f"one of {list(self.kind)!r}"
        if self.kind in {dt.date, dt.datetime}:
            shapes = [f.name if isinstance(f, DateFormat) else f for f in self.formats]
            return f"a {'date' if self.kind is dt.date else 'datetime'} written {' or '.join(shapes)}"
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
    strip: bool = False,
) -> tuple[Field, ...]:
    """Compile *row*'s fields in order; a type no cell reads into raises :class:`TypeError`."""
    by_field, formats_of = missing_by_field, dates
    for name in (*by_field, *formats_of):
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
        if given is not None and kind not in {dt.date, dt.datetime}:
            msg = f"{row.__name__}.{field.name} has a date format but is not a date"
            raise TypeError(msg)
        if not formats and kind in {dt.date, dt.datetime}:
            formats = _ISO_DATE if kind is dt.date else _ISO_DATETIME
        fields.append(
            Field(
                field.name,
                kind,
                nullable,
                missing | by_field.get(field.name, frozenset()),
                formats,
                nonfinite,
                strip,
            )
        )
    return tuple(fields)


def dtype_of(field: Field) -> pl.DataType:
    """Return the polars type of *field*'s column (a ``Decimal``'s scale is its column's, so 0 for an empty one)."""
    if isinstance(field.kind, tuple):
        return pl.Enum(list(field.kind))
    dtypes: dict[object, pl.DataType] = {
        str: pl.String(),
        int: pl.Int64(),
        float: pl.Float64(),
        Decimal: pl.Decimal(38, 0),
        bool: pl.Boolean(),
        dt.date: pl.Date(),
        dt.datetime: pl.Datetime("us"),
    }
    return dtypes[field.kind]


def _decimal_scale(column: pl.Series) -> int:
    longest = column.str.extract(r"\.(\d+)$").str.len_chars().max()
    return int(longest) if isinstance(longest, int) else 0


def _date_read(text: pl.Expr, field: Field) -> pl.Expr:
    reads = []
    for fmt in field.formats:
        if isinstance(fmt, DateFormat):
            reads.append(pl.when(text.str.contains(fmt.pattern)).then(fmt.to_date(text)))
            continue
        parsed = (
            text.str.to_date(fmt, strict=False)
            if field.kind is dt.date
            else text.str.to_datetime(fmt, strict=False, time_unit="us")
        )
        reads.append(pl.when(parsed.dt.strftime(fmt) == text).then(parsed))
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
            {"true": True, "false": False}, default=None, return_dtype=pl.Boolean
        )
    return _date_read(text, field)


def _native_read(column: pl.Expr, dtype: pl.DataType, field: Field) -> tuple[pl.Expr, pl.Expr] | None:
    """Return (value, bad) for a column already typed *dtype*, or ``None`` to read it as text instead."""
    if field.kind is float and dtype.is_numeric():
        number = column.cast(pl.Float64)
        bad = pl.lit(value=False) if field.nonfinite else column.is_not_null() & ~number.is_finite()
        return number, bad
    if field.kind is int and dtype.is_float():
        whole = column.is_finite() & (column == column.floor())
        return pl.when(whole).then(column.cast(pl.Int64)), column.is_not_null() & ~whole
    if field.kind is int and dtype.is_integer():
        return column.cast(pl.Int64), pl.lit(value=False)
    natives: dict[object, pl.DataType] = {bool: pl.Boolean(), dt.date: pl.Date()}
    native = natives.get(field.kind)
    if native is not None and dtype == native:
        return column, pl.lit(value=False)
    if field.kind is dt.datetime and isinstance(dtype, pl.Datetime):
        return column.cast(pl.Datetime("us")), pl.lit(value=False)
    return None  # read as its text: a number into a str, Decimal or serial date, or a type it is not


def _as_text(column: pl.Expr, dtype: pl.DataType) -> pl.Expr:
    """Return a native column as text: a whole float as its integer, every other float its shortest round trip."""
    if dtype.is_float():
        whole = column.is_finite() & (column == column.floor()) & (column.abs() < 2**53)
        return pl.when(whole).then(column.cast(pl.Int64).cast(pl.String)).otherwise(column.cast(pl.String))
    return column.cast(pl.String)


def type_frame(text: pl.DataFrame, fields: Sequence[Field]) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Return *text*'s columns read into *fields*' types, and where a cell failed: both one column per field."""
    values: list[pl.Expr] = []
    bads: list[pl.Expr] = []
    for field in fields:
        column, dtype = pl.col(field.name), text.schema[field.name]
        native = None if dtype == pl.String else _native_read(column, dtype, field)
        if native is not None:
            value, bad = native
            missing = column.is_null()
        else:
            cell = column if dtype == pl.String else _as_text(column, dtype)
            if field.strip:
                cell = cell.str.strip_chars()
            scale = 0
            if field.kind is Decimal:
                scale = _decimal_scale(text.select(cell).to_series())
            missing = cell.is_null() | cell.is_in(list(field.missing))
            value = _text_read(cell, field, scale)
            bad = ~missing & value.is_null()
            value = pl.when(~missing).then(value)
        if not field.nullable:
            bad |= missing
        values.append(value.alias(field.name))
        bads.append(bad.fill_null(value=True).alias(_BAD + field.name))
    both = text.select(*values, *bads)  # one pass: each cell is read once for its value and its fault
    names = [field.name for field in fields]
    return both.select(names), both.select(pl.col(_BAD + name).alias(name) for name in names)


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
