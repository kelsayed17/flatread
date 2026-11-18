"""The header rules: how a header's labels name the struct's columns, when it is not the struct's labels in order.

A table finder finds the header row; a rule (its ``header=``) reads the labels in it. :class:`Headers` takes a file
published under several headers, each the struct's labels in order with some left out (``HeadersCsv``); :class:`Named`
one whose header names its columns in any order (``NamedCsv``). Each drops the columns no field reads (*unread*) and
checks the ones that never hold a value (*blank*) first, and returns the table under the struct's labels, in order, a
column the header lacks null, for the engine to type.
"""

import dataclasses
import typing
from collections.abc import Callable, Collection, Sequence

import polars as pl

from flatread.core import _NUL, Column
from flatread.errors import LayoutError, ReadError, UnknownLayoutError
from flatread.next._model import ReadContext, Table

__all__ = ["Headers", "Named"]

#: The dtype of a column a header lacks in each of a table's frames: its text, kinds, floats and moments.
_LACKED: typing.Final = (pl.String, pl.String, pl.Float64, pl.Datetime("us"))


def _label(name: str) -> str:
    """Return the header cell a table's column *name* is under (a repeated cell's later columns are named apart)."""
    return name.split(_NUL, 1)[0]


def _header(table: Table) -> list[str]:
    return [_label(name) for name in table.cells.columns]


def _dropped(table: Table, unread: Collection[str], blank: Collection[str]) -> Table:
    """Return *table* without its columns under an *unread* or a *blank* label, refusing a blank one holding a value."""
    names = [name for name in table.cells.columns if _label(name) in {*unread, *blank}]
    for name in (name for name in names if _label(name) in blank):
        held = (table.cells[name].fill_null("") != "").arg_true()  # a space too: the column holds nothing
        if held.len():
            at, label = int(held[0]), _label(name)
            row = int(table.numbers[at])
            msg = f"the {label!r} column holds {table.cells[name][at]!r}, and it never holds a value: row {row}"
            raise ReadError(msg, row=row, label=label)
    if not names:
        return table
    return table.with_frames([None if f is None else f.drop(names) for f in table.frames()], table.numbers)


def _laid_out(table: Table, names: dict[str, str], labels: Sequence[str]) -> Table:
    """Return *table*'s columns *names* (each to its label) under *labels*, in order, a label no column has null."""
    by_label = {label: name for name, label in names.items()}
    height = table.cells.height

    def laid_out(frame: pl.DataFrame | None, lacked: pl.DataType | type[pl.DataType]) -> pl.DataFrame | None:
        if frame is None:
            return None
        return pl.DataFrame(
            [
                frame[by_label[label]].alias(label) if label in by_label else pl.Series(label, [None] * height, lacked)
                for label in labels
            ]
        )

    frames = [laid_out(frame, lacked) for frame, lacked in zip(table.frames(), _LACKED, strict=True)]
    return table.with_frames(frames, table.numbers)


def _read(read: ReadContext) -> list[Column]:
    """Return the columns *read*'s header names: the struct's, but those a stage states."""
    return [column for column in read.columns.values() if column.field not in read.stated]


@dataclasses.dataclass(frozen=True)
class Headers:
    """A file published under more than one header, each the struct's labels in order, some left out.

    *lacking* names every header the file has been published with by the fields it lacks: ``((), ("shares",))`` is
    the struct's whole header and the one without ``shares``'s column. A body's header must be one of them, and a field
    it lacks reads as null. A column under an *unread* label is dropped wherever it stands, and one under a *blank*
    label too, once each of its cells is checked empty (Nasdaq's ``Filler``). A field a stage states (a trailer's
    stamp) is in no header. A file whose headers follow a rule rather than a list is a :class:`HeaderRule` of its own.
    """

    lacking: Sequence[Collection[str]] = ((),)
    unread: Collection[str] = ()
    blank: Collection[str] = ()

    def check(self, columns: Sequence[Column], required: Collection[str]) -> None:
        """Refuse a lacked field that is no column, one that holds a value on every row, and a list of no headers."""
        if not self.lacking:
            msg = "Headers names no header the file is published under: lacking is empty"
            raise LayoutError(msg)
        lacked = {field for lacking in self.lacking for field in lacking}
        if stray := lacked - {column.field for column in columns}:
            msg = f"the headers lack {sorted(stray)}, which are no fields read from a column"
            raise LayoutError(msg)
        if held := lacked & set(required):
            msg = f"a header lacking {sorted(held)} could never be read: each holds a value on every row"
            raise LayoutError(msg)

    def match(self, table: Table, read: ReadContext) -> Table:
        """Return *table* under the struct's labels, refusing a header that is none of the file's."""
        header = _header(table)
        table = _dropped(table, self.unread, self.blank)
        columns = _read(read)
        published = [tuple(c.label for c in columns if c.field not in lacking) for lacking in self.lacking]
        if (found := tuple(_header(table))) not in published:
            msg = f"the header {header!r} is none of the file's {[list(h) for h in published]}"
            raise UnknownLayoutError(msg)
        return _laid_out(table, dict(zip(table.cells.columns, found, strict=True)), [c.label for c in columns])


@dataclasses.dataclass(frozen=True)
class Named:
    """A file whose header names its columns: any of the struct's labels, in any order, each once.

    A field's ``Label`` may name *alternatives*, other words a header writes it in, and *heading* folds each header
    cell before it is looked up (``str.lower``, for a file whose headers write its labels in any case). Every header
    names the column of each field that holds a value on every row; a field that may be empty may be left out, and then
    reads as null, unless it is one of *required*: its column is in every header, though its cells may be empty. A
    column under an *unread* label (as written) is dropped, and one under a *blank* label too, once its cells are
    checked empty; any other cell no label names is refused. A field a stage states is in no header.
    """

    required: Collection[str] = ()
    unread: Collection[str] = ()
    blank: Collection[str] = ()
    heading: Callable[[str], str] | None = None

    def check(self, columns: Sequence[Column], required: Collection[str]) -> None:
        """Refuse a word two fields' labels share and a required field that is no column."""
        del required
        _words(columns)
        if stray := set(self.required) - {column.field for column in columns}:
            msg = f"the required fields {sorted(stray)} are no fields read from a column"
            raise LayoutError(msg)

    def match(self, table: Table, read: ReadContext) -> Table:
        """Return *table* under the struct's labels, refusing a cell no label names, a label twice, a required lack."""
        header = _header(table)
        table = _dropped(table, self.unread, self.blank)
        columns = _read(read)
        words = _words(columns)
        labels = {
            name: words.get(_label(name) if self.heading is None else self.heading(_label(name)))
            for name in table.cells.columns
        }
        if unknown := [_label(name) for name, label in labels.items() if label is None]:
            msg = f"the header {header!r} names {unknown}, which are no field's labels"
            raise UnknownLayoutError(msg)
        names = {name: label for name, label in labels.items() if label is not None}
        if len(set(names.values())) != len(names):
            twice = sorted({label for label in names.values() if list(names.values()).count(label) > 1})
            msg = f"the header {header!r} names {twice} twice"
            raise UnknownLayoutError(msg)
        every = read.required | set(self.required)
        if missing := [c.label for c in columns if c.field in every and c.label not in names.values()]:
            msg = f"the header {header!r} lacks {missing}, which every one names"
            raise UnknownLayoutError(msg)
        return _laid_out(table, names, [c.label for c in columns])


def _words(columns: Sequence[Column]) -> dict[str, str]:
    """Return the label each word a header may write names: each field's label and its alternatives.

    Raises ``LayoutError`` for a word two fields share.
    """
    words = [(word, column.label) for column in columns for word in (column.label, *column.aliases)]
    if len(labeled := dict(words)) != len(words):
        msg = f"two fields share a label: {sorted(word for word, _ in words)}"
        raise LayoutError(msg)
    return labeled
