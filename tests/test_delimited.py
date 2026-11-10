import codecs
import datetime as dt

import msgspec
import polars as pl
import pytest

from flatread.delimited import Csv, Line, Pipe, Tsv
from flatread.errors import (
    CellError,
    ControlTotalError,
    EncodingError,
    RaggedRowError,
    TruncatedFileError,
    UnknownLayoutError,
)


class Obs(msgspec.Struct, frozen=True):
    day: dt.date
    value: float | None
    note: str


class Observations(Csv):
    row = Obs
    labels = {"Date": "day", "Value": "value", "Note": "note"}
    missing = frozenset({"", "."})


BODY = b"Date,Value,Note\n2026-11-09,1.5,a\n2026-11-10,.,b\n"


def test_a_csv_reads_typed() -> None:
    assert Observations.frame(BODY).rows() == [(dt.date(2026, 11, 9), 1.5, "a"), (dt.date(2026, 11, 10), None, "b")]
    assert Csv._fast(BODY.split(b"\n", 1)[1])


@pytest.mark.parametrize("quoted", [b"", b'"q",'])
def test_a_short_row_is_ragged_not_padded(quoted: bytes) -> None:
    body = b"Date,Value,Note\n" + quoted.replace(b'"q",', b'2026-11-08,"1",q\n') + b"2026-11-09,1.5\n"
    line = 3 if quoted else 2
    with pytest.raises(RaggedRowError, match=f"line {line} has 2 fields, not the header's 3"):
        Observations.frame(body)


def test_a_long_row_is_ragged() -> None:
    with pytest.raises(RaggedRowError, match="line 3 has 4 fields"):
        Observations.frame(b"Date,Value,Note\n2026-11-09,1,a\n2026-11-10,2,b,c\n")


def test_crlf_with_bom_reads_on_the_fast_path() -> None:
    body = codecs.BOM_UTF8 + BODY.replace(b"\n", b"\r\n")
    assert Csv._fast(body.split(b"\n", 1)[1])
    frame = Observations.frame(body)
    assert frame["note"].to_list() == ["a", "b"]


def test_quoted_commas_read_on_the_fast_path() -> None:
    body = b'Date,Value,Note\n2026-11-09,1.5,"a, b"\n'
    assert Csv._fast(body.split(b"\n", 1)[1])
    assert Observations.frame(body)["note"][0] == "a, b"


def test_a_multiline_quoted_record_reads_on_the_fallback() -> None:
    body = b'Date,Value,Note\n2026-11-09,1,a\n2026-11-10,2,"two\nlines"\n2026-11-11,x,c\n'
    assert not Csv._fast(body.split(b"\n", 1)[1])
    with pytest.raises(CellError, match="line 5: the 'Value' cell 'x'"):
        Observations.frame(body)
    frame = Observations.frame(body.replace(b",x,", b",3,"))
    assert frame["note"].to_list() == ["a", "two\nlines", "c"]


def test_blank_lines_are_framing() -> None:
    body = b"\nDate,Value,Note\n\n2026-11-09,1,a\n\n\n2026-11-10,x,b\n\n"
    with pytest.raises(CellError, match="line 7:"):
        Observations.frame(body)
    assert Observations.frame(body.replace(b",x,", b",2,")).height == 2


def test_a_cut_body_raises_truncated() -> None:
    with pytest.raises(TruncatedFileError):
        Observations.frame(BODY[:-1])


def test_no_final_line_ending_when_declared() -> None:
    class Open(Observations):
        final_line_ending = False

    assert Open.frame(BODY[:-1].replace(b"\n", b"\r\n")).height == 2


def test_header_line_skips_a_varying_preamble() -> None:
    class Preambled(Observations):
        header_line = r"Date,.*"

    body = b"Cboe disclaimer\nProduct: SPX\nDate,Value,Note\n2026-11-09,1,a\n"
    assert Preambled.frame(body).height == 1
    with pytest.raises(UnknownLayoutError, match="no line matches"):
        Preambled.frame(b"nothing here\n")


def test_skip_lines() -> None:
    class Skipped(Observations):
        skip_lines = 2

    assert Skipped.frame(b"title\nas of today\n" + BODY).height == 2


def test_trailing_separator() -> None:
    class Trailing(Observations):
        trailing_separator = True

    assert Trailing.frame(b"Date,Value,Note,\n2026-11-09,1,a,\n").height == 1
    with pytest.raises(RaggedRowError, match="line 2 does not end with the separator"):
        Trailing.frame(b"Date,Value,Note,\n2026-11-09,1,a,x\n")


class Listed(msgspec.Struct, frozen=True):
    symbol: str
    name: str
    generated_at: dt.datetime


class NasdaqListed(Pipe):
    row = Listed
    labels = {"Symbol": "symbol", "Security Name": "name"}
    trailer = Line(r"File Creation Time: (?P<stamp>\d{10}:\d{2})\|", carry={"stamp": "generated_at"})
    dates = {"generated_at": "%m%d%Y%H:%M"}


def test_nasdaq_trailer_carries_generated_at() -> None:
    body = b"Symbol|Security Name\nAAPL|Apple Inc.\nMSFT|Microsoft\nFile Creation Time: 1109202618:05|\n"
    frame = NasdaqListed.frame(body)
    assert frame["generated_at"].to_list() == [dt.datetime(2026, 11, 9, 18, 5)] * 2
    with pytest.raises(TruncatedFileError, match="trailer"):
        NasdaqListed.frame(b"Symbol|Security Name\nAAPL|Apple Inc.\n")


class Margin(msgspec.Struct, frozen=True):
    symbol: str
    rate: float | None
    as_of: dt.date


class IbkrRates(Pipe):
    row = Margin
    labels = {"SYM": "symbol", "RATE": "rate"}
    missing_by_field = {"rate": {"NA"}}
    preamble = Line(r"#BOF\|(?P<day>\d{8})", carry={"day": "as_of"})
    trailer = Line(r"#EOF\|(?P<rows>\d+)", count="rows")
    dates = {"as_of": "%Y%m%d"}
    final_line_ending = False


def test_ibkr_bof_eof_count_is_checked() -> None:
    body = b"#BOF|20261109\r\nSYM|RATE\r\nAAPL|1.5\r\nMSFT|NA\r\n#EOF|2"
    frame = IbkrRates.frame(body)
    assert frame.rows() == [("AAPL", 1.5, dt.date(2026, 11, 9)), ("MSFT", None, dt.date(2026, 11, 9))]
    with pytest.raises(ControlTotalError, match="2 rows, but its trailer says 3"):
        IbkrRates.frame(body.replace(b"#EOF|2", b"#EOF|3"))


def test_a_stray_0x01_takes_the_fallback() -> None:
    body = b"Date,Value,Note\n2026-11-09,1,a\x01\n"
    assert not Csv._fast(body.split(b"\n", 1)[1])
    assert Observations.frame(body)["note"][0] == "a\x01"


def test_non_utf8_raises_encoding_error() -> None:
    with pytest.raises(EncodingError):
        Observations.frame(b"Date,Value,Note\n2026-11-09,1,\xe9t\xe9\n")


def test_another_encoding_is_declared() -> None:
    class Latin(Observations):
        encoding = "latin-1"

    assert Latin.frame(b"Date,Value,Note\n2026-11-09,1,\xe9t\xe9\n")["note"][0] == "été"


def test_an_error_on_the_fast_path_names_the_line() -> None:
    body = b"Date,Value,Note\n2026-11-09,1,a\n2026-11-10,1,b\n2026-11-11,zz,c\n"
    with pytest.raises(CellError, match="line 4: the 'Value' cell 'zz' is not a number"):
        Observations.frame(body)


def test_a_header_that_is_not_the_layout_raises() -> None:
    with pytest.raises(UnknownLayoutError, match="Day"):
        Observations.frame(b"Day,Value,Note\n2026-11-09,1,a\n")


def test_a_headerless_file_reads_by_label_order() -> None:
    class Bare(Observations):
        header = False

    assert Bare.frame(b"2026-11-09,1,a\n").row(0) == (dt.date(2026, 11, 9), 1.0, "a")


def test_a_tsv_and_an_empty_text_cell() -> None:
    class Notes(Tsv):
        row = Obs
        labels = {"Date": "day", "Value": "value", "Note": "note"}
        missing = frozenset({"."})

    frame = Notes.frame(b"Date\tValue\tNote\n2026-11-09\t.\t\n")
    assert frame.row(0) == (dt.date(2026, 11, 9), None, "")


def test_headings_above_the_header() -> None:
    from flatread.core import Long  # noqa: PLC0415

    class Group(msgspec.Struct, frozen=True):
        day: dt.date
        group: str
        series: str
        value: float

    class Grouped(Csv):
        row = Group
        labels = {"Date": "day"}
        long = Long(name="series", value="value", names=r"[A-Z]+")
        headings = "group"

    frame = Grouped.frame(b",Prices,,Rates\nDate,A,B,A\n2026-11-09,1,2,3\n")
    assert frame.select("group", "series", "value").rows() == [
        ("Prices", "A", 1.0),
        ("Prices", "B", 2.0),
        ("Rates", "A", 3.0),
    ]


def test_large_body_reads_natively() -> None:
    rows = b"".join(b"2026-11-09,%d.5,n\n" % i for i in range(50_000))
    frame = Observations.frame(b"Date,Value,Note\n" + rows)
    assert frame.height == 50_000
    assert frame["value"][-1] == 49_999.5
    assert isinstance(frame, pl.DataFrame)
