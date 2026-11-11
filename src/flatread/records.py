"""Records in a JSON body, decoded in one ``msgspec.json.decode`` into structs flatread builds from the layout.

``records`` is the path from the body to its records: dotted keys, each followed by any walks, ``[]`` over a list's
items and ``{}`` over a map's values. A path ending in a walk reads one record per item (``"data[]"``,
``"results[].bars[]"``, ``"[]"`` for a body that is a list, ``"bars{}[]"`` for lists keyed by symbol); a path ending
in a key reads the one object there (``"entity.stats"``, ``""`` for the body itself). A key on the path must be
present, as an error page is one without it; a ``null`` anywhere on it holds no records, and ``min_rows`` judges a
table of none.

A label is a path from its record: a key (``"bid"``), a nested key (``"quote.bid"``), or a position in a record that
is an array (``"[0]"``). ``"../"`` steps out to the item the walk before read, again for the walk before that, and
up to the body itself (``"../meta.as_of"``); ``"{}"`` is the key a map walk read its item by (``"../{}"`` for the
parent's). A key the layout does not name is ignored, and a missing key reads as null where its field is ``| None``:
Socrata, Cboe, SEC and Yahoo all leave out a key that has no value. An array record must be as long as the last
position its labels name. ``next`` names the page cursor, a path from the body, that :meth:`Json.page` returns.

The decode is **native** where it can be: a number field decodes as a JSON number, a bool as a bool. A provider that
writes its numbers as strings with its own missing token (Treasury: every value a string, ``"null"`` for none) is read
in **text** mode instead: each field decodes as its JSON scalar and is typed from its text by the shared core.
"""

import codecs
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Final, override

import msgspec
import polars as pl

from flatread import core
from flatread.cells import TRUTHS, DateFormat
from flatread.core import Plan, Text, column_names
from flatread.errors import UnknownLayoutError
from flatread.layout import Layout

if TYPE_CHECKING:
    from flatread.cells import Field

__all__ = ["Json"]

#: A JSON int a polars Int64 holds; a wider one fails the decode, naming its record.
_INT64: Final = Annotated[int, msgspec.Meta(ge=-(2**63), le=2**63 - 1)]
_NATIVE: Final[dict[object, object]] = {int: _INT64, float: float, bool: bool, Decimal: Decimal}
_COLUMN: Final[dict[object, pl.DataType]] = {int: pl.Int64(), float: pl.Float64(), bool: pl.Boolean()}
_SCALAR: Final = str | int | float | bool | None
type _Text = str | None

_RECORDS_PART: Final = re.compile(r"([^.\[\]{}]*)((?:\[\]|\{\})*)")
_LABEL_PART: Final = re.compile(r"([^.\[\]{}]*)((?:\[\d+\])*)")

#: A step through a decoded body: a key (the struct attribute it decodes to), a position, or a walk.
type _Step = tuple[str, str | int]
_LIST: Final[_Step] = ("walk", "[]")
_MAP: Final[_Step] = ("walk", "{}")


@dataclass(eq=False)
class _Node:
    """What the layout reads at one place in the body: an object's keys, an array's positions, a walk, or a value."""

    kind: str | None = None  # "object", "array", "[]", "{}" or "value"
    keys: dict[str, _Node] = field(default_factory=dict)
    required: set[str] = field(default_factory=set)
    positions: dict[int, _Node] = field(default_factory=dict)
    item: _Node | None = None
    value: object = None
    #: An object's key to the attribute its struct decodes it to.
    attrs: dict[str, str] = field(default_factory=dict)

    def be(self, kind: str, where: str) -> _Node:
        if self.kind not in {None, kind}:
            msg = f"{where} is both {self.kind} and {kind}"
            raise TypeError(msg)
        self.kind = kind
        return self

    def child(self, key: str | int, where: str) -> _Node:
        if isinstance(key, int):
            return self.be("array", where).positions.setdefault(key, _Node())
        return self.be("object", where).keys.setdefault(key, _Node())

    def walk(self, how: str, where: str) -> _Node:
        self.be(how, where)
        self.item = self.item or _Node()
        return self.item

    def decode_type(self, name: str) -> object:
        """Return the msgspec type this node decodes as, each place in it nullable."""
        if self.kind == "value":
            return self.value
        if self.kind == "object":
            fields: list[tuple[str, object] | tuple[str, object, None]] = []
            for i, (key, child) in enumerate(self.keys.items()):
                self.attrs[key] = f"f{i}"
                kind = child.decode_type(f"{name}_{i}")
                fields.append((f"f{i}", kind) if key in self.required else (f"f{i}", kind, None))
            struct = msgspec.defstruct(name, fields, rename={a: k for k, a in self.attrs.items()}, kw_only=True)
            return struct | None
        if self.kind == "array":
            width = max(self.positions) + 1
            kinds = [self.positions[i].decode_type(f"{name}_{i}") if i in self.positions else Any for i in range(width)]
            return tuple[tuple(kinds)] | None  # ty: ignore[invalid-type-form]  # exactly *width* long
        if self.item is not None:
            item = self.item.decode_type(f"{name}_item")
            return (list[item] if self.kind == "[]" else dict[str, item]) | None  # ty: ignore[invalid-type-form]
        return Any  # pragma: no cover  # every node a layout makes is read for something


def _records_steps(path: str, where: str) -> list[tuple[str, str]]:
    """Return the records path's steps: ("key", name) and ("walk", "[]" or "{}")."""
    steps: list[tuple[str, str]] = []
    for i, part in enumerate(path.split(".") if path else []):
        found = _RECORDS_PART.fullmatch(part)
        if found is None or (not found[1] and (i > 0 or not found[2])):
            msg = f"{where}: the records path {path!r} has an empty key or a step it cannot take: {part!r}"
            raise TypeError(msg)
        if found[1]:
            steps.append(("key", found[1]))
        steps += [("walk", found[2][j : j + 2]) for j in range(0, len(found[2]), 2)]
    return steps


def _label_steps(label: str, where: str) -> tuple[int, list[tuple[str, str | int]] | None]:
    """Return how far a label steps out, and its steps from there: keys and positions, or ``None`` for ``{}``."""
    up, rest = 0, label
    while rest.startswith("../"):
        up, rest = up + 1, rest[3:]
    if rest == "{}":
        return up, None
    steps: list[tuple[str, str | int]] = []
    for part in rest.split("."):
        found = _LABEL_PART.fullmatch(part)
        if found is None or (not found[1] and not found[2]):
            msg = f"{where}: the label {label!r} has an empty key or a step it cannot take: {part!r}"
            raise TypeError(msg)
        if found[1]:
            steps.append(("key", found[1]))
        steps += [("position", int(p)) for p in re.findall(r"\[(\d+)\]", found[2])]
    return up, steps


@dataclass(frozen=True, slots=True)
class _Column:
    """How one label's values are read off each record: from which item up, and by which steps."""

    #: The field it fills; ``None`` for a label that only checks its key or position is there.
    field: str | None
    #: The item read from, counted from the record's own (0) up to the body.
    up: int
    #: Attribute names and positions from that item; ``None`` reads the key a map walk read it by.
    steps: tuple[str | int, ...] | None
    #: Whether its values decode natively (a JSON number, a bool) rather than as text.
    native: bool


def _get(value: Any, steps: tuple[str | int, ...]) -> Any:  # noqa: ANN401  # a decoded JSON value
    for step in steps:
        if value is None:
            return None
        value = getattr(value, step) if isinstance(step, str) else value[step]
    return value


class Json(Layout):
    """A JSON body's records."""

    records: ClassVar[str] = "[]"
    next: ClassVar[str | None] = None

    # A base extends its parent's settings.
    _SETTINGS: ClassVar[frozenset[str]] = Layout._SETTINGS | {"records", "next"}  # noqa: SLF001
    _native: ClassVar[bool]
    _decoder: ClassVar[msgspec.json.Decoder[Any]]
    _steps: ClassVar[tuple[_Step, ...]]
    _columns: ClassVar[tuple[_Column, ...]]
    _labels_read: ClassVar[tuple[str, ...]]
    _next_steps: ClassVar[tuple[str | int, ...] | None]
    #: Whether a record is the item its last walk read (``"data[]"``), not an object beneath one (``"a[].b"``).
    _record_walked: ClassVar[bool]

    @classmethod
    @override
    def _compile(cls, row: type) -> Plan:
        if cls.variants:
            msg = f"{cls.__name__}: a JSON layout has no variants; a key it does not name is ignored"
            raise TypeError(msg)
        plan = super()._compile(row)  # ty: ignore[invalid-argument-type]
        fields = {f.name: f for f in plan.fields}
        # Text mode is for a provider's own missing tokens; strip touches text cells only, and native numbers stay.
        cls._native = not (frozenset(cls.missing) - {""}) and not cls.missing_by_field
        where = cls.__name__
        root, items, walks, path = _lay_records(cls.records, where)
        cls._record_walked = not path or path[-1][0] is None
        reads: list[tuple[int, list[tuple[str, str | int]] | None, Field | None]] = []
        for label, name in plan.label_sets[0]:  # a label without a field checks its key or position is there
            up, steps = _label_steps(label, where)
            if up >= len(items):
                msg = f"{where}: the label {label!r} steps out past the body"
                raise TypeError(msg)
            if steps is None and walks[-1 - up] != "{}":
                msg = f"{where}: the label {label!r} reads a key, but no map walk read that item"
                raise TypeError(msg)
            if steps is not None:
                value = Any if name is None else cls._decode_type(fields[name])
                required = name is None or not fields[name].nullable
                _place(items[-1 - up], steps, value, required=required, where=f"{where}: the label {label!r}")
            reads.append((up, steps, None if name is None else fields[name]))
        if cls.next is not None:
            cursor: list[tuple[str, str | int]] = [("key", step) for step in cls.next.split(".")]
            _place(root, cursor, _SCALAR, required=False, where=f"{where}: the cursor {cls.next!r}")
        body = root.decode_type(f"{where}Body") if root.kind is not None else Any
        cls._decoder = msgspec.json.Decoder(body, strict=False)  # a number written as a string ("1.25") reads too
        cls._steps = tuple(("key", p.attrs[n]) if p is not None else _LIST if n == "[]" else _MAP for p, n in path)
        cls._columns = tuple(
            _Column(
                None if f is None else f.name,
                up,
                None if steps is None else _attrs(items[-1 - up], steps),
                f is not None and cls._decode_type(f) is not _Text,
            )
            for up, steps, f in reads
        )
        cls._labels_read = tuple(label for label, _ in plan.label_sets[0])
        cls._next_steps = None if cls.next is None else tuple(_attrs(root, [("key", s) for s in cls.next.split(".")]))
        return plan

    @classmethod
    def _decode_type(cls, field: Field) -> object:
        """Return the type a field's JSON value decodes as: its own where native, else a string typed by the core."""
        if any(isinstance(f, DateFormat) and f.instant for f in field.formats):
            return _INT64 | str | None  # an epoch written as a number or as text
        if cls._native and field.kind in _NATIVE and (field.kind is not bool or field.truths == TRUTHS):
            return _NATIVE[field.kind] | None  # ty: ignore[unsupported-operator]
        return _Text

    @classmethod
    def _decode(cls, body: bytes) -> Any:  # noqa: ANN401  # the struct flatread built for this layout
        try:
            return cls._decoder.decode(body.removeprefix(codecs.BOM_UTF8))
        except msgspec.ValidationError as exc:
            msg = f"the body is not the JSON this layout reads: {exc}"
            raise UnknownLayoutError(msg) from exc
        except msgspec.DecodeError as exc:
            msg = f"the body is not JSON: {exc}"
            raise UnknownLayoutError(msg) from exc

    @classmethod
    def _walk(cls, decoded: Any) -> tuple[list[Any], list[tuple[tuple[Any, str | None], ...]] | None]:  # noqa: ANN401
        """Return every record down the records path, in order, and the items above each when a label reads them."""
        if not any(c.up or c.steps is None for c in cls._columns):  # no label reads an item above its record
            level: list[Any] = [decoded]
            for kind, step in cls._steps:
                if kind == "key":
                    level = [getattr(v, step) if v is not None else None for v in level]  # ty: ignore[invalid-argument-type]
                else:
                    level = [x for v in level if v is not None for x in (v if step == "[]" else v.values())]
            return [v for v in level if v is not None], None
        level = [decoded]
        chains: list[tuple[tuple[Any, str | None], ...]] = [((decoded, None),)]  # each item with the items above it
        for kind, step in cls._steps:
            if kind == "key":
                level = [getattr(v, step) if v is not None else None for v in level]  # ty: ignore[invalid-argument-type]
                continue
            below: list[Any] = []
            below_chains: list[tuple[tuple[Any, str | None], ...]] = []
            for value, chain in zip(level, chains, strict=True):
                if value is None:
                    continue
                for key, item in ((None, x) for x in value) if step == "[]" else value.items():
                    if item is not None:
                        below.append(item)
                        below_chains.append((*chain, (item, key)))
            level, chains = below, below_chains
        kept = [i for i, v in enumerate(level) if v is not None]
        if not cls._record_walked:  # the record is an object beneath its walk's item: it is an item of its own
            return [level[i] for i in kept], [(*chains[i], (level[i], None)) for i in kept]
        return [level[i] for i in kept], [chains[i] for i in kept]

    @classmethod
    def _text(cls, decoded: Any) -> Text:  # noqa: ANN401  # the struct flatread built for this layout
        """Return the records as columns under their labels."""
        records, chains = cls._walk(decoded)
        fields = {f.name: f for f in cls._checked_plan().fields}
        names = column_names(list(cls._labels_read))
        # Records whose every label is one key or position of their own transpose in C: msgspec's astuple, then zip.
        flat = records and all(c.field is None or (not c.up and c.steps and len(c.steps) == 1) for c in cls._columns)
        rows = records if not flat or isinstance(records[0], tuple) else map(msgspec.structs.astuple, records)
        transposed = list(zip(*rows, strict=True)) if flat else []
        columns = []
        for name, column in zip(names, cls._columns, strict=True):
            if column.field is None:
                columns.append(pl.Series(name, [None] * len(records), dtype=pl.String))
                continue
            if column.steps is None:
                values: list[Any] = [chain[-1 - column.up][1] for chain in chains or []]
            elif column.up:
                values = [_get(chain[-1 - column.up][0], column.steps) for chain in chains or []]
            elif flat:
                step = column.steps[0]
                values = list(transposed[step if isinstance(step, int) else int(step[1:])])  # attribute f<i> is i
            else:
                values = [_get(record, column.steps) for record in records]
            kind = fields[column.field].kind
            if column.native and kind in _COLUMN:
                columns.append(pl.Series(name, values, dtype=_COLUMN[kind]))
            elif column.native and kind is Decimal:  # a Decimal's exact digits, typed by the core
                columns.append(pl.Series(name, [None if v is None else str(v) for v in values], dtype=pl.String))
            else:
                columns.append(pl.Series(name, values, dtype=pl.String, strict=False))
        return Text(pl.DataFrame(columns), lambda i: f"record {i + 1}")

    @classmethod
    @override
    def read_text(cls, body: bytes) -> Text:
        return cls._text(cls._decode(body))

    @classmethod
    def page(cls, body: bytes) -> tuple[pl.DataFrame, str | None]:
        """Return one page's table and the cursor to the next, ``None`` when there is none (absent or empty)."""
        if cls._next_steps is None:
            msg = f"{cls.__name__} has no next cursor path"
            raise TypeError(msg)
        plan = cls._checked_plan()
        cls._refuse(body)
        decoded = cls._decode(cls._unwrap(body))
        cursor = _get(decoded, cls._next_steps)
        frame = core.run(plan, cls._text(decoded), **cls._hooks())
        return frame, (str(cursor) if cursor not in {None, ""} else None)


def _lay_records(records: str, where: str) -> tuple[_Node, list[_Node], list[str], list[tuple[_Node | None, str]]]:
    """Return the body's node, the items each walk reads (the body first), how each was read, and the path's steps.

    A record that is an object beneath its last walk's item (``"a[].b"``) is the last item, read by no walk: a label's
    ``"../"`` steps out from it to the walk's item.
    """
    root = _Node()
    node, items, walks = root, [root], [""]
    path: list[tuple[_Node | None, str]] = []
    for kind, name in _records_steps(records, where):
        if kind == "key":
            node.child(name, where)
            node.required.add(name)
            path.append((node, name))
            node = node.keys[name]
        else:
            node = node.walk(name, f"{where}: the body at {records!r}")
            path.append((None, name))
            items.append(node)
            walks.append(name)
    if path and path[-1][0] is not None:
        items.append(node)
        walks.append("")
    return root, items, walks, path


def _place(node: _Node, steps: list[tuple[str, str | int]], value: object, *, required: bool, where: str) -> None:
    """Mark the value *steps* lead to from *node* as read, decoded as *value*; *required* keys must be there."""
    for kind, step in steps:
        parent, node = node, node.child(step, where)
        if kind == "key" and required:
            parent.required.add(str(step))
    node.be("value", where)
    node.value = value


def _attrs(node: _Node, steps: list[tuple[str, str | int]]) -> tuple[str | int, ...]:
    """Return *steps* as the struct attributes and positions they decode to, from *node*."""
    out: list[str | int] = []
    for _, step in steps:
        if isinstance(step, int):
            out.append(step)
            node = node.positions[step]
        else:
            out.append(node.attrs[step])
            node = node.keys[step]
    return tuple(out)
