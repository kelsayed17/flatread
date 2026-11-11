"""flatread: declare a provider's table once, as a class, and read its body on one native polars path, or fail loudly.

A layout is a class: a provider's defaults in one class, each table in a class under it, each setting only where it
differs. ``Layout.frame(body)`` and ``Layout.rows(body)`` read a body into a polars DataFrame or a list of the row
struct, both of the struct's snake_case fields. Every cell is its value, one of its missing tokens, or an error
naming its line, its label and its text.

* :class:`Csv`, :class:`Pipe`, :class:`Tsv`: delimited text; :class:`Line` a preamble's or trailer's line.
* :class:`Sheet` and :class:`Sheets`: Excel sheets (the ``excel`` extra).
* :class:`Json`: records in a JSON body; :class:`FixedWidth`: fixed-width text.
* :class:`Long`, :class:`Erratum`; the date formats :data:`QUARTER`, :data:`YEAR_MONTH`, :data:`EXCEL_SERIAL`,
  :data:`EPOCH_MS`, :data:`EPOCH_S`.
* :func:`report`, :func:`diff`; :func:`unzip`. The errors are in :mod:`flatread.errors`.
"""

from typing import TYPE_CHECKING

from flatread.archives import unzip
from flatread.cells import EPOCH_MS, EPOCH_S, EXCEL_SERIAL, QUARTER, YEAR_MONTH
from flatread.check import diff, report
from flatread.core import Erratum, Long
from flatread.delimited import Csv, Line, Pipe, Tsv
from flatread.fixed import FixedWidth
from flatread.layout import Layout
from flatread.records import Json

if TYPE_CHECKING:
    from flatread.sheets import Sheet, Sheets

__all__ = [
    "EPOCH_MS",
    "EPOCH_S",
    "EXCEL_SERIAL",
    "QUARTER",
    "YEAR_MONTH",
    "Csv",
    "Erratum",
    "FixedWidth",
    "Json",
    "Layout",
    "Line",
    "Long",
    "Pipe",
    "Sheet",
    "Sheets",
    "Tsv",
    "diff",
    "report",
    "unzip",
]


def __getattr__(name: str) -> object:
    """Import the sheet bases on first use: they need the ``excel`` extra, which a core install has not."""
    if name in {"Sheet", "Sheets"}:
        from flatread import sheets  # noqa: PLC0415  # the excel extra, loaded only when asked for

        return getattr(sheets, name)
    msg = f"module 'flatread' has no attribute {name!r}"
    raise AttributeError(msg)
