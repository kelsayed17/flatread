"""Two utilities over a layout: a report of every fault in a body, and a diff of two recordings of one table.

:func:`report` is for a re-recording or a new provider: where a read raises at its first fault, the report counts
every bad cell per column, lists the errata no longer in the body, and names the structural fault (the header, a
ragged row) that stops a read before its cells. It runs the same front-end and core steps as a read, and reads the
typing step's own fault masks: there is no second mode in the core.

:func:`diff` reads two bodies with one layout and compares them by its ``key``: the rows added, removed and changed.
"""

import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING, override

import polars as pl

from flatread import core
from flatread.cells import type_frame
from flatread.errors import ReadError

if TYPE_CHECKING:
    from flatread.core import Erratum, Text
    from flatread.layout import Layout

__all__ = ["CellFaults", "Diff", "Report", "diff", "report"]


@dataclass(frozen=True, slots=True)
class CellFaults:
    """One column's bad cells: how many, and the first three, each where it is and its text."""

    count: int
    first: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Report:
    """Every fault :func:`report` found in a body."""

    ok: bool
    #: The fault that stops a read before its cells (a header, a ragged row, a body that is no file), or ``None``.
    structure: str | None
    #: Each field with a bad cell, by name.
    cells: dict[str, CellFaults]
    #: The errata whose mistake is no longer in the body.
    errata_gone: tuple[Erratum, ...]
    #: The rows the body holds (0 when a structural fault stops the read).
    rows: int = 0
    #: Each field's label, for the text form.
    labels: dict[str, str] = dataclasses.field(default_factory=dict)

    @override
    def __str__(self) -> str:
        """Return the report as lines a person reads."""
        if self.ok:
            return f"ok: {self.rows} rows"
        lines = [] if self.structure is None else [f"structure: {self.structure}"]
        lines += [f"erratum gone: {erratum.why} ({erratum.label!r} {erratum.wrote!r})" for erratum in self.errata_gone]
        for name, faults in self.cells.items():
            shown = ", ".join(f"{where} {text!r}" for where, text in faults.first)
            lines.append(f"{self.labels.get(name, name)}: {faults.count} bad cells, first {shown}")
        return "\n".join(lines)


def report(layout: type[Layout], body: bytes) -> Report:
    """Return every fault in *body* read by *layout*, rather than raising at the first."""
    plan = layout._checked_plan()  # noqa: SLF001  # a utility over a layout's own plan
    try:
        layout._refuse(body)  # noqa: SLF001
        text = layout.read_text(layout._unwrap(body))  # noqa: SLF001
    except ReadError as exc:
        return Report(ok=False, structure=str(exc), cells={}, errata_gone=())
    gone = tuple(e for e in plan.errata if not core.erratum_found(text.frame, e))
    present = dataclasses.replace(plan, errata=tuple(e for e in plan.errata if e not in gone))
    try:
        prepared = core.prepare(present, text, cells=layout._hooks().get("cells"))  # noqa: SLF001
    except ReadError as exc:
        return Report(ok=False, structure=str(exc), cells={}, errata_gone=gone)
    _, bad = type_frame(prepared.select([f.name for f in plan.fields]), plan.fields)
    cells = {}
    for field in plan.fields:
        failing = bad[field.name].arg_true()
        if failing.len():
            first = tuple(
                (text.locate(core.place(prepared, int(i))), str(prepared[field.name][int(i)] or ""))
                for i in failing.head(3)
            )
            cells[field.name] = CellFaults(failing.len(), first)
    ok = not cells and not gone
    return Report(ok, None, cells, gone, rows=prepared.height, labels=core.field_labels(plan))


@dataclass(frozen=True, slots=True)
class Diff:
    """Two recordings of one table compared by its key."""

    #: The new body's rows whose key the old has not.
    added: pl.DataFrame
    #: The old body's rows whose key the new has not.
    removed: pl.DataFrame
    #: One row per changed value: the key's fields, then ``field``, ``old`` and ``new`` as text.
    changed: pl.DataFrame
    #: The old and the new header, where they differ.
    header: tuple[tuple[str, ...], tuple[str, ...]] | None


def _header(layout: type[Layout], body: bytes) -> tuple[str, ...]:
    text: Text = layout.read_text(layout._unwrap(body))  # noqa: SLF001  # a utility over a layout's own read
    return tuple(text.frame.columns)


def diff(layout: type[Layout], old: bytes, new: bytes) -> Diff:
    """Return what changed from *old* to *new*, both read by *layout*, compared by its ``key``."""
    key = list(layout._checked_plan().key)  # noqa: SLF001  # a utility over a layout's own plan
    if not key:
        msg = f"{layout.__name__} has no key to compare rows by"
        raise TypeError(msg)
    before, after = layout.frame(old), layout.frame(new)
    heads = _header(layout, old), _header(layout, new)
    values = [c for c in before.columns if c not in key]
    joined = before.join(after, on=key, how="inner", suffix="\x00new")
    changed = [
        joined.filter(pl.col(c).ne_missing(pl.col(c + "\x00new"))).select(
            *key,
            pl.lit(c).alias("field"),
            pl.col(c).cast(pl.String).alias("old"),
            pl.col(c + "\x00new").cast(pl.String).alias("new"),
        )
        for c in values
    ]
    schema = {**before.select(key).schema, "field": pl.String, "old": pl.String, "new": pl.String}
    return Diff(
        added=after.join(before, on=key, how="anti"),
        removed=before.join(after, on=key, how="anti"),
        changed=pl.concat(changed) if changed else pl.DataFrame(schema=schema),
        header=None if heads[0] == heads[1] else heads,
    )
