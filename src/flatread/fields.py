"""A row struct field's markers for its flat file — stdlib only, so a ``models.py`` names them without importing polars.

A field of a struct that :class:`flatread.delimited.CsvFile` reads says, in ``Annotated``, where its value
comes from and how its text is read: :class:`Label`, the header label of its column (``Annotated[float,
Label("WK_RPX_CNT")]``), and, for a date or a time, :class:`Format`, the ``strptime`` format the file writes it in
(``Annotated[dt.date, Label("WK_ENDING"), Format("%m-%d-%Y")]``), and for a boolean :class:`Flag`, its words; a date
written across two columns (a month's name and its year) names the second with :class:`Joined`. A file whose labels a
format derives (ECB's, the field name upper-cased) needs no ``Label``. A fixed-width file's field gives
its column's position, :class:`Span` (``Annotated[int, Label("FIRM"), Span(22, 31)]``), and an HTML page's field read
from a link, :class:`Link` (``Annotated[str, Label("File"), Link()]``). msgspec ignores the metadata: the struct's JSON
keys stay its field names.
"""

from typing import NamedTuple

__all__ = ["Cell", "Flag", "Format", "Joined", "Label", "Link", "Shape", "Span"]


class Label(NamedTuple):
    """A field's column: the label its file writes in the header (``""``: a sheet's header cell that is empty).

    A nested XML document's field gives the XPath from its record to its node instead (``@id``, ``../statedistrict``).

    *alternatives* are other words some of a file's headers write the same column in (SEC's ``file_no`` for
    ``file_number``); only a header read by name (:class:`flatread.delimited.NamedCsv`) takes them.
    """

    text: str
    alternatives: tuple[str, ...] = ()


class Format(NamedTuple):
    """How a date or time field's cells are written: their ``strptime`` format and, for a time, its zone.

    *time_zone* is the zone a file writes every time of the column in, with no zone in the cells
    (``Format("%m/%d/%Y %-I:%M:%S %p", time_zone="UTC")``): the time is read as aware, in that zone. *alternatives*
    are other formats the same column is written in, cell by cell (``Format("%m/%d/%Y", alternatives=("%-m/%-d/%Y",))``
    for a file that pads its dates but for a few): each cell must be exactly one of them.

    A date written as a quarter uses ``%q``, the quarter's number 1 to 4, beside ``%Y`` and literal text alone
    (``Format("%YQ%q")`` for ``2026Q1``, ``Format("%Y: Q%q")`` for ``2026: Q2``): the date is the quarter's first day.
    A period's year may be written in two digits, ``%y`` (``Format("%b-%y")`` for ``Jan-21``), as ``strptime`` reads
    it (``69`` to ``99`` the 1900s, the rest the 2000s). *end* reads a period as its last day instead
    (``Format("%Y:Q%q", end=True)``: ``2026:Q2`` is 2026-06-30), for a file that dates a period by its end.
    """

    strptime: str
    time_zone: str | None = None
    alternatives: tuple[str, ...] = ()
    end: bool = False


class Joined(NamedTuple):
    """A date written across two columns: its own ``Label``'s cell, then *label*'s, read as one text by its Format.

    The two cells are joined by *separator* (``Annotated[dt.date, Label("Index Month"), Joined("Index Year"),
    Format("%B %Y")]`` reads ``January`` and ``2022`` as ``January 2022``); the second column is the field's too, so it
    has no field of its own. Both cells hold a value, or neither does.
    """

    label: str
    separator: str = " "


class Flag(NamedTuple):
    """How a boolean field's cells are written: its words for true and for false (``Flag("Y", "N")``).

    Without one, a boolean column is ``true`` and ``false``.
    """

    true: str
    false: str


class Span(NamedTuple):
    """Where a fixed-width file's column sits on each line: characters *start* up to *end*, as Python slices count."""

    start: int
    end: int


class Link(NamedTuple):
    """An HTML field whose value is its cell's link: the ``href`` of the cell's one ``<a>``, or the cell's own.

    A cell with no link, or a link with no ``href``, holds no value; one with several raises.

    Only an HTML reader (:class:`flatread.pages.HtmlFile`,
    :class:`flatread.pages.HtmlRecords`) reads it.
    """


class Shape(NamedTuple):
    """A text field's shape: the regex each of its cells fullmatches, every reader checking it.

    ``Shape(r"[0-9]{4}(?::Q[1-4])?")`` is a period a file writes as text, ``2026:Q2`` or ``1946``. A cell of another
    shape raises, naming its row.
    """

    pattern: str


class Cell(NamedTuple):
    """A sheet's field that one cell states for every row, as Excel names the cell (``"I1"``).

    A date over the table, the code atop a column: the field takes no column, and only a sheet reader
    (:class:`flatread.sheets.SheetFile`) reads it, typed as a cell of its column would be.
    """

    address: str
