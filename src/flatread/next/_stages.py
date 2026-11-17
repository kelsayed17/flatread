"""The built-in stages: sources, formats, table finders, shapes and cleaners, each one frozen value with one job."""

import contextlib
import csv
import dataclasses
import re
import typing
from collections.abc import Callable, Collection, Generator, Iterator, Mapping, Sequence

import polars as pl

from flatread.archives import _INFLATED_MAX, _UNZIP_ERRORS, _Inflating, _unzipped
from flatread.core import _NUL, Column, _check_ending
from flatread.delimited import _BOM
from flatread.errors import (
    ControlTotalError,
    CorruptArchiveError,
    EmptyBodyError,
    EncodingError,
    LayoutError,
    RaggedRowError,
    ReadError,
    TruncatedFileError,
    UnknownLayoutError,
)
from flatread.next._model import Cells, Grid, Lines, ReadContext, Records, Stream, Table

if typing.TYPE_CHECKING:
    from lxml import etree

    from flatread.next._reader import Reader
    from flatread.pdf import PdfLine

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
    "Zip",
]


def _numbered(height: int) -> pl.Series:
    return pl.int_range(1, height + 1, dtype=pl.Int64, eager=True).alias("number")


def _unraw(height: int) -> pl.Series:
    return pl.repeat("", height, dtype=pl.String, eager=True).alias("raw")


def lines_of(rows: Sequence[Sequence[str | None]], raw: Sequence[str], numbers: Sequence[int]) -> Lines:
    """Return the lines grid of *rows* of cells, each row's *raw* text and its *numbers* in the file."""
    width = max((len(row) for row in rows), default=0)
    cells = pl.DataFrame(
        [[*row, *[None] * (width - len(row))] for row in rows],
        schema={str(at): pl.String for at in range(width)},
        orient="row",
    )
    return Lines(
        cells=cells,
        widths=pl.Series("width", [len(row) for row in rows], pl.Int64),
        raw=pl.Series("raw", list(raw), pl.String),
        numbers=pl.Series("number", list(numbers), pl.Int64),
    )


def records_of(text: pl.DataFrame) -> Cells:
    """Return the cells grid of a format that knows its labels: *text*'s columns, every row a row, all of it text."""
    return Cells(text=text, raw=_unraw(text.height), numbers=_numbered(text.height), header=text.columns)


#: The line breaks ``str.splitlines`` breaks at but a CSV row does not end at, and a NUL: a body holding one, or a
#: lone CR, is not split fast.
_OTHER_BREAKS = "\x00\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"

# --- Sources ---


@dataclasses.dataclass(frozen=True)
class Zip:
    """The body is a zip; the file is its one member whose whole name fullmatches *member*, read as it inflates.

    A zip :mod:`flatread.archives` refuses (a name listed twice, a member that will not inflate or that inflates past
    its cap) raises ``ReadError`` naming the member; a zip holding no such member, or several, ``UnknownLayoutError``.
    The member's name is noted on the read (``read.member``).
    """

    member: re.Pattern[str]

    @contextlib.contextmanager
    def open(self, body: bytes, read: ReadContext) -> Generator[Stream]:
        """Open the member, streaming."""
        with _unzipped(body, None) as archive:
            names = archive.namelist()
            found = [name for name in names if self.member.fullmatch(name)]
            if len(found) != 1:
                msg = f"the zip holds {len(found)} members matching {self.member.pattern}, not one: {names[:5]!r}"
                raise UnknownLayoutError(msg)
            if (size := archive.getinfo(found[0]).file_size) > _INFLATED_MAX:
                msg = f"the zip's {found[0]!r} inflates to {size} bytes, over {_INFLATED_MAX}"
                raise ReadError(msg)
            try:
                member = archive.open(found[0])
            except _UNZIP_ERRORS as e:  # a damaged header, an encrypted member, a method zipfile lacks
                msg = f"the zip's {found[0]!r} will not inflate: {e}"
                raise CorruptArchiveError(msg) from e
            read.member = found[0]
            with member:  # its reads raise ReadError as it inflates, a cut stream or a CRC that fails
                yield _Inflating(found[0], member)


# --- Formats ---


@dataclasses.dataclass(frozen=True, kw_only=True)
class Csv:
    """A delimited file: rows of cells split at *separator*, a cell quoted with *quote* (``None``: nothing is quoted).

    *final_line_ending* is whether the file always ends its last line with a line ending, so a body that ends otherwise
    was cut; ``None`` leaves it unchecked. A blank line is a row of one empty cell. Raises ``LayoutError`` when built
    for a separator or quote that is not one character, ends a line, or is the other.
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

    def grid(self, stream: Stream, read: ReadContext) -> Lines:
        """Return the file's rows of cells: each line's (a quoted cell may span lines), its raw text and line number."""
        del read
        body = stream.read()
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
        split = self._split(text)
        return self._whole(text) if split is None else split

    def _whole(self, text: str) -> Lines:
        """Return *text*'s rows read by ``csv`` from its lines, a quoted cell running on over as many as it spans."""
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
        return lines_of(rows, raw, numbers)

    def _split(self, text: str) -> Lines | None:
        """Return *text*'s rows where every row is one line, split in polars; ``None`` where a cell may span lines.

        A line holding no quote is split at the separator alone; one holding quotes is read by ``csv`` on its own, and
        a quote it leaves open (a cell running on past the line) leaves the body to :meth:`grid`'s reading of it whole.
        So does a line break other than LF or CRLF, which ``str.splitlines`` breaks at too, and a NUL.
        """
        if any(c in text for c in _OTHER_BREAKS) or text.count("\r") != text.count("\r\n"):
            return None
        lines = pl.Series("raw", text.removesuffix("\n").split("\n"), pl.String).str.strip_suffix("\r")
        parts = lines.str.split(self.separator)
        has_quote = lines.str.contains(self.quote, literal=True) if self.quote is not None else None
        if has_quote is not None and (quoted := has_quote.arg_true()).len():
            read: list[list[str]] = []
            for at in quoted:
                try:
                    read.append(
                        next(csv.reader([lines[at]], delimiter=self.separator, quotechar=self.quote, strict=True))
                    )
                except csv.Error:  # a quote left open, or one out of place: read the body whole, as it reads it
                    return None
            unquoted = pl.DataFrame({"at": pl.int_range(lines.len(), eager=True), "parts": parts}).filter(~has_quote)
            parsed = pl.DataFrame({"at": quoted.cast(pl.Int64), "parts": pl.Series(read, dtype=pl.List(pl.String))})
            parts = pl.concat([unquoted, parsed]).sort("at")["parts"]
        widths = parts.list.len().alias("width")
        width = widths.max()
        assert isinstance(width, int)  # noqa: S101 (a body of one line or more)
        cells = pl.DataFrame({str(at): parts.list.get(at, null_on_oob=True) for at in range(width)}).select(
            pl.all().replace("", None)
        )
        return Lines(
            cells=cells, widths=widths, raw=lines, numbers=pl.int_range(1, lines.len() + 1, eager=True).alias("number")
        )


@dataclasses.dataclass(frozen=True, kw_only=True)
class Sheet:
    """A workbook's sheet (``None``: its one sheet), each cell of the kind the workbook stored, beside its text.

    Its grid is a rectangle of the sheet's cells from row 1, each row numbered as Excel numbers it: a cell is text, a
    number (its text every digit it holds), a boolean, or a moment (a date or time cell), and an empty cell, or one of
    only spaces, is none. Each row's raw text is its cells joined by tabs up to its last filled one, what a
    :class:`Titled` title matches.
    """

    sheet: str | None = None

    def grid(self, stream: Stream, read: ReadContext) -> Cells:
        """Return the sheet's cells and their kinds."""
        del read
        from flatread.sheets import _loading, _sheet_named, _text_grid, _workbook  # noqa: PLC0415 (the excel extra)

        workbook = _workbook(stream.read())
        name = _sheet_named(workbook, self.sheet)
        _, text = _text_grid(workbook, name, ())
        if not text.width:
            return Cells(text=text, raw=_unraw(text.height), numbers=_numbered(text.height))
        with _loading(f"the workbook's {name!r} sheet will not load"):
            loads = {
                kind: workbook.load_sheet(name, header_row=None, skip_rows=0, dtypes=kind).to_polars()
                for kind in ("float", "boolean", "datetime")
            }
        for kind, frame in loads.items():
            if frame.shape != text.shape:
                msg = f"the {name!r} sheet's {kind} load is {frame.shape}, not its text's {text.shape}"
                raise ReadError(msg)
            frame.columns = text.columns
        floats = loads["float"].cast(pl.Float64, strict=False)
        moments = loads["datetime"].cast(pl.Datetime("us"), strict=False)
        bools = loads["boolean"]
        # fastexcel's loads tell a cell's kind apart: a number or a boolean loads as a boolean, a text never, and only
        # a boolean's text is ``true`` or ``false``; a moment loads as a time and never as a float.
        kinds = pl.concat(
            [
                text,
                floats.rename(lambda c: f"{c}:f"),
                bools.rename(lambda c: f"{c}:b"),
                moments.rename(lambda c: f"{c}:d"),
            ],
            how="horizontal",
        ).select(
            pl.when(pl.col(c).is_null())
            .then(None)
            .when(pl.col(f"{c}:b").is_not_null() & pl.col(c).is_in(["true", "false"]))
            .then(pl.lit("bool"))
            .when(pl.col(f"{c}:b").is_not_null() & pl.col(f"{c}:f").is_not_null())
            .then(pl.lit("number"))
            .when(pl.col(f"{c}:d").is_not_null() & pl.col(f"{c}:f").is_null())
            .then(pl.lit("moment"))
            .otherwise(pl.lit("text"))
            .alias(c)
            for c in text.columns
        )
        raw = text.select(pl.concat_str(pl.all().fill_null(""), separator="\t").str.strip_chars_end("\t")).to_series()
        return Cells(
            text=text,
            raw=raw.alias("raw"),
            numbers=_numbered(text.height),
            kinds=kinds,
            floats=floats,
            moments=moments,
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

    def grid(self, stream: Stream, read: ReadContext) -> Cells:
        """Return the records as a grid under the struct's labels."""
        from flatread.pdf import text_layer  # noqa: PLC0415 (the pdf extra loads only when a PDF is read)

        lines = text_layer(stream.read())
        if self.layout is not None:
            lines = self.layout.lines(lines)
        records = self.records.records(lines, self.layout, read)
        fields = list(read.columns)
        for n, record in enumerate(records, 1):
            if unknown := record.keys() - read.columns.keys():
                msg = f"record {n} names fields the row has none of: {sorted(unknown)!r}"
                raise LayoutError(msg)
        return records_of(
            pl.DataFrame(
                [[record.get(field) for field in fields] for record in records],
                schema={read.columns[field].label: pl.String for field in fields},
                orient="row",
            )
        )


@dataclasses.dataclass(frozen=True, kw_only=True)
class Sdmx:
    """An SDMX-ML 1.0 compact data message's data set *data_set*, parsed as it streams: a row per observation.

    The row's first field is the attribute naming each series (its key); the others are each observation's attributes.
    A field's attribute is its label as *names* writes it (upper-cased by default: ``series_name`` is ``SERIES_NAME``).
    *root*, *data_set_element*, *observation* and *annotations* are the message's prefixed element names, *prefixes*
    the format's own beside SDMX-ML 1.0's. The message is read a series at a time, each cleared once read, so a zip's
    member streams through (``source=Zip(...)``). Every part of the message is checked: its root, each data set after
    the last, a series holding only its annotations and observations, each observation only its attributes, nothing
    else between them, and the data set read holding a series. A prefix none binds raises ``LayoutError`` when built.
    """

    data_set: str
    root: str
    data_set_element: str
    observation: str
    annotations: str
    prefixes: Mapping[str, str] = dataclasses.field(default_factory=dict)
    names: Callable[[str], str] = str.upper
    series: str = "Series"
    data_set_id: str = "id"

    def __post_init__(self) -> None:
        for name in (self.root, self.data_set_element, self.observation, self.annotations):
            self._tag(name)

    def _namespaces(self) -> dict[str, str]:
        from flatread.sdmx import _NAMESPACES  # noqa: PLC0415 (the xml extra loads only when SDMX is read)

        return {**_NAMESPACES["1.0"], **self.prefixes}

    def _tag(self, name: str) -> str:
        prefix, _, local = name.partition(":")
        namespaces = self._namespaces()
        if prefix not in namespaces:
            msg = f"the element {name!r} writes a prefix the format's namespaces do not bind"
            raise LayoutError(msg)
        return f"{{{namespaces[prefix]}}}{local}"

    def grid(self, stream: Stream, read: ReadContext) -> Cells:
        """Return the data set's observations: each its series' key and its attributes, under the row's labels."""
        from lxml import etree  # noqa: PLC0415 (the xml extra loads only when SDMX is read)

        namespaces = self._namespaces()
        labels = [column.label for column in read.columns.values()]
        key, *attributes = [self.names(label) for label in labels]
        paths = [
            etree.XPath(f"{self.observation}/@{a}", namespaces=namespaces, smart_strings=False) for a in attributes
        ]
        count = etree.XPath(f"count({self.observation}/@*)", namespaces=namespaces)
        notes = self._tag(self.annotations)
        held: list[list[object]] = [[] for _ in labels]
        name = read.member or "the body"
        for series in self._each(stream, name):
            if (series_key := series.get(key)) is None:
                msg = f"a series of the {self.data_set} data set of {name} carries no {key}"
                raise UnknownLayoutError(msg)
            found = [cells for path in paths if isinstance(cells := path(series), list)]
            observations = len(series) - (series.find(notes) is not None)
            if count(series) != len(attributes) * observations or [len(c) for c in found] != [observations] * len(
                attributes
            ):
                msg = (
                    f"an observation of the series {series_key!r} of the {self.data_set} data set of {name}"
                    f" carries other than {attributes}"
                )
                raise UnknownLayoutError(msg)
            held[0].extend([series_key] * observations)
            for column, cells in zip(held[1:], found, strict=True):
                column.extend(cells)
        return records_of(pl.DataFrame(dict(zip(labels, held, strict=True)), schema=dict.fromkeys(labels, pl.String)))

    def _each(self, stream: Stream, name: str) -> Iterator[etree._Element]:  # noqa: PLR0912, PLR0915 (one pass checks every part)
        """Yield each series of the data set read, every part of the message checked; each is cleared after."""
        from lxml import etree  # noqa: PLC0415

        from flatread.markup import _malformed  # noqa: PLC0415
        from flatread.sdmx import _between_sets, _blank, _drop_read, _Metered, _only_series  # noqa: PLC0415

        namespaces = self._namespaces()
        data_set, root_tag = self._tag(self.data_set_element), self._tag(self.root)
        notes = self._tag(self.annotations)
        junk = etree.XPath(
            f"count({self.observation}/node()) + count(text()[normalize-space()])", namespaces=namespaces
        )
        metered = _Metered(name, stream)  # ty: ignore[invalid-argument-type] (any stream with read())
        sets: list[str] = []
        found = 0
        last: etree._Element | None = None
        parsed = etree.iterparse(
            metered,
            events=("start", "end"),
            tag=(data_set, f"{{*}}{self.series}"),
            resolve_entities=False,
            no_network=True,
            load_dtd=False,
            huge_tree=False,
        )
        try:
            for event, element in ((e, el) for e, el in parsed if etree.iselement(el)):
                if not sets and (root := element.getroottree().getroot().tag) != root_tag:
                    msg = f"the root of {name} is <{root}>, not <{root_tag}>"
                    raise UnknownLayoutError(msg)
                if element.tag == data_set:
                    if event == "start":
                        _between_sets(element, data_set, name, first=not sets)
                        sets.append(element.get(self.data_set_id, ""))
                    else:
                        _only_series(element, self.series, sets[-1], name)
                        if last is not None and (parent := last.getparent()) is not None:
                            parent.remove(last)
                        last = element
                    continue
                metered.series(started=event == "start")
                if event == "start":
                    continue
                parent = element.getparent()
                if parent is None or parent.tag != data_set:
                    msg = f"a <{element.tag}> of {name} sits outside any <{data_set}>"
                    raise UnknownLayoutError(msg)
                if sets[-1] == self.data_set:
                    note = element.find(notes)
                    if (
                        junk(element)
                        or len(element) != len(element.findall(self.observation, namespaces)) + (note is not None)
                        or (note is not None and note.getprevious() is not None)
                    ):
                        msg = (
                            f"a series of the {self.data_set} data set of {name}"
                            " holds other than its annotations and observations"
                        )
                        raise UnknownLayoutError(msg)
                    found += 1
                    yield element
                tail = element.tail
                element.clear()
                element.tail = tail
                _drop_read(element, parent, self.series, sets[-1], name)
        except etree.XMLSyntaxError as e:
            raise _malformed(name, e, metered.end) from e
        if last is not None and (last.getnext() is not None or not _blank(last.tail)):
            msg = f"{name} holds other than data sets after its last"
            raise UnknownLayoutError(msg)
        if self.data_set not in sets:
            msg = f"{name} holds no {self.data_set} data set; it holds {sets!r}"
            raise UnknownLayoutError(msg)
        if not found:
            msg = f"the {self.data_set} data set of {name} holds no series"
            raise ReadError(msg)


@dataclasses.dataclass(frozen=True)
class Derived:
    """A file another reader reads first, its frame then made the row's text cells by *shape*.

    *shape* takes the inner reader's frame and that read's context (its trailer's match) and returns a String frame of
    the row's labels. For a table the file lays out in a way only its whole shows (each index then its estimates).
    """

    inner: Reader[typing.Any]
    shape: Callable[[pl.DataFrame, ReadContext], pl.DataFrame]

    def grid(self, stream: Stream, read: ReadContext) -> Cells:
        """Return the shaped frame as a grid whose header is its columns."""
        frame, inner = self.inner.read(stream.read())
        read.matches.update(inner.matches)
        return records_of(self.shape(frame, inner))


# --- Table finders ---


def _header_table(grid: Grid) -> Table:
    """Return *grid*'s table: its first row the header (or the header its format states), every other row a row."""
    if isinstance(grid, Cells) and grid.header is not None:
        return _named(grid, grid.text.columns, list(grid.header))
    if not grid.height:
        msg = "the body is empty"
        raise EmptyBodyError(msg)
    if isinstance(grid, Lines):
        width = int(grid.widths[0])
        header = [cell or "" for cell in grid.cells.row(0)[:width]]
        rows = grid.slice(1)
        if (wrong := (rows.widths != width).arg_true()).len():
            at = int(wrong[0])
            msg = f"row {rows.numbers[at]} has {rows.widths[at]} fields, not the header's {width}"
            raise RaggedRowError(msg)
        _check_header(header)
        cells = rows.cells.select(rows.cells.columns[:width])
        return Table(cells.rename(dict(zip(cells.columns, _unique(header), strict=True))), rows.numbers)
    # A sheet's header runs to its last filled cell; a row holding a cell right of it, or none at all, is no row.
    first = grid.text.row(0)
    width = max((at + 1 for at, cell in enumerate(first) if cell is not None), default=0)
    header = [cell or "" for cell in first[:width]]
    rows = grid.slice(1)
    past = rows.text.columns[width:]
    beyond = rows.text.select(pl.any_horizontal(pl.col(past).is_not_null())).to_series() if past else None
    if beyond is not None and (wrong := beyond.arg_true()).len():
        msg = f"row {rows.numbers[int(wrong[0])]} holds a cell right of the header's last, column {width}"
        raise RaggedRowError(msg)
    kept = rows.text.columns[:width]
    empty = rows.text.select(pl.all_horizontal(pl.col(kept).is_null())).to_series() if kept else None
    if empty is not None and (at := empty.arg_true()).len():
        msg = f"row {rows.numbers[int(at[0])]} is empty, and a table's rows run to its end"
        raise ReadError(msg)
    _check_header(header)
    return _named(rows, kept, _unique(header))


def _named(grid: Cells, kept: Sequence[str], labels: Sequence[str]) -> Table:
    """Return *grid*'s columns *kept* as a table, each frame's columns named *labels*."""
    names = dict(zip(kept, labels, strict=True))

    def named(frame: pl.DataFrame | None) -> pl.DataFrame | None:
        return None if frame is None else frame.select(kept).rename(names)

    return Table(
        typing.cast("pl.DataFrame", named(grid.text)),
        grid.numbers,
        named(grid.kinds),
        named(grid.floats),
        named(grid.moments),
    )


def _check_header(header: Sequence[str]) -> None:
    if any(_NUL in cell for cell in header):
        msg = f"the header {list(header)!r} holds a NUL, which no heading does"
        raise UnknownLayoutError(msg)


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
    """The table is the grid: its first row the header, every row below it a row.

    A line's row is as wide as the header; a sheet's header runs to its last filled cell, and a row of the sheet must
    hold no cell right of it and some cell under it.
    """

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
    whose value is ``None`` (*empty_cells* ``"keep"``), or no row (``"drop"``). Rows come record by record. A sheet's
    cells keep their kinds.
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
        order = {name: at for at, name in enumerate(keyed)}
        record_columns = table.cells.columns[: len(labels)]

        def long(frame: pl.DataFrame | None) -> pl.DataFrame | None:
            if frame is None:
                return None
            cells = frame.with_row_index("_row").unpivot(
                index=["_row", *record_columns], on=list(keyed), variable_name="_key", value_name=value_column.label
            )
            cells = cells.sort("_row", pl.col("_key").replace_strict(order, return_dtype=pl.Int64))
            key = pl.col("_key").replace_strict(keyed, return_dtype=pl.String).alias(key_column.label)
            return cells.select("_row", *record_columns, key, value_column.label)

        frames = [long(frame) for frame in table.frames()]
        cells = typing.cast("pl.DataFrame", frames[0])
        if frames[1] is not None:  # a key is text, whatever kind its column's cells are
            frames[1] = frames[1].with_columns(pl.lit("text").alias(key_column.label))
        keep = pl.Series([True] * cells.height)
        if self.empty_cells == "drop":
            keep = cells[value_column.label].str.strip_chars().fill_null("") != ""
        numbers = table.numbers.gather(cells.filter(keep)["_row"])
        return table.with_frames([None if f is None else f.filter(keep).drop("_row") for f in frames], numbers)


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
