"""flatread.next, the preview: a reader is a row struct and a pipeline of stage values."""

import dataclasses
import datetime as dt
import io
import pickle
import re
import types
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
    assert (fr.Sheet(sheet="S").typed(body, direct._typed, null_values) is not None) is (loaded and not null_values)
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


def test_a_csv_s_rows_too_wide_for_one_pattern_are_left_to_the_grid() -> None:
    """A width whose row pattern is past the regex engine's size is the grid's to read: no count, and no error."""
    for width, rows in ((1_000, 1), (3_000, None)):
        assert fr.Csv()._rows_of('"x"' + ",x" * (width - 1) + "\n", '"', width) == rows


class TestWhereAReadWentWrong:
    """A flatread.next read's ``ReadError`` carries the row, the label and the member, as values beside its message."""

    def test_a_cell_names_its_row_and_label(self) -> None:
        with pytest.raises(ReadError) as caught:
            fr.Reader(_WhereDay, fr.Csv()).rows(b"Day\n01/02/2026\n1/2/2026\n")
        assert (caught.value.row, caught.value.label, caught.value.member) == (3, "Day", None)

    def test_an_empty_cell_names_its_row_and_label(self) -> None:
        with pytest.raises(ReadError) as caught:
            fr.Reader(_WhereDay, fr.Csv()).rows(b"Day\n01/02/2026\n\n")
        assert (caught.value.row, caught.value.label) == (3, "Day")

    def test_a_ragged_row_names_its_row(self) -> None:
        with pytest.raises(RaggedRowError) as caught:
            fr.Reader(_WhereDay, fr.Csv()).rows(b"Day\n01/02/2026,x\n")
        assert (caught.value.row, caught.value.label) == (2, None)

    def test_a_zip_members_cell_names_the_member(self) -> None:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr("days.csv", b"Day\n1/2/2026\n")
        with pytest.raises(ReadError) as caught:
            fr.Reader(_WhereDay, fr.Csv(), source=fr.Zip(re.compile(r".*\.csv"))).rows(out.getvalue())
        assert (caught.value.row, caught.value.label, caught.value.member) == (2, "Day", "days.csv")

    def test_the_values_survive_pickling(self) -> None:
        error = ReadError("x", row=2, label="Day", member="m")
        copied = pickle.loads(pickle.dumps(error))  # noqa: S301 (an error of its own)
        assert (str(copied), copied.row, copied.label, copied.member) == ("x", 2, "Day", "m")


class _WhereDay(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Label("Day"), Format("%m/%d/%Y")]


class TestFoundByProperties:
    """Defects tests/test_properties.py found, each kept as a case of its own."""

    def test_a_csv_cell_holding_a_vertical_tab_or_a_line_separator_is_one_cell(self) -> None:
        # str.splitlines breaks at both, which a CSV row runs on past; until 2026-11-18 the row read as ragged.
        reader = fr.Reader(_WhereName, fr.Csv())
        assert reader.rows("Name\na\x0bb\nc\u2028d\n".encode()) == [_WhereName("a\x0bb"), _WhereName("c\u2028d")]

    def test_a_csv_cell_holding_a_lone_cr_keeps_it(self) -> None:
        reader = fr.Reader(_WhereName, fr.Csv())
        assert reader.rows(b'Name\na\rb\n"c\rd"\n') == [_WhereName("a\rb"), _WhereName("c\rd")]
        with pytest.raises(ReadError) as caught:
            fr.Reader(_WhereDay, fr.Csv()).rows(b"Day\n1/2\r/2026\n")
        assert caught.value.row == 2

    def test_a_sheets_number_cell_at_2_to_the_53_is_no_int(self) -> None:
        class Count(msgspec.Struct, frozen=True, gc=False):
            count: Annotated[int, Label("Count")]

        reader = fr.Reader(Count, fr.Sheet(sheet="S"))
        assert reader.rows(workbook({"S": [["Count"], [2**53 - 1]]})) == [Count(2**53 - 1)]
        with pytest.raises(
            ReadError, match=r"not a whole number a number cell holds exactly \(below 2\*\*53\): row 2$"
        ):
            reader.rows(workbook({"S": [["Count"], [2**53 + 1]]}))


class _WhereName(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("Name")]


class TestThousandsZippedOrBareAndTranscoded:
    """Three stages the class-per-quirk readers had: ThousandsCsv, ZippedOrBare and Transcoded, as next's values."""

    class Volume(msgspec.Struct, frozen=True, gc=False):
        name: Annotated[str, Label("Name")]
        shares: Annotated[int | None, Label("Shares")]
        price: Annotated[float, Label("Price")]

    def test_thousands_reads_each_grouped_number_and_leaves_text(self) -> None:
        reader = fr.Reader(self.Volume, fr.Csv(separator="|"), cells=[fr.Thousands()])
        assert reader.rows(b"Name|Shares|Price\nA, Inc.|1,782,319|-1,234.5\nB||7\n") == [
            self.Volume("A, Inc.", 1782319, -1234.5),
            self.Volume("B", None, 7.0),
        ]

    @pytest.mark.parametrize(("cell", "what"), [("12,34", "whole number"), ("1,0000", "whole number")])
    def test_thousands_refuses_a_comma_out_of_place_naming_its_row(self, cell: str, what: str) -> None:
        reader = fr.Reader(self.Volume, fr.Csv(separator="|"), cells=[fr.Thousands()])
        with pytest.raises(ReadError, match=rf"^the 'Shares' cell '{cell}' is not a {what}: row 3$") as caught:
            reader.rows(f"Name|Shares|Price\nA|1|1\nB|{cell}|1\n".encode())
        assert (caught.value.row, caught.value.label) == (3, "Shares")

    def test_thousands_reads_as_thousands_csv_does(self) -> None:
        from flatread.delimited import ThousandsCsv  # noqa: PLC0415 (the 7.x reader it replaces)

        body = b"Name|Shares|Price\nA|1,000|2,000.25\nB|-12|3\n"
        assert fr.Reader(self.Volume, fr.Csv(separator="|"), cells=[fr.Thousands()]).rows(body) == ThousandsCsv(
            self.Volume, separator="|"
        ).rows(body)

    @staticmethod
    def _zip(name: str, data: bytes) -> bytes:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr(name, data)
        return out.getvalue()

    def test_a_bare_zip_source_reads_the_member_or_the_body(self) -> None:
        reader = fr.Reader(_WhereName, fr.Csv(), source=fr.Zip(re.compile(r".*\.csv"), bare=True))
        assert reader.rows(b"Name\na\n") == [_WhereName("a")]
        assert reader.rows(self._zip("x.csv", b"Name\nb\n")) == [_WhereName("b")]
        with pytest.raises(ReadError):  # a zip of no such member is read whole: no CSV
            reader.rows(self._zip("x.txt", b"Name\nc\n"))

    def test_a_bare_zip_source_refuses_a_zip_that_will_not_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def unopenable(*_: object) -> typing.NoReturn:
            msg = "a header zipfile cannot read"
            raise OSError(msg)

        monkeypatch.setattr(zipfile, "ZipFile", unopenable)
        reader = fr.Reader(_WhereName, fr.Csv(), source=fr.Zip(re.compile(r".*\.csv"), bare=True))
        with pytest.raises(
            CorruptArchiveError, match=r"^the body is a zip that will not open: a header zipfile cannot"
        ):
            reader.rows(b"PK\x03\x04")

    def test_transcoded_reads_a_legacy_encoding_and_drops_its_eof_byte(self) -> None:
        reader = fr.Reader(_WhereName, fr.Csv(), source=fr.Transcoded(["utf-8", "cp1252"], eof=b"\x1a"))
        assert reader.rows("Name\ncafé\n".encode("cp1252") + b"\x1a") == [_WhereName("café")]
        assert reader.rows("Name\ncafé\n".encode()) == [_WhereName("café")]

    def test_transcoded_reads_a_zips_member(self) -> None:
        source = fr.Transcoded(["cp1252"], source=fr.Zip(re.compile(r".*\.csv")))
        body = self._zip("x.csv", "Name\n€\n".encode("cp1252"))
        assert fr.Reader(_WhereName, fr.Csv(), source=source).rows(body) == [_WhereName("€")]

    def test_transcoded_refuses_what_no_encoding_reads_unless_lossy(self) -> None:
        with pytest.raises(EncodingError, match=r"^the body is not utf-8: "):
            fr.Reader(_WhereName, fr.Csv(), source=fr.Transcoded(["utf-8"])).rows(b"Name\n\xff\n")
        lossy = fr.Reader(_WhereName, fr.Csv(), source=fr.Transcoded(["utf-8"], lossy=True))
        assert lossy.rows(b"Name\n\xff\n") == [_WhereName("�")]

    @pytest.mark.parametrize("encodings", [[], "utf-8", ["no-such-codec"]])
    def test_transcoded_refuses_no_codecs_when_built(self, encodings: list[str] | str) -> None:
        with pytest.raises(LayoutError):
            fr.Transcoded(encodings)


class TestPreamble:
    """One table finder for a CSV's preamble and a sheet's (PreambleCsv, PreambleSheet): its header found."""

    class Count(msgspec.Struct, frozen=True, gc=False):
        name: Annotated[str, Label("Name")]
        n: Annotated[int, Label("N")]

    def test_a_csv_preamble_is_dropped_as_preamble_csv_drops_it(self) -> None:
        from flatread.delimited import PreambleCsv  # noqa: PLC0415 (the 7.x reader it replaces)

        body = b'Daily report\nSource: X\n\n"Name",N\na,1\nb,2\n'
        rows = fr.Reader(self.Count, fr.Csv(), table=fr.Preamble()).rows(body)
        assert rows == PreambleCsv(self.Count).rows(body) == [self.Count("a", 1), self.Count("b", 2)]

    def test_a_sheet_preamble_is_dropped_as_preamble_sheet_drops_it(self) -> None:
        from flatread.sheets import PreambleSheet  # noqa: PLC0415 (the 7.x reader it replaces)

        body = workbook({"S": [["Banner"], [None], ["Name", "N"], ["a", 1]]})
        rows = fr.Reader(self.Count, fr.Sheet(sheet="S"), table=fr.Preamble()).rows(body)
        assert rows == PreambleSheet(self.Count, sheet="S").rows(body) == [self.Count("a", 1)]

    def test_an_error_names_the_files_own_row(self) -> None:
        with pytest.raises(ReadError, match=r"^the 'N' cell 'x' is not a whole number: row 3$"):
            fr.Reader(self.Count, fr.Csv(), table=fr.Preamble()).rows(b"Title\nName,N\na,x\n")

    def test_a_label_of_its_own_finds_the_header(self) -> None:
        reader = fr.Reader(self.Count, fr.Csv(), table=fr.Preamble(label="Name"))
        assert reader.rows(b"Name of the file\nName,N\na,1\n") == [self.Count("a", 1)]

    @pytest.mark.parametrize("body", [b"Title\nNope,N\na,1\n", b"\nTitle\n"], ids=["no such row", "no header at all"])
    def test_a_grid_with_no_header_raises(self, body: bytes) -> None:
        with pytest.raises(UnknownLayoutError, match=r"^no row opens with the header's first label 'Name'$"):
            fr.Reader(self.Count, fr.Csv(), table=fr.Preamble()).rows(body)

    def test_a_preamble_opening_with_blank_lines_is_dropped(self) -> None:
        reader = fr.Reader(self.Count, fr.Csv(), table=fr.Preamble())
        assert reader.rows(b"\n\nTitle\n\nName,N\na,1\n") == [self.Count("a", 1)]

    def test_a_blank_line_where_the_header_stands_is_refused_by_its_row(self) -> None:
        with pytest.raises(UnknownLayoutError, match=r"^the body opens with a blank line, not its header$"):
            fr.Reader(self.Count, fr.Csv()).rows(b"\nName,N\na,1\n")
        reader = fr.Reader(self.Count, fr.Csv(), table=fr.Titled([re.compile("Report")]))
        with pytest.raises(UnknownLayoutError, match=r"^row 2 is blank, not the header$") as caught:
            reader.rows(b"Report\n\nName,N\na,1\n")
        assert caught.value.row == 2


class TestHeaderRules:
    """A finder's header rule: a file under several headers (HeadersCsv), or one named in any order (NamedCsv)."""

    class Holding(msgspec.Struct, frozen=True, gc=False):
        name: Annotated[str, Label("Name")]
        n: Annotated[int, Label("N")]
        shares: Annotated[int | None, Label("Shares")] = None

    @staticmethod
    def _published() -> fr.Reader[TestHeaderRules.Holding]:
        rule = fr.Headers(lacking=((), ("shares",)), blank=("Filler",), unread=("notes",))
        return fr.Reader(TestHeaderRules.Holding, fr.Csv(), table=fr.Header(header=rule))

    @staticmethod
    def _named(required: Sequence[str] = ()) -> fr.Reader[TestHeaderRules.Holding]:
        return fr.Reader(TestHeaderRules.Holding, fr.Csv(), table=fr.Header(header=fr.Named(required=required)))

    @pytest.mark.parametrize(
        "body",
        [b"Name,N,Shares\na,1,2\n", b"Name,N\na,1\n", b"Name,Filler,N,Filler\na,,1,\n", b"notes,Name,N\nhi,a,1\n"],
        ids=["whole", "lacking", "blank columns", "an unread column"],
    )
    def test_a_published_header_reads_as_headers_csv_reads_it(self, body: bytes) -> None:
        from flatread.delimited import HeadersCsv  # noqa: PLC0415 (the 7.x reader it replaces)

        class Published(HeadersCsv[TestHeaderRules.Holding]):
            _headers_lacking = ((), ("shares",))
            _blank_columns = ("Filler", "Filler")
            _unread = ("notes",)

        assert self._published().rows(body) == Published(self.Holding).rows(body)

    @pytest.mark.parametrize("body", [b"N,Name\n1,a\n", b"Name\na\n", b"Name,N,Shares,X\na,1,2,3\n"])
    def test_a_header_none_of_the_files_is_refused(self, body: bytes) -> None:
        with pytest.raises(UnknownLayoutError, match=r"^the header \[.*\] is none of the file's \[\['Name', 'N', 'S"):
            self._published().rows(body)

    @pytest.mark.parametrize("held", ["x", " "])
    def test_a_blank_column_holding_anything_is_refused_by_its_row(self, held: str) -> None:
        # A space too, as HeadersCsv refuses it: a blank column holds nothing.
        with pytest.raises(
            ReadError, match=rf"^the 'Filler' column holds '{held}', and it never holds a value: row 3$"
        ) as e:
            self._published().rows(f"Name,Filler,N\na,,1\nb,{held},2\n".encode())
        assert (e.value.row, e.value.label) == (3, "Filler")

    def test_a_header_the_file_is_not_published_under_is_refused_though_it_is_the_whole(self) -> None:
        reader = fr.Reader(self.Holding, fr.Csv(), table=fr.Header(header=fr.Headers(lacking=[("shares",)])))
        assert reader.rows(b"Name,N\na,1\n") == [self.Holding("a", 1)]
        with pytest.raises(UnknownLayoutError, match="is none of the file's"):
            reader.rows(b"Name,N,Shares\na,1,2\n")

    @pytest.mark.parametrize("body", [b"N,Name\n1,a\n", b"Shares,notes,N,Name\n3,hi,1,a\n", b"Name,N,Filler\na,1,\n"])
    def test_a_named_header_reads_as_named_csv_reads_it(self, body: bytes) -> None:
        from flatread.delimited import NamedCsv  # noqa: PLC0415 (the 7.x reader it replaces)

        class Named(NamedCsv[TestHeaderRules.Holding]):
            _blank_columns = ("Filler",)
            _unread = ("notes",)

        reader = fr.Reader(self.Holding, fr.Csv(), table=fr.Header(header=fr.Named(blank=["Filler"], unread=["notes"])))
        assert reader.rows(body) == Named(self.Holding).rows(body)

    @pytest.mark.parametrize(
        ("body", "words"),
        [
            (b"Name\na\n", r"lacks \['N'\], which every one names"),
            (b"Name,N,Bogus,Nope\na,1,2,3\n", r"names \['Bogus', 'Nope'\], which are no field's labels"),
            (b"Name,N,N\na,1,2\n", r"names \['N'\] twice"),
        ],
        ids=["a required column lacked", "a cell no label names", "a label twice"],
    )
    def test_a_named_header_is_refused_for(self, body: bytes, words: str) -> None:
        with pytest.raises(UnknownLayoutError, match=words):
            self._named().rows(body)

    def test_a_required_field_that_may_be_empty_is_in_every_header(self) -> None:
        reader = self._named(required=["shares"])
        assert reader.rows(b"Shares,N,Name\n,1,a\n") == [self.Holding("a", 1)]
        with pytest.raises(UnknownLayoutError, match=r"lacks \['Shares'\]"):
            reader.rows(b"N,Name\n1,a\n")

    def test_a_header_writes_a_label_as_its_alternative_or_folded(self) -> None:
        class Filed(msgspec.Struct, frozen=True, gc=False):
            number: Annotated[str, Label("file_number", alternatives=("file_no",))]
            form: Annotated[str, Label("form")]

        reader = fr.Reader(Filed, fr.Csv(), table=fr.Header(header=fr.Named(heading=str.lower)))
        assert reader.rows(b"FORM,File_No\n10-K,001\n") == [Filed("001", "10-K")]
        assert reader.rows(b"file_number,form\n002,8-K\n") == [Filed("002", "8-K")]

    def test_a_sheet_reads_under_either_rule(self) -> None:
        named = fr.Reader(self.Holding, fr.Sheet(sheet="S"), table=fr.Header(header=fr.Named()))
        assert named.rows(workbook({"S": [["Shares", "N", "Name"], [3, 1, "a"]]})) == [self.Holding("a", 1, 3)]
        published = fr.Reader(self.Holding, fr.Sheet(sheet="S"), table=fr.Header(header=fr.Headers([(), ("shares",)])))
        assert published.rows(workbook({"S": [["Name", "N"], ["a", 1]]})) == [self.Holding("a", 1)]

    def test_every_finder_takes_a_rule(self) -> None:
        rule = fr.Named()
        trailer = fr.Trailer(re.compile(r"(?P<count>\d+) rows"), header=rule)
        titled = fr.Titled([re.compile("Report")], header=rule)
        for table, body in [
            (fr.Preamble(label="N", header=rule), b"Title\nN,Name\n1,a\n"),
            (trailer, b"N,Name\n1,a\n1 rows\n"),
            (titled, b"Report\nN,Name\n1,a\n"),
        ]:
            assert fr.Reader(self.Holding, fr.Csv(), table=table).rows(body) == [self.Holding("a", 1)]

    def test_a_stated_field_is_in_no_header(self) -> None:
        class Stamped(msgspec.Struct, frozen=True, gc=False):
            name: Annotated[str, Label("Name")]
            on: str

        table = fr.Trailer(re.compile(r"as of (?P<on>\S+)"), header=fr.Headers())
        assert fr.Reader(Stamped, fr.Csv(), table=table).rows(b"Name\na\nas of 2026\n") == [Stamped("a", "2026")]
        named = fr.Trailer(re.compile(r"as of (?P<on>\S+)"), header=fr.Named())
        with pytest.raises(UnknownLayoutError, match=r"names \['on'\]"):
            fr.Reader(Stamped, fr.Csv(), table=named).rows(b"Name,on\na,x\nas of 2026\n")

    @pytest.mark.parametrize(
        ("rule", "words"),
        [
            (fr.Headers(lacking=()), "lacking is empty"),
            (fr.Headers(lacking=[("nope",)]), r"lack \['nope'\], which are no fields"),
            (fr.Headers(lacking=[("n",)]), r"lacking \['n'\] could never be read"),
            (fr.Named(required=["nope"]), r"required fields \['nope'\] are no fields"),
        ],
    )
    def test_a_rule_that_could_never_read_its_struct_is_refused_when_built(
        self, rule: fr.HeaderRule, words: str
    ) -> None:
        with pytest.raises(LayoutError, match=words):
            fr.Reader(self.Holding, fr.Csv(), table=fr.Header(header=rule))

    def test_a_word_two_fields_share_and_an_alternative_no_named_header_reads_are_refused_when_built(self) -> None:
        class Shared(msgspec.Struct, frozen=True):
            a: Annotated[str, Label("a", alternatives=("b",))]
            b: str

        with pytest.raises(LayoutError, match="two fields share a label"):
            fr.Reader(Shared, fr.Csv(), table=fr.Header(header=fr.Named()))
        with pytest.raises(LayoutError, match="only a header read by name takes"):
            fr.Reader(Shared, fr.Csv(), table=fr.Header(header=fr.Headers()))

    def test_a_rule_and_a_shape_are_refused_together(self) -> None:
        with pytest.raises(LayoutError, match="a reader takes one"):
            fr.Reader(
                self.Holding, fr.Csv(), table=fr.Header(header=fr.Named()), shape=fr.Long(keys=re.compile(r"\d+"))
            )


class TestCut:
    """One finder for an irregular grid, a CSV's or a sheet's (CsvText, SheetText and GridFile's pieces): cut."""

    class Count(msgspec.Struct, frozen=True, gc=False):
        name: Annotated[str, Label("Name")]
        n: Annotated[int | None, Label("N")]

    @staticmethod
    def _pieces() -> dict[str, tuple[object, fr.Cut]]:
        from flatread.delimited import CsvText  # noqa: PLC0415 (the 7.x readers it replaces)
        from flatread.grids import BesideGrid, BlankEnded, FilledHeader, TitledGrid  # noqa: PLC0415

        row = TestCut.Count

        class Blank(BlankEnded[row], CsvText[row]):
            pass

        class Titled(TitledGrid[row], CsvText[row]):
            _title = "Table 2"

        class Filled(FilledHeader[row], CsvText[row]):
            pass

        class Beside(BesideGrid[row], CsvText[row]):
            _band = (1, 3)

        return {
            "header": (CsvText(row), fr.Cut()),
            "header_row": (CsvText(row, header_row=3), fr.Cut(header_row=3)),
            "blank ended": (Blank(row), fr.Cut(ends="blank")),
            "titled": (Titled(row), fr.Cut(title="Table 2")),
            "filled": (Filled(row), fr.Cut(filled=(2, 3))),
            "beside": (Beside(row), fr.Cut(band=(1, 3))),
        }

    @pytest.mark.parametrize(
        ("piece", "body"),
        [
            ("header", b"Name,N\na,1\n,\nb,\n"),
            ("header", b"Name,N\na,1,\n"),
            ("header", b"Name,N,\na,1\n"),
            ("header", b"Name,N\na\n"),
            ("header", b"Name,N\n  ,  \na, 2\n"),
            ("header_row", b"Title\n\nName,N\na,1\n"),
            ("blank ended", b"Name,N\na,1\n\nNotes,here\n"),
            ("blank ended", b"Name,N\na,1\n,,\nx,2\n"),
            ("titled", b"Table 1\nX,Y\n1,2\n\nTable 2\n\nName,N\na,1\n"),
            ("filled", b"Title\nName,N\na,1\n"),
            ("beside", b"x,Name,N,y\n,a,1,z\n"),
        ],
    )
    def test_a_csv_grid_is_cut_as_7x_cuts_it(self, piece: str, body: bytes) -> None:
        old, cut = self._pieces()[piece]
        assert fr.Reader(self.Count, fr.Csv(), table=cut).rows(body) == old.rows(body)  # ty: ignore[unresolved-attribute] (a 7.x reader)

    def test_a_sheet_grid_is_cut_as_sheet_text_cuts_it_its_cells_kinds_kept(self) -> None:
        from flatread.sheets import SheetText  # noqa: PLC0415 (the 7.x reader it replaces)

        for rows in [
            [["T"], ["Name", "N"], ["a", 1], [None, None], ["b", 2.0]],
            [[None, "T"], [None, "Name", "N"], [None, "a", 3]],
        ]:
            body = workbook({"S": rows})
            reader = fr.Reader(self.Count, fr.Sheet(sheet="S"), table=fr.Cut(header_row=2))
            assert reader.rows(body) == SheetText(self.Count, sheet="S", header_row=2).rows(body)

    def test_a_null_word_is_an_empty_cell_and_a_row_of_them_no_row(self) -> None:
        reader = fr.Reader(self.Count, fr.Csv(), table=fr.Cut(), null_values=["NA"])
        assert reader.rows(b"Name,N\na,NA\nNA,NA\nb,2\n") == [self.Count("a", None), self.Count("b", 2)]

    @pytest.mark.parametrize(
        ("cut", "body", "error", "words"),
        [
            (fr.Cut(), b"Name,N\na,1\nb,2,x\n", ReadError, r"^row 3 holds a cell past the header \['Name', 'N'\],"),
            (
                fr.Cut(header_row=3),
                b"Title\nName,N\n",
                UnknownLayoutError,
                r"^the body has 2 rows, and its header is row 3$",
            ),
            (
                fr.Cut(title="Table 2"),
                b"Table 2,x\nName,N\n",
                UnknownLayoutError,
                r"^no row is the title 'Table 2' alone$",
            ),
            (
                fr.Cut(title="Table 2"),
                b"Table 2\n\n",
                UnknownLayoutError,
                r"^the title 'Table 2' has no table below it$",
            ),
            (
                fr.Cut(filled=(2, 3)),
                b"No trades\n",
                EmptyBodyError,
                r"^the grid holds no table: no row of it fills 2 cells",
            ),
            (fr.Cut(), b",,Name\na,b,c\n", UnknownLayoutError, r"leaves two cells empty"),
            (fr.Cut(), b",,\na,b,c\n", ReadError, r"^row 2 holds a cell past the header \[\],"),
            (
                fr.Cut(full_rows=True),
                b"Name,N\na,1\n\nb\n",
                RaggedRowError,
                r"^row 4 has 1 fields, not the widest row's 2$",
            ),
        ],
        ids=[
            "past the header",
            "no header row",
            "no title alone",
            "nothing under the title",
            "no table",
            "two empty",
            "an empty header",
            "short",
        ],
    )
    def test_a_grid_cut_raises(self, cut: fr.Cut, body: bytes, error: type[ReadError], words: str) -> None:
        with pytest.raises(error, match=words):
            fr.Reader(self.Count, fr.Csv(), table=cut).rows(body)

    def test_full_rows_reads_a_grid_whose_rows_are_full_and_blank_lines(self) -> None:
        reader = fr.Reader(self.Count, fr.Csv(), table=fr.Cut(full_rows=True))
        assert reader.rows(b"Name,N\na,1\n\nb,2\n") == [self.Count("a", 1), self.Count("b", 2)]

    def test_a_header_row_of_no_labels_over_no_cells_is_no_header_of_the_struct(self) -> None:
        with pytest.raises(UnknownLayoutError, match=r"^the header \[\] is not \['Name', 'N'\]$"):
            fr.Reader(self.Count, fr.Csv(), table=fr.Cut()).rows(b",,\n,,\n")

    def test_a_grid_of_no_table_is_no_rows_where_the_reader_takes_one(self) -> None:
        assert fr.Reader(self.Count, fr.Csv(), table=fr.Cut(filled=(2, 3)), empty=True).rows(b"No trades\n") == []

    def test_a_title_in_a_grid_of_no_columns_is_not_found(self) -> None:
        with pytest.raises(UnknownLayoutError, match="no row is the title"):
            fr.Reader(self.Count, fr.Sheet(sheet="S"), table=fr.Cut(title="T")).rows(workbook({"S": []}))

    def test_a_title_alone_in_a_grid_of_one_column_is_found(self) -> None:
        class Name(msgspec.Struct, frozen=True, gc=False):
            name: Annotated[str, Label("Name")]

        assert fr.Reader(Name, fr.Csv(), table=fr.Cut(title="T")).rows(b"T\nName\na\n") == [Name("a")]

    def test_a_header_over_rows_reads_long_as_long_file_reads_it(self) -> None:
        from flatread.delimited import CsvText  # noqa: PLC0415 (the 7.x reader it replaces)
        from flatread.layout import LongFile  # noqa: PLC0415

        class Rate(msgspec.Struct, frozen=True, gc=False):
            name: Annotated[str, Label("Name")]
            key: str
            value: float | None

        keys = re.compile(r"Rates\x1f\d{4}")

        class Old(LongFile[Rate], CsvText[Rate]):
            _above = 1
            _keys = keys

        body = b",Rates,\nName,2024,2025\na,1,2\nb,,3\n"
        reader = fr.Reader(Rate, fr.Csv(), table=fr.Cut(header_row=2, above=1), shape=fr.Long(keys=keys))
        assert reader.rows(body) == Old(Rate, header_row=2).rows(body)
        with pytest.raises(UnknownLayoutError, match="heads two columns alike"):
            reader.rows(b",Rates,\nName,2024,2024\na,1,2\n")

    def test_a_header_rule_reads_a_cut_header(self) -> None:
        cut = fr.Cut(header=fr.Headers(unread=["notes"]))
        assert fr.Reader(self.Count, fr.Csv(), table=cut).rows(b"Name,notes,N\na,x,1\n") == [self.Count("a", 1)]

    @pytest.mark.parametrize(
        ("keywords", "words"),
        [
            ({"header_row": 0}, "is no row of a grid"),
            ({"title": "T", "header_row": 2}, "one of the three"),
            ({"title": "T", "filled": (2, 3)}, "one of the three"),
            ({"above": 1, "title": "T"}, "above takes a header_row"),
            ({"above": 2, "header_row": 2}, "do not fit above"),
            ({"band": (-1, None)}, "is no columns of a grid"),
            ({"band": (2, 2)}, "is no columns of a grid"),
            ({"filled": (0, 3)}, "is no header's"),
            ({"ends": "notes"}, "not 'notes'"),
        ],
    )
    def test_a_cut_no_grid_has_is_refused_when_built(self, keywords: Mapping[str, typing.Any], words: str) -> None:
        with pytest.raises(LayoutError, match=words):
            fr.Cut(**keywords)


def test_a_misquoted_csv_reads_a_stray_quote_as_text_as_csv_text_does() -> None:
    from flatread.delimited import CsvText  # noqa: PLC0415 (the 7.x reader it replaces)

    class Note(msgspec.Struct, frozen=True, gc=False):
        name: Annotated[str, Label("Name")]
        note: Annotated[str | None, Label("Note")]

    class Misquoted(CsvText[Note]):
        _misquoted = True

    body = b'Name,Note\na,"ts." Holdings o"\nb,"x, y"\n'
    reader = fr.Reader(Note, fr.Csv(misquoted=True), table=fr.Cut())
    assert reader.rows(body) == Misquoted(Note).rows(body) == [Note("a", 'ts. Holdings o"'), Note("b", "x, y")]
    assert fr.Reader(Note, fr.Csv(misquoted=True)).rows(b'Name,Note\na,b"c\n') == [Note("a", 'b"c')]
    with pytest.raises(ReadError):
        fr.Reader(Note, fr.Csv(), table=fr.Cut()).rows(body)


class TestLong:
    """LongFile's switches as Long's: several keys, spacers, scattered and beside columns, repeats, needs_rows."""

    class One(msgspec.Struct, frozen=True, gc=False):
        name: Annotated[str, Label("Name")]
        year: int
        value: float | None

    class Two(msgspec.Struct, frozen=True, gc=False):
        name: Annotated[str, Label("Name")]
        measure: str
        year: int
        value: float | None

    class Noted(msgspec.Struct, frozen=True, gc=False):
        name: Annotated[str, Label("Name")]
        note: Annotated[str | None, Label("Note")]
        year: int
        value: float | None

    class Placed(msgspec.Struct, frozen=True, gc=False):
        name: Annotated[str, Label("Name")]
        year: int
        n: int
        value: float | None

    _YEAR: typing.ClassVar = re.compile(r"\d{4}")
    _MEASURE: typing.ClassVar = re.compile(r"(?P<measure>[a-z_]+)_(?P<year>\d{4})")

    @staticmethod
    def _old(row: type[msgspec.Struct], keys: re.Pattern[str], switches: Mapping[str, object]) -> typing.Any:  # noqa: ANN401 (a 7.x reader)
        from flatread.delimited import CsvText  # noqa: PLC0415 (the 7.x reader it replaces)
        from flatread.layout import LongFile  # noqa: PLC0415

        bases = (LongFile[row], CsvText[row])  # ty: ignore[invalid-type-form] (a reader of the case's row)
        old = types.new_class("Old", bases, exec_body=lambda body: body.update({"_keys": keys, **switches}))
        return old(row)

    @pytest.mark.parametrize(
        ("row", "switches", "long", "body"),
        [
            (
                Two,
                {"_key_count": 2},
                fr.Long(keys=_MEASURE, key_count=2),
                b"Name,mv_2003,mv_2004,debt_2003\na,1,2,3\nb,,5,\n",
            ),
            (One, {"_spacer_columns": True}, fr.Long(keys=_YEAR, spacers=True), b"Name,2001,,2002\na,1,,2\n"),
            (One, {"_scattered": True}, fr.Long(keys=_YEAR, scattered=True), b"2001,Name,2002\n1,a,2\n"),
            (Noted, {"_beside": ("note",)}, fr.Long(keys=_YEAR, beside=["note"]), b"Name,2001,Note,2002\na,1,hi,2\n"),
            (Noted, {"_beside": ("note",)}, fr.Long(keys=_YEAR, beside=["note"]), b"Name,2001,2002\na,1,2\n"),
            (One, {"_empty_cells": "drop"}, fr.Long(keys=_YEAR, empty_cells="drop"), b"Name,2001,2002\na,1,\nb,,\n"),
        ],
        ids=["several keys", "spacers", "scattered", "beside", "beside lacked", "drop"],
    )
    def test_a_long_table_reads_as_long_file_reads_it(
        self, row: type[msgspec.Struct], switches: Mapping[str, object], long: fr.Long, body: bytes
    ) -> None:
        reader = fr.Reader(row, fr.Csv(), table=fr.Cut(), shape=long)
        assert reader.rows(body) == self._old(row, long.keys, switches).rows(body)

    def test_a_key_written_twice_is_two_keys_by_their_place_where_headings_repeat(self) -> None:
        long = fr.Long(keys=re.compile(r"(?P<year>\d{4})"), key_count=2, repeats=True)
        rows = fr.Reader(self.Placed, fr.Csv(), shape=long).rows(b"Name,2001,2001\na,1,2\n")
        assert rows == [self.Placed("a", 2001, 1, 1.0), self.Placed("a", 2001, 2, 2.0)]

    @pytest.mark.parametrize(
        ("row", "long", "body", "error", "words"),
        [
            (Two, fr.Long(keys=_MEASURE, key_count=2), b"Name,mv_2003,mv_2003\na,1,2\n", UnknownLayoutError, "twice"),
            (Two, fr.Long(keys=_MEASURE, key_count=2), b"Name,mv_x\na,1\n", UnknownLayoutError, "are not keys"),
            (
                One,
                fr.Long(keys=_YEAR, spacers=True),
                b"Name,2001,,2002\na,1,x,2\n",
                ReadError,
                r"^a column with no heading holds a cell: row 2$",
            ),
            (One, fr.Long(keys=_YEAR, scattered=True), b"Name,2001,Name\na,1,b\n", UnknownLayoutError, "no one 'Name'"),
            (
                Noted,
                fr.Long(keys=_YEAR, beside=["note"]),
                b"Name,Note,2001,Note\na,x,1,y\n",
                UnknownLayoutError,
                "has 2 'Note' columns",
            ),
            (
                One,
                fr.Long(keys=_YEAR, needs_rows=True),
                b"Name,2001\n",
                EmptyBodyError,
                r"^the body is a header and no",
            ),
            (
                Two,
                fr.Long(keys=re.compile(r"(?P<measure>[a-z]*)_(?P<year>\d{4})"), key_count=2),
                b"Name,_2001\na,1\n",
                UnknownLayoutError,
                r"name no measure, which every row holds",
            ),
        ],
        ids=[
            "a key twice",
            "no key",
            "a spacer holding a cell",
            "scattered twice",
            "beside twice",
            "no rows",
            "no text",
        ],
    )
    def test_a_long_table_is_refused_for(
        self, row: type[msgspec.Struct], long: fr.Long, body: bytes, error: type[ReadError], words: str
    ) -> None:
        with pytest.raises(error, match=words):
            fr.Reader(row, fr.Csv(), shape=long, empty=True).rows(body)

    def test_a_table_of_no_rows_is_one_where_needs_rows_is_unset(self) -> None:
        assert fr.Reader(self.One, fr.Csv(), shape=fr.Long(keys=self._YEAR), empty=True).rows(b"Name,2001\n") == []

    def test_a_header_of_no_keys_is_a_table_of_no_rows(self) -> None:
        reader = fr.Reader(self.One, fr.Csv(), shape=fr.Long(keys=self._YEAR), empty=True)
        assert reader.rows(b"Name\na\n") == []

    def test_a_sheet_s_key_is_its_header_text_typed_as_the_key_field(self) -> None:
        from flatread.layout import LongFile  # noqa: PLC0415 (the 7.x reader it replaces)
        from flatread.sheets import SheetFile  # noqa: PLC0415

        class Old(LongFile[TestLong.One], SheetFile[TestLong.One]):
            _keys = re.compile(r"\d{4}")

        body = workbook({"S": [["Name", "2001", "2002"], ["a", 1, 2.5]]})
        reader = fr.Reader(self.One, fr.Sheet(sheet="S"), shape=fr.Long(keys=self._YEAR))
        expected = [self.One("a", 2001, 1.0), self.One("a", 2002, 2.5)]
        assert reader.rows(body) == Old(self.One, sheet="S").rows(body) == expected

    def test_a_long_shape_no_table_has_is_refused_when_built(self) -> None:
        with pytest.raises(LayoutError, match="at least one key"):
            fr.Long(keys=self._YEAR, key_count=0)
        with pytest.raises(LayoutError, match="kept or dropped"):
            fr.Long(keys=self._YEAR, empty_cells="skip")  # ty: ignore[invalid-argument-type] (refused)

    @pytest.mark.parametrize(
        ("row", "long", "words"),
        [
            (Noted, fr.Long(keys=_YEAR, beside=["nope"]), r"beside names \['nope'\]"),
            (Two, fr.Long(keys=re.compile(r"(?P<year>\d{4})"), key_count=2), r"the key fields \['measure'\] are named"),
        ],
    )
    def test_a_long_shape_its_row_does_not_fit_is_refused_when_read(
        self, row: type[msgspec.Struct], long: fr.Long, words: str
    ) -> None:
        with pytest.raises(LayoutError, match=words):
            fr.Reader(row, fr.Csv(), shape=long).rows(b"Name,2001\na,1\n")


class TestFound:
    """FoundTable's export as Found: its header found by the key field's words below any titles."""

    class Filer(msgspec.Struct, frozen=True, gc=False):
        cik: Annotated[str, Label("CIK", alternatives=("Cik",))]
        name: Annotated[str | None, Label("Name")]
        n: Annotated[int | None, Label("N")]

    class Requested(msgspec.Struct, frozen=True, gc=False):
        cik: Annotated[str, Label("CIK")]
        kind: str | None

    @pytest.mark.parametrize(
        "body",
        [
            b"Report\n\nName,CIK,N\na,1,2\n---,---,---\nb,3,\n",
            b"Title\nCik,N\n1,2\n",
            b"CIK,N\n",
            b"Name,N\na,1\n",
            b"CIK,Bogus\n1,2\n",
            b"CIK,CIK\n1,2\n",
            b"CIK,N\n1\n",
            b"CIK,N,\n1,2,x\n",
            b"CIK,N\n1,2,3\n",
        ],
        ids=["titles, rules", "an alternative", "no rows", "no key", "a stray", "twice", "short", "unlabeled", "past"],
    )
    def test_an_export_reads_or_raises_as_found_csv_does(self, body: bytes) -> None:
        from flatread.found import FoundCsv  # noqa: PLC0415 (the 7.x reader it replaces)

        outcomes = []
        for read in (
            fr.Reader(self.Filer, fr.Csv(), table=fr.Found(key="cik")).rows,
            FoundCsv(self.Filer, key="cik").rows,
        ):
            try:
                outcomes.append(read(body))
            except ReadError as e:
                outcomes.append(type(e))
        assert outcomes[0] == outcomes[1]

    def test_an_error_names_the_row_and_its_cells(self) -> None:
        reader = fr.Reader(self.Filer, fr.Csv(), table=fr.Found(key="cik"))
        with pytest.raises(RaggedRowError, match=r"^row 3 has 1 cells, not the header's 2: \['1'\]$"):
            reader.rows(b"Title\nCIK,N\n1\n")
        with pytest.raises(ReadError, match=r"^row 2 holds a value under no label: \['1', '2', 'x'\]$"):
            reader.rows(b"CIK,N,\n1,2,x\n")

    def test_a_section_line_states_its_field_for_the_rows_below_as_found_csv_does(self) -> None:
        from flatread.found import FoundCsv  # noqa: PLC0415 (the 7.x reader it replaces)

        class Sectioned(FoundCsv[TestFound.Requested]):
            _section_prefix = "Request Type: "
            _section_field = "kind"

        body = b"CIK\n1\nRequest Type: FOIA\n2\n3\nRequest Type: X ,\n4\n"
        found = fr.Found(key="cik", section="Request Type: ", section_field="kind")
        rows = fr.Reader(self.Requested, fr.Csv(), table=found).rows(body)
        assert rows == Sectioned(self.Requested, key="cik").rows(body)
        assert [row.kind for row in rows] == [None, "FOIA", "FOIA", "X"]

    @pytest.mark.parametrize(("body", "read"), [(b",CIK,N\n1,a,2\n2,b,3\n", True), (b",CIK,N\nx,a,2\n", False)])
    def test_an_unlabeled_first_column_of_whole_numbers_numbers_the_rows(self, body: bytes, *, read: bool) -> None:
        reader = fr.Reader(self.Filer, fr.Csv(), table=fr.Found(key="cik", numbered_rows=True))
        if read:
            assert reader.rows(body) == [self.Filer("a", None, 2), self.Filer("b", None, 3)]
        else:
            with pytest.raises(ReadError, match="under no label"):
                reader.rows(body)

    def test_a_heading_folds_each_header_cell(self) -> None:
        reader = fr.Reader(self.Filer, fr.Csv(), table=fr.Found(key="cik", heading=str.upper))
        assert reader.rows(b"cik,n\n1,2\n") == [self.Filer("1", None, 2)]

    def test_an_open_header_reads_each_cell_as_its_column_for_a_shape(self) -> None:
        class Field(msgspec.Struct, frozen=True, gc=False):
            name: Annotated[str, Label("Name")]
            field: str
            value: str | None

        long = fr.Long(keys=re.compile(r"[A-Z]\w*"))
        reader = fr.Reader(Field, fr.Csv(), table=fr.Found(key="name", open=True), shape=long)
        assert reader.rows(b"Roster\nName,Phone,,Fax\na,1,,2\n") == [
            Field("a", "Phone", "1"),
            Field("a", "Fax", "2"),
        ]

    def test_a_sheet_export_reads_as_found_sheet_reads_it(self) -> None:
        from flatread.sheets import FoundSheet  # noqa: PLC0415 (the 7.x reader it replaces)

        class Named(msgspec.Struct, frozen=True, gc=False):
            cik: Annotated[str, Label("CIK")]
            name: Annotated[str | None, Label("Name")]

        body = workbook({"S": [["Title"], [None], ["Name", "CIK"], ["a", "x1"], [None, None], ["b", "x2"]]})
        rows = fr.Reader(Named, fr.Sheet(), table=fr.Found(key="cik")).rows(body)
        assert rows == FoundSheet(Named, key="cik").rows(body) == [Named("x1", "a"), Named("x2", "b")]

    @pytest.mark.parametrize(
        ("found", "words"),
        [
            (fr.Found(key="nope"), "is no field read from a column"),
            (fr.Found(key="cik", section="Type: ", section_field="nope"), "is no field of the row"),
        ],
    )
    def test_a_found_table_its_row_has_not_is_refused(self, found: fr.Found, words: str) -> None:
        with pytest.raises(LayoutError, match=words):
            fr.Reader(self.Filer, fr.Csv(), table=found).rows(b"CIK\n1\n")

    def test_a_section_without_its_field_or_a_word_two_fields_share_is_refused(self) -> None:
        with pytest.raises(LayoutError, match="go together"):
            fr.Found(key="cik", section="Type: ")

        class Shared(msgspec.Struct, frozen=True):
            a: Annotated[str, Label("a", alternatives=("b",))]
            b: str

        with pytest.raises(LayoutError, match="two fields share a label"):
            fr.Reader(Shared, fr.Csv(), table=fr.Found(key="a")).rows(b"a\nx\n")


class TestReport:
    """ReportFile's report as Text and Report, and KeyedReport's as Keyed: each held to the 7.x reader."""

    @staticmethod
    def _sales(**report: typing.Any) -> fr.Report:  # noqa: ANN401 (Report's switches)
        from tests.test_flat import _Sales  # noqa: PLC0415 (the 7.x report it replaces)

        sales = fr.Report(line=_Sales._line, header_lines=_Sales._header_lines, framing=_Sales._framing)
        return dataclasses.replace(sales, **report)

    def test_rows_are_read_among_their_framing_as_report_file_reads_them(self) -> None:
        from tests.test_flat import _SALES, _Sale, _Sales  # noqa: PLC0415 (the 7.x report it replaces)

        reader = fr.Reader(_Sale, fr.Text(), table=self._sales(), null_values=["n.a."])
        assert reader.rows(_SALES) == _Sales(_Sale, null_values=("n.a.",)).rows(_SALES)
        assert len(reader.rows(_SALES)) == 3

    def test_a_line_of_no_kind_raises_naming_it(self) -> None:
        from tests.test_flat import _SALES, _Sale  # noqa: PLC0415 (the 7.x report)

        reader = fr.Reader(_Sale, fr.Text(), table=self._sales(), null_values=["n.a."])
        with pytest.raises(
            ReadError, match=r"^line 8 ' MILL n\.a\.' is neither a row nor a line framing the table$"
        ) as e:
            reader.rows(_SALES.replace(b" MILL           3   n.a.", b" MILL n.a."))
        assert e.value.row == 8

    def test_a_report_cut_short_of_its_last_line_raises(self) -> None:
        from tests.test_flat import _SALES, _Ended, _Sale  # noqa: PLC0415 (the 7.x report)

        reader = fr.Reader(_Sale, fr.Text(), table=self._sales(last_line=_Ended._last_line), null_values=["n.a."])
        ended = _SALES + b"  ***** END OF REPORT *****\n\n"
        assert reader.rows(ended) == _Ended(_Sale, null_values=("n.a.",)).rows(ended)
        with pytest.raises(TruncatedFileError, match=r"the report ends with ' PIER .*': it was cut"):
            reader.rows(_SALES)

    def test_a_report_of_no_rows_is_one_where_the_reader_takes_none(self) -> None:
        from tests.test_flat import _SALES, _Sale, _Sales  # noqa: PLC0415 (the 7.x report)

        framing = (*_Sales._framing, re.compile(r"NO SALES"))
        report = fr.Report(line=_Sales._line, header_lines=_Sales._header_lines, framing=framing)
        empty = _SALES.split(b"REGION NORTH")[0] + b"NO SALES\n"
        assert fr.Reader(_Sale, fr.Text(), table=report, empty=True).rows(empty) == []
        with pytest.raises(EmptyBodyError, match=r"^the report holds no rows$"):
            fr.Reader(_Sale, fr.Text(), table=report).rows(empty)
        with pytest.raises(UnknownLayoutError, match="no column header"):
            fr.Reader(_Sale, fr.Text(), table=report, empty=True).rows(
                b"SALES REPORT   DAY 11/06/2026   PAGE 1\nNO SALES\n"
            )

    def test_a_format_checks_its_lines_by_subclassing(self) -> None:
        from tests.test_flat import _SALES, _Sale, _Sales  # noqa: PLC0415 (the 7.x report)

        @dataclasses.dataclass(frozen=True, kw_only=True)
        class Counted(fr.Report):
            @typing.override
            def check(self, read: Sequence[tuple[int, re.Match[str]]]) -> None:
                super().check(read)
                if (rows := sum(match.re is self.line for _, match in read)) != int(read[-1][1][1]):
                    msg = f"the report counts {read[-1][1][1]} stores, and holds {rows}"
                    raise ReadError(msg)

        report = Counted(
            line=_Sales._line,
            header_lines=_Sales._header_lines,
            framing=_Sales._framing,
            last_line=re.compile(r"STORES +(\d+)"),
        )
        reader = fr.Reader(_Sale, fr.Text(), table=report, null_values=["n.a."])
        assert len(reader.rows(_SALES + b"STORES 3\n")) == 3
        with pytest.raises(ReadError, match="the report counts 4 stores, and holds 3"):
            reader.rows(_SALES + b"STORES 4\n")

    def test_thousands_cleans_a_report_s_numbers_naming_the_line(self) -> None:
        from tests.test_flat import _SALES, _Sale  # noqa: PLC0415 (the 7.x report)

        reader = fr.Reader(_Sale, fr.Text(), table=self._sales(), cells=[fr.Thousands()], null_values=["n.a."])
        assert reader.rows(_SALES.replace(b"MAIN ST       12", b"MAIN ST    1,200"))[0].units == 1200
        with pytest.raises(ReadError, match=r"the 'units' cell '12,00' is not a whole number: row 7$"):
            reader.rows(_SALES.replace(b"MAIN ST       12", b"MAIN ST    12,00"))

    def test_a_preamble_is_dropped_to_the_header_s_first_run_as_report_file_drops_it(self) -> None:
        from tests.test_flat import _Sale, _Sales  # noqa: PLC0415 (the 7.x report)

        class Preambled(_Sales):
            _preamble = True

        body = (
            b"anything, ( at all\n STORE      UNITS   UNIT\n             SOLD  PRICE\n"
            b"SALES REPORT   DAY 11/06/2026   PAGE 1\nREGION NORTH\n MAIN ST       12   1.50\n"
        )
        reader = fr.Reader(_Sale, fr.Text(), table=self._sales(preamble=True))
        assert reader.rows(body) == Preambled(_Sale).rows(body)
        assert len(reader.rows(body)) == 1
        with pytest.raises(UnknownLayoutError, match="holds no run of the header's lines"):
            reader.rows(b"anything at all\n")

    def test_a_blank_line_read_is_a_line_of_its_kind(self) -> None:
        class Word(msgspec.Struct, frozen=True, gc=False):
            word: str

        report = fr.Report(line=re.compile(r"(?P<word>\w+)"), framing=[re.compile("")], blank_lines="read")
        assert fr.Reader(Word, fr.Text(), table=report).rows(b"a\n\nb\n") == [Word("a"), Word("b")]
        strict = fr.Report(line=re.compile(r"(?P<word>\w+)"), blank_lines="read")
        with pytest.raises(ReadError, match=r"^line 2 '' is neither a row"):
            fr.Reader(Word, fr.Text(), table=strict).rows(b"a\n\nb\n")

    def test_text_lines_split_as_splitlines_splits_them_in_latin_1_too(self) -> None:
        class Word(msgspec.Struct, frozen=True, gc=False):
            word: str

        report = fr.Report(line=re.compile(r"(?P<word>\w+)"))
        reader = fr.Reader(Word, fr.Text(splitlines=True), table=report)
        assert reader.rows(b"caf\xe9\x0cb\n") == [Word("café"), Word("b")]
        with pytest.raises(ReadError, match="always ends its last line"):
            fr.Reader(Word, fr.Text(final_line_ending=True), table=report).rows(b"a")
        with pytest.raises(EmptyBodyError, match=r"^the body is empty$"):
            reader.rows(b" \n")

    @pytest.mark.parametrize(
        ("report", "words"),
        [
            ({"line": "nope"}, "compiled from str"),
            ({"line": re.compile("x"), "blank_lines": "skip"}, "not 'skip'"),
            ({"line": re.compile("x"), "preamble": True}, "it names none"),
        ],
    )
    def test_a_report_no_file_has_is_refused_when_built(self, report: Mapping[str, typing.Any], words: str) -> None:
        with pytest.raises(LayoutError, match=words):
            fr.Report(**report)

    def test_a_report_whose_groups_are_not_its_fields_is_refused(self) -> None:
        from tests.test_flat import _SALES, _Sale, _Sales  # noqa: PLC0415 (the 7.x report)

        stray = self._sales(framing=(*_Sales._framing, re.compile(r"CLERK (?P<clerk>\w+)")))
        both = self._sales(framing=(*_Sales._framing, re.compile(r"STORE (?P<store>\w+)")))
        with pytest.raises(LayoutError, match=r"the report's groups \['clerk'\] name no field of the row"):
            fr.Reader(_Sale, fr.Text(), table=stray).rows(_SALES)
        with pytest.raises(LayoutError, match=r"the fields \['store'\] are both a row's cells and stated"):
            fr.Reader(_Sale, fr.Text(), table=both).rows(_SALES)

    def test_a_keyed_report_reads_long_as_keyed_report_reads_it(self) -> None:
        from tests.test_flat import _HELD, _Held, _Shares  # noqa: PLC0415 (the 7.x report it replaces)

        reader = fr.Reader(
            _Shares,
            fr.Text(),
            table=fr.Report(line=_Held._line, framing=_Held._framing),
            shape=fr.Keyed(key_field="month"),
            null_values=["-"],
        )
        assert reader.rows(_HELD) == _Held(_Shares, null_values=("-",)).rows(_HELD)
        with pytest.raises(RaggedRowError, match=r"^row 3 holds 3 values, not 4: 2 for each key above it$"):
            reader.rows(_HELD.replace(b"4.5 -", b"4.5"))

    def test_a_keyed_report_finds_its_keys_by_their_own_cell(self) -> None:
        class Row(msgspec.Struct, frozen=True, gc=False):
            name: str
            year: int
            value: str

        report = fr.Report(
            line=re.compile(r"(?P<name>[a-z]+)\s+(?P<value>.*)"), framing=[re.compile(r"Name\s+(?P<year>.*)")]
        )
        keyed = fr.Keyed(cell=re.compile(r"x\d+", re.IGNORECASE), key_cell=re.compile(r"\d+"))
        reader = fr.Reader(Row, fr.Text(), table=report, shape=keyed)
        assert reader.rows(b"Name 2020 2021\nabc X1 x2\n") == [Row("abc", 2020, "X1"), Row("abc", 2021, "x2")]

    @pytest.mark.parametrize(
        ("keyed", "words"),
        [
            ({"cell": re.compile(r"(?<=\s)\d+")}, "none polars' engine reads"),
            ({"cell": re.compile(r"\d+", re.ASCII)}, "ASCII"),
            ({"key_cell": re.compile(r"(?<=\s)\d+")}, "none polars' engine reads"),
        ],
    )
    def test_a_keyed_pattern_polars_cannot_read_is_refused_when_built(
        self, keyed: Mapping[str, typing.Any], words: str
    ) -> None:
        with pytest.raises(LayoutError, match=words):
            fr.Keyed(**keyed)

    def test_a_keyed_report_its_row_does_not_fit_is_refused(self) -> None:
        class Row(msgspec.Struct, frozen=True, gc=False):
            name: str
            value: str

        report = fr.Report(line=re.compile(r"(?P<name>[a-z]+)\s+(?P<value>.*)"))
        for keyed, words in [
            (fr.Keyed(key_field="nope"), "no field of the row"),
            (fr.Keyed(key_field="value"), "a value field after it"),
        ]:
            with pytest.raises(LayoutError, match=words):
                fr.Reader(Row, fr.Text(), table=report, shape=keyed).rows(b"a 1\n")
        with pytest.raises(LayoutError, match="reads text lines, not a sheet's cells"):
            fr.Reader(Row, fr.Sheet(sheet="S"), shape=fr.Keyed(key_field="name")).rows(
                workbook({"S": [["name", "value"], ["a", "1"]]})
            )
