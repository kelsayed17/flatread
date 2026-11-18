"""The one typing engine: a table's cells typed by the row struct's fields, then checked, the same for every format.

The engine is built once per reader, from its row struct: each field's column (its label, type and markers), the
fields that must hold a value, the defaults. It holds no read's state: a read hands it the table and the read's
:class:`ReadContext`, and it returns the frame. A text cell is typed by the rules 7.x's text readers share (the same
functions: a whole number or a number trimmed, ``true`` or ``false`` or a ``Flag``'s words, a date by its ``Format``,
exactly), but in a format that writes each cell exactly (a CSV's, as 7.x's ``CsvFile`` reads it) a padded number is
refused and a text cell of spaces kept. A cell its format stored as a number, a boolean or a moment (a sheet's) is
taken as that value, and a cell of another kind than its field reads is refused, so a sheet's number never becomes
text without a word.
"""

import dataclasses
import datetime as dt
import inspect
import types
import typing
from collections.abc import Collection, Sequence

import msgspec
import polars as pl

from flatread.core import (
    _BOOLEANS,
    _DTYPES,
    _MOMENTS,
    _NUL,
    Column,
    _bare,
    _check_markers,
    _flagged,
    _found,
    _fullmatching,
    _literal,
    _marker,
    _moment,
    _scalar_type,
    _temporal,
    _typed,
)
from flatread.errors import EmptyBodyError, LayoutError, ReadError, UnknownLayoutError
from flatread.fields import Cell, Flag, Format, Joined, Label, Shape, Span
from flatread.next._model import Cleaner, ReadContext, Table

__all__ = ["AsWritten", "Engine"]


@dataclasses.dataclass(frozen=True)
class AsWritten:
    """A text field's marker: a cell its format stored as a number, a boolean or a moment is read as its text.

    Without it, such a cell in a text field is refused, so a sheet's ``7`` is never ``"7"`` without a word, and a
    CUSIP or ZIP code stored as a number does not lose its leading zeros unnoticed.
    """


#: A bare date field's format: ISO 8601's, exactly.
_ISO_DATE: typing.Final = Format("%Y-%m-%d")


class Engine[RowT: msgspec.Struct]:
    """Types and checks a table as rows of *row*: built once, it serves every read and every thread.

    *null_values* are the file's words for no value; a field of *as_written* keeps its text exactly as the file
    writes it, empty text and whitespace included; *rows_required* is whether a table of no rows is refused.
    *exact* is whether the format writes each cell exactly as its value (a CSV's, as 7.x's ``CsvFile`` reads it): a
    number cell padded with whitespace (`` 5``) is refused, not trimmed, and a text cell of spaces is that text, not
    no value; a :class:`~flatread.next.Padded` cleaner strips them first. In every format a float cell whose digits
    are not all zero but which reads as ``0.0`` (``1e-400``, an underflow) is refused.
    """

    def __init__(  # noqa: PLR0913 (a field's rules each format sets)
        self,
        row: type[RowT],
        *,
        null_values: Collection[str] = (),
        as_written: Collection[str] = (),
        rows_required: bool = True,
        exact: bool = False,
        moments: bool = False,
    ) -> None:
        if keyword := [p.name for p in inspect.signature(row).parameters.values() if p.kind is p.KEYWORD_ONLY]:
            msg = f"the fields {keyword} of {row.__name__} are keyword-only, and a reader builds its rows by position"
            raise LayoutError(msg)
        self.row = row
        fields = msgspec.structs.fields(row)
        # A label two fields share is two columns, by place (a header writes ``%`` twice): the later ones are named
        # as a finder names a repeated heading's later columns, by the label, a NUL and their count.
        seen: dict[str, int] = {}
        self.columns = []
        for field in fields:
            column = _column(row, field, moments=moments)
            seen[column.label] = count = seen.get(column.label, -1) + 1
            self.columns.append(column._replace(label=f"{column.label}{_NUL}{count}") if count else column)
        if stray := set(as_written) - {column.field for column in self.columns}:
            msg = f"as_written names {sorted(stray)}, which are no fields of {row.__name__}"
            raise LayoutError(msg)
        self.fields = [field.name for field in fields]
        self.labels = [column.label for column in self.columns]
        self._null_values = list(null_values)
        self._as_written = frozenset(as_written)
        self._rows_required = rows_required
        self._exact = exact
        self._numbers_as_text = frozenset(
            field.name for field in fields if any(isinstance(meta, AsWritten) for meta in _metadata(field.type))
        )
        self._defaults = {
            field.name: field.default
            for field in fields
            if field.default is not msgspec.NODEFAULT and field.default is not None
        }
        self._required = [
            field.name
            for field in fields
            if types.NoneType not in typing.get_args(_bare(field.type)) and field.name not in self._defaults
        ]
        #: The fields that hold a value on every row: none admits ``None`` or has a default.
        self.required = frozenset(self._required)
        self._metas = {field.name: _metas(field.type) for field in fields}
        self.dtypes = {
            column.field: _DTYPES.get(column.scalar) or _temporal(column.scalar, column.written)
            for column in self.columns
        }

    def frame(self, table: Table, read: ReadContext, cleaners: Sequence[Cleaner] = ()) -> pl.DataFrame:
        """Return *table* typed as the row's fields, with what *read* states on every row, checked; or raise."""
        expected = [column.label for column in self.columns if column.field not in read.stated]
        if table.cells.columns != expected:
            msg = f"the header {[_shown(c) for c in table.cells.columns]} is not {[_shown(c) for c in expected]}"
            raise UnknownLayoutError(msg)
        numbers = table.numbers
        # A column a format read typed (a CSV's numbers, by polars' parser) is its values.
        fused = {
            column.field: table.cells[column.label].alias(column.field)
            for column in self.columns
            if column.label in table.cells.columns and table.cells.schema[column.label] != pl.String
        }
        if table.kinds is None and not cleaners:
            fused |= self._fused(table, fused)
        typed = []
        for column in self.columns:
            if column.field in fused:
                typed.append(fused[column.field])
            elif column.label in table.cells.columns:
                typed.append(self._typed(column, table, cleaners, numbers))
            else:  # a field the file states once, for every row (a trailer's stamp)
                text = pl.Series(column.label, [read.stated[column.field]], pl.String)
                value = self._from_text(column, self._blanked(column, text, cleaners), 1)
                typed.append(value.new_from_index(0, table.cells.height))
        frame = pl.DataFrame(typed)
        return self._checked(frame, numbers)

    def rows(self, frame: pl.DataFrame) -> list[RowT]:
        """Return *frame*'s rows as structs, by position: :meth:`frame` typed and checked every column."""
        return list(map(self.row, *(column.to_list() for column in frame.iter_columns()), strict=True))

    # --- typing ---

    def _fused(self, table: Table, done: Collection[str] = ()) -> dict[str, pl.Series]:
        """Return the text table's plain columns (text, numbers, booleans) typed in one select, by field.

        The same rules as :meth:`_typed`, as expressions polars runs together: a column a cell of which is not its
        type is left out, for :meth:`_typed` to raise on, naming the cell, as it reads a column alone. A column of
        *done*'s fields a format has typed already.
        """
        plain = [
            column
            for column in self.columns
            if column.label in table.cells.columns
            and column.written is None
            and column.flag is None
            and column.field not in done
        ]
        if not plain:
            return {}
        # Three steps, each column's intermediate a column of its own, so polars computes it once: its text stripped,
        # then its text blanked, then its value and whether a cell is not of its type.
        stripped = [pl.col(column.label).str.strip_chars().alias(f"{column.field}\0s") for column in plain]
        blanked = [
            (
                pl.col(column.label)
                if column.field in self._as_written
                else pl.col(column.label).replace(["", *self._null_values], None)
                if self._exact and column.scalar is str
                else pl.when(pl.col(f"{column.field}\0s") != "")
                .then(pl.col(column.label))
                .replace(["", *self._null_values], None)
            ).alias(f"{column.field}\0b")
            for column in plain
        ]
        typed = (
            table.cells.lazy()
            .with_columns(stripped)
            .with_columns(blanked)
            .select(expr for column in plain for expr in self._plain(column, exact=self._exact))
            .collect()
        )
        refused = typed.select(
            (pl.col(f"{column.field}\0b").is_not_null() & pl.col(column.field).is_null()).any()
            | _any_underflowed(column, pl.col(f"{column.field}\0b"), pl.col(column.field))
            for column in plain
        ).row(0)
        return {column.field: typed[column.field] for column, bad in zip(plain, refused, strict=True) if not bad}

    @staticmethod
    def _plain(column: Column, *, exact: bool) -> list[pl.Expr]:
        """Return the expressions of a plain column's value, from its blanked text, and that text, kept to check it.

        A number is cast from its text stripped, but in an *exact* format, where padding leaves it no number.
        """
        text = pl.col(f"{column.field}\0b")
        if column.scalar is str:
            value = text
        elif column.scalar is bool:
            value = text.replace_strict(_BOOLEANS, default=None, return_dtype=pl.Boolean)
        else:
            number = text if exact else pl.col(f"{column.field}\0s")
            value = pl.when(text.is_not_null()).then(number.cast(_DTYPES[column.scalar], strict=False))
        return [value.alias(column.field), text]

    def _blanked(self, column: Column, text: pl.Series, cleaners: Sequence[Cleaner]) -> pl.Series:
        """Return *column*'s text cells, a blank one no value, cleaned, and one of the null words no value."""
        if column.field in self._as_written:
            return text
        if not (self._exact and column.scalar is str):  # a CSV's text cell of spaces is that text
            text = text.to_frame().select(pl.when(pl.first().str.strip_chars() != "").then(pl.first())).to_series()
        for cleaner in cleaners:
            text = cleaner.clean(text, column)
        return text.replace(["", *self._null_values], None)

    def _from_cells(
        self, column: Column, cells: pl.Series, cleaners: Sequence[Cleaner], numbers: pl.Series
    ) -> pl.Series:
        """Return *column*'s values from a text table's *cells*, blanked, cleaned and typed.

        A column of few distinct cells (a long table's record, its date repeated on each key's row) is typed a cell
        once, unless a cleaner cleans it, which may read the column whole.
        """
        uniques = cells.unique(maintain_order=True)
        if cleaners or uniques.len() * 2 > cells.len():
            return self._from_text(column, self._blanked(column, cells, cleaners), numbers)
        try:
            values = self._from_text(column, self._blanked(column, uniques, cleaners), 1)
        except ReadError:  # typed whole again, to name the first row holding the cell refused
            return self._from_text(column, self._blanked(column, cells, cleaners), numbers)
        typed = pl.DataFrame({"cell": uniques, "value": values})
        joined = (
            cells.alias("cell").to_frame().join(typed, on="cell", how="left", nulls_equal=True, maintain_order="left")
        )
        return joined["value"].alias(values.name)

    def _from_text(self, column: Column, text: pl.Series, numbers: int | pl.Series) -> pl.Series:
        """Return *column*'s values from its text cells: the rules every text format shares.

        In an exact format a number cell padded with whitespace is refused; in any, a float that underflows to 0.0.
        """
        if column.written is None and column.scalar in _MOMENTS - {dt.date}:  # a sheet's moment field, as text here
            _refuse(text.is_not_null(), text, column, "date cell: its field names no Format for text", numbers)
            return pl.repeat(None, text.len(), dtype=_temporal(column.scalar, None), eager=True).alias(column.field)
        number = column.written is None and column.flag is None and column.scalar in (int, float)
        what = "whole number" if column.scalar is int else "number"
        if number and self._exact:
            _refuse(text.is_not_null() & (text != text.str.strip_chars()), text, column, what, numbers)
        values = _typed(column._replace(label=_shown(column.label)), text, f"{_shown(column.label)!r} cell", numbers)
        if (
            number
            and column.scalar is float
            and pl.select(_any_underflowed(column, pl.lit(text), pl.lit(values))).item()
        ):
            under = (values == 0) & text.str.contains(_DIGIT)
            _refuse(under, text, column, "number a float holds: it underflows to 0.0", numbers)
        return values

    def _typed(  # noqa: PLR0911 (one answer a scalar)
        self, column: Column, table: Table, cleaners: Sequence[Cleaner], numbers: pl.Series
    ) -> pl.Series:
        if table.kinds is None or column.label in table.written:
            return self._from_cells(column, table.cells[column.label], cleaners, numbers)
        text = self._blanked(column, table.cells[column.label], cleaners)
        assert table.floats is not None  # noqa: S101 (a Cells grid has all three)
        assert table.moments is not None  # noqa: S101
        kinds = table.kinds[column.label]
        floats, moments = table.floats[column.label], table.moments[column.label]
        is_text = kinds.is_null() | (kinds == "text")
        if column.scalar is str:
            if column.field in self._numbers_as_text:
                return text.alias(column.field)
            _refuse(text.is_not_null() & ~is_text, text, column, "text cell: mark the field AsWritten()", numbers)
            return text.alias(column.field)
        if column.scalar in (int, float):
            _refuse(
                text.is_not_null() & (kinds != "number").fill_null(value=False), text, column, "number cell", numbers
            )
            if column.scalar is int:
                _refuse(floats != floats.floor(), text, column, "whole number", numbers)
                _refuse(
                    ~floats.is_between(*_INT_RANGE, closed="left"), text, column, "whole number of 64 bits", numbers
                )
                _refuse(
                    floats.abs() >= _EXACT_INT,
                    text,
                    column,
                    "whole number a number cell holds exactly (below 2**53)",
                    numbers,
                )
                return floats.cast(pl.Int64, strict=True).alias(column.field)
            return floats.alias(column.field)
        if column.scalar is bool:
            stored = (kinds == "bool").fill_null(value=False)
            _refuse(text.is_not_null() & ~stored & ~is_text, text, column, "boolean or text cell", numbers)
            spelled = text.zip_with(~stored, _nulls(text)).alias(column.label)
            values = (
                _flagged(spelled, column.flag, numbers)
                if column.flag is not None
                else _booleans(spelled, column, numbers)
            )
            native = text.replace_strict(_BOOLEANS, default=None, return_dtype=pl.Boolean)
            return native.zip_with(stored, values).alias(column.field)
        # A date or time: a moment cell is its value; a text cell is read by its field's format.
        stored = text.is_not_null() & (kinds == "moment").fill_null(value=False)
        if column.written is None and column.scalar is not dt.date:  # no Format: a moment cell alone
            _refuse(
                text.is_not_null() & ~stored, text, column, "date cell: its field names no Format for text", numbers
            )
        _refuse(text.is_not_null() & ~stored & ~is_text, text, column, "date cell or text", numbers)
        written = column.written or _ISO_DATE
        dtype = _temporal(column.scalar, written)
        spelled = text.zip_with(~stored, _nulls(text))
        parsed = _moment(spelled.alias(column.label), dtype, written, numbers)
        native = (
            moments.dt.date()
            if column.scalar is dt.date
            else moments.dt.time()
            if column.scalar is dt.time
            else moments
        )
        return parsed.zip_with(~stored, native.cast(dtype)).alias(column.field)

    # --- checks ---

    def _checked(self, frame: pl.DataFrame, numbers: pl.Series) -> pl.DataFrame:
        if wrong := {name: str(dtype) for name, dtype in frame.schema.items() if dtype != self.dtypes[name]}:
            msg = f"the engine typed {wrong}, not {self.row.__name__}'s {[str(self.dtypes[n]) for n in wrong]}"
            raise LayoutError(msg)
        if self._defaults:
            frame = frame.with_columns(pl.col(name).fill_null(value) for name, value in self._defaults.items())
        if self._rows_required and frame.is_empty():
            msg = "the body is a header and no rows"
            raise EmptyBodyError(msg)
        labels = {column.field: _shown(column.label) for column in self.columns}
        for name, dtype in frame.schema.items():
            if dtype == pl.Float64 and (found := _found(~frame[name].is_finite().fill_null(value=True), numbers)):
                msg = f"the [{labels[name]!r}] column holds NaN or an infinity, which is no number here: row {found[1]}"
                raise ReadError(msg, row=found[1], label=labels[name])
        empty = {labels[name]: found[1] for name in self._required if (found := _found(frame[name].is_null(), numbers))}
        if empty:
            row = min(empty.values())
            msg = (
                f"the {list(empty)} columns have an empty cell, a short row or no column, and every cell holds a value:"
                f" row {row}"
            )
            raise ReadError(msg, row=row, label=next(label for label, at in empty.items() if at == row))
        for column in self.columns:
            cells = frame[column.field]
            if column.shape is not None:
                _refuse_values(
                    cells,
                    ~cells.str.contains(_fullmatching(column.shape)),
                    column,
                    f"of its shape {column.shape.pattern!r}",
                    numbers,
                )
            if column.literal is not None:
                _refuse_values(
                    cells, ~cells.is_in(column.literal), column, f"one of its values {list(column.literal)!r}", numbers
                )
            for meta in self._metas[column.field]:
                for bound, (passes, words) in _BOUNDS.items():
                    if (limit := getattr(meta, bound)) is not None:
                        _refuse_values(cells, ~passes(cells, limit), column, f"{words} {limit!r}", numbers)
        return frame


def _shown(label: str) -> str:
    """Return the label a column is shown by: a repeated label's later column's name, without its NUL and count."""
    return label.partition(_NUL)[0]


def _column(row: type[msgspec.Struct], field: msgspec.structs.FieldInfo, *, moments: bool = False) -> Column:
    """Return *field*'s column: its label (its own name, where it names no ``Label``), type and markers.

    A date-and-time or time field names the ``Format`` its text is written in, unless the format stores *moments*
    (a sheet's date cells): then a field of none reads a moment cell alone, and refuses a text one.
    """
    metadata = _metadata(field.type)
    for marker in (Span, Joined, Cell):
        if _marker(metadata, marker) is not None:
            msg = f"the field {row.__name__}.{field.name} is a {marker.__name__}'s, which flatread.next lacks"
            raise LayoutError(msg)
    named = _marker(metadata, Label)
    try:
        scalar = _scalar_type(field.type)
    except LayoutError as e:
        msg = f"the field {field.name!r}: {e}"
        raise LayoutError(msg) from e
    written = _marker(metadata, Format)
    if written is None and scalar is dt.date:
        written = _ISO_DATE  # flatread.next: a bare date is ISO 8601's, exactly
    if written is None and scalar in _MOMENTS - {dt.date} and not moments:
        msg = f"the field {field.name!r} needs the Format its text is written in"
        raise LayoutError(msg)
    column = Column(
        field.name,
        field.name if named is None else named.text,
        scalar,
        written,
        _marker(metadata, Flag),
        None,
        aliases=() if named is None else tuple(named.alternatives),
        shape=_marker(metadata, Shape),
        literal=_literal(field.type),
    )
    _check_markers(column)
    return column


def _metadata(annotation: object) -> tuple[object, ...]:
    return typing.get_args(annotation)[1:] if typing.get_origin(annotation) is typing.Annotated else ()


def _nulls(text: pl.Series) -> pl.Series:
    return pl.repeat(None, text.len(), dtype=pl.String, eager=True)


def _booleans(text: pl.Series, column: Column, numbers: int | pl.Series) -> pl.Series:
    values = text.replace_strict(_BOOLEANS, default=None, return_dtype=pl.Boolean)
    _refuse(text.is_not_null() & values.is_null(), text, column, "true or false", numbers)
    return values


#: The whole numbers an ``int`` field holds, 64 bits' (a float load reads ``1e20`` as one), as floats: low, high.
_INT_RANGE: typing.Final = (-(2.0**63), 2.0**63)
#: The bound below which a sheet's number cell, a double, holds a whole number exactly (``flatread.sheets._EXACT_INT``).
_EXACT_INT: typing.Final = 2.0**53


#: A number's text whose digits before any exponent are not all zero.
_DIGIT: typing.Final = r"^[^eE]*[1-9]"


def _any_underflowed(column: Column, text: pl.Expr, value: pl.Expr) -> pl.Expr:
    """Return whether a float *column*'s *value* is 0.0 where its *text*'s digits before any exponent are not all zero.

    polars reads ``1e-400``, past a float's least, as ``0.0``: a value the file did not write. Only the zeros' text is
    looked at.
    """
    if column.scalar is not float or column.written is not None or column.flag is not None:
        return pl.lit(value=False)
    return text.filter(value == 0).str.contains(_DIGIT).any()


def _refuse(wrong: pl.Series, text: pl.Series, column: Column, what: str, numbers: int | pl.Series) -> None:
    """Raise ``ReadError`` naming the first cell of *text* where *wrong* holds, which is not *what*."""
    if (found := _found(wrong.fill_null(value=False), numbers)) is not None:
        place, row = found
        msg = f"the {_shown(column.label)!r} cell {text[place]!r} is not a {what}: row {row}"
        raise ReadError(msg, row=row, label=_shown(column.label))


def _refuse_values(values: pl.Series, wrong: pl.Series, column: Column, what: str, numbers: pl.Series) -> None:
    """Raise ``ReadError`` naming the first value where *wrong* holds, which is not *what*, by its row."""
    if (found := _found(wrong.fill_null(value=False), numbers)) is not None:
        place, row = found
        msg = f"the {_shown(column.label)!r} cell {values[place]!r} is not {what}: row {row}"
        raise ReadError(msg, row=row, label=_shown(column.label))


#: Each msgspec ``Meta`` bound, the polars test a value passes, and how a failure reads.
_BOUNDS: typing.Final[dict[str, tuple[typing.Callable[[pl.Series, typing.Any], pl.Series], str]]] = {
    "ge": (lambda value, bound: value >= bound, "at least"),
    "gt": (lambda value, bound: value > bound, "more than"),
    "le": (lambda value, bound: value <= bound, "at most"),
    "lt": (lambda value, bound: value < bound, "less than"),
    "multiple_of": (lambda value, bound: value % bound == 0, "a multiple of"),
    "min_length": (lambda value, bound: value.str.len_chars() >= bound, "at least this long:"),
    "max_length": (lambda value, bound: value.str.len_chars() <= bound, "at most this long:"),
    "pattern": (lambda value, bound: value.str.contains(str(bound)), "matching"),
}


def _metas(annotation: object) -> list[msgspec.Meta]:
    """Return every msgspec ``Meta`` in *annotation*, through ``Annotated`` and unions."""
    found: list[msgspec.Meta] = []
    stack = [annotation]
    while stack:
        current = stack.pop()
        if typing.get_origin(current) is typing.Annotated:
            found += [meta for meta in typing.get_args(current)[1:] if isinstance(meta, msgspec.Meta)]
        stack.extend(typing.get_args(current))
    return found
