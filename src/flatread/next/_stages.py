"""The built-in stages: formats, table finders, shapes and cleaners, each one frozen value with one job."""

import csv
import dataclasses
import functools
import re
import typing
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence

import msgspec
import polars as pl

from flatread.core import _NUL, Column, _check_ending
from flatread.delimited import _BOM
from flatread.errors import (
    ControlTotalError,
    EmptyBodyError,
    EncodingError,
    LayoutError,
    RaggedRowError,
    ReadError,
    TruncatedFileError,
    UnknownLayoutError,
)
from flatread.next._model import Grid, ReadContext, Records, Table

if typing.TYPE_CHECKING:
    from flatread.next._reader import Reader
    from flatread.pdf import PdfLine
    from flatread.sdmx import SdmxData

__all__ = [
    "Csv",
    "Derived",
    "Header",
    "Layout",
    "Long",
    "Observations",
    "Padded",
    "Pdf",
    "Sdmx",
    "Sheet",
    "Titled",
    "Trailer",
]


def grid_of(rows: Sequence[Sequence[str | None]], raw: Sequence[str], numbers: Sequence[int]) -> Grid:
    """Return the grid of *rows* of cells, each row's *raw* text and its *numbers* in the file."""
    width = max((len(row) for row in rows), default=0)
    cells = pl.DataFrame(
        [[*row, *[None] * (width - len(row))] for row in rows],
        schema={str(at): pl.String for at in range(width)},
        orient="row",
    )
    return Grid(
        cells=cells,
        widths=pl.Series("width", [len(row) for row in rows], pl.Int64),
        raw=pl.Series("raw", list(raw), pl.String),
        numbers=pl.Series("number", list(numbers), pl.Int64),
    )


# --- Formats ---


@dataclasses.dataclass(frozen=True, kw_only=True)
class Csv:
    """A delimited file: rows of cells split at *separator*, a cell quoted with *quote* (``None``: nothing is quoted).

    *final_line_ending* is whether the file always ends its last line with a line ending, so a body that ends otherwise
    was cut; ``None`` leaves it unchecked. A blank line is a row of one empty cell. Raises ``LayoutError`` when built
    for a separator or
    quote that is not one character, ends a line, or is the other.
    """

    separator: str = ","
    quote: str | None = '"'
    final_line_ending: bool | None = None

    def __post_init__(self) -> None:
        for what, character in (("separator", self.separator), ("quote", self.quote)):
            if character is not None and (len(character) != 1 or character in "\r\n"):
                msg = f"the Csv {what} is {character!r}, not one character that ends no line"
                raise LayoutError(msg)
        if self.separator == self.quote:
            msg = f"the Csv separator {self.separator!r} is its quote too, which tells no cell's end"
            raise LayoutError(msg)

    def grid(self, body: bytes, read: ReadContext) -> Grid:
        """Return *body*'s rows of cells: each line's (a quoted cell may span lines), its raw text and line number."""
        del read
        if not body or body.isspace():
            msg = "the body is empty"
            raise EmptyBodyError(msg)
        if self.final_line_ending is not None:
            _check_ending(body, final_line_ending=self.final_line_ending)
        try:
            text = body.removeprefix(_BOM).decode("utf-8")
        except UnicodeDecodeError as e:
            msg = f"the body is not UTF-8: {e}"
            raise EncodingError(msg) from e
        if text.startswith(("\n", "\r\n")):
            msg = "the body opens with a blank line, not its header"
            raise UnknownLayoutError(msg)
        lines = text.splitlines(keepends=True)
        taken: list[int] = []

        def feed() -> Iterator[str]:
            for line in lines:
                taken.append(1)
                yield line

        quoting = csv.QUOTE_NONE if self.quote is None else csv.QUOTE_MINIMAL
        reader = csv.reader(feed(), delimiter=self.separator, quotechar=self.quote or '"', quoting=quoting, strict=True)
        rows: list[list[str | None]] = []
        raw: list[str] = []
        numbers: list[int] = []
        start = 0
        try:
            for cells in reader:
                end = len(taken)
                # A blank line is a row of one empty cell, which a table as wide as its header refuses.
                rows.append([cell or None for cell in cells] or [None])
                raw.append("".join(lines[start:end]).rstrip("\r\n"))
                numbers.append(start + 1)
                start = end
        except csv.Error as e:
            # A quoted cell open at the body's end was cut; any other quote out of place is a malformed row.
            msg = f"row {len(rows) + 1}: {e}"
            raise (TruncatedFileError if "unexpected end of data" in str(e) else ReadError)(msg) from e
        return grid_of(rows, raw, numbers)


@dataclasses.dataclass(frozen=True, kw_only=True)
class Sheet:
    """A workbook's sheet (``None``: its one sheet), its every cell as text: a number with every digit it holds.

    Its grid is rectangular: each row of the sheet from row 1, numbered as Excel numbers it, each row's raw text its
    cells joined by tabs up to its last filled one (what a :class:`Titled` title matches), and its width the column of
    that last filled cell. An empty cell, or one of only spaces, is no value.
    """

    sheet: str | None = None

    def grid(self, body: bytes, read: ReadContext) -> Grid:
        """Return the sheet's rows of text cells."""
        del read
        from flatread.sheets import sheet_grid  # noqa: PLC0415 (the sheets extra loads only when a sheet is read)

        cells = sheet_grid(body, self.sheet)
        filled = (
            cells.select(
                pl.max_horizontal(
                    pl.when(pl.col(name).is_not_null()).then(pl.lit(at + 1)).otherwise(0)
                    for at, name in enumerate(cells.columns)
                ).alias("width")
            )
            if cells.width
            else pl.DataFrame({"width": pl.Series([], dtype=pl.Int64)})
        )
        raw = (
            cells.select(pl.concat_str(pl.all().fill_null(""), separator="\t").str.strip_chars_end("\t"))
            if cells.width
            else None
        )
        return Grid(
            cells=cells,
            widths=filled.to_series().cast(pl.Int64).alias("width"),
            raw=(pl.repeat("", cells.height, dtype=pl.String, eager=True) if raw is None else raw.to_series()).alias(
                "raw"
            ),
            numbers=pl.int_range(1, cells.height + 1, dtype=pl.Int64, eager=True).alias("number"),
            rectangular=True,
        )


@dataclasses.dataclass(frozen=True, kw_only=True)
class Layout:
    """How a PDF lays its table out, in points, and the three steps that read one by its columns' places.

    Each page's lines are sorted top to bottom, then left to right, and segments on one baseline (tops less than
    *baseline* apart) are one line. :meth:`edges` finds each column's left edge on its header line, :meth:`runs` splits
    lines into rows (no more than *row_gap* apart), and :meth:`cells` gives each column its characters, a character
    belonging to a column though it sits up to *slack* left of its edge.
    """

    row_gap: float
    slack: float = 0.0
    baseline: float = 1.0

    def lines(self, lines: Sequence[PdfLine]) -> list[PdfLine]:
        """Return *lines* (:func:`~flatread.pdf.text_layer`'s) as the pages lay them out."""
        from flatread.pdf import _joined  # noqa: PLC0415 (the pdf extra loads only when a PDF is read)

        drawn = sorted(
            (line for line in lines if line.text.strip()), key=lambda line: (line.page, -line.top, line.chars[0][0])
        )
        merged: list[list[PdfLine]] = []
        for line in drawn:
            if merged and merged[-1][0].page == line.page and merged[-1][0].top - line.top < self.baseline:
                merged[-1].append(line)
            else:
                merged.append([line])
        return [_joined(segments) for segments in merged]

    def edges(self, line: PdfLine, labels: Sequence[str]) -> list[float] | None:
        """Return each column's left edge, where its label's first word starts on *line* (``None``: one is not)."""
        edges: list[float] = []
        at = 0
        for label in labels:
            found = re.compile(rf"(?<!\S){re.escape(label.split()[0])}(?!\S)", re.IGNORECASE).search(line.text, at)
            if found is None:
                return None
            edges.append(line.chars[found.start()][0])
            at = found.end()
        return edges

    def cells(self, lines: Sequence[PdfLine], edges: Sequence[float]) -> list[str]:
        """Return each column's cell: every character at or right of its edge, less *slack*, its lines joined."""
        cells: list[list[str]] = [[] for _ in edges]
        for line in lines:
            parts: list[list[str]] = [[] for _ in edges]
            for x, c in line.chars:
                parts[max((i for i, edge in enumerate(edges) if edge <= x + self.slack), default=0)].append(c)
            for cell, part in zip(cells, parts, strict=True):
                if "".join(part).strip():
                    cell.append("".join(part))
        return [" ".join(" ".join(cell).split()) for cell in cells]

    def runs(self, lines: Sequence[PdfLine]) -> list[list[PdfLine]]:
        """Split *lines* into rows: runs no more than *row_gap* points apart, a page break ending one."""
        runs: list[list[PdfLine]] = []
        for before, line in zip([None, *lines], lines, strict=False):
            if before is None or line.page != before.page or before.top - line.top > self.row_gap:
                runs.append([])
            runs[-1].append(line)
        return runs


@dataclasses.dataclass(frozen=True)
class Pdf:
    """A PDF's text layer, its records read out of its lines by *records*, laid out by *layout* if one is given.

    No header names a PDF's columns: each record holds its cells by field name, and the grid's header is the row
    struct's labels. A record naming a field the struct lacks is a fault of the reader, raised as ``LayoutError``.
    """

    records: Records
    layout: Layout | None = None

    def grid(self, body: bytes, read: ReadContext) -> Grid:
        """Return the records as a grid under the struct's labels."""
        from flatread.pdf import text_layer  # noqa: PLC0415 (the pdf extra loads only when a PDF is read)

        lines = text_layer(body)
        if self.layout is not None:
            lines = self.layout.lines(lines)
        records = self.records.records(lines, self.layout, read)
        fields = list(read.columns)
        for n, record in enumerate(records, 1):
            if unknown := record.keys() - read.columns.keys():
                msg = f"record {n} names fields the row has none of: {sorted(unknown)!r}"
                raise LayoutError(msg)
        cells = pl.DataFrame(
            [[record.get(field) for field in fields] for record in records],
            schema={read.columns[field].label: pl.String for field in fields},
            orient="row",
        )
        return Grid(
            cells=cells,
            widths=pl.repeat(cells.width, cells.height, dtype=pl.Int64, eager=True),
            raw=pl.repeat("", cells.height, dtype=pl.String, eager=True),
            numbers=pl.int_range(1, cells.height + 1, dtype=pl.Int64, eager=True).alias("number"),
            header=cells.columns,
        )


@dataclasses.dataclass(frozen=True, kw_only=True, eq=False)  # by identity: its parse is cached per value
class Sdmx:
    """An SDMX-ML 1.0 compact data message's data set *data_set*, streamed: each observation a row of its series.

    The row's first field is the attribute naming each series (its key); the others are each observation's attributes.
    A field's attribute is its label as *names* writes it (upper-cased by default: ``series_name`` is ``SERIES_NAME``).
    *root*, *data_set_element*, *observation* and *annotations* are the message's prefixed element names, *prefixes*
    the format's own beside SDMX-ML 1.0's. With *member*, the body is a zip and the message its one member whose whole
    name matches it, read as it inflates. Every part of the message is checked, as 7.x's ``SdmxData`` checks it.
    """

    data_set: str
    root: str
    data_set_element: str
    observation: str
    annotations: str
    prefixes: Mapping[str, str] = dataclasses.field(default_factory=dict)
    member: re.Pattern[str] | None = None
    names: Callable[[str], str] = str.upper

    def grid(self, body: bytes, read: ReadContext) -> Grid:
        """Return the data set's observations: each its series' key and its attributes, under the row's labels."""
        from lxml import etree  # noqa: PLC0415 (the xml extra loads only when SDMX is read)

        assert read.row is not None  # noqa: S101 (a Reader always names it)
        parser = _sdmx_parser(self, read.row)
        columns = list(read.columns.values())
        labels = [column.label for column in columns]
        key, *attributes = [self.names(label) for label in labels]
        paths = [
            etree.XPath(f"{self.observation}/@{name}", namespaces=parser._namespaces, smart_strings=False)  # noqa: SLF001
            for name in attributes
        ]
        count = etree.XPath(f"count({self.observation}/@*)", namespaces=parser._namespaces)  # noqa: SLF001
        held: list[list[object]] = [[] for _ in labels]
        for name, series in parser._each(body):  # noqa: SLF001 (shortcut: 7.x's streaming, checked parse)
            read.member = name
            if (series_key := series.get(key)) is None:
                msg = f"a series of the {self.data_set} data set of {name} carries no {key}"
                raise UnknownLayoutError(msg)
            found = [cells for path in paths if isinstance(cells := path(series), list)]
            observations = len(series) - (series.find(parser._notes) is not None)  # noqa: SLF001
            if count(series) != len(attributes) * observations or [len(cells) for cells in found] != [
                observations
            ] * len(attributes):
                msg = (
                    f"an observation of the series {series_key!r} of the {self.data_set} data set of {name}"
                    f" carries other than {attributes}"
                )
                raise UnknownLayoutError(msg)
            held[0].extend([series_key] * observations)
            for column, cells in zip(held[1:], found, strict=True):
                column.extend(cells)
        cells = pl.DataFrame(dict(zip(labels, held, strict=True)), schema=dict.fromkeys(labels, pl.String))
        return Grid(
            cells=cells,
            widths=pl.repeat(cells.width, cells.height, dtype=pl.Int64, eager=True),
            raw=pl.repeat("", cells.height, dtype=pl.String, eager=True),
            numbers=pl.int_range(1, cells.height + 1, dtype=pl.Int64, eager=True).alias("number"),
            header=cells.columns,
        )


@functools.cache
def _sdmx_parser(sdmx: Sdmx, row: type[msgspec.Struct]) -> SdmxData[msgspec.Struct]:
    """Return 7.x's streaming parse of *sdmx*'s message, for rows of *row* (shortcut: 8.0 proper owns it)."""
    from flatread.sdmx import SdmxData  # noqa: PLC0415

    parser = type(
        "_Parse",
        (SdmxData,),
        {
            "_prefixes": dict(sdmx.prefixes),
            "_root": sdmx.root,
            "_data_set": sdmx.data_set_element,
            "_observation": sdmx.observation,
            "_annotations": sdmx.annotations,
            "_zipped_member": sdmx.member,
            "_read": lambda _self, _body: None,
        },
    )
    return parser(row, sdmx.data_set)  # ty: ignore[unsound-return-statement] (type() builds an SdmxData)


@dataclasses.dataclass(frozen=True)
class Derived[RowT]:
    """A file another reader reads first, its frame then made the row's text cells by *shape*.

    *shape* takes the inner reader's frame and that read's context (its trailer's match) and returns a String frame of
    the row's labels. For a table the file lays out in a way only its whole shows (each index then its estimates).
    """

    inner: Reader[typing.Any]
    shape: Callable[[pl.DataFrame, ReadContext], pl.DataFrame]

    def grid(self, body: bytes, read: ReadContext) -> Grid:
        """Return the shaped frame as a grid whose header is its columns."""
        frame, inner = self.inner.read(body)
        read.matches.update(inner.matches)
        cells = self.shape(frame, inner)
        numbers = pl.int_range(1, cells.height + 1, dtype=pl.Int64, eager=True).alias("number")
        return Grid(
            cells=cells,
            widths=pl.repeat(cells.width, cells.height, dtype=pl.Int64, eager=True),
            raw=pl.repeat("", cells.height, dtype=pl.String, eager=True),
            numbers=numbers,
            header=cells.columns,
        )


# --- Table finders ---


def _header_table(grid: Grid) -> Table:
    """Return *grid*'s table: its first row the header (or the header its format states), every other row a row."""
    if grid.header is not None:
        return Table(grid.cells.rename(dict(zip(grid.cells.columns, grid.header, strict=True))), grid.numbers)
    if not grid.height:
        msg = "the body is empty"
        raise EmptyBodyError(msg)
    width = int(grid.widths[0])
    header = [cell or "" for cell in grid.cells.row(0)[:width]]
    rows = grid.slice(1)
    # A line's row is as wide as its fields; a sheet's rows are all the sheet's width, a row past the header's last
    # column holding nothing there.
    wrong = (rows.widths > width) if grid.rectangular else (rows.widths != width)
    if (wrong := wrong.arg_true()).len():
        at = int(wrong[0])
        msg = f"row {rows.numbers[at]} has {rows.widths[at]} fields, not the header's {width}"
        raise RaggedRowError(msg)
    cells = rows.cells.select(rows.cells.columns[:width])
    if grid.rectangular and (empty := cells.select(pl.all_horizontal(pl.all().is_null())).to_series().arg_true()).len():
        msg = f"row {rows.numbers[int(empty[0])]} is empty, and a table's rows run to its end"
        raise ReadError(msg)
    if any(_NUL in cell for cell in header):
        msg = f"the header {header!r} holds a NUL, which no heading does"
        raise UnknownLayoutError(msg)
    return Table(cells.rename(dict(zip(cells.columns, _unique(header), strict=True))), rows.numbers)


def _unique(header: Sequence[str]) -> list[str]:
    """Return *header* with a repeated label's later columns named by it, a NUL and its count, apart."""
    seen: dict[str, int] = {}
    named = []
    for label in header:
        seen[label] = seen.get(label, -1) + 1
        named.append(f"{label}{_NUL}{seen[label]}" if seen[label] else label)
    return named


@dataclasses.dataclass(frozen=True)
class Header:
    """The table is the grid: its first row the header, every row below it a row, each as wide as the header."""

    stated: frozenset[str] = frozenset()
    vouches: bool = False

    def find(self, grid: Grid, read: ReadContext) -> Table:
        """Return the grid's table."""
        del read
        return _header_table(grid)


@dataclasses.dataclass(frozen=True)
class Trailer:
    """The file's last row is a trailer (a record count, a stamp), its text fullmatching *pattern*, not a row.

    A group ``count`` is the number of rows, checked; a group named for a field is that field's text on every row; an
    unnamed group states nothing (a later stage reads the match: ``read.matches["trailer"]``). The trailer is what a cut
    body lacks, so it vouches for a table of no rows.
    """

    pattern: re.Pattern[str]
    vouches: bool = True

    @property
    def stated(self) -> frozenset[str]:
        """The fields the trailer's groups name."""
        return frozenset(self.pattern.groupindex.keys() - {"count"})

    def find(self, grid: Grid, read: ReadContext) -> Table:
        """Return the grid's table above its trailer, noting the trailer's match and what it states."""
        last = grid.raw[-1] if grid.height else ""
        if grid.height < 2 or (match := self.pattern.fullmatch(last)) is None:  # noqa: PLR2004 (a header and a trailer)
            msg = f"the last line {last!r} is not the file's trailer: the body was cut"
            raise TruncatedFileError(msg)
        read.matches["trailer"] = match
        table = _header_table(grid.slice(0, -1))
        if "count" in self.pattern.groupindex and int(match["count"]) != table.cells.height:
            msg = f"the trailer counts {match['count']} rows, and the body holds {table.cells.height}"
            raise ControlTotalError(msg)
        read.stated.update({field: match[field] for field in self.stated})
        return table


@dataclasses.dataclass(frozen=True)
class Titled:
    """The table sits below title rows, each row's raw text fullmatching its title in *titles*, in order.

    The header is the row below the last title. A title states the fields its named groups name, on every row (an
    as-of date), and a group ``count`` is the number of rows, checked. The same finder serves any format: a CSV's raw
    row is its line, a sheet's its cells joined by tabs (an empty row is ``re.compile("")``).
    """

    titles: Sequence[re.Pattern[str]]
    vouches: bool = False

    def __post_init__(self) -> None:
        if not self.titles:
            msg = "Titled names no titles: a table below none is Header()"
            raise LayoutError(msg)

    @property
    def stated(self) -> frozenset[str]:
        """The fields the titles' groups name."""
        return frozenset(group for title in self.titles for group in title.groupindex if group != "count")

    def find(self, grid: Grid, read: ReadContext) -> Table:
        """Return the table below the grid's titles, noting what they state."""
        if grid.height <= len(self.titles):
            msg = f"the body has {grid.height} rows, not its {len(self.titles)} titles and a header"
            raise UnknownLayoutError(msg)
        for at, title in enumerate(self.titles):
            row = grid.raw[at]
            if (match := title.fullmatch(row)) is None:
                msg = f"row {grid.numbers[at]} {row[:200]!r} is not its title {title.pattern!r}"
                raise UnknownLayoutError(msg)
            read.stated.update({group: match[group] for group in title.groupindex if group != "count"})
            read.matches[f"title {at}"] = match
        table = _header_table(grid.slice(len(self.titles)))
        for match in read.matches.values():
            if "count" in match.re.groupindex and int(match["count"]) != table.cells.height:
                msg = f"a title counts {match['count']} rows, and the body holds {table.cells.height}"
                raise ControlTotalError(msg)
        return table


# --- Shapes ---


@dataclasses.dataclass(frozen=True, kw_only=True)
class Long:
    """A wide table read long: a row per cell under a key's header, the struct's last two fields its key and value.

    The fields before them are the record's, their columns leading the header; every header after them fullmatches
    *keys*, and *key* gives the key's text from the match (its whole text by default). An empty value cell is a row
    whose value is ``None`` (*empty_cells* ``"keep"``), or no row (``"drop"``). Rows come record by record.
    """

    keys: re.Pattern[str]
    key: Callable[[re.Match[str]], str | None] = lambda match: match[0]
    empty_cells: typing.Literal["keep", "drop"] = "keep"

    def shape(self, table: Table, columns: Sequence[Column], read: ReadContext) -> Table:
        """Return *table* read long, under the record's labels and the key and value fields' names."""
        stated = [column for column in columns if column.field in read.stated]
        *record, key_column, value_column = [
            column for column in columns if column not in stated or column in columns[-2:]
        ]
        labels = [column.label for column in record]
        header = [name.partition(_NUL)[0] for name in table.cells.columns]
        if header[: len(labels)] != labels:
            msg = f"the header opens {header[: len(labels)]!r}, not the record's {labels!r}"
            raise UnknownLayoutError(msg)
        keyed: dict[str, str | None] = {}
        for name, heading in zip(table.cells.columns[len(labels) :], header[len(labels) :], strict=True):
            if (match := self.keys.fullmatch(heading)) is None:
                msg = f"the header {heading!r} is not a key: it does not fullmatch {self.keys.pattern!r}"
                raise UnknownLayoutError(msg)
            keyed[name] = self.key(match)
        if len(set(keyed.values())) != len(keyed):
            msg = f"the header names a key twice: {list(keyed.values())!r}"
            raise UnknownLayoutError(msg)
        cells = table.cells.with_row_index("_row").unpivot(
            index=["_row", *table.cells.columns[: len(labels)]],
            on=list(keyed),
            variable_name=key_column.label,
            value_name=value_column.label,
        )
        cells = cells.with_columns(pl.col(key_column.label).replace_strict(keyed, return_dtype=pl.String))
        order = {name: at for at, name in enumerate(keyed)}
        cells = cells.sort(
            "_row",
            pl.col(key_column.label).replace_strict({v: order[k] for k, v in keyed.items()}, return_dtype=pl.Int64),
        )
        if self.empty_cells == "drop":
            cells = cells.filter(pl.col(value_column.label).str.strip_chars().fill_null("") != "")
        numbers = table.numbers.gather(cells["_row"])
        return Table(cells.drop("_row"), numbers)


@dataclasses.dataclass(frozen=True, kw_only=True)
class Observations:
    """Observations whose value hangs on their status: a number under *normal*, one of *absent* (no value) otherwise.

    The table's columns are named by *period*, *value* and *status*. A status not one of *statuses*, a period not a day
    written *date_format* exactly, a value under *normal* not fullmatching *number*, or one under any other status not
    one of *absent*, raises ``ReadError`` naming the observation. Each value under another status becomes no value.
    """

    statuses: Collection[str]
    absent: Collection[str]
    date_format: str
    normal: str = "A"
    number: str = r"-?[0-9]+(?:\.[0-9]+)?"
    period: str = "time_period"
    value: str = "obs_value"
    status: str = "obs_status"

    def shape(self, table: Table, columns: Sequence[Column], read: ReadContext) -> Table:
        """Return *table* checked, each value under another status no value."""
        del columns
        cells = table.cells
        status, value = pl.col(self.status), pl.col(self.value)
        normal = status == self.normal
        period = pl.col(self.period).str.to_date(self.date_format, strict=False)
        for condition, what in (
            (~status.is_in(list(self.statuses)), "has no status of its code list"),
            (
                period.is_null() | (period.dt.strftime(self.date_format) != pl.col(self.period)),
                f"is not a day written {self.date_format}",
            ),
            (normal & ~value.str.contains(f"^(?:{self.number})$"), "is not a number"),
            (~normal & ~value.is_in(list(self.absent)), "is not the format's no-value marker"),
        ):
            if len(bad := cells.filter(condition)):
                row = bad.row(0, named=True)
                msg = f"the observation {row} in {read.member} {what}"
                raise ReadError(msg)
        return Table(cells.with_columns(pl.when(normal).then(value).alias(self.value)), table.numbers)


# --- Cleaners ---


@dataclasses.dataclass(frozen=True)
class Padded:
    """Cells padded with spaces (``"ABC  "``), stripped of them before they are typed."""

    def clean(self, cells: pl.Series, column: Column) -> pl.Series:
        """Return *cells* stripped of spaces."""
        del column
        return cells.str.strip_chars(" ")
