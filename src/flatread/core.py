"""The one path every format shares: a :class:`Text` table under the provider's labels, read into the row's fields.

A format base (CSV, sheet, JSON, fixed width) only turns a body into a :class:`Text`: its cells as written (or already
typed, where the format types them), named by label, and how to name a row in an error. :func:`run` does the rest,
in order: the header against the layout's label sets, the errata, the ``cells`` hook, the long melt, the carried
values, the typing (:func:`~flatread.cells.type_frame`), the ``check`` hook, the key, ``where`` and ``min_rows``.
"""

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import polars as pl

from flatread.cells import Field, first_bad, type_frame
from flatread.errors import CheckError, EmptyBodyError, ErratumGoneError, UnknownLayoutError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from collections.abc import Set as AbstractSet

    import msgspec

__all__ = [
    "Erratum", "Long", "Plan", "Text", "column_names", "erratum_found", "field_labels", "match_header",
    "place",
    "prepare", "run",
]  # fmt: skip

#: The hidden column that keeps each row's place in the text, for an error, through the melt and the typing.
_ROW: Final = "\x00row"
#: The hidden column a melted column's name is held in until the label's fields are read off it.
_NAME: Final = "\x00name"

type LabelSet = tuple[tuple[str, str | None], ...]


@dataclass(slots=True)
class Text:
    """A body's table before typing: its cells under the provider's labels, and how to name a row in an error."""

    #: The cells, one column per label as written, a label's repeats ``label#2``, ``label#3`` (:func:`column_names`).
    frame: pl.DataFrame
    #: The row at an index of *frame* as an error names it: ``"line 12"``, ``"sheet 'Data' row 5"``, ``"record 3"``.
    locate: Callable[[int], str]
    #: Values read off the body outside the table (a stamp, a trailer, an envelope), by field, as text; ``None`` for
    #: one the body leaves out.
    carried: dict[str, str | None] = field(default_factory=dict)
    #: The row above the header, one cell per column of *frame*, for a layout whose ``headings`` groups its columns.
    headings: tuple[str | None, ...] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Long:
    """A wide table read long: every column ``labels`` does not name melts into rows, one per filled cell."""

    #: The field each melted cell becomes.
    value: str
    #: The regex every melted column's label matches whole: a renamed column raises instead of becoming a new series.
    #: Its named groups fill the fields they name (a series, a vintage, a maturity), typed as those fields are.
    names: str
    #: The field the whole label fills, if any (a date for dated columns).
    name: str | None = None
    #: Whether a melted cell that is missing yields no row; otherwise it is the value field's missing value.
    drop_missing: bool = False


@dataclass(frozen=True, slots=True)
class Erratum:
    """A known mistake of the provider's, fixed on the text cell before typing; one no longer in the body raises."""

    #: The label of the cell's column.
    label: str
    #: The text the provider wrote.
    wrote: str
    #: The text it means.
    means: str
    #: Why: the provider's mistake, in a few words, for the error raised when it is gone.
    why: str
    #: Other cells of the row, by label, as written: which row, where *wrote* is not unique.
    where: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class Plan:
    """A layout's settings, checked and compiled once, when its class is built."""

    row: type[msgspec.Struct]
    fields: tuple[Field, ...]
    #: ``labels`` and then each of ``variants``, as ordered (label, field) pairs.
    label_sets: tuple[LabelSet, ...]
    long: Long | None
    headings: str | None
    errata: tuple[Erratum, ...]
    where: pl.Expr | None
    min_rows: int
    key: tuple[str, ...]


def column_names(labels: Sequence[str]) -> list[str]:
    """Return a frame's column names for *labels* in order: each as written, its repeats ``label#2``, ``label#3``."""
    seen: dict[str, int] = {}
    names = []
    for label in labels:
        seen[label] = seen.get(label, 0) + 1
        names.append(label if seen[label] == 1 else f"{label}#{seen[label]}")
    return names


def match_header(plan: Plan, columns: list[str]) -> LabelSet:
    """Return the label set *columns* are written in; none raises, naming what was found."""
    for labels in plan.label_sets:
        names = column_names([label for label, _ in labels])
        if (columns[: len(names)] if plan.long else columns) == names:
            return labels
    found = columns if plan.long is None else columns[: len(plan.label_sets[0])]
    expected = [label for label, _ in plan.label_sets[0]]
    others = f" (or one of its {len(plan.label_sets) - 1} variants)" if len(plan.label_sets) > 1 else ""
    msg = f"the header is {found!r}, not {expected!r}{others}"
    raise UnknownLayoutError(msg)


def _erratum_at(erratum: Erratum) -> pl.Expr:
    at = pl.col(erratum.label) == erratum.wrote
    for label, written in erratum.where.items():
        at &= pl.col(label) == written
    return at


def erratum_found(frame: pl.DataFrame, erratum: Erratum) -> bool:
    """Return whether *frame*, a text frame under labels, still holds the mistake *erratum* fixes."""
    if not {erratum.label, *erratum.where} <= set(frame.columns):
        return False
    return bool(frame.select(_erratum_at(erratum).any()).item())


def _fix(frame: pl.DataFrame, errata: Sequence[Erratum]) -> pl.DataFrame:
    for erratum in errata:
        if not erratum_found(frame, erratum):
            msg = (
                f"the erratum {erratum.why!r} is gone: no {erratum.label!r} cell reads {erratum.wrote!r} where expected"
            )
            raise ErratumGoneError(msg)
        at = _erratum_at(erratum)
        frame = frame.with_columns(
            pl.when(at).then(pl.lit(erratum.means)).otherwise(erratum.label).alias(erratum.label)
        )
    return frame


def _melt(plan: Plan, frame: pl.DataFrame, index: list[str], groups: Mapping[str, str | None]) -> pl.DataFrame:
    """Melt every column but *index* into the long plan's fields, in row order then column order."""
    long = plan.long
    assert long is not None  # noqa: S101  # only a long plan melts
    melted = [c for c in frame.columns if c not in {*index, _ROW}]
    columns = set(frame.columns)
    label_of = {c: _label(c, columns) for c in melted}
    matched = pl.Series(list(label_of.values()), dtype=pl.String).str.contains(f"^(?:{long.names})$")
    if bad := [label for label, ok in zip(label_of.values(), matched, strict=True) if not ok]:  # one match, any width
        msg = f"the columns {bad!r} are not series this layout reads (their labels must match {long.names!r})"
        raise UnknownLayoutError(msg)
    frame = frame.unpivot(melted, index=[_ROW, *index], variable_name=_NAME, value_name=long.value)
    if plan.headings is not None:
        heading = pl.col(_NAME).replace_strict({c: groups.get(c) for c in melted}, return_dtype=pl.String)
        frame = frame.with_columns(heading.alias(plan.headings))
    label = pl.col(_NAME).replace_strict(label_of, return_dtype=pl.String)
    parts = re.compile(long.names).groupindex
    if parts:
        found = label.str.extract_groups(f"^(?:{long.names})$")
        frame = frame.with_columns(found.struct.field(group).alias(group) for group in parts)
    if long.name is not None:
        frame = frame.with_columns(label.alias(long.name))
    return frame.drop(_NAME).sort(_ROW, maintain_order=True)


def _label(name: str, columns: AbstractSet[str]) -> str:
    """Return the label a column name stands for: ``A#2`` is the second ``A`` when the frame has an ``A``."""
    head, sep, tail = name.rpartition("#")
    return head if sep and tail.isdigit() and head in columns else name


def _groups(columns: Sequence[str], headings: tuple[str | None, ...] | None) -> dict[str, str | None]:
    """Return each column's heading, a heading carrying rightwards over the empty cells after it."""
    groups: dict[str, str | None] = {}
    current = None
    for column, heading in zip(columns, headings or (), strict=False):
        current = heading if heading not in {None, ""} else current
        groups[column] = current
    return groups


def place(frame: pl.DataFrame, i: int) -> int:
    """Return the text row a prepared frame's row *i* came from: itself, or where a melt took it from."""
    return int(frame[_ROW][i]) if _ROW in frame.columns else i


def field_labels(plan: Plan) -> dict[str, str]:
    """Return each field's label, from the first label set that has one, for an error."""
    return {f: label for labels in reversed(plan.label_sets) for label, f in labels if f is not None}


def prepare(plan: Plan, text: Text, *, cells: Callable[[pl.DataFrame], pl.DataFrame] | None = None) -> pl.DataFrame:
    """Return *text*'s cells under the row's field names, before typing: steps 1 to 4 of :func:`run`.

    A long frame keeps a hidden row column, so a typing error can name the row's place in the body after the melt.
    """
    frame = text.frame
    labels = match_header(plan, frame.columns)
    frame = _fix(frame, plan.errata)
    if cells is not None:
        frame = cells(frame)
    groups = _groups(frame.columns, text.headings)
    names = column_names([label for label, _ in labels])
    renames = {name: f for name, (_, f) in zip(names, labels, strict=True) if f is not None}
    frame = frame[[n for n in frame.columns if n not in names or n in renames]]  # a label without a field dropped
    frame.columns = [renames.get(n, n) for n in frame.columns]  # indexing and renaming in place run no query
    if plan.long is not None:  # only a melt moves rows; a row index would also merge polars' parallel chunks
        frame = frame.with_row_index(_ROW)
    if plan.long is not None:
        frame = _melt(plan, frame, list(renames.values()), groups)
        value = next(f for f in plan.fields if f.name == plan.long.value)
        filled = pl.col(plan.long.value).is_not_null()
        if frame.schema[plan.long.value] == pl.String:  # a typed column (a sheet's floats) has no missing tokens
            filled &= ~pl.col(plan.long.value).is_in(list(value.missing))
        if plan.long.drop_missing:
            frame = frame.filter(filled)
    if text.carried:
        frame = frame.with_columns(pl.lit(value, dtype=pl.String).alias(name) for name, value in text.carried.items())
    absent = [f.name for f in plan.fields if f.name not in frame.columns]
    if absent:  # a nullable field the header's label set leaves unfilled (the compile refused a required one)
        frame = frame.with_columns(pl.lit(None, dtype=pl.String).alias(name) for name in absent)
    return frame


def _type_carried(plan: Plan, carried: Mapping[str, str | None]) -> None:
    """Raise where a value read off the body outside the table (a stamp, a count's line) is not its field's type.

    Typed once, apart from the rows: a file with no rows still has its stamp, and a bad one is no row's fault.
    """
    fields = [f for f in plan.fields if f.name in carried]
    if not fields:
        return
    text = pl.DataFrame({f.name: [carried[f.name]] for f in fields}, schema=dict.fromkeys(carried, pl.String))
    _, bad = type_frame(text.select([f.name for f in fields]), fields)
    if bad.select(pl.any_horizontal(pl.all()).any()).item():
        raise first_bad(text, bad, fields, {f.name: f.name for f in fields}, lambda _: "outside the table")


def run(
    plan: Plan,
    text: Text,
    *,
    cells: Callable[[pl.DataFrame], pl.DataFrame] | None = None,
    check: Callable[[pl.DataFrame], None] | None = None,
) -> pl.DataFrame:
    """Return *text* read into *plan*'s row: a frame of its fields, typed, checked and filtered."""
    _type_carried(plan, text.carried)
    frame = prepare(plan, text, cells=cells)
    fields = [f.name for f in plan.fields]
    typed, bad = type_frame(frame[fields], plan.fields)
    if any(column.any() for column in bad.iter_columns()):
        raise first_bad(frame, bad, plan.fields, field_labels(plan), lambda i: text.locate(place(frame, i)))
    if check is not None:
        check(typed)
    if plan.key and (dupes := typed.filter(pl.struct(plan.key).is_duplicated())).height:
        repeated = ", ".join(f"{k}={v}" for k, v in zip(plan.key, dupes.select(plan.key).row(0), strict=True))
        msg = f"the key {list(plan.key)!r} repeats: {repeated}"
        raise CheckError(msg)
    if plan.where is not None:
        typed = typed.filter(plan.where)
    if typed.height < plan.min_rows:
        msg = f"the file has {typed.height} rows, fewer than {plan.min_rows}"
        raise EmptyBodyError(msg)
    return typed
