"""The ``Layout`` base: a table declared as a class, its settings checked once, its readers derived from them.

A provider's package declares a class per provider (its defaults) and a class per table under it (what differs).
Settings are class attributes, inherited and overridden as any are; an override replaces the parent's value whole.
Building a class with a ``row`` checks every setting and compiles them into a :class:`~flatread.core.Plan`, so a read
does no set-up work. ``frame`` and ``rows`` are classmethods, so they pass straight to headgate's ``body_reader``.
"""

from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, Final

import polars as pl

from flatread import core
from flatread.cells import Format, fields_of
from flatread.core import Erratum, Long, Plan, Text
from flatread.errors import EmptyBodyError, ReadError

if TYPE_CHECKING:
    import msgspec

__all__ = ["Layout"]

type Labels = Mapping[str, str | None] | Sequence[tuple[str, str | None]]

#: How far into a body a refusal is looked for: a provider's "no file" page says so near its top.
_REFUSAL_WINDOW: Final = 64 * 1024

# Read-only defaults of the mapping settings, so no class can change another's.
_NO_TOKENS: Final[Mapping[str, AbstractSet[str]]] = MappingProxyType[str, AbstractSet[str]]({})
_NO_DATES: Final[Mapping[str, Format | tuple[Format, ...]]] = MappingProxyType[str, Format | tuple[Format, ...]]({})
_NO_REFUSALS: Final[Mapping[bytes, type[ReadError]]] = MappingProxyType[bytes, type[ReadError]]({})


def _pairs(labels: Labels) -> tuple[tuple[str, str | None], ...]:
    items: list[tuple[str, str | None]] = list(labels.items() if isinstance(labels, Mapping) else labels)
    return tuple(items)


def _compiles(pattern: str, where: str) -> None:
    try:
        pl.Series([""]).str.contains(pattern)
    except pl.exceptions.PolarsError as exc:
        msg = f"{where}: polars cannot compile the regex {pattern!r}: {exc}"
        raise TypeError(msg) from exc


class Layout:
    """One table of a provider's: its row struct, its labels, and only the settings where it differs.

    A subclass without ``row`` is a provider's defaults, never read itself. See the design document for each
    setting; the hooks are :meth:`cells` and :meth:`check`, and a format base implements :meth:`read_text`.
    """

    #: The msgspec struct a row is; its fields, in order, are the frame's columns.
    row: ClassVar[type[msgspec.Struct] | None] = None
    #: The provider's label to field, in the file's column order; ``None`` checks a column present and drops it.
    labels: ClassVar[Labels] = ()
    #: Other whole label maps, tried in order when the header is not ``labels``'s.
    variants: ClassVar[Sequence[Labels]] = ()
    #: Texts that stand for no value, in every column.
    missing: ClassVar[AbstractSet[str]] = frozenset({""})
    #: Extra missing tokens for one field.
    missing_by_field: ClassVar[Mapping[str, AbstractSet[str]]] = _NO_TOKENS
    #: Take whitespace off both ends of every text cell first.
    strip: ClassVar[bool] = False
    #: Field to ``strftime`` format, a named format, or a tuple of them.
    dates: ClassVar[Mapping[str, Format | tuple[Format, ...]]] = _NO_DATES
    #: Whether a float field admits NaN and infinities.
    nonfinite: ClassVar[bool] = False
    #: A wide table read long.
    long: ClassVar[Long | None] = None
    #: The field a group heading above the header fills, for a long table.
    headings: ClassVar[str | None] = None
    #: Known provider mistakes, fixed before typing; each must still be in the body.
    errata: ClassVar[Sequence[Erratum]] = ()
    #: Body text to the error a provider's "no file" answer raises; ``b""`` is an empty body.
    refusals: ClassVar[Mapping[bytes, type[ReadError]]] = _NO_REFUSALS
    #: Fewer rows raise :class:`~flatread.errors.EmptyBodyError`.
    min_rows: ClassVar[int] = 1
    #: The fields that identify a row: checked unique, and the key :func:`flatread.diff` compares by.
    key: ClassVar[Sequence[str]] = ()
    #: A filter of the read frame: one body, several tables.
    where: ClassVar[pl.Expr | None] = None

    #: The setting names a class may set; a format base adds its own.
    _SETTINGS: ClassVar[frozenset[str]] = frozenset({
        "row", "labels", "variants", "missing", "missing_by_field", "strip", "dates", "nonfinite", "long",
        "headings", "errata", "refusals", "min_rows", "key", "where",
    })  # fmt: skip
    _plan: ClassVar[Plan]

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Check the class's settings; with a ``row``, compile its plan."""
        super().__init_subclass__(**kwargs)
        for name, value in vars(cls).items():
            settable = not name.startswith("_") and not callable(value) and not isinstance(value, classmethod)
            if settable and name not in cls._SETTINGS:
                msg = f"{cls.__name__} sets {name!r}, which is not a setting of {cls.__mro__[1].__name__}"
                raise TypeError(msg)
        if cls.row is not None:
            cls._plan = cls._compile(cls.row)

    @classmethod
    def _compile(cls, row: type[msgspec.Struct]) -> Plan:
        """Check the settings against *row* and return the plan they make."""
        fields = fields_of(
            row,
            missing=frozenset(cls.missing),
            missing_by_field={k: frozenset(v) for k, v in cls.missing_by_field.items()},
            dates=cls.dates,
            nonfinite=cls.nonfinite,
            strip=cls.strip,
        )
        names = set(row.__struct_fields__)
        label_sets = tuple(_pairs(labels) for labels in (cls._labels(), *cls.variants))
        for labels in label_sets:
            for label, field in labels:
                if field is not None and field not in names:
                    msg = f"{cls.__name__}: the label {label!r} names no field {field!r} of {row.__name__}"
                    raise TypeError(msg)
        filled = {f for _, f in label_sets[0] if f is not None} | cls._carried_fields()
        if cls.long is not None:
            _compiles(cls.long.names, f"{cls.__name__}.long.names")
            filled |= {cls.long.name, cls.long.value}
        if cls.headings is not None:
            filled.add(cls.headings)
        for field in (*cls.key, *(f for f in (cls.headings,) if f is not None)):
            if field not in names:
                msg = f"{cls.__name__} names no field {field!r} of {row.__name__}"
                raise TypeError(msg)
        if unfilled := [f for f in row.__struct_fields__ if f not in filled]:
            msg = f"{cls.__name__}: no label fills the fields {unfilled!r} of {row.__name__}"
            raise TypeError(msg)
        return Plan(
            row=row,
            fields=fields,
            label_sets=label_sets,
            long=cls.long,
            headings=cls.headings,
            errata=tuple(cls.errata),
            where=cls.where,
            min_rows=cls.min_rows,
            key=tuple(cls.key),
        )

    @classmethod
    def _labels(cls) -> Labels:
        """Return the label set the header is matched against first (a format without labels makes its own)."""
        return cls.labels

    @classmethod
    def _carried_fields(cls) -> frozenset[str]:
        """Return the fields a format fills from outside the table (a trailer's stamp, a JSON envelope)."""
        return frozenset[str]()

    @classmethod
    def _checked_plan(cls) -> Plan:
        if cls.row is None:
            msg = f"{cls.__name__} has no row: it is a provider's defaults, read through a table class under it"
            raise TypeError(msg)
        return cls._plan

    @classmethod
    def read_text(cls, body: bytes) -> Text:
        """Return *body*'s table as text under its labels: what each format base implements."""
        raise NotImplementedError

    @classmethod
    def cells(cls, frame: pl.DataFrame) -> pl.DataFrame:
        """Return the text cells, under the provider's labels, before typing: override for a provider's own fix."""
        return frame

    @classmethod
    def check(cls, frame: pl.DataFrame) -> None:
        """Check the typed frame, before ``where``: override for a rule of the provider's own; raise ``CheckError``."""

    @classmethod
    def _hooks(cls) -> dict[str, Callable[..., Any]]:
        """Return the hooks the class overrides, so a read runs only those."""
        below = cls.__mro__[: cls.__mro__.index(Layout)]
        return {name: getattr(cls, name) for name in ("cells", "check") if any(name in vars(k) for k in below)}

    @classmethod
    def _refuse(cls, body: bytes) -> None:
        head = body[:_REFUSAL_WINDOW]
        for marker, error in cls.refusals.items():
            if (not marker and not body.strip()) or (marker and marker in head):
                msg = f"the body is the provider's answer that it has no file: {marker!r}"
                raise error(msg)

    @classmethod
    def frame(cls, body: bytes) -> pl.DataFrame:
        """Return *body*'s table: a frame of the row's fields, typed, checked and filtered."""
        plan = cls._checked_plan()
        cls._refuse(body)
        body = cls._unwrap(body)
        if not body.strip():
            if plan.min_rows == 0:
                return core.empty_frame(plan)
            msg = "the body is empty"
            raise EmptyBodyError(msg)
        return core.run(plan, cls.read_text(body), **cls._hooks())

    @classmethod
    def _unwrap(cls, body: bytes) -> bytes:
        """Return the file inside *body*: the body itself, or a format's or archive's member."""
        return body

    @classmethod
    def rows(cls, body: bytes) -> list[Any]:
        """Return *body*'s table as row structs, in order: :meth:`frame`'s rows."""
        row = cls._checked_plan().row
        return list(map(row, *(column.to_list() for column in cls.frame(body).iter_columns()), strict=True))

    @classmethod
    def from_text(cls, frame: pl.DataFrame) -> pl.DataFrame:
        """Return a text frame under the provider's labels read as :meth:`frame` reads a body's: for tests."""
        return core.run(cls._checked_plan(), Text(frame, lambda i: f"row {i + 1}"), **cls._hooks())
