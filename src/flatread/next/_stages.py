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
from flatread.core import _NUL, _THOUSANDS, _UNIT, Column, _check_above, _check_ending, _decoded, _stacked
from flatread.delimited import _BOM, _underflows
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
from flatread.next._headers import _LACKED, _laid_out
from flatread.next._model import Cells, Grid, HeaderRule, Lines, ReadContext, Records, Source, Stream, Table

if typing.TYPE_CHECKING:
    from lxml import etree

    from flatread.next._reader import Reader
    from flatread.pdf import PdfLine

__all__ = [
    "Csv",
    "Cut",
    "Derived",
    "Found",
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
    was cut; ``None`` leaves it unchecked. A blank line is a row of one empty cell. *misquoted* reads a quote that does
    not close its cell as text, and a cell left open at the end runs to it (``csv``'s ``strict`` off), for a file whose
    prose notes misquote (``"ts." Holdings o"``); a cell quoted cleanly still holds its separator. Raises
    ``LayoutError`` when built for a separator or quote that is not one character, ends a line, or is the other.

    *closing* is a separator closing a line, past its last cell: ``"always"``, every line's, the header's too (a line
    without it raises ``RaggedRowError``, naming it, and so does a quoted cell spanning lines, whose first line ends
    inside it); ``"sometimes"``, a line's that may write one (a separator just before a line ending is dropped,
    wherever it stands, so a row whose real last cell is empty is one short and raises). 7.x's ``_closing_separator``.

    A CSV writes each cell exactly (:attr:`exact`): a number padded with whitespace (`` 5``) is refused and a text
    cell of spaces is that text, unless a :class:`Padded` cleaner strips them. One byte order mark opens the body or
    none; a second raises ``EncodingError``.
    """

    #: The engine reads its cells as written: no number trimmed, no text of spaces taken for no value.
    exact: typing.ClassVar[bool] = True

    separator: str = ","
    quote: str | None = '"'
    final_line_ending: bool | None = None
    misquoted: bool = False
    closing: typing.Literal["always", "sometimes"] | None = None

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
        body = self._closed(stream.read())
        text = self._text(body)
        if self.misquoted:
            return self._whole(text, body, parse=False)
        parsed = self._parsed(body, text)
        if isinstance(parsed, Lines):
            return parsed
        split = self._spanning(body, text) if parsed == "spans" else self._split(text)
        return self._whole(text, body) if split is None else split

    def _closed(self, body: bytes) -> bytes:
        """Return *body* without the separator closing its lines (*closing*), refusing a line without one (always)."""
        if self.closing is None:
            return body
        sep = re.escape(self.separator.encode())
        if self.closing == "always":
            unclosed = re.search(rb"(?<!" + sep + rb")(?:\r\n|(?<!\r)\n)", body)
            last = body.rsplit(b"\n", 1)[-1].removesuffix(b"\r")
            if unclosed is not None or (last and not last.endswith(self.separator.encode())):
                line = body.count(b"\n", 0, unclosed.start()) + 1 if unclosed is not None else body.count(b"\n") + 1
                msg = f"line {line} does not end with its closing separator {self.separator!r}"
                raise RaggedRowError(msg, row=line)
        return re.sub(sep + rb"(?=\r?\n|\Z)", b"", body)

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
        # polars' parser drops a byte order mark too: a second would leave the first label's text unread.
        if text.startswith("\ufeff"):
            msg = "the body opens with two byte order marks: the second, U+FEFF, is the first cell's text"
            raise EncodingError(msg)
        return text

    def typed(  # noqa: PLR0911 (a None for each doubt)
        self, body: bytes, dtypes: Mapping[str, type[pl.DataType]], null_values: Sequence[str]
    ) -> Table | None:
        """Return the table of a file that is its header of *dtypes*' labels and rows as wide, read typed in one pass.

        polars' parser reads each column as its dtype in *dtypes* (``Int64``, ``Float64``, else ``String``), each of
        *null_values* no value, so the engine has the text columns alone to type; where a cell may open with a space
        or a tab (:meth:`_may_pad`), which the parser would take before a number, every cell is read as text and the
        numbers cast, as the grid's are. ``None`` where the body
        may be anything else: another header, a row of another width (counted from its separators outside quoted
        cells), a quote out of place (as :meth:`_spanning` finds one), a line break polars does not break at, a cell
        the cast refuses (a number padded with whitespace) or a float that underflows (``1e-400`` as 0.0);
        :meth:`grid`'s reading then reads it, and names what is wrong. A body that is empty, cut, not UTF-8 or opening
        blank raises as :meth:`grid` raises. The cast is the grid's, so the table types as the grid's would; its rows
        are numbered by place, which only a refusal names, and the engine's refusal of it reads the body again by
        :meth:`grid`.
        """
        body = self._closed(body)
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
        # polars' parser takes a number after spaces or a tab (`` 5``), which its cast refuses, as the grid's reading
        # does: a body where a cell may open so has every cell read as text, its numbers then cast.
        padded = self._may_pad(body, quote)
        schema = {
            label: dtype if dtype in (pl.Int64, pl.Float64) and not padded else pl.String
            for label, dtype in dtypes.items()
        }

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
        # A cell its cast refuses (padded, empty but quoted) or a float that underflows (``1e-400`` as 0.0): the
        # grid's reading names it.
        numbers = {label: dtype for label, dtype in dtypes.items() if dtype in (pl.Int64, pl.Float64)}
        if numbers and padded:
            cast = frame.with_columns(pl.col(label).cast(dtype, strict=False) for label, dtype in numbers.items())
            if any(cast[label].null_count() != frame[label].null_count() for label in numbers):
                return None
            frame = cast
        if numbers and _underflows(body, frame, numbers):
            return None
        frame = frame.with_columns(pl.col(pl.String).replace("", None))
        return Table(frame, pl.int_range(2, frame.height + 2, eager=True).alias("number"))

    def _may_pad(self, body: bytes, quote: str | None) -> bool:
        """Return whether a cell of *body* may open with a space or a tab, after a separator, a line break or a quote.

        A few substring searches in polars, over the body's bytes.
        """
        opens = [self.separator.encode(), b"\n", *([quote.encode()] if quote is not None else [])]
        if body.removeprefix(_BOM).startswith((b" ", b"\t")):
            return True
        found = pl.lit(pl.Series([body], dtype=pl.Binary))
        pairs = [found.bin.contains(start + blank) for start in opens for blank in (b" ", b"\t")]
        return bool(pl.select(pl.any_horizontal(pairs)).item())

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
        reader = csv.reader(
            lines, delimiter=self.separator, quotechar=self.quote or '"', quoting=quoting, strict=not self.misquoted
        )
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


@dataclasses.dataclass(frozen=True, kw_only=True)
class Cut:
    """A table cut from an irregular grid (CsvText's, SheetText's): its header in a row, every filled row below it.

    The grid is taken whole: a short row's missing cells are no value, and so is a cell of spaces or one of the file's
    words for none. Its header is in row *header_row* of the file (from 1), to its last filled cell, and its rows are
    every row below it but an empty one, each numbered by its row in the file; a cell past the header's last raises.

    *above* rows right over the header head its columns too (a group over its labels): a cell heads its own column and
    those to its right with none of their own, each column's header its cells top to bottom joined by U+001F, as a
    :class:`Long` shape's keys read them. The header is found, not placed, under *title* (the first row whose first
    cell is it, alone: the header is the first filled row below) or by *filled*, ``(fewest, short_by)``: the first row
    filling at least *fewest* cells and no more than *short_by* fewer than the fullest. A grid with no such row holds
    no table: no rows, where the reader takes a table of none (``empty``), and refused otherwise.

    *ends* ``"blank"`` ends the rows at the first empty one (notes or another table below); *band* ``(first, past)``
    cuts the grid's columns to those, ``past`` ``None`` the grid's end (a second table beside it); *full_rows* refuses
    a text file's row narrower than its widest but a blank line. One finder for a CSV's grid and a sheet's, which keeps
    its cells' kinds; a *header* rule reads the header, as under every finder.
    """

    header_row: int = 1
    above: int = 0
    title: str | None = None
    filled: tuple[int, int] | None = None
    ends: typing.Literal["end", "blank"] = "end"
    band: tuple[int, int | None] = (0, None)
    full_rows: bool = False
    stated: frozenset[str] = frozenset()
    vouches: bool = False
    header: HeaderRule | None = None

    def __post_init__(self) -> None:
        if self.header_row < 1:
            msg = f"the header row {self.header_row} is no row of a grid, whose rows count from 1"
            raise LayoutError(msg)
        if (self.title is not None) + (self.filled is not None) + (self.header_row != 1) > 1:
            msg = "a Cut's header is in its header_row, under its title or by how filled it is: one of the three"
            raise LayoutError(msg)
        if self.above and (self.title is not None or self.filled is not None):
            msg = "the rows over a found header are not known to head its columns: above takes a header_row"
            raise LayoutError(msg)
        _check_above(self.above, self.header_row)
        first, past = self.band
        if first < 0 or (past is not None and past <= first):
            msg = f"the band {self.band} is no columns of a grid: its first from 0, and its past after it"
            raise LayoutError(msg)
        if self.filled is not None and (self.filled[0] < 1 or self.filled[1] < 0):
            msg = f"filled {self.filled} is no header's: it fills at least one cell, and no fewer than none short"
            raise LayoutError(msg)
        if self.ends not in ("end", "blank"):
            msg = f"a Cut's rows end at the grid's end or a blank row, not {self.ends!r}"
            raise LayoutError(msg)

    def find(self, grid: Grid, read: ReadContext) -> Table:
        """Return the table cut from the grid."""
        cells = self._cells(grid, read)
        first, past = self.band
        cells = _columns(cells, cells.cells.columns[first:past])
        text = cells.cells
        empty = text.select(pl.all_horizontal(pl.all().is_null())).to_series() if text.width else None
        at = self._header_at(text, empty, read)
        if at is None:
            if not read.empty:
                assert self.filled is not None  # noqa: S101 (only a header found by how filled it is goes unfound)
                msg = f"the grid holds no table: no row of it fills {self.filled[0]} cells, as a header does"
                raise EmptyBodyError(msg)
            return _laid_empty(read)
        header = self._header_of(text, at)
        if self.above and len(set(header)) != len(header):
            msg = f"the header {header} heads two columns alike, which no format of the grid tells apart"
            raise UnknownLayoutError(msg, row=at)
        if header.count("") > 1:
            msg = f"the header {header} leaves two cells empty, which no format of the grid reads"
            raise UnknownLayoutError(msg, row=at)
        below = _sliced(cells, at)
        if self.ends == "blank" and empty is not None and (end := empty.slice(at).arg_true()).len():
            below = _sliced(below, 0, int(end[0]))
        past_cells = below.cells.columns[len(header) :]
        if (
            past_cells
            and (held := below.cells.select(pl.any_horizontal(pl.col(past_cells).is_not_null())).to_series()).any()
        ):
            row = int(below.numbers[int(held.arg_true()[0])])
            msg = f"row {row} holds a cell past the header {header!r}, which no column of the table is"
            raise ReadError(msg, row=row)
        kept = below.cells.columns[: len(header)]
        rows = _columns(below, kept)
        if kept:
            filled = ~rows.cells.select(pl.all_horizontal(pl.all().is_null())).to_series()
            rows = rows.with_frames(
                [None if f is None else f.filter(filled) for f in rows.frames()], rows.numbers.filter(filled)
            )
        _check_header(header)
        named = _unique(header)
        frames = [None if f is None else f.rename(dict(zip(kept, named, strict=True))) for f in rows.frames()]
        return _matched(rows.with_frames(frames, rows.numbers), self.header, read)

    def _cells(self, grid: Grid, read: ReadContext) -> Table:
        """Return *grid* as a table of its columns ``0``, ``1``, ..., a cell of spaces or of a null word none."""
        if isinstance(grid, Lines):
            if self.full_rows and grid.height:
                widest = typing.cast("int", grid.widths.max())
                if (short := ((grid.widths < widest) & (grid.raw != "")).arg_true()).len():
                    at = int(short[0])
                    row = int(grid.numbers[at])
                    msg = f"row {row} has {grid.widths[at]} fields, not the widest row's {widest}"
                    raise RaggedRowError(msg, row=row)
            table = Table(grid.cells, grid.numbers)
        else:
            table = Table(grid.text, grid.numbers, grid.kinds, grid.floats, grid.moments)
        nulls = list(read.null_values)
        text = table.cells.with_columns(
            pl.when((pl.all().str.strip_chars() != "") & ~pl.all().is_in(nulls)).then(pl.all()).name.keep()
        )
        return table.with_frames([text, *table.frames()[1:]], table.numbers)

    def _header_at(self, text: pl.DataFrame, empty: pl.Series | None, read: ReadContext) -> int | None:
        """Return the grid row (from 1) of the header, or ``None`` for a grid of no table (by *filled*)."""
        del read
        if self.title is not None:
            if not text.width:
                msg = f"no row is the title {self.title!r} alone"
                raise UnknownLayoutError(msg)
            lead, rest = pl.col(text.columns[0]), pl.exclude(text.columns[0])
            alone = (lead == self.title) & pl.all_horizontal(rest.is_null()) if text.width > 1 else lead == self.title
            if not (titled := text.select(alone.fill_null(value=False)).to_series().arg_true()).len():
                msg = f"no row is the title {self.title!r} alone"
                raise UnknownLayoutError(msg)
            title = int(titled[0])
            assert empty is not None  # noqa: S101 (a grid of columns)
            if not (header := (~empty.slice(title + 1)).arg_true()).len():
                msg = f"the title {self.title!r} has no table below it"
                raise UnknownLayoutError(msg)
            return title + 2 + int(header[0])
        if self.filled is not None:
            fewest, short_by = self.filled
            counts = text.select(pl.sum_horizontal(pl.all().is_not_null())).to_series() if text.width else None
            fullest = typing.cast("int", counts.max()) if counts is not None and counts.len() else 0
            if fullest < fewest:
                return None
            assert counts is not None  # noqa: S101 (a fullest row)
            return int((counts >= max(fewest, fullest - short_by)).arg_true()[0]) + 1
        return self.header_row

    def _header_of(self, text: pl.DataFrame, at: int) -> list[str]:
        """Return the labels the header row *at* writes, to its last filled cell, each under the rows over it."""
        if text.height < at:
            msg = f"the body has {text.height} rows, and its header is row {at}"
            raise UnknownLayoutError(msg)
        rows = [text.row(row) for row in range(at - 1 - self.above, at)]
        if not self.above:
            header: list[str] = [cell or "" for cell in rows[-1]]
            while header and not header[-1]:
                header.pop()
            return header
        cells = [[None if cell is None else cell.strip() or None for cell in row] for row in rows]
        width = max((place + 1 for row in cells for place, cell in enumerate(row) if cell is not None), default=0)
        return _stacked([row[:width] for row in cells])


@dataclasses.dataclass(frozen=True, kw_only=True)
class Found:
    """An export whose header is found below any title lines (FoundTable's), each column read by its label.

    The header is the first row with a cell that is a word of the *key* field's label (its ``Label`` and alternatives,
    each cell as *heading* folds it). Each cell of it names a column: one word of a field's label, which is that
    field's column, a field it lacks reading as null. A cell no field's words name, two cells naming one field, a row
    narrower than the header (cut inside its last row), or a value past the header or under a blank header cell raises,
    naming the row. A row of nothing but spaces and ``-`` rules is skipped. *open* reads every header cell as a
    column of its own, as written, for a shape that reads it (an adviser roster's, read long): a blank one names none,
    and what is under it is not the file's. A row whose first cell opens with *section* and holds nothing past it
    states *section_field* for the rows below, the text after the prefix; *numbered_rows* reads a first column with no
    header cell, its cells whole numbers, as the export's own row numbers, no data. A sheet's cells keep their kinds.
    """

    key: str
    heading: Callable[[str], str] | None = None
    open: bool = False
    section: str = ""
    section_field: str = ""
    numbered_rows: bool = False
    stated: frozenset[str] = frozenset()
    vouches: bool = True
    #: A header read by name: a field's Label may name alternatives.
    by_name: typing.ClassVar[bool] = True

    def __post_init__(self) -> None:
        if bool(self.section) != bool(self.section_field):
            msg = (
                "a section's prefix and the field it states go together: set both section and section_field, or neither"
            )
            raise LayoutError(msg)

    def find(self, grid: Grid, read: ReadContext) -> Table:
        """Return the table below the row naming the key field, a column per field (or per cell, *open*)."""
        columns, by_word, key_words = self._words(read)
        if isinstance(grid, Lines):
            table, widths = Table(grid.cells, grid.numbers), grid.widths
        else:
            table = Table(grid.text, grid.numbers, grid.kinds, grid.floats, grid.moments)
            widths = pl.repeat(grid.text.width, grid.height, dtype=pl.Int64, eager=True)
        at, names = self._header(table, widths, by_word, key_words, read)
        below, widths = _sliced(table, at + 1), widths.slice(at + 1)
        keep, sections = self._kept(below.cells)
        self._check_rows(below, widths, keep, names)
        rows = below.with_frames(
            [None if f is None else f.filter(keep) for f in below.frames()], below.numbers.filter(keep)
        )
        placed = {name: below.cells.columns[place] for place, name in enumerate(names) if name is not None}
        labels = list(placed) if self.open else [column.label for column in columns]
        laid = _laid_out(rows, {column: label for label, column in placed.items()}, labels)
        if not self.section:
            return laid
        # The section's text is the stage's, not a cell's: typed as text, whatever a sheet's kinds.
        label = read.columns[self.section_field].label
        frames = [
            None
            if f is None
            else f.with_columns(sections.filter(keep).alias(label) if place == 0 else pl.lit(None, dtype).alias(label))
            for place, (f, dtype) in enumerate(zip(laid.frames(), _LACKED, strict=True))
        ]
        return dataclasses.replace(laid.with_frames(frames, laid.numbers), written=laid.written | {label})

    def _words(self, read: ReadContext) -> tuple[list[Column], dict[str, str], set[str]]:
        """Return the columns a header may name, the label each word names, and the key field's words."""
        if not (keyed := [column for column in read.columns.values() if column.field == self.key]):
            msg = f"the key {self.key!r} is no field read from a column"
            raise LayoutError(msg)
        if self.section_field and self.section_field not in read.columns:
            msg = f"the section field {self.section_field!r} is no field of the row"
            raise LayoutError(msg)
        columns = [c for c in read.columns.values() if c.field not in read.stated and c.field != self.section_field]
        words = [
            (word, column.label) for column in columns for word in (column.label.partition(_NUL)[0], *column.aliases)
        ]
        if len(by_word := dict(words)) != len(words):
            msg = f"two fields share a label: {sorted(word for word, _ in words)}"
            raise LayoutError(msg)
        return columns, by_word, {keyed[0].label, *keyed[0].aliases}

    def _header(
        self, table: Table, widths: pl.Series, by_word: Mapping[str, str], key_words: Collection[str], read: ReadContext
    ) -> tuple[int, list[str | None]]:
        """Return the header's place, the first row naming the key field, and the column each of its cells names."""
        fold = self.heading or (lambda cell: cell)
        rows = enumerate(table.cells.iter_rows())
        at = next((n for n, row in rows if set(key_words) & {fold(cell) for cell in row if cell is not None}), None)
        if at is None:
            msg = f"the file has no header naming {self.key}: it is not a {getattr(read.row, '__name__', 'row')} file"
            raise UnknownLayoutError(msg)
        # The header is as wide as its row: a line's own cells, a sheet's every column.
        cells: list[str] = [cell or "" for cell in table.cells.row(at)[: int(widths[at])]]
        names: list[str | None] = [
            (cell.strip() if self.open else by_word.get(fold(cell))) if cell.strip() else None for cell in cells
        ]
        row = int(table.numbers[at])
        if unknown := [cell for cell, name in zip(cells, names, strict=True) if cell.strip() and name is None]:
            msg = f"the header names columns the row does not declare: {unknown}"
            raise UnknownLayoutError(msg, row=row)
        if len(named := [name for name in names if name is not None]) != len(set(named)):
            msg = f"the header names one field twice: {cells}"
            raise UnknownLayoutError(msg, row=row)
        return at, names

    def _kept(self, cells: pl.DataFrame) -> tuple[pl.Series, pl.Series]:
        """Return which rows are the table's (no blank or rule, no section's), and each row's section."""
        # A grid with a header has a column, so a row has cells to join.
        bare = cells.select(pl.concat_str(pl.all().fill_null(""), separator="").str.strip_chars(" -") == "").to_series()
        if not self.section:
            return ~bare, pl.repeat(None, cells.height, dtype=pl.String, eager=True)
        lead, rest = pl.col(cells.columns[0]), cells.columns[1:]
        alone = (
            pl.concat_str(pl.col(rest).fill_null(""), separator="").str.strip_chars() == "" if rest else pl.lit(1) == 1
        )
        heads = cells.select(lead.str.starts_with(self.section).fill_null(value=False) & alone).to_series() & ~bare
        # Each row's section: the text of the last section row above it.
        text = lead.str.strip_prefix(self.section).str.strip_chars()
        sections = cells.select(pl.when(heads).then(text)).to_series().fill_null(strategy="forward")
        return ~(bare | heads), sections

    def _check_rows(self, below: Table, widths: pl.Series, keep: pl.Series, names: Sequence[str | None]) -> None:
        """Refuse a row narrower than the header, or holding a value past it or under no label of *names*."""
        cells, numbers, width = below.cells, below.numbers, len(names)
        if (short := (keep & (widths < width)).arg_true()).len():
            place = int(short[0])
            row = int(numbers[place])
            msg = f"row {row} has {widths[place]} cells, not the header's {width}: {_written(cells, widths, place)}"
            raise RaggedRowError(msg, row=row)
        unnamed = [] if self.open else [cells.columns[place] for place, name in enumerate(names) if name is None]
        under = [pl.col(column) for column in unnamed]
        # An unnamed first column of whole numbers holds the export's row numbers (2013's BDC file), and is no data.
        if self.numbered_rows and names and names[0] is None and unnamed and unnamed[0] == cells.columns[0]:
            first = pl.col(cells.columns[0])
            under[0] = pl.when(~first.str.strip_chars().str.contains(r"^\d+$").fill_null(value=False)).then(first)
        checked = [*under, *(pl.col(column) for column in cells.columns[width:])]
        if not checked:
            return
        held = cells.select(pl.any_horizontal(c.str.strip_chars().fill_null("") != "" for c in checked)).to_series()
        if (wrong := (keep & held).arg_true()).len():
            place = int(wrong[0])
            row = int(numbers[place])
            msg = f"row {row} holds a value under no label: {_written(cells, widths, place)}"
            raise ReadError(msg, row=row)


def _written(cells: pl.DataFrame, widths: pl.Series, place: int) -> list[str]:
    """Return row *place*'s cells as written, to its width, an empty one ``""``."""
    return [cell or "" for cell in cells.row(place)[: int(widths[place])]]


def _columns(table: Table, names: Sequence[str]) -> Table:
    """Return *table*'s columns *names*, in every frame."""
    return table.with_frames([None if f is None else f.select(names) for f in table.frames()], table.numbers)


def _sliced(table: Table, at: int, length: int | None = None) -> Table:
    """Return *table*'s rows from *at*, *length* of them (``None``: to its end), in every frame."""
    frames = [None if f is None else f.slice(at, length) for f in table.frames()]
    return table.with_frames(frames, table.numbers.slice(at, length))


def _laid_empty(read: ReadContext) -> Table:
    """Return a table of no rows under the struct's labels, but those a stage states: a grid that holds no table."""
    labels = [column.label for column in read.columns.values() if column.field not in read.stated]
    return Table(pl.DataFrame(schema=dict.fromkeys(labels, pl.String)), pl.Series("number", [], pl.Int64))


# --- Shapes ---


@dataclasses.dataclass(frozen=True, kw_only=True)
class Long:
    """A wide table read long: a row per cell under a key's header, the struct's last fields its keys and value.

    The fields before them are the record's, their columns leading the header (by their labels, a header over several
    rows by its last line), or found by label anywhere in it (*scattered*: an empty label is the first column's); a
    field of *beside* stands among the keys, found by its label wherever it is, and is ``None`` where the header has
    none. Every other header fullmatches *keys*: *key* gives the key's text from the match (its whole text by
    default), or, for a header naming several keys (``market_value_residence_2003``), *key_count* fields before the
    value each take the text of the group named for it (an empty one no text, where the field admits none). A heading
    with no text heads a spacer, a column holding nothing, where *spacers* is set; no two headings give one key, and
    none is written twice, unless *repeats*: then a key field no group names is its heading's place among the keys,
    from 1. An empty value cell is a row whose value is ``None`` (*empty_cells* ``"keep"``), or no row (``"drop"``).
    Rows come record by record, a record's keys in header order; a table of a header and no rows is refused where
    *needs_rows* is set, though the reader takes a table of none (one of rows with no value is still an answer). A
    sheet's cells keep their kinds.
    """

    keys: re.Pattern[str]
    key: Callable[[re.Match[str]], str | None] = lambda match: match[0]
    key_count: int = 1
    empty_cells: typing.Literal["keep", "drop"] = "keep"
    spacers: bool = False
    scattered: bool = False
    beside: Collection[str] = ()
    repeats: bool = False
    needs_rows: bool = False

    def __post_init__(self) -> None:
        if self.key_count < 1:
            msg = f"a long table's header names at least one key, not {self.key_count}"
            raise LayoutError(msg)
        if self.empty_cells not in ("keep", "drop"):
            msg = f"an empty value cell is kept or dropped, not {self.empty_cells!r}"
            raise LayoutError(msg)

    def shape(self, table: Table, columns: Sequence[Column], read: ReadContext) -> Table:
        """Return *table* read long, under the record's labels and the key and value fields' labels."""
        *fields, value_column = columns[: len(columns)]
        key_columns, fields = fields[len(fields) - self.key_count :], fields[: len(fields) - self.key_count]
        if stray := set(self.beside) - {column.field for column in fields}:
            msg = f"beside names {sorted(stray)}, which are no record fields of the long row"
            raise LayoutError(msg)
        if (
            self.key_count > 1
            and not self.repeats
            and (unnamed := [k.field for k in key_columns if k.field not in self.keys.groupindex])
        ):
            msg = f"the key fields {unnamed} are named by no group of {self.keys.pattern!r}, and no heading repeats"
            raise LayoutError(msg)
        fields = [column for column in fields if column.field not in read.stated]
        record = [column for column in fields if column.field not in self.beside]
        names = table.cells.columns
        header = [name.partition(_NUL)[0] for name in names]
        tails = [heading.rpartition(_UNIT)[2] for heading in header]
        places = self._record_places(header, tails, record)
        besides: dict[str, str] = {}
        for column in (column for column in fields if column.field in self.beside):
            found = [names[at] for at, tail in enumerate(tails) if tail == column.label and at not in places]
            if len(found) > 1:
                msg = f"the header {header!r} has {len(found)} {column.label!r} columns, not one"
                raise UnknownLayoutError(msg)
            if found:
                besides[column.label] = found[0]
        taken = {*(names[at] for at in places), *besides.values()}
        rest = [at for at, name in enumerate(names) if name not in taken]
        spacers = [names[at] for at in rest if self.spacers and not tails[at]]
        keyed = [at for at in rest if names[at] not in spacers]
        if stray_keys := [header[at] for at in keyed if not self.keys.fullmatch(header[at])]:
            msg = f"the header's columns {stray_keys!r} are not keys, which match {self.keys.pattern!r}"
            raise UnknownLayoutError(msg)
        headings = [header[at] for at in keyed]
        if not self.repeats and len(set(headings)) != len(headings):
            msg = f"the header names a key twice: {headings!r}"
            raise UnknownLayoutError(msg)
        texts = self._key_texts(headings, key_columns, read)
        if len(set(texts)) != len(texts):
            shown = [text[0] for text in texts] if self.key_count == 1 else [list(text) for text in texts]
            msg = f"the header names a key twice: its headings {headings!r} name the keys {shown!r}"
            raise UnknownLayoutError(msg)
        if spacers and (held := table.cells.select(pl.any_horizontal(pl.col(spacers).is_not_null())).to_series()).any():
            row = int(table.numbers[int(held.arg_true()[0])])
            msg = f"a column with no heading holds a cell: row {row}"
            raise ReadError(msg, row=row)
        if self.needs_rows and not table.cells.height:
            msg = "the body is a header and no rows"
            raise EmptyBodyError(msg)
        return self._long(
            table,
            record=[(names[at], column.label) for at, column in places.items()],
            besides=besides,
            fields=fields,
            keyed=[names[at] for at in keyed],
            texts=texts,
            keys=key_columns,
            value=value_column,
        )

    def _record_places(
        self, header: Sequence[str], tails: Sequence[str], record: Sequence[Column]
    ) -> dict[int, Column]:
        """Return the record's columns by their place in the header: leading it, or found anywhere (*scattered*)."""
        labels = [column.label for column in record]
        if not self.scattered:
            if (opening := list(tails[: len(labels)])) != labels:
                msg = f"the header opens {opening!r}, not the record's {labels!r}"
                raise UnknownLayoutError(msg)
            return dict(enumerate(record))
        places = {}
        for column in record:
            # An empty label is the first column's, under an empty cell: a spacer's is empty too.
            found = [at for at, tail in enumerate(tails) if tail == column.label and (column.label or at == 0)]
            if len(found) != 1:
                msg = f"the header {list(header)!r} has no one {column.label!r} column"
                raise UnknownLayoutError(msg)
            places[found[0]] = column
        return dict(sorted(places.items()))

    def _key_texts(
        self, headings: Sequence[str], keys: Sequence[Column], read: ReadContext
    ) -> list[tuple[str | None, ...]]:
        """Return each heading's key texts, one per key field; a required key that one names none of raises."""
        matches = [typing.cast("re.Match[str]", self.keys.fullmatch(heading)) for heading in headings]
        texts: list[tuple[str | None, ...]]
        if self.key_count == 1:
            texts = [(self.key(match),) for match in matches]
        else:
            texts = [
                tuple((match[key.field] or None) if key.field in self.keys.groupindex else str(place) for key in keys)
                for place, match in enumerate(matches, 1)
            ]
        for at, key in enumerate(keys):
            if key.field in read.required and (
                empty := [h for h, t in zip(headings, texts, strict=True) if t[at] is None]
            ):
                msg = f"the headers {empty!r} name no {key.field}, which every row holds"
                raise UnknownLayoutError(msg)
        return texts

    def _long(  # noqa: PLR0913 (the table and how its columns lay out)
        self,
        table: Table,
        *,
        record: Sequence[tuple[str, str]],
        besides: Mapping[str, str],
        fields: Sequence[Column],
        keyed: Sequence[str],
        texts: Sequence[tuple[str | None, ...]],
        keys: Sequence[Column],
        value: Column,
    ) -> Table:
        """Return *table*'s cells under the *keyed* columns read long, a row per cell, record by record."""
        by_label = {label: name for name, label in record} | dict(besides)
        index = [by_label[column.label] for column in fields if column.label in by_label]
        lacked = (pl.String, pl.String, pl.Float64, pl.Datetime("us"))

        def long(
            frame: pl.DataFrame | None, kind: pl.DataType | type[pl.DataType], *, text: bool
        ) -> pl.DataFrame | None:
            if frame is None:
                return None
            if not keyed:
                cells = (
                    frame.select(index)
                    .clear()
                    .with_columns(pl.lit(None, kind).alias("_key"), pl.lit(None, kind).alias(value.label))
                )
            else:
                cells = frame.with_row_index("_row").unpivot(
                    index=["_row", *index], on=list(keyed), variable_name="_key", value_name=value.label
                )
                # unpivot writes key by key; row r's key j is at j * height + r, so rows come record by record.
                height, at = frame.height, pl.int_range(frame.height * len(keyed))
                cells = cells.select(pl.all().gather((at % len(keyed)) * height + at // len(keyed)))
            out = [
                pl.col(by_label[column.label]).alias(column.label)
                if column.label in by_label
                else pl.lit(None, kind).alias(column.label)
                for column in fields
            ]
            for place, key in enumerate(keys):
                named = {name: text_of[place] for name, text_of in zip(keyed, texts, strict=True)}
                key_text = (
                    pl.col("_key").replace_strict(named, return_dtype=pl.String) if keyed else pl.lit(None, pl.String)
                )
                out.append(key_text.alias(key.label) if text else pl.lit(None, kind).alias(key.label))
            return cells.select(*(["_row"] if keyed else []), *out, value.label)

        frames = [
            long(frame, kind, text=at in (0, 1))
            for at, (frame, kind) in enumerate(zip(table.frames(), lacked, strict=True))
        ]
        cells = typing.cast("pl.DataFrame", frames[0])
        if frames[1] is not None:  # a key is text, whatever kind its column's cells are
            frames[1] = frames[1].with_columns(pl.lit("text").alias(key.label) for key in keys)
        keep = pl.repeat(True, cells.height, dtype=pl.Boolean, eager=True)  # noqa: FBT003 (a mask's value)
        if self.empty_cells == "drop":
            keep = cells[value.label].str.strip_chars().fill_null("") != ""
        numbers = table.numbers.gather(cells.filter(keep)["_row"]) if keyed else table.numbers.clear()
        table = dataclasses.replace(table, written=frozenset(key.label for key in keys))
        frames = [None if f is None else f.filter(keep).drop("_row", strict=False) for f in frames]
        return table.with_frames(frames, numbers)


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
