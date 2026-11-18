"""HTML pages: a page's one table (HtmlFile's), or records that are elements a CSS selector finds (HtmlRecords').

Both formats know their fields, as an HTML cell is read its field's way: as its text, as its link's ``href`` for a
:class:`~flatread.fields.Link` field, and as one text run for a field that is no text (a number, a date or time, a
boolean), so that markup splitting a cell (``5.3<sup>1</sup>``, ``1<br>25``) raises rather than reading ``5.31``.
Several fields may read one cell, each its own way (a link's text and its ``href``). A format reads a page its own way
by subclassing: :meth:`Html.tables`, :meth:`Html.rows` and either format's ``value``. The page is parsed as
:mod:`flatread.pages` parses it, with its helpers (``parse_html``, ``table_header``, ``table_cells``, ``cell_text``).
"""

import dataclasses
import functools
import re
import types
import typing
from collections import Counter
from collections.abc import Mapping, Sequence

import msgspec
import polars as pl

from flatread.core import Column
from flatread.errors import EmptyBodyError, LayoutError, ReadError, TruncatedFileError, UnknownLayoutError
from flatread.fields import Link
from flatread.next._headers import _label
from flatread.next._model import Cells, ReadContext, Stream
from flatread.next._stages import _check_header, _unique, records_of

if typing.TYPE_CHECKING:
    from scrapling.parser import Selector as Element

__all__ = ["Html", "HtmlRecords", "HtmlRows"]


class HtmlRows(typing.NamedTuple):
    """A table as :class:`Html` reads it: its header's cells, its rows' cells under them, each row's number.

    *stated* is the text of fields the table writes once for every row (a title row's report date), by field.
    """

    header: list[str]
    rows: list[list[Element]]
    numbers: list[int]
    stated: Mapping[str, str | None] = types.MappingProxyType({})


@functools.cache
def _links(row: type | None) -> frozenset[str]:
    """Return the fields of *row* that read their cell's link: each ``Link`` field."""
    if row is None or not issubclass(row, msgspec.Struct):  # a stage read outside a reader: no field is a link
        return frozenset()
    return frozenset(
        field.name
        for field in msgspec.structs.fields(row)
        if typing.get_origin(field.type) is typing.Annotated
        and any(isinstance(meta, Link) for meta in typing.get_args(field.type)[1:])
    )


def _value(cell: Element, column: Column, read: ReadContext) -> str:
    """Return the text *column* reads from *cell*: its link's ``href``, its text, or its one text run."""
    from flatread.pages import _runs, cell_text, href  # noqa: PLC0415 (the html extra loads only when a page is read)

    if column.field in _links(read.row):
        return href(cell)
    if column.scalar is str:
        return cell_text(cell)
    if len(runs := _runs(cell)) > 1:
        msg = f"holds the text runs {runs!r}, not one"
        raise ValueError(msg)
    return "".join(runs)


def _read_cell(
    value: typing.Callable[[Element, Column, ReadContext], str],
    cell: Element,
    column: Column,
    read: ReadContext,
    where: str,
) -> str:
    """Return *value*'s reading of *cell*, a ``ValueError`` raised as a ``ReadError`` naming the cell and *where*."""
    try:
        return value(cell, column, read)
    except LayoutError:  # a reader declared wrong stays one: no ValueError below relabels it
        raise
    except ValueError as e:
        label = _label(column.label)
        msg = f"{where}: its {f'{label!r} ' if label else ''}cell {e}"
        raise ReadError(msg) from e


@dataclasses.dataclass(frozen=True, kw_only=True)
class Html:
    """A page's one table, the one *table* (a CSS selector) finds: its first row the header, a row a record.

    The grid is the table's header over its rows, each cell read by the field its header cell names (by its label or
    one of its alternatives): a label the header writes as often as fields read it is theirs by place, and one it
    writes once may be read by several fields, each its own way, as a column of its own. A header cell no field reads
    is read as its text, or, with *unread_cells* ``"value"``, as one text run (a long table's value cells, typed by a
    shape). A ``<tr>`` with no cells is no row, nor is a ``<tfoot>``'s (totals); each row is numbered among the rows
    read, from 1 under the header. A page with another count of tables raises, unless it has none and the reader takes
    a table of no rows, and so does a page cut short: one in which a ``<table>`` has no ``</table>``.

    A format reads a page its own way by subclassing: :meth:`tables` picks the page's tables, :meth:`rows` lays out a
    table of another shape (a title row above its header, a spanning cell carried down), and :meth:`value` reads a
    field's cell (a suffix dropped); a ``ValueError`` it raises is a ``ReadError`` naming the cell and its row.
    """

    table: str = "table"
    unread_cells: typing.Literal["text", "value"] = "text"

    def __post_init__(self) -> None:
        if self.unread_cells not in ("text", "value"):
            msg = f"an Html reads a cell no field reads as text or as a value, not {self.unread_cells!r}"
            raise LayoutError(msg)

    def tables(self, doc: Element) -> list[Element]:
        """Return the page's tables for this file: every one *table* finds."""
        return list(doc.css(self.table))

    def rows(self, table: Element) -> HtmlRows:
        """Return *table* as its header's cells and its rows' cells: its first row the header, the rest its rows."""
        from flatread.pages import table_cells, table_header  # noqa: PLC0415 (the html extra)

        if (header := table_header(table)) is None:
            msg = "the table has no row"
            raise ReadError(msg)
        return HtmlRows(header, *table_cells(table, header))

    def value(self, cell: Element, column: Column, read: ReadContext) -> str:
        """Return the text *column* reads from *cell*: its link's ``href`` (``Link``), its text, or its one run."""
        return _value(cell, column, read)

    def grid(self, stream: Stream, read: ReadContext) -> Cells:
        """Return the table's cells, each read by its field, under its header's cells."""
        from flatread.pages import _unclosed, parse_html  # noqa: PLC0415 (the html extra)

        body = stream.read()
        if (doc := parse_html(body)) is None:
            msg = "the body is empty"
            raise EmptyBodyError(msg)
        if _unclosed(body):
            msg = "the page was cut: a <table> has no </table>"
            raise TruncatedFileError(msg)
        tables = self.tables(doc)
        if not tables and read.empty:
            labels = [_label(c.label) for c in read.columns.values() if c.field not in read.stated]
            return records_of(pl.DataFrame(schema=dict.fromkeys(_unique(labels), pl.String)))
        if len(tables) != 1:
            msg = f"the page holds {len(tables)} of the file's tables, not one"
            raise UnknownLayoutError(msg)
        header, found, numbers, stated = self.rows(tables[0])
        _check_header(header)
        read.stated.update(stated)
        labels, cells = self._read(header, found, numbers, read)
        names = _unique(labels)
        frame = pl.DataFrame(dict(zip(names, cells, strict=True)), schema=dict.fromkeys(names, pl.String))
        return dataclasses.replace(records_of(frame), numbers=pl.Series("number", numbers, pl.Int64))

    def _read(
        self, header: Sequence[str], found: Sequence[Sequence[Element]], numbers: Sequence[int], read: ReadContext
    ) -> tuple[list[str], list[list[str]]]:
        """Return each column's header cell and its cells' text, each read by the field its header cell names."""
        from flatread.pages import _one_run, cell_text  # noqa: PLC0415 (the html extra)

        fields: dict[str, list[Column]] = {}
        for column in read.columns.values():
            if column.field not in read.stated:
                for word in dict.fromkeys((_label(column.label), *column.aliases)):
                    fields.setdefault(word, []).append(column)
        written = Counter(header)
        if twice := sorted(
            label for label, count in written.items() if count > 1 and count != len(fields.get(label, ()))
        ):
            msg = f"the table's header {list(header)!r} names {twice!r} more than once"
            raise UnknownLayoutError(msg)
        before: Counter[str] = Counter()
        labels: list[str] = []
        cells: list[list[str]] = []
        for place, label in enumerate(header):
            reading = fields.get(label, [])
            columns = reading if written[label] == 1 else [reading[before[label]]]
            before[label] += 1
            if not columns:
                labels.append(label)
                if self.unread_cells == "value":
                    cells.append(
                        [_one_run(row[place], label, number) for row, number in zip(found, numbers, strict=True)]
                    )
                else:
                    cells.append([cell_text(row[place]) for row in found])
            for column in columns:
                labels.append(label)
                cells.append(
                    [
                        _read_cell(self.value, row[place], column, read, f"row {number}")
                        for row, number in zip(found, numbers, strict=True)
                    ]
                )
        return labels, cells


@dataclasses.dataclass(frozen=True, kw_only=True)
class HtmlRecords:
    """Records that are elements, not table rows: each one *record* (a CSS selector) finds, a field per ``Label``.

    A field's ``Label`` is a CSS selector inside the record (``".fomc-meeting__month"``, ``'a[href$=".pdf" i]'``),
    ``""`` the record itself, and it must find exactly one element (or none, for a field that admits ``None``: no
    value), read as its text (one text run for a field that is no text) or, for a ``Link`` field, its link's ``href``.
    Records whose fields share a selector (two fields of ``""``) each read the element their own way.

    Records that sit under headings (a year's panel) name them: each element *heading* finds before a record heads it
    when its text fullmatches *heading_pattern* (another is no heading: a ``p`` of prose), each named group the text of
    the field of its name on every record it heads; such a field's ``Label`` is not read. A record under no such
    heading, or a heading that heads no record, raises. A format finds its records its own way by subclassing:
    :meth:`records` checks the page, :meth:`is_record` leaves some out, and :meth:`value` reads a field's element.
    """

    record: str
    heading: str | None = None
    heading_pattern: re.Pattern[str] = re.compile("")

    def records(self, doc: Element) -> list[Element]:
        """Return every element *record* finds, in page order: a format checks the page here."""
        return list(doc.css(self.record))

    def is_record(self, element: Element) -> bool:
        """Return whether *element*, which *record* finds, is a record: yes, unless a format says no."""
        del element
        return True

    def value(self, cell: Element, column: Column, read: ReadContext) -> str:
        """Return the text *column* reads from *cell*: its link's ``href`` (``Link``), its text, or its one run."""
        return _value(cell, column, read)

    @property
    def _headed_fields(self) -> frozenset[str]:
        return frozenset(self.heading_pattern.groupindex) if self.heading is not None else frozenset()

    def grid(self, stream: Stream, read: ReadContext) -> Cells:
        """Return each record's fields' text, under the struct's labels, every row a record."""
        from flatread.pages import parse_html  # noqa: PLC0415 (the html extra)

        if (doc := parse_html(stream.read())) is None:
            msg = "the body is empty"
            raise EmptyBodyError(msg)
        found = self.records(doc)
        headed = zip(found, self._headed(doc, found), strict=True) if found else ()
        records = [(record, stated) for record, stated in headed if self.is_record(record)]
        if not records and not read.empty:
            msg = "the page holds none of the file's records"
            raise EmptyBodyError(msg)
        columns = [column for column in read.columns.values() if column.field not in read.stated]
        heads = self._headed_fields
        rows: list[list[str | None]] = []
        for number, (record, stated) in enumerate(records, 1):
            if stated is None:
                msg = f"record {number} sits under no heading matching {self.heading_pattern.pattern!r}"
                raise ReadError(msg)
            values: list[str | None] = []
            for column in columns:
                if column.field in heads:
                    values.append(stated[column.field])
                    continue
                under = _label(column.label)
                elements = [record] if not under else list(record.css(under))
                if not elements and column.field not in read.required:
                    values.append(None)
                    continue
                if len(elements) != 1:
                    msg = f"record {number} holds {len(elements)} elements {under!r} finds, not one"
                    raise ReadError(msg)
                values.append(_read_cell(self.value, elements[0], column, read, f"record {number}"))
            rows.append(values)
        return records_of(pl.DataFrame(rows, schema={column.label: pl.String for column in columns}, orient="row"))

    def _headed(self, doc: Element, found: list[Element]) -> list[dict[str, str | None] | None]:
        """Return what each of the *found* records is stated by its heading (``None``: it sits under no heading).

        A record's heading is the last stating heading that ends before it starts, found in one walk of the page in
        document order, as :class:`flatread.pages.HtmlRecords` finds it.
        """
        from lxml.etree import iterwalk  # noqa: PLC0415 (the html extra)

        from flatread.pages import _check_heads, cell_text  # noqa: PLC0415

        if self.heading is None:
            return [{} for _ in found]
        at = {record._root: n for n, record in enumerate(found)}  # noqa: SLF001 (the element's identity)
        stating = {
            heading._root: (heading, match)  # noqa: SLF001
            for heading in doc.css(self.heading)
            if (match := self.heading_pattern.fullmatch(cell_text(heading)))
        }
        headed: list[dict[str, str | None] | None] = [None] * len(found)
        heading: tuple[Element, re.Match[str]] | None = None
        heads = 0
        for event, element in iterwalk(doc._root, events=("start", "end")):  # noqa: SLF001
            if event == "start" and element in at and heading is not None:
                headed[at[element]] = dict(heading[1].groupdict(default=None))
                heads += 1
            elif event == "end" and element in stating:
                _check_heads(heading, heads)
                heading, heads = stating[element], 0
        _check_heads(heading, heads)
        return headed
