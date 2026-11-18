"""The built-in stages: sources, formats, table finders, shapes and cleaners, each one frozen value with one job."""

import codecs
import concurrent.futures
import contextlib
import csv
import dataclasses
import io
import re
import typing
import zipfile
from collections.abc import Callable, Collection, Generator, Iterator, Mapping, Sequence

import polars as pl

from flatread.archives import _INFLATED_MAX, _UNZIP_ERRORS, _Inflating, _unzipped
from flatread.core import _NUL, _THOUSANDS, Column, _check_ending, _decoded
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
from flatread.next._model import Cells, Grid, HeaderRule, Lines, ReadContext, Records, Source, Stream, Table

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
#: A CR that ends no CRLF: polars reads it as a cell's text.
_LONE_CR = re.compile(r"\r(?!\n)")


def _breaks_as_polars(text: str) -> bool:
    """Return whether *text* breaks its lines only where polars' parser does, at LF and CRLF, and holds no NUL.

    ``str.splitlines`` breaks at a CR alone and at the other breaks too (``_OTHER_BREAKS``), where polars reads on.
    """
    return not any(c in text for c in _OTHER_BREAKS) and ("\r" not in text or text.count("\r") == text.count("\r\n"))


#: The text fastexcel gives a moment cell (a date, a time, both): what makes a cell's column loaded as times too.
_MOMENT_TEXT = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d+)?$"

# --- Sources ---


@dataclasses.dataclass(frozen=True)
class Zip:
    """The body is a zip; the file is its one member whose whole name fullmatches *member*, read as it inflates.

    A zip :mod:`flatread.archives` refuses (a name listed twice, a member that will not inflate or that inflates past
    its cap) raises ``ReadError`` naming the member; a zip holding no such member, or several, ``UnknownLayoutError``.
    The member's name is noted on the read (``read.member``).

    With *bare*, a file published zipped or not: a body that is no zip, or a zip with no member matching (a workbook is
    a zip of its parts), is the file itself, read whole; a zip :mod:`zipfile` opens only to fail on raises
    ``CorruptArchiveError``.
    """

    member: re.Pattern[str]
    bare: bool = False

    @contextlib.contextmanager
    def open(self, body: bytes, read: ReadContext) -> Generator[Stream]:
        """Open the member, streaming; with *bare*, the body itself where it holds none."""
        if self.bare and not self._zipped(body):
            yield io.BytesIO(body)
            return
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

    def _zipped(self, body: bytes) -> bool:
        """Return whether *body* is a zip holding a member *member* matches."""
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                return any(self.member.fullmatch(name) for name in archive.namelist())
        except zipfile.BadZipFile:
            return False
        except _UNZIP_ERRORS as e:
            msg = f"the body is a zip that will not open: {e}"
            raise CorruptArchiveError(msg) from e


@dataclasses.dataclass(frozen=True)
class Transcoded:
    """A file written in a legacy encoding (EIA's Windows-1252), read as UTF-8 by the format.

    The file is the body, or what *source* opens of it (a zip's member); it is decoded by the first of *encodings*
    that reads it (``utf-16`` only where it opens with its byte-order mark), after dropping *eof*, a byte it ends with
    after its last line (DOS's end-of-file mark). One none decodes raises ``EncodingError``, unless *lossy*: then a
    byte the last does not decode is U+FFFD. Raises ``LayoutError``, when built, for no encodings or one Python lacks.
    """

    encodings: Sequence[str]
    eof: bytes = b""
    lossy: bool = False
    source: Source | None = None

    def __post_init__(self) -> None:
        if isinstance(self.encodings, (str, bytes)) or not self.encodings:
            msg = f"Transcoded names {self.encodings!r}, not the codecs its body may be written in"
            raise LayoutError(msg)
        for encoding in self.encodings:
            try:
                codecs.lookup(encoding)
            except LookupError as e:
                msg = f"Transcoded's encoding {encoding!r} is no codec Python reads"
                raise LayoutError(msg) from e

    @contextlib.contextmanager
    def open(self, body: bytes, read: ReadContext) -> Generator[Stream]:
        """Open the file as UTF-8."""
        if self.source is None:
            yield io.BytesIO(self._utf8(body))
            return
        with self.source.open(body, read) as stream:
            data = stream.read()
        yield io.BytesIO(self._utf8(data))

    def _utf8(self, data: bytes) -> bytes:
        return _decoded(data.removesuffix(self.eof), self.encodings, lossy=self.lossy).encode()


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
        text = self._text(body)
        parsed = self._parsed(body, text)
        if isinstance(parsed, Lines):
            return parsed
        split = self._spanning(body, text) if parsed == "spans" else self._split(text)
        return self._whole(text, body) if split is None else split

    def _text(self, body: bytes) -> str:
        """Return *body*'s text, or raise for a body empty, cut (by *final_line_ending*) or not UTF-8.

        A blank line where the header stands is the table finder's to refuse: a preamble may open with one.
        """
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
        return text

    def typed(  # noqa: PLR0911 (a None for each doubt)
        self, body: bytes, dtypes: Mapping[str, type[pl.DataType]], null_values: Sequence[str]
    ) -> Table | None:
        """Return the table of a file that is its header of *dtypes*' labels and rows as wide, read typed in one pass.

        polars' parser reads each column as its dtype in *dtypes* (``Int64``, ``Float64``, else ``String``), each of
        *null_values* no value, so the engine has the text columns alone to type. ``None`` where the body may be
        anything else: another header, a row of another width (counted from its separators outside quoted cells), a
        quote out of place (as :meth:`_spanning` finds one), a line break polars does not break at, or a cell its
        parser refuses as its dtype; :meth:`grid`'s reading then reads it, and names what is wrong. A body that is
        empty, cut, not UTF-8 or opening blank raises as :meth:`grid` raises. Every number polars' parser takes, its
        cast from text takes as the same value, so the table types as the grid's would; its rows are numbered by
        place, which only a refusal names, and the engine's refusal of it reads the body again by :meth:`grid`.
        """
        text = self._text(body)
        if len(self.separator.encode()) != 1 or not _breaks_as_polars(text):
            return None
        quote = self.quote if self.quote is not None and self.quote in text else None
        labels = list(dtypes)
        try:
            header = next(
                csv.reader([text.partition("\n")[0]], delimiter=self.separator, quotechar=quote or '"', strict=True)
            )
        except csv.Error:  # a header cell running on past its line
            return None
        if _unique(header) != labels:
            return None
        schema = {label: dtype if dtype in (pl.Int64, pl.Float64) else pl.String for label, dtype in dtypes.items()}

        def parsed() -> pl.DataFrame | None:
            try:
                return pl.read_csv(
                    body.removeprefix(_BOM),
                    separator=self.separator,
                    quote_char=quote,
                    schema=schema,
                    null_values=list(null_values),
                )
            except pl.exceptions.PolarsError:  # a cell its parser refuses as its dtype: the grid's reading names it
                return None

        if quote is None:
            data = body.removeprefix(_BOM)
            rows = pl.read_csv(data, has_header=False, separator="\x00", quote_char=None, schema={"raw": pl.String})
            lines = rows["raw"].fill_null("")
            if (lines.str.count_matches(self.separator, literal=True) != len(labels) - 1).any():
                return None
            frame, breaks = parsed(), lines.len()
        else:
            # The rows checked while polars reads them, each in a thread of its own: neither holds Python's lock.
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                checking = pool.submit(self._rows_of, text, quote, len(labels))
                frame = parsed()
                breaks = checking.result()
            if breaks is None or frame is None:
                return None
            # Each row ends at a line break outside its quoted cells: the body's breaks less those inside a cell read.
            breaks -= (
                frame.select(pl.col(pl.String).str.count_matches("\n", literal=True).sum()).sum_horizontal().item()
            )
        if frame is None or frame.height != breaks - 1:
            return None
        frame = frame.with_columns(pl.col(pl.String).replace("", None))
        return Table(frame, pl.int_range(2, frame.height + 2, eager=True).alias("number"))

    def _rows_of(self, text: str, quote: str, width: int) -> int | None:
        """Return how many rows *text* holds, where each is *width* cells quoted whole or holding no quote, or ``None``.

        A quoted cell opens and closes with *quote*, each it holds doubled, and may hold the separator and line breaks;
        any other cell holds none of them. One fullmatch of polars' regex engine, so a quote out of place, a row of
        another width or a quoted cell that does not close is ``None``, as :meth:`_spanning` finds each; so is a width
        whose pattern is past the engine's size, which the grid's reading reads. The rows are counted as the text's
        line breaks, those inside its quoted cells too, and one more where its last line has none.
        """
        q, sep = re.escape(quote), re.escape(self.separator)
        cell = f"(?:{q}[^{q}]*(?:{q}{q}[^{q}]*)*{q}|[^{sep}\r\n{q}]*)"
        row = f"{cell}(?:{sep}{cell}){{{width - 1}}}"
        whole = pl.col("text")
        try:
            rows, breaks = (
                pl.DataFrame({"text": [text]})
                .select(
                    whole.str.contains(f"^(?:{row}\r?\n)*{row}(?:\r?\n)?$"),
                    whole.str.count_matches("\n", literal=True).alias("n"),
                )
                .row(0)
            )
        except pl.exceptions.ComputeError:  # a pattern past the regex engine's size
            return None
        if not rows:
            return None
        assert isinstance(breaks, int)  # noqa: S101 (a count)
        return breaks + (not text.endswith("\n"))

    def _parsed(self, body: bytes, text: str) -> Lines | typing.Literal["spans"] | None:
        """Return *text*'s rows where each is one line, none wider than the first, read by polars' parser.

        ``None`` where a line breaks other than at LF or CRLF, a quote is out of place on its line (a cell running on
        past it, or a quote inside a cell not quoted), or a row is wider than the first. Each line is read twice by
        ``read_csv``: whole (its separator a NUL, which no such body holds), then split into the first row's width, a
        short row padded with nulls and a long one cut. A row's width is its separators outside its quoted cells, plus
        one; where nothing is quoted, its line's bytes less its cells', so a row cut shows as wider than the first.
        ``"spans"`` where a line holds an odd number of quotes, so a quoted cell seems to run on past it: the
        readings of the body whole are tried at once, as the line-by-line one would leave it to them (every reading
        reads as ``csv`` does, so any may).
        """
        if len(self.separator.encode()) != 1 or not _breaks_as_polars(text):
            return None
        body = body.removeprefix(_BOM)
        lines = pl.read_csv(body, has_header=False, separator="\x00", quote_char=None, schema={"raw": pl.String})
        lines = lines["raw"].fill_null("")
        quote = self.quote if self.quote is not None and self.quote in text else None
        bare, widths = lines, None
        if quote is not None:
            # A line of odd quotes leaves a quoted cell open (or holds a quote out of place, which a reading of the
            # body whole finds).
            if (lines.str.count_matches(quote, literal=True) % 2 == 1).any():
                return "spans"
            # Each quoted cell one NUL: a quote left is out of place, as is a cell that opens or closes mid-cell.
            q, sep = re.escape(quote), re.escape(self.separator)
            bare = lines.str.replace_all(f"{q}(?:[^{q}]|{q}{q})*{q}", "\x00")
            if (bare.str.contains(quote, literal=True) | bare.str.contains(f"[^{sep}]\x00|\x00[^{sep}]")).any():
                return None
            widths = bare.str.count_matches(self.separator, literal=True) + 1
        width = bare[0].count(self.separator) + 1
        cells = pl.read_csv(
            body,
            has_header=False,
            separator=self.separator,
            quote_char=quote,
            schema={str(at): pl.String for at in range(width)},
            truncate_ragged_lines=True,
        )
        if lines.len() != cells.height:  # the parsers disagree on the lines: split them as the line splitter does
            return None
        if widths is None:
            held = cells.select(pl.sum_horizontal(pl.all().str.len_bytes().fill_null(0)))[:, 0]
            widths = lines.str.len_bytes() - held + 1
        if quote is not None:  # a quoted empty cell is read as text, and holds no value as an unquoted one does
            cells = cells.select(pl.all().replace("", None))
        if widths.max() != width:  # a row wider than the first, its surplus cut by the parser: split a line at a time
            return None
        numbers = pl.int_range(1, lines.len() + 1, eager=True).alias("number")
        return Lines(cells=cells, widths=widths.cast(pl.UInt32).alias("width"), raw=lines, numbers=numbers)

    def _spanning(self, body: bytes, text: str) -> Lines | None:
        """Return *text*'s rows where a quoted cell runs on over lines, read by polars, every quote in place.

        ``None`` where a quote is out of place: one left once each quoted cell (its doubled quotes too) is a NUL, or a
        cell that opens or closes mid-cell. With every quote a quoted cell's, a line break is inside one where the
        quotes before it are odd in number, so a row's lines are those up to one ending outside, and its width its
        separators outside them, plus one. :meth:`_parsed` has checked the line breaks are polars'.
        """
        assert self.quote is not None  # noqa: S101 (a body with a quote, which a format quoting nothing leaves alone)
        q, sep = re.escape(self.quote), re.escape(self.separator)
        bare = pl.Series([text]).str.replace_all(f"{q}(?:[^{q}]|{q}{q})*{q}", "\x00")
        if bare.str.contains(f"{q}|[^{sep}\n]\x00|\x00[^{sep}\r\n]").any():
            return None
        rows = bare.str.strip_suffix("\n").str.split("\n").explode().str.strip_suffix("\r")
        lines = pl.Series([text.removesuffix("\n")]).str.split("\n").explode()
        inside = lines.str.count_matches(self.quote, literal=True).cum_sum() % 2 == 1
        starts = (~inside).shift(1, fill_value=True)
        # Each row's text is the body's from its first line's start to the line break ending it.
        offsets = (lines.str.len_chars() + 1).cum_sum().shift(1, fill_value=0).filter(starts).to_list()
        ends = [*(at - 1 for at in offsets[1:]), len(text.removesuffix("\n"))]
        raw = pl.Series("raw", [text[at:end] for at, end in zip(offsets, ends, strict=True)], pl.String)
        raw = raw.str.strip_chars_end("\r\n")
        widths = rows.str.count_matches(self.separator, literal=True) + 1
        cells = pl.read_csv(
            body.removeprefix(_BOM),
            has_header=False,
            separator=self.separator,
            quote_char=self.quote,
            schema={str(at): pl.String for at in range(widths[0])},
            truncate_ragged_lines=True,
        ).select(pl.all().replace("", None))
        if not rows.len() == raw.len() == cells.height or widths.max() != widths[0]:
            return None  # a row wider than the first, or rows the parser reads otherwise: csv reads them
        numbers = starts.arg_true().cast(pl.Int64) + 1
        return Lines(
            cells=cells,
            widths=widths.cast(pl.Int64).alias("width"),
            raw=raw.alias("raw"),
            numbers=numbers.alias("number"),
        )

    def _whole(self, text: str, body: bytes, *, parse: bool = True) -> Lines:
        """Return *text*'s rows read by ``csv`` from its lines, a quoted cell running on over as many as it spans.

        ``csv`` reads every row, to check it and to count its cells and the lines it spans, each row's raw text its
        lines joined. Where the body breaks its lines only at LF or CRLF, as polars' parser does, the parser reads the
        cells; elsewhere (a line break ``str.splitlines`` alone breaks at, or a first row narrower
        than the widest, which the parser refuses) ``csv``'s rows are the cells.
        """
        # Lines end at LF alone (a CRLF's CR stays, which csv reads as the row's end): str.splitlines also breaks at a
        # lone CR, a form feed, a vertical tab and the rest of _OTHER_BREAKS, which a CSV row runs on past.
        # A lone CR is a cell's text, as polars reads it, which csv refuses in an unquoted cell: it reads a stand-in.
        lone = _LONE_CR.search(text) is not None
        stand_in = next(chr(c) for c in range(0xE000, 0xF900) if chr(c) not in text) if lone else ""
        split = (_LONE_CR.sub(stand_in, text) if lone else text).split("\n")
        lines = [line + "\n" for line in split[:-1]] + ([split[-1]] if split[-1] else [])
        quoting = csv.QUOTE_NONE if self.quote is None else csv.QUOTE_MINIMAL
        reader = csv.reader(lines, delimiter=self.separator, quotechar=self.quote or '"', quoting=quoting, strict=True)
        parsed = parse and _breaks_as_polars(text) and len(self.separator.encode()) == 1
        rows: list[list[str | None]] = []
        widths: list[int] = []
        ends: list[int] = []
        try:
            for cells in reader:
                # A blank line is a row of one empty cell, which a table as wide as its header refuses.
                widths.append(len(cells) or 1)
                ends.append(reader.line_num)
                if not parsed:
                    rows.append([(cell.replace(stand_in, "\r") if lone else cell) or None for cell in cells] or [None])
        except csv.Error as e:
            # A quoted cell open at the body's end was cut; any other quote out of place is a malformed row.
            msg = f"row {len(widths) + 1}: {e}"
            raise (TruncatedFileError if "unexpected end of data" in str(e) else ReadError)(msg) from e
        starts = [1, *(end + 1 for end in ends[:-1])]
        raw = ["".join(lines[start - 1 : end]).rstrip("\r\n") for start, end in zip(starts, ends, strict=True)]
        if lone:
            raw = [line.replace(stand_in, "\r") for line in raw]
        if not parsed:
            return lines_of(rows, raw, starts)
        try:
            cells = pl.read_csv(
                body.removeprefix(_BOM),
                has_header=False,
                separator=self.separator,
                quote_char=self.quote,
                schema={str(at): pl.String for at in range(max(widths))},
            ).select(pl.all().replace("", None))
        except pl.exceptions.PolarsError:  # a first row narrower than the widest, which the parser refuses
            return self._whole(text, body, parse=False)
        if cells.height != len(widths):  # the two read the rows otherwise: csv's rows are the cells
            return self._whole(text, body, parse=False)
        return Lines(
            cells=cells,
            widths=pl.Series("width", widths, pl.Int64),
            raw=pl.Series("raw", raw, pl.String),
            numbers=pl.Series("number", starts, pl.Int64),
        )

    def _split(self, text: str) -> Lines | None:
        """Return *text*'s rows where every row is one line, split in polars; ``None`` where a cell may span lines.

        A line holding no quote is split at the separator alone; one holding quotes is read by ``csv`` on its own, and
        a quote it leaves open (a cell running on past the line) leaves the body to :meth:`grid`'s reading of it whole.
        So does a line break other than LF or CRLF, which ``str.splitlines`` breaks at too, and a NUL.
        """
        if not _breaks_as_polars(text):
            return None
        lines = pl.Series("raw", [text.removesuffix("\n")], pl.String).str.split("\n").explode().str.strip_suffix("\r")
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

    def typed(self, body: bytes, dtypes: Mapping[str, type[pl.DataType]], null_values: Sequence[str]) -> Table | None:  # noqa: PLR0911 (a None for each doubt)
        """Return the table of a sheet that is its header of *dtypes*' labels and full rows, read typed in one load.

        fastexcel's strict load types each column by every one of its cells and refuses a column mixing kinds, so a
        column it loads as its field's kind (``Float64`` a number's, ``Boolean`` a boolean's, ``String`` a text's)
        holds cells of that kind alone, as :meth:`grid`'s kinds would find. ``None`` where the sheet may be anything
        else: another header, a cell right of it, a column of another kind or of mixed kinds, a number not whole in an
        integer's column (or past 64 bits), any empty cell (a cell the load reads as none though it may hold an error
        or a word for none), or a row the grid finds empty (each of its cells empty text or spaces); :meth:`grid`'s
        reading then reads it, and names what is wrong. So is a read with *null_values*: a sheet that writes a word for
        none writes it among numbers, which the load refuses, so it is the grid's to read from the start. The sheet is
        bounded first, as every load of it is; its text columns are left to the engine, which blanks them.
        """
        if null_values:  # a sheet that writes a word for none mixes kinds where it does: the grid's loads read it
            return None
        from flatread.sheets import _BoundedXlsx, _failed, _loading, _sheet_named, _workbook  # noqa: PLC0415
        from flatread.sheets import _whole as _whole_numbers  # noqa: PLC0415 (Csv's own _whole is another)

        workbook = _workbook(body)
        name = _sheet_named(workbook, self.sheet)
        if isinstance(workbook, _BoundedXlsx):
            with _loading(f"the workbook's {name!r} sheet will not load"):
                workbook.bound(name)
        try:
            frame = workbook.load_sheet(
                name, header_row=0, dtype_coercion="strict", schema_sample_rows=None
            ).to_polars()
        except BaseException as e:  # a mixed column, or a cell calamine panics on typed: the grid's loads read it
            if not _failed(e):
                raise
            return None
        if frame.columns != list(dtypes) or frame.null_count().sum_horizontal().item():
            return None
        typed = []
        for label, dtype in dtypes.items():
            column = frame[label]
            if dtype == pl.Int64 and column.dtype == pl.Float64:
                if not _whole_numbers(column).all():
                    return None
                column = column.cast(pl.Int64)
            if column.dtype != dtype:  # a column of another kind than its field's: the grid's to refuse or read
                return None
            typed.append(column)
        # A row of text cells each of spaces alone, or empty text, is a row the grid finds empty (and refuses).
        if all(dtype == pl.String for dtype in dtypes.values()) and (
            frame.select(pl.all_horizontal(pl.all().str.strip_chars() == "").any()).item()
        ):
            return None
        return Table(pl.DataFrame(typed), pl.int_range(2, frame.height + 2, eager=True).alias("number"))

    def grid(self, stream: Stream, read: ReadContext) -> Cells:
        """Return the sheet's cells and their kinds, the sheet loaded three times, a column holding a moment four."""
        del read
        import fastexcel  # noqa: PLC0415 (the excel extra)

        from flatread.sheets import _BoundedXlsx, _loading, _sheet_named, _workbook  # noqa: PLC0415

        body = stream.read()
        workbook = _workbook(body)
        name = _sheet_named(workbook, self.sheet)
        if isinstance(workbook, _BoundedXlsx):  # bounded once, before any load: the others open it unbounded
            with _loading(f"the workbook's {name!r} sheet will not load"):
                workbook.bound(name)

        def load(kind: typing.Literal["float", "boolean"]) -> pl.DataFrame:
            """Load the sheet as *kind* in a workbook of its own: one workbook loads one sheet at a time."""
            with _loading(f"the workbook's {name!r} sheet will not load"):
                opened = fastexcel.read_excel(body)
                return opened.load_sheet(name, header_row=None, skip_rows=0, dtypes=kind).to_polars()

        # The three loads at once, each its own parse of the sheet: a load holds no lock of Python's.
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            loading = [pool.submit(load, kind) for kind in ("float", "boolean")]
            with _loading(f"the workbook's {name!r} sheet will not load"):
                sheet = workbook.load_sheet(name, header_row=None, skip_rows=0, dtypes="string")
                places = [column.absolute_index for column in sheet.available_columns()]
                text = sheet.to_polars()
            floats, bools = (future.result() for future in loading)
        if not text.width:
            return Cells(text=text, raw=_unraw(text.height), numbers=_numbered(text.height))
        names = [str(at) for at in range(text.width)]
        for kind, frame in (("text", text), ("float", floats), ("boolean", bools)):
            if frame.shape != (text.height, len(places)):
                msg = f"the {name!r} sheet's {kind} load is {frame.shape}, not {(text.height, len(places))}"
                raise ReadError(msg)
            frame.columns = names
        floats = floats.cast(pl.Float64, strict=False)
        # A number's text with every digit it holds (fastexcel's text rounds it), and a cell of spaces none.
        text = pl.concat([text, floats.rename(lambda c: f"{c}:f")], how="horizontal").select(
            pl.when(pl.col(f"{c}:f").is_not_null() & (pl.col(c).cast(pl.Float64, strict=False) != pl.col(f"{c}:f")))
            .then(pl.col(f"{c}:f").cast(pl.String))
            .otherwise(pl.col(c))
            .alias(c)
            for c in names
        )
        text = text.with_columns(pl.when(pl.all().str.strip_chars() != "").then(pl.all()).name.keep())
        # A cell's kind, from its loads: a number loads as a float and a boolean, a boolean as both too but its text
        # is ``true`` or ``false``, a text loads as neither (a numeral as a float alone). A moment loads as neither
        # and its text is a time: only a column holding such a cell is loaded a fourth time, as times, to tell a
        # moment from a text written like one (and so a column of numbers alone is never loaded as times, which
        # panics on a number such as -1e300, far outside the years a time holds).
        both = pl.concat([text, floats.rename(lambda c: f"{c}:f"), bools.rename(lambda c: f"{c}:b")], how="horizontal")
        kinds = both.select(
            pl.when(pl.col(c).is_null())
            .then(None)
            .when(pl.col(f"{c}:b").is_not_null() & pl.col(c).is_in(["true", "false"]))
            .then(pl.lit("bool"))
            .when(pl.col(f"{c}:b").is_not_null() & pl.col(f"{c}:f").is_not_null())
            .then(pl.lit("number"))
            .when(pl.col(f"{c}:b").is_null() & pl.col(f"{c}:f").is_null() & pl.col(c).str.contains(_MOMENT_TEXT))
            .then(pl.lit("maybe"))
            .otherwise(pl.lit("text"))
            .alias(c)
            for c in names
        )
        moments = pl.DataFrame({c: pl.Series(c, [None] * text.height, pl.Datetime("us")) for c in names})
        if timed := [c for c in names if (kinds[c] == "maybe").any()]:
            with _loading(f"the workbook's {name!r} sheet will not load its times"):
                loaded = workbook.load_sheet(
                    name,
                    header_row=None,
                    skip_rows=0,
                    use_columns=[places[int(c)] for c in timed],
                    dtypes={places[int(c)]: "datetime" for c in timed},
                ).to_polars()
            if loaded.shape != (text.height, len(timed)):
                msg = f"the {name!r} sheet's datetime load is {loaded.shape}, not {(text.height, len(timed))}"
                raise ReadError(msg)
            loaded.columns = timed
            moments = moments.with_columns(loaded.cast(pl.Datetime("us"), strict=False))
            kinds = kinds.with_columns(
                pl.when(pl.col(c) == "maybe")
                .then(pl.when(moments[c].is_not_null()).then(pl.lit("moment")).otherwise(pl.lit("text")))
                .otherwise(pl.col(c))
                .alias(c)
                for c in timed
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
        if grid.raw[0] == "":
            number = int(grid.numbers[0])
            msg = (
                "the body opens with a blank line, not its header"
                if number == 1
                else f"row {number} is blank, not the header"
            )
            raise UnknownLayoutError(msg, row=number)
        width = int(grid.widths[0])
        header = [cell or "" for cell in grid.cells.row(0)[:width]]
        rows = grid.slice(1)
        if (wrong := (rows.widths != width).arg_true()).len():
            at = int(wrong[0])
            msg = f"row {rows.numbers[at]} has {rows.widths[at]} fields, not the header's {width}"
            raise RaggedRowError(msg, row=int(rows.numbers[at]))
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
        row = int(rows.numbers[int(wrong[0])])
        msg = f"row {row} holds a cell right of the header's last, column {width}"
        raise RaggedRowError(msg, row=row)
    kept = rows.text.columns[:width]
    empty = rows.text.select(pl.all_horizontal(pl.col(kept).is_null())).to_series() if kept else None
    if empty is not None and (at := empty.arg_true()).len():
        row = int(rows.numbers[int(at[0])])
        msg = f"row {row} is empty, and a table's rows run to its end"
        raise ReadError(msg, row=row)
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


def _matched(table: Table, header: HeaderRule | None, read: ReadContext) -> Table:
    """Return *table* under the struct's labels by its *header* rule, or as its header writes them for none."""
    return table if header is None else header.match(table, read)


@dataclasses.dataclass(frozen=True)
class Header:
    """The table is the grid: its first row the header, every row below it a row.

    A line's row is as wide as the header; a sheet's header runs to its last filled cell, and a row of the sheet must
    hold no cell right of it and some cell under it. The header is the struct's labels in order, unless a *header*
    rule reads it (:class:`Headers`, :class:`Named`), as it does under every finder.
    """

    stated: frozenset[str] = frozenset()
    vouches: bool = False
    header: HeaderRule | None = None

    def find(self, grid: Grid, read: ReadContext) -> Table:
        """Return the grid's table."""
        return _matched(_header_table(grid), self.header, read)


@dataclasses.dataclass(frozen=True)
class Preamble:
    """A table below a preamble of any length (a title, a source, notes, blank rows): its header found, not placed.

    The header is the first row whose first cell is *label*, by default the struct's first column's; the rows above it
    are dropped unread, and the table below it read as :class:`Header` reads one. A grid with no such row raises
    ``UnknownLayoutError``. One stage for a CSV's and a sheet's (``PreambleCsv``, ``PreambleSheet``); a preamble whose
    every row is known is a :class:`Titled`'s.
    """

    label: str | None = None
    stated: frozenset[str] = frozenset()
    vouches: bool = False
    header: HeaderRule | None = None

    def find(self, grid: Grid, read: ReadContext) -> Table:
        """Return the table under the first row opening with the header's first label."""
        label = self.label if self.label is not None else next(iter(read.columns.values())).label
        first = (grid.cells if isinstance(grid, Lines) else grid.text).get_column("0", default=None)
        found = None if first is None else (first == label).fill_null(value=False).arg_true()
        if found is None or not found.len():
            msg = f"no row opens with the header's first label {label!r}"
            raise UnknownLayoutError(msg)
        return _matched(_header_table(grid.slice(int(found[0]))), self.header, read)


@dataclasses.dataclass(frozen=True)
class Trailer:
    """The file's last row is a trailer (a record count, a stamp), its text fullmatching *pattern*, not a row.

    A group ``count`` is the number of rows, checked; a group named for a field is that field's text on every row; an
    unnamed group states nothing (a later stage reads the match: ``read.matches["trailer"]``). The trailer is what a cut
    body lacks, so it vouches for a table of no rows.
    """

    pattern: re.Pattern[str]
    vouches: bool = True
    header: HeaderRule | None = None

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
        read.stated.update({field: match[field] for field in self.stated})
        table = _matched(_header_table(grid.slice(0, -1)), self.header, read)
        if "count" in self.pattern.groupindex and int(match["count"]) != table.cells.height:
            msg = f"the trailer counts {match['count']} rows, and the body holds {table.cells.height}"
            raise ControlTotalError(msg)
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
    header: HeaderRule | None = None

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
                raise UnknownLayoutError(msg, row=int(grid.numbers[at]))
            read.stated.update({group: match[group] for group in title.groupindex if group != "count"})
            read.matches[f"title {at}"] = match
        table = _matched(_header_table(grid.slice(len(self.titles))), self.header, read)
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
        record_columns = table.cells.columns[: len(labels)]

        def long(frame: pl.DataFrame | None) -> pl.DataFrame | None:
            if frame is None:
                return None
            cells = frame.with_row_index("_row").unpivot(
                index=["_row", *record_columns], on=list(keyed), variable_name="_key", value_name=value_column.label
            )
            # unpivot writes key by key; row r's key j is at j * height + r, so rows come record by record in key order.
            height, at = frame.height, pl.int_range(frame.height * len(keyed))
            cells = cells.select(pl.all().gather((at % len(keyed)) * height + at // len(keyed)))
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


@dataclasses.dataclass(frozen=True)
class Thousands:
    """Numbers written with thousands separators (``1,782,319``), each read as its number.

    A number field's cell grouped so loses its commas; one holding a comma otherwise (``12,34``) keeps it, and so is
    refused as no number, naming its row. Text fields are left as written.
    """

    def clean(self, cells: pl.Series, column: Column) -> pl.Series:
        """Return a number *column*'s *cells*, each grouped in thousands without its commas."""
        if column.scalar not in {int, float}:
            return cells
        grouped = cells.str.contains(_THOUSANDS)
        return pl.select(
            pl.when(grouped).then(cells.str.replace_all(",", "", literal=True)).otherwise(cells)
        ).to_series()
