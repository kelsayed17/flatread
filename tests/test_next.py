"""flatread.next, the 8.0 prototype: a reader is a row struct and a pipeline of stage values."""

import dataclasses
import datetime as dt
import io
import re
import typing
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import msgspec
import polars as pl
import pytest

from flatread import next as fr
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
from flatread.fields import Cell as At
from flatread.fields import Flag, Format, Joined, Label, Span
from flatread.fields import Shape as Shaped
from flatread.next._stages import _breaks_as_polars
from flatread.pdf import PdfLine
from tests.support.pdf import pdf
from tests.support.xlsx import Cell, workbook
from tests.test_hardening_archives import _HOSTILE

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class _Price(msgspec.Struct, frozen=True):
    symbol: str
    close: float
    day: dt.date


_PRICES = fr.Reader(_Price, fr.Csv())


def test_a_csv_s_header_names_each_field_by_its_own_name_and_a_bare_date_is_iso_8601() -> None:
    assert _PRICES.rows(b"symbol,close,day\nACME,1.5,2026-11-16\n") == [_Price("ACME", 1.5, dt.date(2026, 11, 16))]


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (b"", EmptyBodyError),
        (b"symbol,close,day\nACME,1.5\n", RaggedRowError),
        (b"symbol,close,day\nACME,1.5,2026-11-16\n\nBETA,2,2026-11-16\n", RaggedRowError),
        (b"symbol,close,day\nACME,x,2026-11-16\n", ReadError),
        (b"symbol,close,day\nACME,1.5,11/16/2026\n", ReadError),
        (b'symbol,close,day\n"ACME,1.5,2026-11-16\n', TruncatedFileError),
        (b"\nsymbol,close,day\n", UnknownLayoutError),
    ],
)
def test_a_csv_that_is_not_its_table_raises(body: bytes, error: type[ReadError]) -> None:
    with pytest.raises(error):
        _PRICES.frame(body)


@pytest.mark.parametrize("separator", ["", "||", "\n"])
def test_a_csv_separator_that_is_not_one_character_is_refused_when_built(separator: str) -> None:
    with pytest.raises(LayoutError, match="separator"):
        fr.Csv(separator=separator)


def test_a_misspelled_stage_setting_is_a_type_error_at_the_call() -> None:
    with pytest.raises(TypeError):
        fr.Csv(sep="|")  # ty: ignore[unknown-argument]


class _Stamped(msgspec.Struct, frozen=True):
    symbol: str
    stamp: Annotated[dt.date, Format("%Y%m%d")]


def test_a_trailer_states_a_field_on_every_row_and_counts_them() -> None:
    reader = fr.Reader(
        _Stamped, fr.Csv(separator="|"), table=fr.Trailer(re.compile(r"(?P<stamp>\d{8})\|(?P<count>\d+)"))
    )
    assert reader.rows(b"symbol\nACME\nBETA\n20261116|2\n") == [
        _Stamped("ACME", dt.date(2026, 11, 16)),
        _Stamped("BETA", dt.date(2026, 11, 16)),
    ]
    assert reader.rows(b"symbol\n20261116|0\n") == []  # the trailer vouches for a table of no rows
    with pytest.raises(ControlTotalError):
        reader.frame(b"symbol\nACME\n20261116|2\n")
    with pytest.raises(TruncatedFileError):
        reader.frame(b"symbol\nACME\nBETA\n")


def test_one_titled_finder_serves_a_csv_and_a_sheet() -> None:
    titled = fr.Titled([re.compile(r"Prices as of (?P<stamp>\d{8})"), re.compile("")])
    csv = fr.Reader(_Stamped, fr.Csv(), table=titled)
    sheet = fr.Reader(_Stamped, fr.Sheet(), table=titled)
    expected = [_Stamped("ACME", dt.date(2026, 11, 16))]
    assert csv.rows(b"Prices as of 20261116\n\nsymbol\nACME\n") == expected
    assert sheet.rows(workbook({"S": [["Prices as of 20261116"], [None], ["symbol"], ["ACME"]]})) == expected
    with pytest.raises(UnknownLayoutError, match="title"):
        csv.frame(b"Prices on 20261116\n\nsymbol\nACME\n")


class _Release(msgspec.Struct, frozen=True):
    period: Annotated[dt.date, Label("Date"), Format("%Y:Q%q")]
    first: Annotated[float | None, Label("First")]


def test_a_sheet_reads_its_cells_as_text_with_every_digit_and_refuses_an_empty_row_inside() -> None:
    reader = fr.Reader(_Release, fr.Sheet(sheet="DATA"), null_values=["#N/A"])
    body = workbook({"DATA": [["Date", "First"], ["2026:Q1", 0.1 + 0.2], ["2026:Q2", "#N/A"]]})
    assert reader.rows(body) == [_Release(dt.date(2026, 1, 1), 0.1 + 0.2), _Release(dt.date(2026, 4, 1), None)]
    with pytest.raises(ReadError, match="empty"):
        reader.frame(workbook({"DATA": [["Date", "First"], ["2026:Q1", 1.0], [None, None], ["2026:Q2", 2.0]]}))
    with pytest.raises(RaggedRowError):
        reader.frame(workbook({"DATA": [["Date", "First"], ["2026:Q1", 1.0, "stray"]]}))


class _Reading(msgspec.Struct, frozen=True):
    station: Annotated[str, Label("")]
    month: str
    value: float | None


def test_a_wide_table_read_long_and_its_cells_cleaned_in_order() -> None:
    reader = fr.Reader(
        _Reading,
        fr.Csv(),
        shape=fr.Long(keys=re.compile(r"[A-Z][a-z]{2}"), empty_cells="drop"),
        cells=[fr.Padded()],
    )
    assert reader.rows(b",Jan,Feb\nA, 1.5 ,\nB,2, 3\n") == [
        _Reading("A", "Jan", 1.5),
        _Reading("B", "Jan", 2.0),
        _Reading("B", "Feb", 3.0),
    ]
    with pytest.raises(UnknownLayoutError, match="key"):
        reader.frame(b",Jan,Total\nA,1,1\n")


class _Bounded(msgspec.Struct, frozen=True):
    symbol: Annotated[str, msgspec.Meta(pattern=r"^[A-Z]+$", max_length=5)]
    close: Annotated[float, msgspec.Meta(ge=0)]


@pytest.mark.parametrize(("row", "words"), [("ACME,-1", "at least 0"), ("acme,1", "matching"), ("ACMEXY,1", "long")])
def test_a_msgspec_meta_is_honored_naming_the_row_by_its_line(row: str, words: str) -> None:
    reader = fr.Reader(_Bounded, fr.Csv())
    assert reader.rows(b"symbol,close\nACME,0\n") == [_Bounded("ACME", 0.0)]
    with pytest.raises(ReadError, match=f"{words}.*: row 3"):
        reader.frame(f"symbol,close\nACME,1\n{row}\n".encode())


class _Trade(msgspec.Struct, frozen=True):
    asset: Annotated[str, Label("Asset")]
    shares: Annotated[int, Label("Shares")]
    note: str | None = None


class _Trades:
    """A custom stage: the records of a PDF's table, by its columns' places under its header line."""

    def records(
        self, lines: Sequence[PdfLine], layout: fr.Layout | None, read: fr.ReadContext
    ) -> list[Mapping[str, str]]:
        del read
        assert layout is not None
        edges = layout.edges(lines[0], ["Asset", "Shares"])
        assert edges is not None
        rows = []
        for run in layout.runs(lines[1:]):
            asset, shares = layout.cells(run, edges)
            rows.append({"asset": asset, "shares": shares})
        return rows


def test_a_pdf_s_records_come_from_a_custom_stage_with_the_library_s_layout() -> None:
    reader = fr.Reader(_Trade, fr.Pdf(_Trades(), layout=fr.Layout(row_gap=14.0)), empty=True)
    header = [(72, 720, "Asset"), (300, 720, "Shares")]
    body = pdf([*header, (72, 690, "ACME Corp"), (300, 690, "100"), (72, 660, "BETA"), (300, 660, "7")])
    assert reader.rows(body) == [_Trade("ACME Corp", 100), _Trade("BETA", 7)]
    with pytest.raises(LayoutError, match="names fields"):
        fr.Reader(_Trade, fr.Pdf(_Wrong(), layout=fr.Layout(row_gap=14.0))).frame(body)


class _Wrong:
    def records(
        self, lines: Sequence[PdfLine], layout: fr.Layout | None, read: fr.ReadContext
    ) -> list[Mapping[str, str]]:
        del lines, layout, read
        return [{"asset": "A", "shares": "1", "price": "2"}]


def test_a_reader_holds_no_read_s_state() -> None:
    reader = fr.Reader(_Stamped, fr.Csv(separator="|"), table=fr.Trailer(re.compile(r"(?P<stamp>\d{8})")))
    _, first = reader.read(b"symbol\nACME\n20261116\n")
    _, second = reader.read(b"symbol\nBETA\n20261117\n")
    assert first.stated == {"stamp": "20261116"}
    assert second.stated == {"stamp": "20261117"}


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (b"symbol,close,day\nACME,1.5,2026-11-16", TruncatedFileError),
        (b"symbol,close,day\n\xffACME,1.5,2026-11-16\n", EncodingError),
        (b'symbol,close,day\n"AC"ME,1.5,2026-11-16\n', ReadError),
        (b"symbol,cl\x00ose,day\n", UnknownLayoutError),
    ],
)
def test_a_csv_that_ends_otherwise_is_not_utf_8_or_misquotes_raises(body: bytes, error: type[ReadError]) -> None:
    with pytest.raises(error):
        fr.Reader(_Price, fr.Csv(final_line_ending=True), empty=True).frame(body)


def test_a_csv_quote_that_is_its_separator_is_refused_when_built() -> None:
    with pytest.raises(LayoutError, match="its quote too"):
        fr.Csv(separator="'", quote="'")


def test_a_repeated_heading_is_a_column_of_its_own_and_so_not_the_row_s() -> None:
    class Pair(msgspec.Struct, frozen=True):
        rate: Annotated[str, Label("Rate")]

    with pytest.raises(UnknownLayoutError, match="is not"):
        fr.Reader(Pair, fr.Csv()).frame(b"Rate,Rate\n1,2\n")


def test_titled_names_titles_and_counts_rows() -> None:
    with pytest.raises(LayoutError, match="no titles"):
        fr.Titled([])
    reader = fr.Reader(_Price, fr.Csv(), table=fr.Titled([re.compile(r"(?P<count>\d+) rows")]))
    assert reader.rows(b"1 rows\nsymbol,close,day\nACME,1,2026-11-16\n") == [_Price("ACME", 1.0, dt.date(2026, 11, 16))]
    with pytest.raises(ControlTotalError):
        reader.frame(b"2 rows\nsymbol,close,day\nACME,1,2026-11-16\n")
    with pytest.raises(UnknownLayoutError, match="titles and a header"):
        reader.frame(b"1 rows\n")


def test_a_sheet_with_no_cells_is_empty() -> None:
    with pytest.raises(EmptyBodyError):
        fr.Reader(_Release, fr.Sheet()).frame(workbook({"S": []}))


def test_a_long_table_s_record_and_keys_are_checked() -> None:
    reader = fr.Reader(_Reading, fr.Csv(), shape=fr.Long(keys=re.compile(r"[A-Z][a-z]{2}"), key=lambda m: m[0][:2]))
    assert reader.rows(b",Jan,Feb\nA,1,\n") == [_Reading("A", "Ja", 1.0), _Reading("A", "Fe", None)]
    with pytest.raises(UnknownLayoutError, match="twice"):
        reader.frame(b",Jan,Jab\nA,1,2\n")
    with pytest.raises(UnknownLayoutError, match="opens"):
        reader.frame(b"station,Jan\nA,1\n")


def test_a_layout_s_cells_and_rows() -> None:
    layout = fr.Layout(row_gap=14.0, slack=1.0)
    header = PdfLine(0, 700.0, tuple(zip([10.0, 11.0, 12.0, 30.0, 31.0], "As By", strict=True)))
    assert layout.edges(header, ["As", "By"]) == [10.0, 30.0]
    assert layout.edges(header, ["As", "Cz"]) is None
    row = PdfLine(0, 690.0, ((5.0, "x"), (29.5, "y"), (40.0, " ")))
    below, next_page = PdfLine(0, 600.0, ((10.0, "z"),)), PdfLine(1, 690.0, ((10.0, "w"),))
    assert layout.cells([row, PdfLine(0, 689.0, ())], [10.0, 30.0]) == ["x", "y"]
    assert layout.runs([header, row, below, next_page]) == [[header, row], [below], [next_page]]


def test_a_pdf_read_as_drawn_without_a_layout() -> None:
    class Line(msgspec.Struct, frozen=True):
        text: str

    class Lines:
        def records(
            self, lines: Sequence[PdfLine], layout: fr.Layout | None, read: fr.ReadContext
        ) -> list[Mapping[str, str]]:
            del read
            assert layout is None
            return [{"text": line.words} for line in lines]

    assert fr.Reader(Line, fr.Pdf(Lines())).rows(pdf(["one", "two"])) == [Line("one"), Line("two")]


class _Observation(msgspec.Struct, frozen=True):
    series_name: str
    time_period: Annotated[dt.date, Format("%Y-%m")]
    obs_value: float | None
    obs_status: str


_M = b'xmlns:message="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message"'
_X = b'xmlns:common="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/common" xmlns:x="urn:x"'


def _message(*obs: bytes, name: bytes = b'SERIES_NAME="S1"') -> bytes:
    return (
        b"<message:MessageGroup " + _M + b" " + _X + b'><x:DataSet id="D" xmlns:k="urn:k">'
        b"<k:Series " + name + b">" + b"".join(obs) + b"</k:Series></x:DataSet></message:MessageGroup>"
    )


def _obs(period: bytes = b"2026-01", value: bytes = b"1.5", status: bytes = b"A") -> bytes:
    return b'<x:Obs TIME_PERIOD="' + period + b'" OBS_VALUE="' + value + b'" OBS_STATUS="' + status + b'"/>'


_SDMX = fr.Reader(
    _Observation,
    fr.Sdmx(
        data_set="D",
        root="message:MessageGroup",
        data_set_element="x:DataSet",
        observation="x:Obs",
        annotations="x:Annotations",
        prefixes={"x": "urn:x"},
    ),
    shape=fr.Observations(statuses=("A", "M"), absent=("",), date_format="%Y-%m"),
)


def test_an_sdmx_data_set_s_observations_with_their_status() -> None:
    assert _SDMX.rows(_message(_obs(), _obs(b"2026-02", b"", b"M"))) == [
        _Observation("S1", dt.date(2026, 1, 1), 1.5, "A"),
        _Observation("S1", dt.date(2026, 2, 1), None, "M"),
    ]


@pytest.mark.parametrize(
    ("body", "words"),
    [
        (_message(_obs(status=b"X")), "status"),
        (_message(_obs(period=b"2026-1")), "a day written"),
        (_message(_obs(value=b"1e5")), "not a number"),
        (_message(_obs(value=b"1", status=b"M")), "no-value marker"),
        (_message(_obs(), name=b'NAME="S1"'), "carries no SERIES_NAME"),
        (_message(b'<x:Obs TIME_PERIOD="2026-01" OBS_STATUS="A"/>'), "carries other than"),
    ],
)
def test_an_sdmx_observation_out_of_its_form_raises(body: bytes, words: str) -> None:
    with pytest.raises(ReadError, match=words):
        _SDMX.frame(body)


def test_a_derived_format_reads_another_reader_s_frame_and_its_context() -> None:
    inner = fr.Reader(_Stamped, fr.Csv(separator="|"), table=fr.Trailer(re.compile(r"(?P<stamp>\d{8})")))

    class Count(msgspec.Struct, frozen=True):
        rows: int
        stamp: str

    def shape(frame: pl.DataFrame, read: fr.ReadContext) -> pl.DataFrame:
        return pl.DataFrame({"rows": [str(frame.height)], "stamp": [read.matches["trailer"][0]]})

    reader = fr.Reader(Count, fr.Derived(inner, shape))
    assert reader.rows(b"symbol\nACME\nBETA\n20261116\n") == [Count(2, "20261116")]


class _Security(msgspec.Struct, frozen=True):
    cusip: str
    shares: int
    held: bool
    since: dt.date | None


_SECURITIES = fr.Reader(_Security, fr.Sheet())


def test_a_sheet_s_cells_are_read_as_the_kind_the_workbook_stored() -> None:
    body = workbook({"S": [["cusip", "shares", "held", "since"], ["037833100", 7, True, dt.date(2026, 1, 2)]]})
    assert _SECURITIES.rows(body) == [_Security("037833100", 7, held=True, since=dt.date(2026, 1, 2))]
    text = workbook({"S": [["cusip", "shares", "held", "since"], ["037833100", 7, "false", "2026-01-02"]]})
    assert _SECURITIES.rows(text) == [_Security("037833100", 7, held=False, since=dt.date(2026, 1, 2))]


@pytest.mark.parametrize(
    ("cells", "words"),
    [
        ([37833100, 7, True, None], "not a text cell: mark the field AsWritten"),
        (["037833100", "7", True, None], "not a number cell"),
        (["037833100", 7.5, True, None], "not a whole number"),
        (["037833100", 7, 1, None], "not a boolean or text cell"),
        (["037833100", 7, True, 46000], "not a date cell or text"),
    ],
)
def test_a_sheet_s_cell_of_another_kind_than_its_field_reads_is_refused(cells: list[Cell], words: str) -> None:
    body = workbook({"S": [["cusip", "shares", "held", "since"], cells]})
    with pytest.raises(ReadError, match=f"{words}.*: row 2"):
        _SECURITIES.frame(body)


def test_a_number_cell_reads_as_text_where_the_field_is_as_written() -> None:
    class Code(msgspec.Struct, frozen=True):
        code: Annotated[str, fr.AsWritten()]

    assert fr.Reader(Code, fr.Sheet()).rows(workbook({"S": [["code"], [12345], ["A1"]]})) == [Code("12345"), Code("A1")]


def test_a_sheet_s_moment_cell_is_its_value_and_its_text_is_read_by_its_format() -> None:
    class Stamp(msgspec.Struct, frozen=True):
        at: Annotated[dt.datetime, Format("%Y-%m-%d %H:%M")]
        flag: Annotated[bool, Flag("Y", "N")]

    body = workbook({"S": [["at", "flag"], [dt.date(2026, 1, 2), "Y"], ["2026-01-03 09:30", True]]})
    assert fr.Reader(Stamp, fr.Sheet()).rows(body) == [
        Stamp(dt.datetime(2026, 1, 2), flag=True),
        Stamp(dt.datetime(2026, 1, 3, 9, 30), flag=True),
    ]


def test_a_sheet_s_header_runs_to_its_last_filled_cell() -> None:
    with pytest.raises(EmptyBodyError):
        _SECURITIES.frame(workbook({"S": [["cusip", "shares", "held", "since"]]}))
    assert fr.Reader(_Release, fr.Sheet()).rows(workbook({"S": [["Date", "First", None], ["2026:Q1", 1.5]]})) == [
        _Release(dt.date(2026, 1, 1), 1.5)
    ]


def _zipped(members: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return out.getvalue()


def test_a_zip_source_streams_its_member_to_the_format_and_notes_its_name() -> None:
    reader = fr.Reader(_Price, fr.Csv(), source=fr.Zip(re.compile(r"prices_\d+\.csv")))
    body = _zipped({"readme.txt": b"x", "prices_1.csv": b"symbol,close,day\nACME,1.5,2026-11-16\n"})
    frame, read = reader.read(body)
    assert frame.height == 1
    assert read.member == "prices_1.csv"
    with pytest.raises(UnknownLayoutError, match="0 members"):
        reader.frame(_zipped({"readme.txt": b"x"}))
    with pytest.raises(CorruptArchiveError):
        reader.frame(b"not a zip")


def test_an_sdmx_message_streams_from_a_zip_and_its_faults_are_named() -> None:
    reader = fr.Reader(
        _Observation,
        _SDMX.format,
        shape=_SDMX.shape,
        source=fr.Zip(re.compile(r".*\.xml")),
    )
    assert reader.rows(_zipped({"D.xml": _message(_obs())})) == [_Observation("S1", dt.date(2026, 1, 1), 1.5, "A")]
    with pytest.raises(TruncatedFileError):
        reader.frame(_zipped({"D.xml": _message(_obs())[:-40]}))
    with pytest.raises(UnknownLayoutError, match="holds no D data set"):
        reader.frame(_zipped({"D.xml": _message(_obs()).replace(b'id="D"', b'id="E"')}))
    with pytest.raises(LayoutError, match="prefix"):
        fr.Sdmx(
            data_set="D",
            root="message:MessageGroup",
            data_set_element="y:DataSet",
            observation="x:Obs",
            annotations="x:A",
        )


def test_a_zip_member_too_large_or_that_will_not_inflate_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = fr.Reader(_Price, fr.Csv(), source=fr.Zip(re.compile(r"x\.csv")))
    for body in _HOSTILE.values():
        with pytest.raises(ReadError):
            reader.frame(body)
    monkeypatch.setattr("flatread.next._stages._INFLATED_MAX", 4)
    with pytest.raises(ReadError, match="inflates to"):
        reader.frame(_zipped({"x.csv": b"symbol,close,day\n"}))


_ROOT = b"<message:MessageGroup " + _M + b" " + _X + b">"
_SERIES = b'<k:Series SERIES_NAME="S1">' + _obs() + b"</k:Series>"


def _sets(inside: bytes) -> bytes:
    return _ROOT + inside + b"</message:MessageGroup>"


def _set(name: bytes, inside: bytes) -> bytes:
    return b'<x:DataSet id="' + name + b'" xmlns:k="urn:k">' + inside + b"</x:DataSet>"


def test_an_sdmx_message_reads_its_data_set_among_others() -> None:
    body = _sets(_set(b"E", _SERIES) + _set(b"D", _SERIES))
    assert _SDMX.rows(body) == [_Observation("S1", dt.date(2026, 1, 1), 1.5, "A")]


@pytest.mark.parametrize(
    ("body", "error", "words"),
    [
        (_message(_obs()).replace(b"message:MessageGroup", b"message:Other"), UnknownLayoutError, "the root of"),
        (_sets(b'<k:Series xmlns:k="urn:k"/>' + _set(b"D", _SERIES)), UnknownLayoutError, "outside any"),
        (_sets(_set(b"D", _SERIES.replace(b"</k:Series>", b"<x:Junk/></k:Series>"))), UnknownLayoutError, "other"),
        (_sets(_set(b"D", _SERIES) + b"<x:Junk/>"), UnknownLayoutError, "after its last"),
        (_sets(_set(b"D", b"")), ReadError, "holds no series"),
    ],
)
def test_an_sdmx_message_out_of_its_layout_raises(body: bytes, error: type[ReadError], words: str) -> None:
    with pytest.raises(error, match=words):
        _SDMX.frame(body)


def test_a_field_s_marker_the_prototype_lacks_or_a_format_it_needs_is_refused_when_built() -> None:
    class Keyworded(msgspec.Struct, frozen=True, kw_only=True):
        a: str

    class Spanned(msgspec.Struct, frozen=True):
        a: Annotated[str, Span(0, 2)]

    class Joins(msgspec.Struct, frozen=True):
        a: Annotated[str, Joined("a")]

    class Addressed(msgspec.Struct, frozen=True):
        a: Annotated[str, At("A1")]

    class Aliased(msgspec.Struct, frozen=True):
        a: Annotated[str, Label("a", alternatives=("b",))]

    class Mixed(msgspec.Struct, frozen=True):
        a: Literal[1, "b"]

    class Timed(msgspec.Struct, frozen=True):
        a: dt.datetime

    for row, words in [
        (Keyworded, "keyword-only"),
        (Spanned, "Span"),
        (Joins, "Joined"),
        (Addressed, "Cell"),
        (Aliased, "alternatives"),
        (Mixed, "the field 'a'"),
        (Timed, "needs the Format"),
    ]:
        with pytest.raises(LayoutError, match=words):
            fr.Reader(row, fr.Csv())
    with pytest.raises(LayoutError, match="no fields"):
        fr.Reader(_Price, fr.Csv(), as_written=["nope"])


class _Checked(msgspec.Struct, frozen=True):
    code: Annotated[str, Shaped(r"[A-Z]+")]
    side: Literal["buy", "sell"]
    size: float
    lots: int = 5


_CHECKED = fr.Reader(_Checked, fr.Csv())


@pytest.mark.parametrize(
    ("row", "words"),
    [
        ("ab,buy,1,", "shape"),
        ("AB,hold,1,", "one of its values"),
        ("AB,buy,nan,", "NaN or an infinity"),
        ("AB,buy,,", "an empty cell"),
    ],
)
def test_a_cell_out_of_its_field_s_bounds_raises_naming_its_row(row: str, words: str) -> None:
    with pytest.raises(ReadError, match=f"{words}.*row 2"):
        _CHECKED.frame(f"code,side,size,lots\n{row}\n".encode())


def test_a_field_s_default_fills_its_empty_cell_and_as_written_keeps_its_text() -> None:
    assert _CHECKED.rows(b"code,side,size,lots\nAB,buy,1,\n") == [_Checked("AB", "buy", 1.0)]
    reader = fr.Reader(_Price, fr.Csv(), as_written=["symbol"])
    assert reader.rows(b"symbol,close,day\n ACME ,1,2026-11-16\n")[0].symbol == " ACME "


def test_the_engine_refuses_a_frame_it_typed_otherwise_than_its_row(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = fr.Reader(_Price, fr.Csv())
    monkeypatch.setitem(reader.engine.dtypes, "close", pl.Int64)
    with pytest.raises(LayoutError, match="the engine typed"):
        reader.frame(b"symbol,close,day\nACME,1,2026-11-16\n")


def test_a_rust_panic_is_a_read_error_and_any_other_base_exception_passes() -> None:
    class PanicException(BaseException):
        """A stand-in for pyo3's, matched by its name."""

    @dataclasses.dataclass(frozen=True)
    class Raising:
        raised: BaseException

        def grid(self, stream: fr.Stream, read: fr.ReadContext) -> fr.Grid:
            del stream, read
            raise self.raised

    with pytest.raises(ReadError, match="panic"):
        fr.Reader(_Price, Raising(PanicException("index out of bounds"))).frame(b"")
    with pytest.raises(KeyboardInterrupt):
        fr.Reader(_Price, Raising(KeyboardInterrupt())).frame(b"")


@pytest.mark.parametrize("kind", ["float", "datetime"])
def test_a_sheet_s_loads_that_disagree_on_its_shape_raise(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    import fastexcel  # noqa: PLC0415 (the excel extra)

    load = fastexcel.ExcelReader.load_sheet

    def short(self: fastexcel.ExcelReader, *args: typing.Any, **kwargs: typing.Any) -> object:  # noqa: ANN401
        sheet = load(self, *args, **kwargs)
        dtypes = kwargs.get("dtypes")
        loads = dtypes == kind or (isinstance(dtypes, dict) and kind in dtypes.values())
        return _Short(sheet) if loads else sheet

    class _Short:
        def __init__(self, sheet: typing.Any) -> None:  # noqa: ANN401
            self.sheet = sheet

        def to_polars(self) -> pl.DataFrame:
            frame = self.sheet.to_polars()
            assert isinstance(frame, pl.DataFrame)
            return frame.head(0)

    monkeypatch.setattr(fastexcel.ExcelReader, "load_sheet", short)
    with pytest.raises(ReadError, match=f"{kind} load"):
        _SECURITIES.frame(workbook({"S": [["cusip", "shares", "held", "since"], ["a", 7, True, dt.date(2026, 1, 2)]]}))


def test_titled_states_its_titles_groups_and_a_long_sheet_s_keys_are_text() -> None:
    assert fr.Titled([re.compile(r"(?P<count>\d+) rows of (?P<day>.+)")]).stated == frozenset({"day"})
    reader = fr.Reader(_Reading, fr.Sheet(), shape=fr.Long(keys=re.compile(r"[A-Z][a-z]{2}"), empty_cells="drop"))
    assert reader.rows(workbook({"S": [[None, "Jan"], ["A", 1.5]]})) == [_Reading("A", "Jan", 1.5)]


@pytest.mark.parametrize(
    "text",
    [
        "a,b\n1,2\n",
        "a,b\r\n1,2\r\n3",
        "a,b\n\n1,2,3\n4\n",
        'a,b\n"x,y",z\n"q""r",\n',
        'a,b\nx"y,z\n',
        "a,b\n,\n ,  \n",
        "a\n1,2\n",
        'a,b\n"x\ny",z\n\n"p\r\nq"\n',
        'a\n"x\ny",z\n',
        'a,"b"\n"x"y,z\n',
        'a,b\n"x\n',
        "a,b\n1\x0c2\n",
        "a,b\n1\r2\n",
    ],
)
@pytest.mark.parametrize("fmt", [fr.Csv(), fr.Csv(separator="|", quote=None), fr.Csv(separator=";")])
def test_a_csv_s_every_reading_is_its_reading_by_csv(text: str, fmt: fr.Csv) -> None:
    """Each faster reading, where it reads a body, reads it as ``csv`` does; where ``csv`` refuses it, none reads it."""
    body = text.encode()
    try:
        by_csv = fmt._whole(text, body, parse=False)
    except ReadError:
        assert not isinstance(fmt._parsed(body, text), fr.Lines)
        assert fmt._split(text) is None
        assert fmt.quote is None or fmt.quote not in text or fmt._spanning(body, text) is None
        with pytest.raises(ReadError):
            fmt._whole(text, body)
        return
    spanning = fmt.quote is not None and fmt.quote in text and _breaks_as_polars(text)
    for grid in (
        fmt._parsed(body, text),
        fmt._split(text),
        fmt._spanning(body, text) if spanning else None,
        fmt._whole(text, body),
    ):
        if not isinstance(grid, fr.Lines):
            continue
        assert grid.cells.rows() == by_csv.cells.rows()
        for name in ("widths", "raw", "numbers"):
            assert getattr(grid, name).to_list() == getattr(by_csv, name).to_list(), name


def test_a_csv_the_parser_reads_otherwise_than_its_lines_is_read_by_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Where polars' parser reads other rows than the lines (never yet seen), the body is read as ``csv`` reads it."""
    read_csv = pl.read_csv

    def short(*args: typing.Any, **kwargs: typing.Any) -> pl.DataFrame:  # noqa: ANN401
        frame = read_csv(*args, **kwargs)
        return frame if kwargs.get("separator") == "\x00" else frame.head(-1)

    monkeypatch.setattr(pl, "read_csv", short)
    for text in ("a,b\n1,2\n", 'a,b\n"x\ny",z\n'):
        body = text.encode()
        by_csv = fr.Csv()._whole(text, body, parse=False)
        assert fr.Csv().grid(io.BytesIO(body), fr.ReadContext(body, row=_Price)).cells.rows() == by_csv.cells.rows()


@pytest.mark.parametrize("text", ['a,b\n"x\ny",z\n', "a,b\n1\x0c2\n", "a,b\n1\r2\n", 'a,b\n"x"y,z\n'])
def test_a_csv_a_cell_may_span_lines_in_is_read_whole(text: str) -> None:
    assert fr.Csv()._split(text) is None


def test_a_csv_s_dates_alone_booleans_and_as_written_text_under_a_cleaner() -> None:
    class Day(msgspec.Struct, frozen=True):
        day: dt.date

    assert fr.Reader(Day, fr.Csv()).rows(b"day\n2026-11-16\n") == [Day(dt.date(2026, 11, 16))]

    class Open(msgspec.Struct, frozen=True):
        name: str
        open: bool

    reader = fr.Reader(Open, fr.Csv(), cells=[fr.Padded()], as_written=["name"])
    assert reader.rows(b"name,open\n a ,true\nb, false\n") == [Open(" a ", open=True), Open("b", open=False)]
    with pytest.raises(ReadError, match="'yes' is not a true or false: row 2"):
        fr.Reader(Open, fr.Csv()).frame(b"name,open\na,yes\n")


def test_a_sheet_reads_a_huge_number_and_a_text_written_like_a_moment() -> None:
    class Mark(msgspec.Struct, frozen=True):
        size: float
        note: str
        at: dt.date | None

    body = workbook(
        {"S": [["size", "note", "at"], [-1e300, "2026-01-02 03:04:05", dt.date(2026, 1, 2)], [2, "x", None]]}
    )
    assert fr.Reader(Mark, fr.Sheet()).rows(body) == [
        Mark(-1e300, "2026-01-02 03:04:05", dt.date(2026, 1, 2)),
        Mark(2.0, "x", None),
    ]


def test_a_column_of_few_distinct_cells_is_typed_a_cell_once_and_a_bad_one_named_by_its_row() -> None:
    class Dated(msgspec.Struct, frozen=True):
        day: Annotated[dt.date | None, Format("%Y-%m-%d")]
        v: int

    reader = fr.Reader(Dated, fr.Csv())
    days = ["2026-11-16", "", "2026-11-16", "2026-11-16", "2026-11-17", "2026-11-16"]
    body = ("day,v\n" + "".join(f"{day},{n}\n" for n, day in enumerate(days))).encode()
    expected = [dt.date.fromisoformat(day) if day else None for day in days]
    assert [row.day for row in reader.rows(body)] == expected
    with pytest.raises(ReadError, match="row 6"):
        reader.frame(body.replace(b"2026-11-17", b"2026-13-17"))


class _Mixed(msgspec.Struct, frozen=True):
    s: str | None
    i: int | None
    f: float | None
    b: bool | None
    d: Annotated[dt.date | None, Format("%Y-%m-%d")]


_MIXED_BODIES = [
    b"s,i,f,b,d\na,1,1.5,true,2026-11-16\n",
    b"s,i,f,b,d\r\n,,,,\r\n  ,+3,.5,false,\r\n",
    b's,i,f,b,d\n"x,y",-2,1e-400,,\n"q""r",7,nan,,\n',
    b's,i,f,b,d\n"a\nb",1,2,true,2026-11-16\n',
    b's,i,f,b,d\na"b,1,2,true,\n',
    b'"s\nt",i,f,b,d\na,1,2,true,\n',
    b"s,i,f,b,d\na,x,2,true,\n",
    b"s,i,f,b,d\na,1,2,true\n",
    b"s,i,f,b,d\na,1,2,true,,\n",
    b"s,i,f,b,d\n\na,1,2,true,\n",
    b"s,i,f,b,d\nNA,NA,NA,NA,NA\n",
    b"s,i,f\n",
    b"s,i,f,b,d\na,1,2,True,\n",
    b"s,i,f,b,d\na,1,2,true,2026-13-01\n",
]


@pytest.mark.parametrize("body", _MIXED_BODIES)
@pytest.mark.parametrize("null_values", [(), ("NA",)])
def test_a_csv_s_typed_read_is_its_grid_s_reading(body: bytes, null_values: tuple[str, ...]) -> None:
    """A header and its rows read typed in one pass give the grid's frame, or its error, whichever path reads them."""
    direct = fr.Reader(_Mixed, fr.Csv(), null_values=null_values, empty=True)
    by_grid = fr.Reader(_Mixed, fr.Csv(), null_values=null_values, empty=True)
    by_grid._typed = None
    try:
        expected = by_grid.frame(body)
    except ReadError as e:
        with pytest.raises(type(e), match=re.escape(str(e))):
            direct.frame(body)
        return
    assert direct.frame(body).equals(expected)


def test_a_csv_s_typed_read_its_parser_reads_otherwise_is_left_to_the_grid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Where polars' typed read holds other rows than the lines (never yet seen), the grid reads the body."""
    read_csv = pl.read_csv

    def short(*args: typing.Any, **kwargs: typing.Any) -> pl.DataFrame:  # noqa: ANN401
        frame = read_csv(*args, **kwargs)
        return frame if "has_header" in kwargs else frame.head(-1)

    monkeypatch.setattr(pl, "read_csv", short)
    reader = fr.Reader(_Mixed, fr.Csv())
    assert reader._typed is not None
    assert fr.Csv().typed(_MIXED_BODIES[0], reader._typed, []) is None


class _Plain(msgspec.Struct, frozen=True):
    s: str | None
    i: int | None
    f: float | None
    b: bool | None


_HEADER: list[Cell] = ["s", "i", "f", "b"]

#: Each sheet's rows below the header, and whether its one load reads it: the grid's loads read every one the same.
_PLAIN_SHEETS: list[tuple[list[list[Cell]], bool]] = [
    ([["a", 1, 1.5, True], ["b", -2, 3, False]], True),
    ([["  ", 1, 1.5, True]], True),  # a cell of spaces among others, which the engine blanks
    ([["NA", 1, 1.5, True]], True),  # a word for none, which the engine reads as one where it is
    ([["a", 1.5, 1.5, True]], False),  # a number not whole in an integer's column
    ([["a", 2.0**63, 1.5, True]], False),  # a whole number past an integer's
    ([["a", 1, "x", True]], False),  # a text cell among numbers: kinds mixed
    ([["a", 1, 1.5, "true"]], False),  # a boolean's column of text
    ([[1, 1, 1.5, True]], False),  # a text's column of numbers
    ([["a", 1, 1.5, True], ["b", None, 2.5, False]], False),  # an empty cell
    ([["a", 1, 1.5, True], [None, None, None, None], ["b", 1, 1.5, True]], False),  # an empty row
    ([["a", 1, 1.5, True, "stray"]], False),  # a cell right of the header
    ([["a", 1, dt.date(2026, 11, 16), True]], False),  # a moment
    ([], False),  # a header and no rows
]


@pytest.mark.parametrize(("rows", "loaded"), _PLAIN_SHEETS)
@pytest.mark.parametrize("null_values", [(), ("NA",)])
def test_a_sheet_s_typed_read_is_its_grid_s_reading(
    rows: list[list[Cell]],
    loaded: bool,  # noqa: FBT001 (a parameter of the test's)
    null_values: tuple[str, ...],
) -> None:
    """A header and its full rows read typed in one load give the grid's frame, or its error, whichever reads them."""
    body = workbook({"S": [_HEADER, *rows]})
    direct = fr.Reader(_Plain, fr.Sheet(sheet="S"), null_values=null_values, empty=True)
    by_grid = fr.Reader(_Plain, fr.Sheet(sheet="S"), null_values=null_values, empty=True)
    by_grid._typed = None
    assert direct._typed is not None
    assert (fr.Sheet(sheet="S").typed(body, direct._typed, null_values) is not None) is loaded
    try:
        expected = by_grid.frame(body)
    except ReadError as e:
        with pytest.raises(type(e), match=re.escape(str(e))):
            direct.frame(body)
        return
    assert direct.frame(body).equals(expected)


def test_a_sheet_s_typed_read_of_text_alone_leaves_a_row_of_spaces_to_the_grid() -> None:
    """A row whose every cell is text of spaces is one the grid finds empty, and refuses: the one load leaves it."""

    class Text(msgspec.Struct, frozen=True):
        s: str | None
        t: str | None

    body = workbook({"S": [["s", "t"], ["a", "b"], [" ", ""]]})
    reader = fr.Reader(Text, fr.Sheet(sheet="S"))
    assert reader._typed is not None
    assert fr.Sheet(sheet="S").typed(body, reader._typed, []) is None
    with pytest.raises(ReadError, match="row 3 is empty"):
        reader.frame(body)
    assert fr.Sheet(sheet="S").typed(workbook({"S": [["s", "t"], ["a", " "]]}), reader._typed, []) is not None


def test_a_sheet_reads_by_its_grid_where_a_field_is_not_plain() -> None:
    """A date's field, or a flag's, is the grid's to read: its column's kind does not say what each cell holds."""

    class Dated(msgspec.Struct, frozen=True):
        s: str
        d: Annotated[dt.date, Format("%Y-%m-%d")]

    assert fr.Reader(Dated, fr.Sheet())._typed is None
    assert fr.Reader(_Plain, fr.Sheet(), as_written=["s"])._typed is None


def test_a_sheet_s_typed_read_raises_what_is_no_failure_of_fastexcel_s(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one load leaves fastexcel's failures to the grid's loads, and raises any other error, as they would."""
    import fastexcel  # noqa: PLC0415 (the excel extra)

    def broken(*args: typing.Any, **kwargs: typing.Any) -> typing.NoReturn:  # noqa: ANN401
        raise RuntimeError(args, kwargs)

    monkeypatch.setattr(fastexcel.ExcelReader, "load_sheet", broken)
    with pytest.raises(RuntimeError):
        fr.Sheet().typed(workbook({"S": [_HEADER, ["a", 1, 1.5, True]]}), {"s": pl.String}, [])


def test_an_xls_sheet_is_read_by_its_grid_where_its_one_load_finds_another_header() -> None:
    """An Excel 97 workbook, bounded as it opens: its one load finds another header, and its grid names the row."""

    class Note(msgspec.Struct, frozen=True):
        note: str

    body = (Path(__file__).parent / "fixtures" / "eia_ngsstats.xls").read_bytes()
    reader = fr.Reader(Note, fr.Sheet(sheet="ngsstats 2026 (2021-2025)"))
    assert reader._typed is not None
    assert fr.Sheet(sheet="ngsstats 2026 (2021-2025)").typed(body, reader._typed, []) is None
    with pytest.raises(RaggedRowError, match="row 2 holds a cell right of the header's last"):
        reader.frame(body)
