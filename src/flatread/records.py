"""Records in a JSON body, decoded in one ``msgspec.json.decode`` into structs flatread builds from the layout.

``records`` is the path to the list of records: dotted keys, where ``name[]`` walks a list on the way (``"data"``,
``"results[].bars"``); ``""`` is a body that is the list itself. ``envelope`` copies values at other paths onto every
row, and ``next`` names the page cursor that :meth:`Json.page` returns beside the table.

The decode is **native** where it can be: a number field decodes as a JSON number, a bool as a bool. A provider that
writes its numbers as strings with its own missing token (Treasury: every value a string, ``"null"`` for none) is read
in **text** mode instead: each field decodes as its JSON scalar and is typed from its text by the shared core. A key
the layout does not name is ignored: a JSON record names its values, so an added key cannot shift the others, as an
added column in a CSV does. A missing key is an error unless its field is ``| None``.
"""

from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Final, override

import msgspec
import polars as pl

from flatread import core
from flatread.core import Plan, Text, column_names
from flatread.errors import UnknownLayoutError
from flatread.layout import Layout

if TYPE_CHECKING:
    from collections.abc import Mapping

    from flatread.cells import Field

__all__ = ["Json"]

#: A JSON int a polars Int64 holds; a wider one fails the decode, naming its record.
_INT64: Final = Annotated[int, msgspec.Meta(ge=-(2**63), le=2**63 - 1)]
_NATIVE: Final[dict[object, object]] = {int: _INT64, float: float, bool: bool, Decimal: Decimal}
_COLUMN: Final[dict[object, pl.DataType]] = {int: pl.Int64(), float: pl.Float64(), bool: pl.Boolean()}
type _Text = str | None


def _path(path: str) -> list[tuple[str, bool]]:
    """Return a dotted path's keys, each with whether a list is walked after it."""
    return [(part.removesuffix("[]"), part.endswith("[]")) for part in path.split(".")] if path else []


class Json(Layout):
    """A JSON body's records."""

    records: ClassVar[str] = ""
    envelope: ClassVar[Mapping[str, str]] = MappingProxyType[str, str]({})
    next: ClassVar[str | None] = None

    # A base extends its parent's settings.
    _SETTINGS: ClassVar[frozenset[str]] = Layout._SETTINGS | {"records", "envelope", "next"}  # noqa: SLF001
    _native: ClassVar[bool]
    _decoder: ClassVar[msgspec.json.Decoder[Any]]

    @classmethod
    @override
    def _carried_fields(cls) -> frozenset[str]:
        return frozenset(cls.envelope.values())

    @classmethod
    @override
    def _compile(cls, row: type) -> Plan:
        if cls.variants:
            msg = f"{cls.__name__}: a JSON layout has no variants; a key it does not name is ignored"
            raise TypeError(msg)
        if not cls.records and (cls.envelope or cls.next is not None):
            msg = f"{cls.__name__}: a body that is a list of records has no envelope or cursor"
            raise TypeError(msg)
        plan = super()._compile(row)  # ty: ignore[invalid-argument-type]
        fields = {f.name: f for f in plan.fields}
        cls._native = not cls.strip and not (frozenset(cls.missing) - {""}) and not cls.missing_by_field
        pairs = [(label, f) for label, f in plan.label_sets[0] if f is not None]
        cls._pairs = pairs
        record = _struct(
            f"{cls.__name__}Record",
            {label: (cls._decode_type(fields[f]), not fields[f].nullable) for label, f in pairs},
        )
        envelope = [path.split(".") for path in (*cls.envelope, *((cls.next,) if cls.next is not None else ()))]
        body: object = (
            list[record] if not cls.records else _level(cls.__name__, 0, envelope, _path(cls.records), record)  # ty: ignore[invalid-type-form]
        )
        cls._decoder = msgspec.json.Decoder(body)
        return plan

    _pairs: ClassVar[list[tuple[str, str]]]

    @classmethod
    def _decode_type(cls, field: Field) -> object:
        """Return the type a field's JSON value decodes as: its own where native, else a string typed by the core."""
        if cls._native and field.kind in _NATIVE:
            return _NATIVE[field.kind] | None  # ty: ignore[unsupported-operator]
        return _Text

    @classmethod
    def _decode(cls, body: bytes) -> Any:  # noqa: ANN401  # the struct flatread built for this layout
        try:
            return cls._decoder.decode(body)
        except msgspec.ValidationError as exc:
            msg = f"the body is not the JSON this layout reads: {exc}"
            raise UnknownLayoutError(msg) from exc
        except msgspec.DecodeError as exc:
            msg = f"the body is not JSON: {exc}"
            raise UnknownLayoutError(msg) from exc

    @staticmethod
    def _at(value: Any, path: str) -> Any:  # noqa: ANN401  # a decoded JSON value
        for key in path.split("."):
            if value is None:
                return None
            value = _get(value, key)
        return value

    @classmethod
    def _records(cls, decoded: Any) -> list[Any]:  # noqa: ANN401  # the struct flatread built for this layout
        """Return every record down the records path, in order."""
        if not cls.records:
            return decoded
        level = [decoded]
        *head, (last, _) = _path(cls.records)
        for key, walks in head:
            children = [_get(node, key) for node in level]
            level = [x for c in children if c is not None for x in (c if walks else [c])]
        return [record for node in level if (found := _get(node, last)) is not None for record in found]

    @classmethod
    def _text(cls, decoded: Any) -> Text:  # noqa: ANN401  # the struct flatread built for this layout
        """Return the records as columns under their labels, and the envelope's values."""
        records = cls._records(decoded)
        fields = {f.name: f for f in cls._checked_plan().fields}
        values = list(zip(*map(msgspec.structs.astuple, records), strict=True)) or [()] * len(cls._pairs)
        columns = []
        for name, (_, f), column in zip(column_names([lb for lb, _ in cls._pairs]), cls._pairs, values, strict=True):
            kind = fields[f].kind
            if cls._native and kind in _COLUMN:
                columns.append(pl.Series(name, column, dtype=_COLUMN[kind]))
            elif cls._native and kind is Decimal:  # a Decimal's exact digits, typed by the core
                columns.append(pl.Series(name, [None if v is None else str(v) for v in column], dtype=pl.String))
            else:
                columns.append(pl.Series(name, column, dtype=pl.String, strict=False))
        carried: dict[str, str | None] = {}
        for path, f in cls.envelope.items():
            value = cls._at(decoded, path)
            carried[f] = value if value is None or isinstance(value, str) else msgspec.json.encode(value).decode()
        return Text(pl.DataFrame(columns), lambda i: f"record {i + 1}", carried)

    @classmethod
    @override
    def read_text(cls, body: bytes) -> Text:
        return cls._text(cls._decode(body))

    @classmethod
    def page(cls, body: bytes) -> tuple[pl.DataFrame, str | None]:
        """Return one page's table and the cursor to the next, ``None`` when there is none (absent or empty)."""
        if cls.next is None:
            msg = f"{cls.__name__} has no next cursor path"
            raise TypeError(msg)
        plan = cls._checked_plan()
        cls._refuse(body)
        decoded = cls._decode(cls._unwrap(body))
        cursor = cls._at(decoded, cls.next)
        frame = core.run(plan, cls._text(decoded), **cls._hooks())
        return frame, (str(cursor) if cursor not in {None, ""} else None)


def _struct(name: str, keys: dict[str, tuple[object, bool]]) -> type:
    """Return a struct decoding an object's *keys*, each (type, required); a key it does not name is ignored."""
    return msgspec.defstruct(
        name,
        [(f"f{i}", t) if required else (f"f{i}", t, None) for i, (t, required) in enumerate(keys.values())],
        rename={f"f{i}": key for i, key in enumerate(keys)},
        kw_only=True,
    )


def _level(name: str, depth: int, envelope: list[list[str]], records: list[tuple[str, bool]], record: type) -> type:
    """Return the struct of one level of the body: the envelope's keys under it and the records path through it."""
    keys: dict[str, tuple[object, bool]] = {}
    below: dict[str, list[list[str]]] = {}
    for path in envelope:
        if len(path) == 1:
            keys[path[0]] = (str | int | float | bool | None, False)
        else:
            below.setdefault(path[0], []).append(path[1:])
    head, walks = records[0] if records else ("", False)
    for key, paths in below.items():
        if key != head:
            keys[key] = (_level(name, depth + 1, paths, [], record), False)
    if records:
        if len(records) == 1:
            keys[head] = (list[record], True)  # ty: ignore[invalid-type-form]
        else:  # the records path is required and not null: a body without it is an error page, not an empty one
            inner = _level(name, depth + 1, below.get(head, []), records[1:], record)
            keys[head] = (list[inner] if walks else inner, True)  # ty: ignore[invalid-type-form]
    return _struct(f"{name}Level{depth}", keys)


def _get(node: Any, key: str) -> Any:  # noqa: ANN401  # a decoded JSON value
    """Return *key* of a decoded node: a struct flatread built renames each key to ``f<i>``."""
    for name, encoded in zip(node.__struct_fields__, node.__struct_encode_fields__, strict=True):
        if encoded == key:
            return getattr(node, name)
    return None
