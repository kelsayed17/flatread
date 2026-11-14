"""``flatread``: a flat file read natively, as a frame of its row struct's fields or typed rows."""

import codecs
import datetime as dt
import io
import re
import struct
import time
import types
import typing
import zipfile
from typing import Annotated, Final, Literal, NewType

import msgspec
import polars as pl
import pytest

from flatread import fields, sheets
from flatread.archives import Transcoded, ZippedFile, ZippedFiles, ZippedOrBare
from flatread.core import (
    BesideGrid,
    BlankEnded,
    Carried,
    File,
    FilledHeader,
    GridFile,
    Reshaped,
    TextFile,
    Thousands,
    TitledGrid,
    _Column,
)
from flatread.delimited import (
    CsvFile,
    CsvText,
    HeadersCsv,
    NamedCsv,
    PaddedCsv,
    PreambleCsv,
    SectionedCsv,
    SpilledCsv,
    ThousandsCsv,
    TitledCsv,
    TrailerCsv,
)
from flatread.fields import Flag, Format, Joined, Label, Shape, Span
from flatread.fixed import FixedWidthFile
from flatread.found import FoundCsv, FoundSheet
from flatread.layout import LongFile
from flatread.markup import NestedXml, XmlFile
from flatread.reports import LongReport, ReportFile
from flatread.sheets import (
    BesideSheet,
    BiffSheet,
    CellHeaderSheet,
    DatedSheet,
    GroupedSheet,
    NotedSheet,
    NumberTextSheet,
    OnlySheet,
    PaddedSheet,
    PreambleSheet,
    SheetFile,
    SheetsFile,
    SheetText,
    TitledSheet,
    sheet_grid,
)
from tests.support.workbooks import swapped
from tests.support.xlsx import Cell, workbook

if typing.TYPE_CHECKING:
    from collections.abc import Sequence

    import fastexcel

_Code = NewType("_Code", str)


class _Row(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("NAME")]
    value: Annotated[float | None, Label("VALUE")]
    note: Annotated[str | None, Label("NOTE")]


class _Dated(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Label("DAY"), Format("%m-%d-%Y")]
    value: Annotated[float, Label("VALUE")]


class _Typed(msgspec.Struct, frozen=True, gc=False):
    code: Annotated[_Code, Label("CODE")]
    side: Annotated[Literal["buy", "sell"], Label("SIDE")]
    count: Annotated[int, Label("COUNT")]
    open: Annotated[bool, Label("OPEN")]
    rate: Annotated[float | None, Label("RATE")]


class TestAFile:
    def test_it_is_abstract_over_its_format(self) -> None:
        assert File.__abstractmethods__ == frozenset({"_read"})

    def test_a_reader_whose_columns_are_not_the_fields_raises(self) -> None:
        class Raw(File[_Row]):
            @typing.override
            def _read(self, body: bytes) -> pl.DataFrame:
                return pl.read_csv(body)

        with pytest.raises(ValueError, match=r"the reader's columns \['NAME', 'VALUE', 'NOTE'\] are not _Row's fields"):
            Raw(_Row).rows(b"NAME,VALUE,NOTE\na,1.5,x\n")

    def test_a_reader_whose_column_is_not_its_field_s_type_raises(self) -> None:
        """A struct built from a frame of the wrong type holds it untyped: ``'2'`` in an ``int`` field."""

        class Untyped(File[_Row]):
            @typing.override
            def _read(self, body: bytes) -> pl.DataFrame:
                return pl.read_csv(body, new_columns=["name", "value", "note"], infer_schema=False)

        with pytest.raises(
            ValueError, match=r"^the reader's \{'value': 'String'\} columns are not typed as _Row's fields"
        ):
            Untyped(_Row).rows(b"NAME,VALUE,NOTE\na,1.5,x\n")


class TestACsvFile:
    def test_the_frame_and_the_rows_are_one_table(self) -> None:
        flat = CsvFile(_Dated)
        body = b"DAY,VALUE\n01-14-2005,100.00\n01-21-2005,75.70\n"
        frame, rows = flat.frame(body), flat.rows(body)
        assert frame.columns == ["day", "value"]
        assert frame.dtypes == [pl.Date, pl.Float64]
        assert rows == [_Dated(dt.date(2005, 1, 14), 100.0), _Dated(dt.date(2005, 1, 21), 75.7)]
        assert [msgspec.structs.astuple(row) for row in rows] == frame.rows()

    def test_each_column_takes_its_field_type(self) -> None:
        flat = CsvFile(_Typed)
        frame = flat.frame(b"CODE,SIDE,COUNT,OPEN,RATE\n007,buy,30,true,\n")
        assert frame.dtypes == [pl.String, pl.String, pl.Int64, pl.Boolean, pl.Float64]
        assert frame.row(0) == ("007", "buy", 30, True, None)

    def test_the_columns_are_the_fields_labels_in_the_fields_order_and_the_json_keys_the_fields(self) -> None:
        rows = CsvFile(_Row).rows(b"NOTE,NAME,VALUE\nx,a,1.5\n")
        assert rows == [_Row("a", 1.5, "x")]
        assert msgspec.json.encode(rows) == b'[{"name":"a","value":1.5,"note":"x"}]'

    def test_a_field_without_a_column_raises(self) -> None:
        class Unlabeled(msgspec.Struct, frozen=True, gc=False):
            name: Annotated[str, Label("NAME")]
            value: float

        with pytest.raises(ValueError, match=r"the field Unlabeled\.value names no column: annotate it with a Label"):
            CsvFile(Unlabeled)

    def test_a_date_field_without_a_format_raises(self) -> None:
        class Undated(msgspec.Struct, frozen=True, gc=False):
            day: Annotated[dt.date, Label("DAY")]

        with pytest.raises(ValueError, match="the date field 'day' needs the Format its file writes it in"):
            CsvFile(Undated)

    def test_a_format_on_another_field_raises(self) -> None:
        class Misdated(msgspec.Struct, frozen=True, gc=False):
            value: Annotated[float, Label("VALUE"), Format("%Y")]

        with pytest.raises(ValueError, match="the field 'value' is no date or time, so it takes no Format"):
            CsvFile(Misdated)

    def test_a_zone_on_a_date_raises(self) -> None:
        class Zoned(msgspec.Struct, frozen=True, gc=False):
            day: Annotated[dt.date, Label("DAY"), Format("%Y-%m-%d", time_zone="UTC")]

        with pytest.raises(ValueError, match="the date field 'day' has no time, so its Format takes no zone"):
            CsvFile(Zoned)

    def test_a_field_no_column_reads_raises(self) -> None:
        class Lasting(msgspec.Struct, frozen=True, gc=False):
            took: Annotated[dt.timedelta, Label("TOOK")]

        with pytest.raises(ValueError, match="the field 'took' is a timedelta, which no column reads"):
            CsvFile(Lasting)

    def test_a_time_no_one_moment_in_its_zone_raises(self) -> None:
        class Local(msgspec.Struct, frozen=True, gc=False):
            at: Annotated[dt.datetime, Label("AT"), Format("%m/%d/%Y %H:%M", time_zone="America/New_York")]

        with pytest.raises(ValueError, match="the 'at' column holds a time that is no one moment in its zone"):
            CsvFile(Local).rows(b"AT\n11/01/2026 01:30\n")

    def test_a_time_is_read_naive_or_aware_in_its_zone(self) -> None:
        hour = "%m/%d/%Y %-I:%M:%S %p"

        class Hour(msgspec.Struct, frozen=True, gc=False):
            local: Annotated[dt.datetime, Label("LOCAL"), Format(hour)]
            utc: Annotated[dt.datetime, Label("UTC"), Format(hour, time_zone="UTC")]

        flat = CsvFile(Hour)
        body = b"LOCAL,UTC\n04/01/2026 1:00:00 AM,04/01/2026 8:00:00 AM\n04/01/2026 12:00:00 PM,04/01/2026 7:00:00 PM\n"
        assert flat.rows(body) == [
            Hour(dt.datetime(2026, 4, 1, 1), dt.datetime(2026, 4, 1, 8, tzinfo=dt.UTC)),
            Hour(dt.datetime(2026, 4, 1, 12), dt.datetime(2026, 4, 1, 19, tzinfo=dt.UTC)),
        ]
        assert flat.frame(body).schema["utc"] == pl.Datetime("us", "UTC")
        with pytest.raises(ValueError, match="the 'local' cell '04/01/2026 01:00:00 AM' is not a time written"):
            flat.rows(b"LOCAL,UTC\n04/01/2026 01:00:00 AM,04/01/2026 8:00:00 AM\n")

    def test_a_time_of_day_reads_by_its_format_exactly(self) -> None:
        class Clocked(msgspec.Struct, frozen=True, gc=False):
            at: Annotated[dt.time, Label("AT"), Format("%-I:%M%P")]

        assert CsvFile(Clocked).rows(b"AT\n2:15pm\n11:30am\n") == [Clocked(dt.time(14, 15)), Clocked(dt.time(11, 30))]
        assert CsvFile(Clocked).frame(b"AT\n2:15pm\n").schema["at"] == pl.Time
        with pytest.raises(ValueError, match="the 'at' cell '02:15pm' is not a time of day written %-I:%M%P: row 1"):
            CsvFile(Clocked).rows(b"AT\n02:15pm\n")

    def test_a_time_of_day_needs_its_format_and_takes_no_zone(self) -> None:
        class Bare(msgspec.Struct, frozen=True, gc=False):
            at: Annotated[dt.time, Label("AT")]

        class Zoned(msgspec.Struct, frozen=True, gc=False):
            at: Annotated[dt.time, Label("AT"), Format("%H:%M", time_zone="UTC")]

        with pytest.raises(ValueError, match="the time-of-day field 'at' needs the Format its file writes it in"):
            CsvFile(Bare)
        with pytest.raises(ValueError, match="the time-of-day field 'at' has no date, so its Format takes no zone"):
            CsvFile(Zoned)

    def test_a_label_written_twice_is_two_columns_by_their_places(self) -> None:
        class Filled(msgspec.Struct, frozen=True, gc=False):
            symbol: Annotated[str, Label("Symbol")]
            first: Annotated[str | None, Label("Filler")]
            second: Annotated[str | None, Label("Filler")]

        filled = CsvFile(Filled, separator="|")
        assert filled.rows(b"Symbol|Filler|Filler\nA|x|\n") == [Filled("A", "x", None)]
        with pytest.raises(ValueError, match=r"column names not specified in schema .*\[\"Other\"\]"):
            filled.rows(b"Symbol|Filler|Other\nA|x|\n")

    def test_each_date_column_takes_its_own_format(self) -> None:
        class Span(msgspec.Struct, frozen=True, gc=False):
            start: Annotated[dt.date, Label("START"), Format("%Y-%m")]
            end: Annotated[dt.date, Label("END"), Format("%m/%d/%Y")]

        flat = CsvFile(Span)
        assert flat.rows(b"START,END\n2026-01,01/31/2026\n") == [Span(dt.date(2026, 1, 1), dt.date(2026, 1, 31))]

    def test_a_date_not_in_its_format_raises(self) -> None:
        with pytest.raises(ValueError, match="the 'day' cell '2005-01-14' is not a date written %m-%d-%Y"):
            CsvFile(_Dated).rows(b"DAY,VALUE\n2005-01-14,100.00\n")

    @pytest.mark.parametrize(
        "day",
        ["01-14-05", "1-14-2005", "02-30-2026", "01-14-0000"],
        ids=["short year", "no zero pad", "no such day", "the year 0"],
    )
    def test_a_date_must_be_written_as_its_format_writes_it(self, day: str) -> None:
        with pytest.raises(ValueError, match=f"the 'day' cell '{day}' is not a date written %m-%d-%Y"):
            CsvFile(_Dated).rows(f"DAY,VALUE\n{day},100.00\n".encode())

    def test_a_row_of_empty_cells_is_a_row_not_a_blank_line(self) -> None:
        with pytest.raises(ValueError, match=r"the \['day', 'value'\] columns have an empty cell"):
            CsvFile(_Dated).rows(b"DAY,VALUE\n01-14-2005,100.00\n,\n")

    def test_an_optional_field_takes_an_empty_cell(self) -> None:
        flat = CsvFile(_Row)
        assert flat.rows(b"NAME,VALUE,NOTE\na,1.5,x\nb,,\n") == [_Row("a", 1.5, "x"), _Row("b", None, None)]
        with pytest.raises(ValueError, match=r"the \['name'\] columns have an empty cell"):
            flat.rows(b"NAME,VALUE,NOTE\n,1.5,x\n")

    def test_a_quoted_empty_cell_is_no_value(self) -> None:
        assert CsvFile(_Row).rows(b'NAME,VALUE,NOTE\na,1.5,""\n') == [_Row("a", 1.5, None)]
        with pytest.raises(ValueError, match=r"the \['name'\] columns have an empty cell"):
            CsvFile(_Row).rows(b'NAME,VALUE,NOTE\n"",1.5,x\n')

    def test_a_short_row_beside_a_nul_is_counted_by_the_csv_module(self) -> None:
        with pytest.raises(ValueError, match="row 2 has 2 fields, not the header's 3"):
            CsvFile(_Row).rows(b"NAME,VALUE,NOTE\na" + bytes(1) + b",1.5,x\nb,2.5\n")
        with pytest.raises(ValueError, match="the body is not UTF-8"):
            CsvFile(_Row).rows(b"NAME,VALUE,NOTE\na" + bytes(1) + b",1.5,x\nb\xff,2.5\n")

    def test_a_blank_line_is_a_row_too_short(self) -> None:
        """A clean file has none: a blank line is a row of one empty field, named like any short row."""
        with pytest.raises(ValueError, match=r"^row 2 has 1 fields, not the header's 3$"):
            CsvFile(_Row).rows(b"NAME,VALUE,NOTE\na,1.5,x\n\nb,2.5,y\n")
        with pytest.raises(ValueError, match=r"^row 2 has 1 fields, not the header's 2$"):
            CsvFile(_Dated).rows(b"DAY,VALUE\n01-14-2005,100.00\n\n")

    @pytest.mark.parametrize(
        ("body", "match"),
        [
            (b"NAME,AMOUNT,NOTE\na,1.5,x\n", "column names not specified in schema"),
            (b"NAME,VALUE,NOTE\na,n/a,x\n", "could not parse `n/a` as dtype `f64`"),
            (b"", "the body is empty"),
            (b"\r\n\n", "the body is empty"),
            (b"NAME,VALUE,NOTE\n", "the body is a header and no rows"),
            (b"NAME,VALUE,NOTE\n\n\n", r"^row 1 has 1 fields, not the header's 3$"),
            (b"NAME,VALUE,NOTE\na,NaN,x\n", r"the \['value'\] columns hold NaN or an infinity"),
            (b"NAME,VALUE,NOTE\na,inf,x\nb,-inf,y\n", r"the \['value'\] columns hold NaN or an infinity"),
        ],
        ids=[
            "another header",
            "a cell not a number",
            "an empty body",
            "only blank lines",
            "a header only",
            "a header and blank lines",
            "NaN",
            "an infinity",
        ],
    )
    def test_a_body_that_is_not_the_file_raises(self, body: bytes, match: str) -> None:
        with pytest.raises(ValueError, match=match):
            CsvFile(_Row).rows(body)

    @pytest.mark.parametrize(
        ("body", "match"),
        [
            (b"NAME,VALUE,NOTE\na,1.5,x\nb,2.5\n", r"^row 2 has 2 fields, not the header's 3$"),
            (b"NAME,VALUE,NOTE\na,1.5,x,extra\n", r"^row 1 has 4 fields, not the header's 3$"),
            (b'NAME,VALUE,NOTE\n"a,b",1.5\n', r"^row 1 has 2 fields, not the header's 3$"),
            (b'NAME,VALUE,NOTE\n"a\nb",1.5,x\nc,2.5\n', r"^row 2 has 2 fields, not the header's 3$"),
        ],
        ids=["short", "long", "a quoted comma is no separator", "a quoted line ending is no row"],
    )
    def test_a_row_not_as_wide_as_the_header_raises(self, body: bytes, match: str) -> None:
        """Even where its missing cells would be optional ones: polars would read them as nulls."""
        with pytest.raises(ValueError, match=match):
            CsvFile(_Row).rows(body)

    def test_a_quote_inside_a_cell_is_text(self) -> None:
        """A quote is a cell's quoting only where it opens the cell, as the csv module and RFC 4180 read it."""
        rows = CsvFile(_Row).rows(b'NAME,VALUE,NOTE\n12" pipe,1.5,a"b\n')
        assert rows == [_Row('12" pipe', 1.5, 'a"b')]
        with pytest.raises(ValueError, match=r"^row 1 has 4 fields, not the header's 3$"):
            CsvFile(_Row).rows(b'NAME,VALUE,NOTE\na"b,c"d,1.5,x\n')

    def test_a_quoted_cell_still_holds_its_quote_beside_a_quote_inside_a_cell(self) -> None:
        assert CsvFile(_Row).rows(b'NAME,VALUE,NOTE\n"a, ""b""",1.5,x\n') == [_Row('a, "b"', 1.5, "x")]

    def test_a_quoted_line_ending_is_a_cell(self) -> None:
        assert CsvFile(_Row).rows(b'NAME,VALUE,NOTE\n"a\nb",1.5,x\n') == [_Row("a\nb", 1.5, "x")]


class _Published(HeadersCsv[_Row]):
    _headers = (("NAME", "VALUE"), ("NOTE", "NAME"), ("VALUE", "NOTE"))


class TestTheReadCsvOptions:
    def test_a_token_for_no_value_reads_as_null(self) -> None:
        flat = CsvFile(_Row, null_values=["NA", "--"])
        rows = flat.rows(b"NAME,VALUE,NOTE\na,NA,x\nb,--,NA\nc,2.5,\n")
        assert rows == [_Row("a", None, "x"), _Row("b", None, None), _Row("c", 2.5, None)]

    def test_without_the_token_the_cell_is_not_a_number(self) -> None:
        with pytest.raises(ValueError, match="could not parse `NA` as dtype `f64`"):
            CsvFile(_Row).rows(b"NAME,VALUE,NOTE\na,NA,x\n")

    def test_a_column_a_published_header_leaves_out_reads_as_nulls(self) -> None:
        flat = _Published(_Row)
        assert flat.rows(b"NAME,VALUE\na,1.5\n") == [_Row("a", 1.5, None)]
        assert flat.frame(b"NOTE,NAME\nx,a\n").columns == ["name", "value", "note"]

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (b"NAME,VALUE\na,1.5,x\n", r"^row 1 has 3 fields, not the header's 2$"),
            (b"NAME,VALUE\na,1.5\nb\n", r"^row 2 has 1 fields, not the header's 2$"),
            (b"NAME,VALUE\na,1.5,x\nb\n", r"^row 1 has 3 fields, not the header's 2$"),
        ],
        ids=["long", "short", "a long row beside a short one, as many separators as full rows"],
    )
    def test_a_row_not_as_wide_as_a_published_header_raises(self, body: bytes, message: str) -> None:
        """A long row's cell would fill a column the header leaves out, where polars inserts it."""
        with pytest.raises(ValueError, match=message):
            _Published(_Row).rows(body)

    def test_a_header_the_file_was_never_published_with_raises(self) -> None:
        flat = _Published(_Row)
        with pytest.raises(ValueError, match=r"the header \['NAME', 'NOTE'\] is none of the 3 the _Row file has had"):
            flat.rows(b"NAME,NOTE\na,x\n")

    def test_a_required_column_a_published_header_leaves_out_raises(self) -> None:
        flat = _Published(_Row)
        with pytest.raises(ValueError, match=r"the \['name'\] columns have an empty cell, a short row or no column"):
            flat.rows(b"VALUE,NOTE\n1.5,x\n")

    def test_a_header_naming_a_label_no_field_has_is_a_layout_bug(self) -> None:
        class Misheaded(HeadersCsv[_Row]):
            _headers = (("NAME", "RATE"),)

        with pytest.raises(ValueError, match=r"the headers name \['RATE'\], which no field of _Row labels"):
            Misheaded(_Row)

    def test_a_column_the_file_leaves_out_raises_by_default(self) -> None:
        with pytest.raises(ValueError, match="column names specified in schema not found"):
            CsvFile(_Row).rows(b"NAME,VALUE\na,1.5\n")


class _Renamed(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("NAME", alternatives=("TITLE", "ENTITY"))]
    value: Annotated[float | None, Label("VALUE")]
    note: Annotated[str | None, Label("NOTE")]


class _Named(NamedCsv[_Renamed]):
    _carried = ("NAME",)


class _Folded(_Named):
    @typing.override
    def _heading(self, cell: str) -> str:
        return cell.upper()


class TestANamedCsv:
    def test_any_of_its_labels_read_in_any_order_and_a_column_it_lacks_is_null(self) -> None:
        frame = _Named(_Renamed).frame(b"NOTE,NAME\nx,a\n")
        assert frame.columns == ["name", "value", "note"]
        assert frame.rows() == [("a", None, "x")]
        assert _Named(_Renamed).rows(b"VALUE,NOTE,NAME\n1.5,y,b\n") == [_Renamed("b", 1.5, "y")]

    def test_a_label_s_alternative_reads_as_its_field(self) -> None:
        assert _Named(_Renamed).rows(b"TITLE,VALUE\na,1.5\n") == [_Renamed("a", 1.5, None)]
        assert _Named(_Renamed).rows(b"VALUE,ENTITY\n2,b\n") == [_Renamed("b", 2.0, None)]

    def test_a_format_folds_its_headers_wording(self) -> None:
        assert _Folded(_Renamed).rows(b"name,Value\na,1.5\n") == [_Renamed("a", 1.5, None)]

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (b"NAME,RATE\na,1\n", r"^the header names \['RATE'\], which no field of _Renamed labels$"),
            (b"NAME,TITLE\na,b\n", r"^the header \['NAME', 'TITLE'\] names a column twice$"),
            (b"NAME,NAME\na,b\n", r"names a column twice$"),
            (b"VALUE,NOTE\n1.5,x\n", r"^the header \['VALUE', 'NOTE'\] lacks \['NAME'\], which every one names$"),
            (b"NAME,VALUE\na,1.5,x\n", r"^row 1 has 3 fields, not the header's 2$"),
            (b"NAME,VALUE\na,1.5\nb\n", r"^row 2 has 1 fields, not the header's 2$"),
            (b"NAME,VAL\xe9\na,1\n", r"^the header is not UTF-8"),
        ],
        ids=[
            "an unknown label",
            "a label and its alternative",
            "a label twice",
            "no carried label",
            "a long row",
            "a short row",
            "a header not UTF-8",
        ],
    )
    def test_a_header_or_row_that_is_not_the_file_s_raises(self, body: bytes, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            _Named(_Renamed).rows(body)

    def test_a_label_two_fields_share_or_a_carried_label_no_field_has_is_a_layout_bug(self) -> None:
        class Shared(msgspec.Struct, frozen=True, gc=False):
            name: Annotated[str, Label("NAME", alternatives=("NOTE",))]
            note: Annotated[str, Label("NOTE")]

        class Stray(NamedCsv[_Renamed]):
            _carried = ("RATE",)

        with pytest.raises(ValueError, match=r"^two fields of Shared share a label"):
            NamedCsv(Shared)
        with pytest.raises(ValueError, match=r"^the carried labels \['RATE'\] are no field of _Renamed's labels$"):
            Stray(_Renamed)

    def test_a_reader_that_does_not_read_a_header_by_name_refuses_alternatives(self) -> None:
        with pytest.raises(ValueError, match=r"^the field 'name' names Label alternatives, which only a header read"):
            CsvFile(_Renamed)

    def test_a_format_derives_a_date_s_format(self) -> None:
        class Day(msgspec.Struct, frozen=True, gc=False):
            day: Annotated[dt.date, Label("DAY")]

        class Derived(NamedCsv[Day]):
            @typing.override
            def _format(self, scalar: object) -> Format | None:
                return Format("%Y%m%d") if scalar is dt.date else None

        assert Derived(Day).rows(b"DAY\n20261112\n") == [Day(dt.date(2026, 11, 12))]


class TestTheLineEnding:
    def test_lines_ended_by_cr_alone_are_refused_as_a_parse_error(self) -> None:
        with pytest.raises(ValueError, match=r"^the header line is not one row of cells: "):
            CsvFile(_Row).rows(b"NAME,VALUE,NOTE\ra,1.5,x\r")

    @pytest.mark.parametrize(
        ("final_line_ending", "body"),
        [(True, b"NAME,VALUE,NOTE\na,1.5,x"), (False, b"NAME,VALUE,NOTE\na,1.5,x\n")],
        ids=["always ends one, cut", "never ends one, cut"],
    )
    def test_a_body_that_ends_unlike_the_file_was_cut(self, *, final_line_ending: bool, body: bytes) -> None:
        with pytest.raises(ValueError, match="the body's end is not the file's"):
            CsvFile(_Row, final_line_ending=final_line_ending).rows(body)

    @pytest.mark.parametrize("body", [b"NAME,VALUE,NOTE\na,1.5,x", b"NAME,VALUE,NOTE\na,1.5,x\r\n"])
    def test_unchecked_it_reads_either_end(self, body: bytes) -> None:
        assert CsvFile(_Row).rows(body) == [_Row("a", 1.5, "x")]


class TestTheRowAnErrorNames:
    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (b"DAY,VALUE\n01-14-2005,100.00\n01-21-2005,\n", "columns have an empty cell"),
            (b"DAY,VALUE\n01-14-2005,100.00\n01-21-2005,NaN\n", "columns hold NaN or an infinity"),
            (b"DAY,VALUE\n01-14-2005,100.00\n1-21-2005,75.70\n", "is not a date written %m-%d-%Y"),
        ],
        ids=["an empty cell", "NaN", "a date written another way"],
    )
    def test_it_is_the_first_bad_row_after_the_header(self, body: bytes, message: str) -> None:
        with pytest.raises(ValueError, match=rf"{message}.*: row 2$"):
            CsvFile(_Dated).rows(body)


class TestASeparator:
    def test_a_pipe_separated_file_reads_its_cells_and_counts_its_rows_by_it(self) -> None:
        flat = CsvFile(_Row, separator="|")
        assert flat.rows(b'NAME|VALUE|NOTE\na,b|1.5|"x|y"\n') == [_Row("a,b", 1.5, "x|y")]
        with pytest.raises(ValueError, match=r"^row 1 has 2 fields, not the header's 3$"):
            flat.rows(b"NAME|VALUE|NOTE\na|1.5\n")


class _Unquoted(CsvFile[_Row]):
    _quote_char = None


class TestAFormatThatQuotesNothing:
    def test_a_lone_quote_in_a_cell_is_text(self) -> None:
        flat = _Unquoted(_Row, separator="|")
        assert flat.rows(b'NAME|VALUE|NOTE\n12" pipe|1.5|x\n') == [_Row('12" pipe', 1.5, "x")]
        with pytest.raises(ValueError, match=r"^row 1 has 4 fields, not the header's 3$"):
            flat.rows(b'NAME|VALUE|NOTE\n"a|b"|1.5|x\n')

    def test_where_quotes_are_quoting_a_lone_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="malformed"):
            CsvFile(_Row, separator="|").rows(b'NAME|VALUE|NOTE\n12" pipe|1.5|x\n')


class _Inch(msgspec.Struct, frozen=True, gc=False):
    size: Annotated[str, Label('Size 12"')]
    other: Annotated[str, Label("B")]


class _InchCsv(CsvFile[_Inch]):
    _quote_char = None


class TestAHeaderWithAnOddQuote:
    def test_a_format_that_quotes_nothing_reads_a_header_with_a_lone_quote(self) -> None:
        assert _InchCsv(_Inch).rows(b'Size 12",B\na,b\n') == [_Inch("a", "b")]

    def test_a_quoted_label_may_hold_a_line_ending_where_the_format_quotes(self) -> None:
        class Held(msgspec.Struct, frozen=True, gc=False):
            size: Annotated[str, Label("A\nB")]
            other: Annotated[str, Label("C")]

        assert CsvFile(Held).rows(b'"A\nB",C\na,b\n') == [Held("a", "b")]

    def test_a_body_whose_quote_never_closes_is_refused_in_linear_work(self) -> None:
        started = time.perf_counter()
        with pytest.raises(ValueError, match=r"malformed|not found|header"):
            CsvFile(_Row).rows(b'"NAME,VALUE,NOTE\n' + b"a,1,x\n" * 600_000)
        assert time.perf_counter() - started < 5  # recounting the quotes from byte 0 a line takes minutes here


# --- A header-less CsvFile ---


class _Unlabeled(msgspec.Struct, frozen=True, gc=False):
    name: str
    count: int
    note: str | None


class _Bare(CsvFile[_Unlabeled]):
    _header = False


class TestAHeaderlessCsv:
    def test_every_line_is_a_row_its_cells_the_fields_by_position(self) -> None:
        bare = _Bare(_Unlabeled, separator="\t")
        assert bare.rows(b"a\t1\t\nb\t2\tx\n") == [_Unlabeled("a", 1, None), _Unlabeled("b", 2, "x")]
        assert bare.frame(b"a\t1\t\n").columns == ["name", "count", "note"]

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (b"a\t1\t\nb\t2\n", "row 2 has 2 fields, not row 1's 3"),
            (b"a\t1\t\nb\t2\tx\ty\n", "row 2 has 4 fields, not row 1's 3"),
            (b"a\t1\nb\t2\n", "row 1 has 2 fields, not the file's 3"),
        ],
        ids=["short", "long", "every row short"],
    )
    def test_a_row_not_as_wide_as_the_fields_raises(self, body: bytes, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            _Bare(_Unlabeled, separator="\t").rows(body)


# --- TextFile._as_written ---


class _Cells(TextFile[_Row]):
    _as_written = ("name",)

    def __init__(self) -> None:
        super().__init__(_Row)

    @typing.override
    def _cells(self, body: bytes) -> pl.DataFrame:
        lines = [line.split(",") for line in body.decode().split("\n") if line]
        return pl.DataFrame(lines, schema=dict.fromkeys(self._labels, pl.String), orient="row")


class TestATextFileKeepingTextAsWritten:
    def test_a_named_field_keeps_empty_and_blank_text_and_the_others_do_not(self) -> None:
        assert _Cells().rows(b",1.5,x\n  ,2.5, \n") == [_Row("", 1.5, "x"), _Row("  ", 2.5, None)]


# --- SpilledCsv ---


class _Spills(msgspec.Struct, frozen=True, gc=False):
    id: Annotated[int, Label("ID")]
    name: Annotated[str, Label("NAME")]
    value: Annotated[float, Label("VALUE")]


class _Spilled(SpilledCsv[_Spills]):
    _quote_char = None
    _spilled = "name"


class _Edges(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("NAME")]
    value: Annotated[float, Label("VALUE")]
    note: Annotated[str, Label("NOTE")]


class TestASpilledCsv:
    def _read(self, body: bytes) -> list[_Spills]:
        return _Spilled(_Spills, separator="|").rows(body)

    def test_a_wide_row_keeps_its_separator_in_the_field(self) -> None:
        body = b"ID|NAME|VALUE\n1|BAM| ENTMT INC|1.5\n2|PLAIN|2.5\n3|A|B|C|3.5\n"
        assert self._read(body) == [
            _Spills(1, "BAM| ENTMT INC", 1.5),
            _Spills(2, "PLAIN", 2.5),
            _Spills(3, "A|B|C", 3.5),
        ]

    def test_the_place_is_the_body_s_header_s_not_the_struct_s(self) -> None:
        body = b"NAME|ID|VALUE\nBAM| X|7|1.5\nPLAIN|8|2.5\n"
        assert self._read(body) == [_Spills(7, "BAM| X", 1.5), _Spills(8, "PLAIN", 2.5)]

    def test_the_field_may_be_the_first_or_the_last_column_and_lines_may_end_in_crlf(self) -> None:
        assert self._read(b"NAME|ID|VALUE\r\nA|B|7|1.5\r\n") == [_Spills(7, "A|B", 1.5)]

        class _Tail(SpilledCsv[_Edges]):
            _quote_char = None
            _spilled = "note"

        assert _Tail(_Edges, separator="|").rows(b"NAME|VALUE|NOTE\r\na|1.5|x|y|z\r\nb|2.5|w\r\n") == [
            _Edges("a", 1.5, "x|y|z"),
            _Edges("b", 2.5, "w"),
        ]

    def test_a_carrier_in_the_body_is_not_used(self) -> None:
        assert self._read(b"ID|NAME|VALUE\n1|A\x1fB|C|1.5\n") == [_Spills(1, "A\x1fB|C", 1.5)]

    def test_a_short_row_still_raises(self) -> None:
        with pytest.raises(ValueError, match="row 1"):
            self._read(b"ID|NAME|VALUE\n1|BAM\n")

    def test_a_header_without_the_field_raises(self) -> None:
        with pytest.raises(ValueError, match="lacks 'NAME'"):
            self._read(b"ID|VALUE\n1|1.5\n")

    def test_a_format_that_cannot_spill_raises_at_construction(self) -> None:
        class _Unknown(SpilledCsv[_Spills]):
            _quote_char = None
            _spilled = "nothing"

        class _Number(SpilledCsv[_Spills]):
            _quote_char = None
            _spilled = "value"

        class _Quoted(SpilledCsv[_Spills]):
            _spilled = "name"

        class _Headerless(SpilledCsv[_Spills]):
            _quote_char = None
            _header = False
            _spilled = "name"

        for kind, message in (
            (_Unknown, "is no text field"),
            (_Number, "is no text field"),
            (_Quoted, "quotes nothing"),
            (_Headerless, "quotes nothing"),
        ):
            with pytest.raises(ValueError, match=message):
                kind(_Spills, separator="|")


# --- TrailerCsv and Flag ---


class _Counted(TrailerCsv[_Row]):
    _trailer = re.compile(r"(?P<count>\d+)")


class _Stamped(TrailerCsv[_Row]):
    _trailer = re.compile(r"\d{14}")


class _Generated(msgspec.Struct, frozen=True, gc=False):
    generated_at: Annotated[dt.datetime, Format("%Y%m%d%H%M%S")]
    name: Annotated[str, Label("NAME")]
    value: Annotated[float | None, Label("VALUE")]


class _StampedRows(TrailerCsv[_Generated]):
    _trailer = re.compile(r"(?P<generated_at>\d{14})")


class _Both(_Counted, HeadersCsv[_Row]):
    _headers = (("NAME", "VALUE", "NOTE"), ("NAME", "VALUE"))


class TestATrailerCsv:
    def test_its_last_line_is_the_trailer_and_its_count_the_rows(self) -> None:
        assert _Counted(_Row).rows(b"NAME,VALUE,NOTE\na,1.5,x\nb,2.5,y\n2\n") == [
            _Row("a", 1.5, "x"),
            _Row("b", 2.5, "y"),
        ]
        assert _Counted(_Row).rows(b"NAME,VALUE,NOTE\na,1.5,x\n1") == [_Row("a", 1.5, "x")]
        assert _Stamped(_Row).rows(b"NAME,VALUE,NOTE\na,1.5,x\r\n20261106030225\r\n") == [_Row("a", 1.5, "x")]

    def test_the_trailer_vouches_for_a_table_of_no_rows(self) -> None:
        assert _Counted(_Row).rows(b"NAME,VALUE,NOTE\n0\n") == []
        assert _Stamped(_Row).rows(b"NAME,VALUE,NOTE\n20261106030225\n") == []

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (
                b"NAME,VALUE,NOTE\na,1.5,x\nb,2.5,y\n",
                r"the last line 'b,2.5,y' is not the file's trailer: the body was cut",
            ),
            (b"NAME,VALUE,NOTE\na,1.5,x\n3\n", "the trailer counts 3 rows, and the body holds 1"),
            (b"2\n", "the last line '2' is not the file's trailer"),
        ],
        ids=["cut", "another count", "the trailer alone"],
    )
    def test_a_body_its_trailer_does_not_vouch_for_raises(self, body: bytes, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            _Counted(_Row).rows(body)

    def test_a_group_named_for_a_field_is_its_value_on_every_row(self) -> None:
        stamped = _StampedRows(_Generated)
        at = dt.datetime(2026, 11, 6, 3, 2, 25)
        assert stamped.rows(b"NAME,VALUE\na,1.5\nb,\n20261106030225\n") == [
            _Generated(at, "a", 1.5),
            _Generated(at, "b", None),
        ]
        assert stamped.frame(b"NAME,VALUE\n20261106030225\n").columns == ["generated_at", "name", "value"]
        with pytest.raises(ValueError, match="the 'generated_at' cell '20261399030225' is not a time written"):
            stamped.rows(b"NAME,VALUE\na,1.5\n20261399030225\n")
        with pytest.raises(ValueError, match="column names not specified in schema"):
            stamped.rows(b"generated_at,NAME,VALUE\nx,a,1.5\n20261106030225\n")

    def test_a_group_named_for_a_whole_number_field_is_that_number(self) -> None:
        class Batched(msgspec.Struct, frozen=True, gc=False):
            batch: int
            name: Annotated[str, Label("NAME")]

        class Trailed(TrailerCsv[Batched]):
            _trailer = re.compile(r"batch (?P<batch>\S+)")

        assert Trailed(Batched).rows(b"NAME\na\nb\nbatch 7\n") == [Batched(7, "a"), Batched(7, "b")]
        assert Trailed(Batched).frame(b"NAME\nbatch 7\n").schema["batch"] == pl.Int64
        with pytest.raises(ValueError, match="the 'batch' cell 'x' is not a whole number"):
            Trailed(Batched).rows(b"NAME\na\nbatch x\n")

    def test_a_quoted_line_ending_takes_a_line_and_no_row(self) -> None:
        """The rows above the trailer are not counted by its lines."""
        assert _Counted(_Row).rows(b'NAME,VALUE,NOTE\n"a\nb",1.5,x\nc,2.5,y\n2\n') == [
            _Row("a\nb", 1.5, "x"),
            _Row("c", 2.5, "y"),
        ]

    def test_a_blank_line_above_the_trailer_is_a_short_row(self) -> None:
        with pytest.raises(ValueError, match=r"^row 2 has 1 fields, not the header's 3$"):
            _Counted(_Row).rows(b"NAME,VALUE,NOTE\na,1.5,x\n\nc,2.5,y\n2\n")

    def test_it_composes_with_headers(self) -> None:
        assert _Both(_Row).rows(b"NAME,VALUE\na,1.5\n1\n") == [_Row("a", 1.5, None)]
        with pytest.raises(ValueError, match="the header \\['VALUE', 'NOTE'\\] is none of the 2"):
            _Both(_Row).rows(b"VALUE,NOTE\n1.5,x\n1\n")


# --- TitledCsv ---


class _Priced(TitledCsv[_Row]):
    _titles = (re.compile(r"PRICES as of \d{2}/\d{2}/\d{4}"), re.compile(r"(?P<count>\d+) rows"), re.compile(""))


class _Framed(_Priced, _Both):
    """Titles above, a trailer below, either header: every CSV piece at once."""


class _AsOf(TitledCsv[_Generated]):
    """A title stating a field: the time the file was written, on every row."""

    _titles = (re.compile(r"as of (?P<generated_at>\d{14})"),)


class TestATitledCsv:
    def test_its_header_sits_below_its_titles_and_their_count_is_the_rows(self) -> None:
        assert _Priced(_Row).rows(b"PRICES as of 11/09/2026\n2 rows\n\nNAME,VALUE,NOTE\na,1.5,x\nb,2.5,y\n") == [
            _Row("a", 1.5, "x"),
            _Row("b", 2.5, "y"),
        ]
        assert _Priced(_Row).rows(
            codecs.BOM_UTF8 + b"PRICES as of 11/09/2026\r\n1 rows\r\n\r\nNAME,VALUE,NOTE\r\na,1.5,x\r\n"
        ) == [_Row("a", 1.5, "x")]

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (b"PRICES\n1 rows\n\nNAME,VALUE,NOTE\na,1.5,x\n", r"line 1 'PRICES' is not the file's title 'PRICES as of"),
            (
                b"PRICES as of 11/09/2026\n1 rows\nNAME,VALUE,NOTE\na,1.5,x\n",
                "line 3 'NAME,VALUE,NOTE' is not the file's title ''",
            ),
            (b"PRICES as of 11/09/2026\n1 rows", r"the body ends before line 2, the file's title '\(\?P<count>"),
            (
                b"PRICES as of 11/09/2026\n3 rows\n\nNAME,VALUE,NOTE\na,1.5,x\n",
                "the title counts 3 rows, and the body holds 1",
            ),
        ],
        ids=["another title", "a title missing", "cut in its titles", "another count"],
    )
    def test_a_body_its_titles_do_not_open_raises(self, body: bytes, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            _Priced(_Row).rows(body)

    def test_a_group_named_for_a_field_states_it_on_every_row(self) -> None:
        written = dt.datetime(2026, 11, 9, 16, 30)
        assert _AsOf(_Generated).rows(b"as of 20261109163000\nNAME,VALUE\na,1.5\nb,\n") == [
            _Generated(written, "a", 1.5),
            _Generated(written, "b", None),
        ]
        assert _AsOf(_Generated).frame(b"as of 20261109163000\nNAME,VALUE\na,1.5\n").columns == [
            "generated_at",
            "name",
            "value",
        ]

    def test_a_title_group_that_did_not_match_states_nothing_and_a_required_field_raises(self) -> None:
        class Maybe(TitledCsv[_Generated]):
            _titles = (re.compile(r"as of (?P<generated_at>\d{14})|a note"),)

        assert Maybe(_Generated).rows(b"as of 20261109163000\nNAME,VALUE\na,1.5\n")[0].name == "a"
        with pytest.raises(ValueError, match="have an empty cell"):
            Maybe(_Generated).rows(b"a note\nNAME,VALUE\na,1.5\n")

    def test_a_count_group_is_the_rows_not_a_field_and_a_count_column_reads(self) -> None:
        class Counted(msgspec.Struct, frozen=True, gc=False):
            count: Annotated[int, Label("COUNT")]

        class Titled(TitledCsv[Counted]):
            _titles = (re.compile(r"(?P<count>\d+) rows"),)

        assert Titled(Counted).rows(b"2 rows\nCOUNT\n7\n8\n") == [Counted(7), Counted(8)]
        with pytest.raises(ValueError, match="the title counts 3 rows, and the body holds 2"):
            Titled(Counted).rows(b"3 rows\nCOUNT\n7\n8\n")

    def test_a_group_named_for_a_whole_number_field_is_that_number(self) -> None:
        class Batched(msgspec.Struct, frozen=True, gc=False):
            batch: int
            name: Annotated[str, Label("NAME")]

        class Titled(TitledCsv[Batched]):
            _titles = (re.compile(r"batch (?P<batch>\S+)"),)

        assert Titled(Batched).rows(b"batch 7\nNAME\na\n") == [Batched(7, "a")]
        with pytest.raises(ValueError, match="the 'batch' cell 'x' is not a whole number"):
            Titled(Batched).rows(b"batch x\nNAME\na\n")

    def test_it_composes_with_a_trailer_and_headers(self) -> None:
        assert _Framed(_Row).rows(b"PRICES as of 11/09/2026\n1 rows\n\nNAME,VALUE\na,1.5\n1\n") == [
            _Row("a", 1.5, None)
        ]
        with pytest.raises(ValueError, match="the trailer counts 2 rows, and the body holds 1"):
            _Framed(_Row).rows(b"PRICES as of 11/09/2026\n1 rows\n\nNAME,VALUE\na,1.5\n2\n")


class _Flagged(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("NAME")]
    open: Annotated[bool | None, Label("OPEN"), Flag("Y", "N")]


class TestAFlag:
    def test_a_boolean_reads_by_its_words(self) -> None:
        assert CsvFile(_Flagged).rows(b"NAME,OPEN\na,Y\nb,N\nc,\n") == [
            _Flagged("a", open=True),
            _Flagged("b", open=False),
            _Flagged("c", open=None),
        ]
        with pytest.raises(ValueError, match="the 'open' cell 'yes' is not 'Y' or 'N': row 1"):
            CsvFile(_Flagged).rows(b"NAME,OPEN\na,yes\n")

    def test_a_flag_on_another_field_raises(self) -> None:
        class Misflagged(msgspec.Struct, frozen=True, gc=False):
            name: Annotated[str, Label("NAME"), Flag("Y", "N")]

        with pytest.raises(ValueError, match="the field 'name' is no boolean, so it takes no Flag"):
            CsvFile(Misflagged)


# --- SheetFile ---


class _Sheet(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("Name")]
    count: Annotated[int, Label("Count")]
    rate: Annotated[float | None, Label("Rate")]
    open: Annotated[bool, Label("Open")]
    day: Annotated[dt.date, Label("Day")]
    written: Annotated[dt.date | None, Label("Written"), Format("%d-%b-%Y")]
    stamp: Annotated[dt.datetime, Label("Stamp")]


_HEADER: list[Cell] = ["Name", "Count", "Rate", "Open", "Day", "Written", "Stamp"]
_LINE: list[Cell] = ["a", 2, 13442.008862616791, True, dt.date(2026, 1, 2), "14-Jun-1961", dt.date(2026, 1, 3)]


def _book(*rows: list[Cell], sheet: str = "S") -> bytes:
    """Return a workbook of one sheet *sheet*: *rows*, the first of them its first row."""
    return workbook({sheet: rows})


def _with(**cells: Cell) -> list[Cell]:
    """Return ``_LINE`` with the cells named by their header labels replaced."""
    return [cells.get(label, cell) for label, cell in zip(_HEADER, _LINE, strict=True)]


class TestASheetFile:
    def test_a_clean_table_reads_typed_each_number_whole(self) -> None:
        sheet = SheetFile(_Sheet, sheet="S")
        body = _book(_HEADER, _LINE, _with(Name="b", Rate=None, Written=None))
        first, second = sheet.rows(body)
        assert msgspec.structs.astuple(first) == (
            "a",
            2,
            13442.008862616791,
            True,
            dt.date(2026, 1, 2),
            dt.date(1961, 6, 14),
            dt.datetime(2026, 1, 3),
        )
        assert (second.name, second.rate, second.written) == ("b", None, None)
        schema = sheet.frame(body).schema
        assert (schema["count"], schema["day"], schema["stamp"]) == (pl.Int64, pl.Date, pl.Datetime("us"))

    def test_titles_above_the_header_are_skipped_and_rows_take_the_sheets_numbers(self) -> None:
        sheet = SheetFile(_Sheet, sheet="S", header_row=3)
        body = _book(["A title"], [], _HEADER, _LINE, _with(Count=None))
        with pytest.raises(ValueError, match=r"the \['count'\] columns have an empty cell.*: row 5$"):
            sheet.rows(body)
        assert len(sheet.rows(_book(["A title"], [], _HEADER, _LINE))) == 1

    def test_the_sheets_words_for_none_read_as_none(self) -> None:
        sheet = SheetFile(_Sheet, sheet="S", null_values=["#N/A"])
        assert sheet.rows(_book(_HEADER, _with(Rate="#N/A")))[0].rate is None
        with pytest.raises(ValueError, match="the 'Rate' cell '#N/A' is not a number: row 2"):
            SheetFile(_Sheet, sheet="S").rows(_book(_HEADER, _with(Rate="#N/A")))

    def test_an_empty_text_cell_is_none(self) -> None:
        class Named(msgspec.Struct, frozen=True, gc=False):
            name: Annotated[str | None, Label("Name")]
            count: Annotated[int, Label("Count")]

        assert SheetFile(Named, sheet="S").rows(_book(["Name", "Count"], ["", 1])) == [Named(None, 1)]

    @pytest.mark.parametrize(
        ("cells", "message"),
        [
            ({"Count": 1.5}, "the 'Count' cell '1.5' is not a whole number: row 2"),
            ({"Count": "two"}, "the 'Count' cell 'two' is not a whole number: row 2"),
            ({"Rate": "high"}, "the 'Rate' cell 'high' is not a number: row 2"),
            ({"Open": "Yes"}, "the 'Open' cell 'Yes' is not a boolean cell: row 2"),
            ({"Open": 1}, "the 'Open' cell '1' is not a boolean cell: row 2"),
            ({"Rate": True}, "the 'Rate' cell 'true' is not a number: row 2"),
            ({"Day": 45000}, "the 'Day' cell '45000' is not a date cell: row 2"),
            ({"Day": "2026-01-02"}, "the 'Day' cell '2026-01-02' is not a date cell: row 2"),
            ({"Day": "2026-01-02 00:00:00"}, "the 'Day' cell '2026-01-02 00:00:00' is not a date cell: row 2"),
            ({"Written": "1961-06-14"}, "the 'Written' cell '1961-06-14' is not a date written %d-%b-%Y: row 2"),
            ({"Stamp": "noon"}, "the 'Stamp' cell 'noon' is not a date or time cell: row 2"),
        ],
        ids=[
            "fraction",
            "text in int",
            "text in float",
            "text in bool",
            "number in bool",
            "bool in float",
            "number in date",
            "text in date",
            "text like a date cell",
            "format",
            "text in time",
        ],
    )
    def test_a_cell_not_its_columns_type_raises(self, cells: dict[str, Cell], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            SheetFile(_Sheet, sheet="S").rows(_book(_HEADER, _with(**cells)))

    def test_a_column_under_an_empty_header_cell_is_a_field_labeled_empty(self) -> None:
        class Dated(msgspec.Struct, frozen=True, gc=False):
            day: Annotated[dt.date, Label("")]
            rate: Annotated[float, Label("Rate")]
            note: Annotated[str | None, Label("")]

        sheet = SheetFile(Dated, sheet="S")
        assert sheet.rows(_book([None, "Rate", None], [dt.date(2026, 1, 2), 1.5, "x"])) == [
            Dated(dt.date(2026, 1, 2), 1.5, "x")
        ]
        with pytest.raises(ValueError, match=r"header \(row 1\) \['Day', 'Rate', ''\] is not \['', 'Rate', ''\]"):
            sheet.rows(_book(["Day", "Rate", None], [dt.date(2026, 1, 2), 1.5, "x"]))
        # A label fastexcel would give an empty cell is still a label, not an empty cell.
        with pytest.raises(ValueError, match="is not"):
            sheet.rows(_book(["__UNNAMED__0", "Rate", None], [dt.date(2026, 1, 2), 1.5, "x"]))
        with pytest.raises(ValueError, match="the 'day' cell 'soon' is not a date cell: row 2"):
            sheet.rows(_book([None, "Rate", None], ["soon", 1.5, "x"]))

    def test_a_time_of_day_holds_the_sheets_time_cells(self) -> None:
        class Clocked(msgspec.Struct, frozen=True, gc=False):
            release: Annotated[str, Label("Release")]
            at: Annotated[dt.time | None, Label("Time")]

        sheet = SheetFile(Clocked, sheet="S")
        assert sheet.rows(_book(["Release", "Time"], ["a", dt.time(9, 15)], ["b", None])) == [
            Clocked("a", dt.time(9, 15)),
            Clocked("b", None),
        ]
        assert sheet.frame(_book(["Release", "Time"], ["a", dt.time(9, 15)])).schema["at"] == pl.Time
        with pytest.raises(ValueError, match="the 'Time' cell '2026-01-02 00:00:00' is not a time cell: row 2"):
            sheet.rows(_book(["Release", "Time"], ["a", dt.date(2026, 1, 2)]))
        with pytest.raises(ValueError, match="the 'Time' cell '9:15' is not a time cell: row 2"):
            sheet.rows(_book(["Release", "Time"], ["a", "9:15"]))

    def test_a_label_written_twice_is_two_columns_by_their_places(self) -> None:
        class Rates(msgspec.Struct, frozen=True, gc=False):
            week: Annotated[dt.date, Label("Week")]
            first: Annotated[float, Label("Rate")]
            second: Annotated[float | None, Label("Rate")]

        sheet = SheetFile(Rates, sheet="S")
        assert sheet.rows(_book(["Week", "Rate", "Rate"], [dt.date(2026, 1, 1), 6.5, None])) == [
            Rates(dt.date(2026, 1, 1), 6.5, None)
        ]
        with pytest.raises(ValueError, match=r"\['Week', 'Rate', 'Fees'\] is not \['Week', 'Rate', 'Rate_1'\]"):
            sheet.rows(_book(["Week", "Rate", "Fees"], [dt.date(2026, 1, 1), 6.5, None]))

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (b"not a workbook", "the body is not a workbook"),
            (_book(_HEADER, _LINE, sheet="T"), r"the workbook has no sheet 'S'; its sheets are \['T'\]"),
            (_book(_HEADER[:-1], _LINE[:-1]), r"the 'S' sheet's header \(row 1\) .* is not"),
            (_book(_HEADER, _LINE, [*([None] * 7), "a note"]), r"the 'S' sheet's header \(row 1\) .*'Stamp', ''\]"),
            (_book(_HEADER, _LINE, [], _LINE), "the 'S' sheet's row 3 is empty"),
            (_book(_HEADER), "a header and no rows"),
        ],
        ids=["not a workbook", "no sheet", "another header", "a cell outside", "an empty row", "no rows"],
    )
    def test_a_workbook_that_is_not_the_table_raises(self, body: bytes, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            SheetFile(_Sheet, sheet="S").rows(body)

    def test_a_sheet_with_gaps_drops_its_empty_rows_and_names_its_own_rows(self) -> None:
        class Gapped(SheetFile[_Sheet]):
            _gaps = True

        sheet = Gapped(_Sheet, sheet="S")
        assert [row.name for row in sheet.rows(_book(_HEADER, _LINE, [], _with(Name="b")))] == ["a", "b"]
        with pytest.raises(ValueError, match=r"'Count' cell 'x' is not a whole number: row 4"):
            sheet.rows(_book(_HEADER, _LINE, [], _with(Count="x")))


class _Gapped[RowT: msgspec.Struct](SheetFile[RowT]):
    _gaps = True


class _Line(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str | None, Label("Name")]
    note: Annotated[str | None, Label("Note")]


class TestASheetWithGaps:
    def test_a_table_of_only_gaps_is_no_table(self) -> None:
        with pytest.raises(ValueError, match="a header and no rows"):
            _Gapped(_Line, sheet="S").rows(_book(["Name", "Note"], [], []))


class TestABlankRowSheet:
    def test_a_sheet_numbering_its_rows_by_place_keeps_a_blank_row(self) -> None:
        class Blank(SheetFile[_Optional]):
            _blank_rows = True

        rows = Blank(_Optional, sheet="S").rows(_book(["Name", "Count"], ["a", 1], [], [None, 2]))
        assert [(row.name, row.count) for row in rows] == [("a", 1), (None, None), (None, 2)]


class _Optional(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str | None, Label("Name")]
    count: Annotated[int | None, Label("Count")]


class TestANotedSheet:
    def test_the_table_ends_at_its_first_row_with_nothing_past_its_first_column(self) -> None:
        sheet = NotedSheet(_Sheet, sheet="S")
        with_source = _book(_HEADER, _LINE, ["Source: EIA"], ["Note: revised"])
        with_blank = _book(_HEADER, _LINE, _with(Name="b"), [], ["A disclosure"], [], ["Another"])
        assert [row.name for row in sheet.rows(with_source)] == ["a"]
        assert [row.name for row in sheet.rows(with_blank)] == ["a", "b"]
        assert [row.name for row in sheet.rows(_book(_HEADER, _LINE))] == ["a"]

    def test_a_cell_past_the_first_column_below_the_table_raises_naming_its_row(self) -> None:
        body = _book(_HEADER, _LINE, ["Notes:"], ["a note"], _LINE)
        with pytest.raises(ValueError, match=r"^the 'S' sheet's row 5 holds a cell past its first column under its"):
            NotedSheet(_Sheet, sheet="S").rows(body)

    def test_notes_and_no_rows_are_no_table(self) -> None:
        with pytest.raises(ValueError, match="a header and no rows"):
            NotedSheet(_Sheet, sheet="S").rows(_book(_HEADER, ["Notes:"], ["a note"]))


class _CodeCount(msgspec.Struct, frozen=True, gc=False):
    code: Annotated[str, Label("Code")]
    count: Annotated[int, Label("Count")]


# --- Several header rows, spacers and scattered record columns (LongFile over a sheet) ---


class _Stacked(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Label("Day")]
    group: str
    series: str
    value: float


class _StackedSheet(LongFile[_Stacked], DatedSheet[_Stacked]):
    _above = 1
    _key_count = 2
    _empty_rows = False
    _spacers = True
    _keys = re.compile(r"(?P<group>[^\x1f]+)\x1f(?P<series>[^\x1f]+)")


_D1, _D2 = dt.date(2026, 1, 2), dt.date(2026, 1, 9)


class TestAStackedHeader:
    def test_a_heading_over_the_header_names_each_column_to_its_right(self) -> None:
        body = _book(
            [None, "Stocks", None, "Flows", None],
            ["Day", "East", "West", "East", None],
            [_D1, 1, 2, None, None],
            [_D2, None, 3, 4, None],
            ["A note"],
            sheet="S",
        )
        sheet = _StackedSheet(_Stacked, sheet="S", header_row=2)
        assert sheet.rows(body) == [
            _Stacked(_D1, "Stocks", "East", 1.0),
            _Stacked(_D1, "Stocks", "West", 2.0),
            _Stacked(_D2, "Stocks", "West", 3.0),
            _Stacked(_D2, "Flows", "East", 4.0),
        ]

    def test_a_cell_in_a_column_with_no_heading_raises_naming_its_row(self) -> None:
        body = _book([None, "Stocks", None], ["Day", "East", None], [_D1, 1, "x"])
        with pytest.raises(ValueError, match=r"a column with no heading holds a cell: row 3$"):
            _StackedSheet(_Stacked, sheet="S", header_row=2).rows(body)

    def test_a_header_with_a_key_twice_or_none_raises(self) -> None:
        twice = _book(["Stocks", "Stocks", "Stocks"], ["Day", "East", "East"], [_D1, 1, 2])
        with pytest.raises(ValueError, match="names no key, or a column twice"):
            _StackedSheet(_Stacked, sheet="S", header_row=2).rows(twice)
        with pytest.raises(ValueError, match="the header opens"):
            _StackedSheet(_Stacked, sheet="S", header_row=2).rows(_book(["Day", "East"], [_D1, 1]))

    def test_the_header_rows_must_fit_the_sheet(self) -> None:
        with pytest.raises(ValueError, match="the header opens"):
            _StackedSheet(_Stacked, sheet="S", header_row=2).rows(_book([None, "Stocks"], ["Other", "East"], [_D1, 1]))

    def test_a_week_with_no_values_is_a_week_and_a_cell_under_the_dates_raises(self) -> None:
        sheet = _StackedSheet(_Stacked, sheet="S", header_row=2)
        head: list[list[Cell]] = [[None, "Stocks"], ["Day", "East"]]
        assert sheet.rows(_book(*head, [_D1, None], [_D2, 5])) == [_Stacked(_D2, "Stocks", "East", 5.0)]
        with pytest.raises(ValueError, match=r"row 5 holds a cell under its table"):
            sheet.rows(_book(*head, [_D1, 1], ["Notes"], ["x", 2]))


class _Scattered(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Label("Day")]
    last: Annotated[dt.date | None, Label("Last")]
    region: str
    value: float


class _ScatteredSheet(LongFile[_Scattered], DatedSheet[_Scattered]):
    _empty_rows = False
    _scattered = True
    _keys = re.compile(r".+")


class TestScatteredRecordColumns:
    def test_the_records_columns_are_found_by_label_among_the_keys(self) -> None:
        body = _book(["Day", "East", "Last", "West"], [_D1, 1, _D2, 2])
        assert _ScatteredSheet(_Scattered, sheet="S").rows(body) == [
            _Scattered(_D1, _D2, "East", 1.0),
            _Scattered(_D1, _D2, "West", 2.0),
        ]

    def test_a_record_column_missing_raises(self) -> None:
        with pytest.raises(ValueError, match="has no one 'Last' column"):
            _ScatteredSheet(_Scattered, sheet="S").rows(_book(["Day", "East"], [_D1, 1]))


class _Year(msgspec.Struct, frozen=True, gc=False):
    year: int
    name: Annotated[str, Label("Name")]


class TestASheetsFile:
    @staticmethod
    def _reader() -> SheetsFile[_Year]:
        class _Inner(msgspec.Struct, frozen=True, gc=False):
            name: Annotated[str, Label("Name")]

        return SheetsFile(
            _Year, sheets=re.compile(r"Report (?P<year>\d{4})"), each=lambda sheet: SheetFile(_Inner, sheet=sheet)
        )

    def test_every_sheet_is_read_and_states_its_year(self) -> None:
        body = workbook({"Report 2025": [["Name"], ["a"]], "Report 2026": [["Name"], ["b"], ["c"]]})
        assert self._reader().rows(body) == [_Year(2025, "a"), _Year(2026, "b"), _Year(2026, "c")]

    def test_a_sheet_of_another_name_or_a_table_that_is_not_its_own_raises(self) -> None:
        with pytest.raises(ValueError, match="the workbook's sheet 'Contents' is not one of"):
            self._reader().rows(workbook({"Report 2025": [["Name"], ["a"]], "Contents": [["x"]]}))
        with pytest.raises(ValueError, match="the 'Report 2026' sheet: "):
            self._reader().rows(workbook({"Report 2025": [["Name"], ["a"]], "Report 2026": [["Other"], ["b"]]}))
        with pytest.raises(ValueError, match="not a workbook"):
            self._reader().rows(b"<html/>")

    def test_a_sheet_text_per_sheet_reads_its_grid_from_the_workbook_opened_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Inner(msgspec.Struct, frozen=True, gc=False):
            name: Annotated[str, Label("Name")]

        reader = SheetsFile(
            _Year, sheets=re.compile(r"Report (?P<year>\d{4})"), each=lambda sheet: SheetText(_Inner, sheet=sheet)
        )
        body = workbook({"Report 2025": [["Name"], ["a"]], "Report 2026": [["Name"], ["b"]]})

        opened = sheets._workbook
        opens: list[bytes] = []

        def _counted(body: bytes) -> fastexcel.ExcelReader:
            opens.append(body)
            return opened(body)

        monkeypatch.setattr(sheets, "_workbook", _counted)
        assert reader.rows(body) == [_Year(2025, "a"), _Year(2026, "b")]
        assert len(opens) == 1


class TestASheetsFileOfNoRows:
    def test_a_year_of_no_rows_is_an_empty_frame_typed_as_the_rest(self) -> None:
        class Inner(msgspec.Struct, frozen=True, gc=False):
            name: Annotated[str, Label("Name")]

        class Open(SheetFile[Inner]):
            _rows_required = False

        class Years(SheetsFile[_Year]):
            _rows_required = False

        reader = Years(_Year, sheets=re.compile(r"Report (?P<year>\d{4})"), each=lambda sheet: Open(Inner, sheet=sheet))
        empty = reader.frame(workbook({"Report 2025": [["Name"]]}))
        assert empty.is_empty()
        assert empty.schema == {"year": pl.Int64, "name": pl.String}
        assert reader.rows(workbook({"Report 2025": [["Name"]], "Report 2026": [["Name"], ["a"]]})) == [
            _Year(2026, "a")
        ]


class TestACutSheet:
    def test_a_workbook_whose_sheet_is_cut_raises_value_error(self) -> None:
        whole = zipfile.ZipFile(io.BytesIO(_book(["Name", "Count"], ["a", 1])))
        cut = io.BytesIO()
        with zipfile.ZipFile(cut, "w") as archive:
            for name in whole.namelist():
                data = whole.read(name)
                archive.writestr(name, data[: len(data) // 2] if name == "xl/worksheets/sheet1.xml" else data)
        with pytest.raises(ValueError, match="the 'S' sheet cannot be loaded as the table: "):
            SheetFile(_CodeCount, sheet="S").rows(cut.getvalue())


class TestASheetOfLines:
    def test_a_format_may_keep_the_gaps_as_rows_of_no_value_for_the_sheet_s_row_numbers(self) -> None:
        class Line(msgspec.Struct, frozen=True, gc=False):
            text: Annotated[str | None, Label("Notes")]

        class Lines(SheetFile[Line]):
            _gaps = True

            @typing.override
            def _gapless(self, frame: pl.DataFrame, empty: pl.Series) -> pl.DataFrame:
                return frame

        body = _book(["Notes"], [], ["one"], [], ["two"])
        assert Lines(Line, sheet="S").rows(body) == [Line(None), Line("one"), Line(None), Line("two")]
        with pytest.raises(ValueError, match="row 2 is empty"):
            SheetFile(Line, sheet="S").rows(body)


# --- Sectioned and transcoded files, repeated headings read long ---


class _Cell(msgspec.Struct, frozen=True, gc=False):
    line: Annotated[str, Label("LINE")]
    column: int
    heading: str
    value: float


class _Tables(Transcoded[_Cell], LongFile[_Cell], SectionedCsv[_Cell], CsvFile[_Cell]):
    _encodings = ("cp1252",)
    _eof = bytes([0x1A])
    _section = re.compile(rb'^"LINE"', re.MULTILINE)
    _empty_rows = False
    _repeats = True
    _key_count = 2
    _keys = re.compile(r"(?P<heading>.*)", re.DOTALL)


_TWO = b'"LINE","Week","Change","Change"\r\n"a","1","2","3"\r\n"b","4","",""\r\n"LINE","Year"\r\n"c","5"\r\n'


class TestASectionedCsv:
    def test_each_table_reads_as_a_file_of_its_own(self) -> None:
        first = _Tables(_Cell, section=0, sections=2, final_line_ending=True)
        second = _Tables(_Cell, section=1, sections=2, final_line_ending=True)
        assert first.rows(_TWO) == [
            _Cell("a", 1, "Week", 1.0),
            _Cell("a", 2, "Change", 2.0),
            _Cell("a", 3, "Change", 3.0),
            _Cell("b", 1, "Week", 4.0),
        ]
        assert second.rows(_TWO) == [_Cell("c", 1, "Year", 5.0)]

    def test_the_file_must_open_with_a_header_and_hold_that_many_tables(self) -> None:
        with pytest.raises(ValueError, match=r"the body has 2 tables, at bytes \[0, \d+\], not 1 from the first byte"):
            _Tables(_Cell).rows(_TWO)
        with pytest.raises(ValueError, match=r"the body has 0 tables"):
            _Tables(_Cell).rows(b'"X"\r\n1\r\n')

    def test_a_table_the_file_does_not_hold_is_a_bug(self) -> None:
        with pytest.raises(ValueError, match="the file's 2 tables have no table 2"):
            _Tables(_Cell, section=2, sections=2)


class TestATranscodedFile:
    def test_the_body_is_read_in_its_encoding_and_its_end_of_file_byte_dropped(self) -> None:
        body = '"LINE","Week"\r\n"caf\u00e9","1"\r\n'.encode("cp1252") + bytes([0x1A])
        assert _Tables(_Cell, final_line_ending=True).rows(body) == [_Cell("café", 1, "Week", 1.0)]

    def test_a_body_that_does_not_decode_raises(self) -> None:
        with pytest.raises(ValueError, match="the body is not cp1252"):
            _Tables(_Cell).rows(b'"LINE","Week"\r\n"\x81","1"\r\n')


class _NameCount(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("Name")]
    count: Annotated[int, Label("Count")]


class _AnyEncoding(Transcoded[_NameCount], CsvFile[_NameCount]):
    _encodings = ("utf-16", "utf-8", "latin-1")


class _UTF8OrNothing(Transcoded[_NameCount], CsvFile[_NameCount]):
    _encodings = ("utf-8",)


class _StrayBytes(_UTF8OrNothing):
    _lossy = True


class TestATranscodedFilesEncodings:
    def test_each_encoding_is_tried_in_order(self) -> None:
        text = "Name,Count\r\ncafé,1\r\n"
        for body in (text.encode("utf-16"), text.encode("utf-8"), text.encode("latin-1")):
            assert _AnyEncoding(_NameCount).rows(body) == [_NameCount("café", 1)]

    def test_utf_16_is_tried_only_on_a_body_opening_with_its_byte_order_mark(self) -> None:
        body = "Name,Count\r\ncafé,123\r\n".encode("latin-1")
        assert len(body) % 2 == 0  # so UTF-16 would decode it, into other characters
        assert _AnyEncoding(_NameCount).rows(body) == [_NameCount("café", 123)]

    def test_a_body_no_encoding_decodes_raises_unless_it_is_lossy(self) -> None:
        body = b"Name,Count\r\nx\xff,1\r\n"
        with pytest.raises(ValueError, match="the body is not utf-8: "):
            _UTF8OrNothing(_NameCount).rows(body)
        assert _StrayBytes(_NameCount).rows(body) == [_NameCount("x\ufffd", 1)]


class _ClosedLines(CsvFile[_NameCount]):
    _closing_separator = "always"


class _ClosedRows(CsvFile[_NameCount]):
    _closing_separator = "sometimes"


class _Fillers(CsvFile[_NameCount]):
    _blank_columns = ("Filler", "Filler")
    _unread = ("Notes",)


class TestACsvsColumnsNoFieldReads:
    def test_every_line_closed_by_the_separator_reads_without_its_empty_last_cell(self) -> None:
        rows = _ClosedLines(_NameCount).rows(b"Name,Count,\r\nx,1,\r\ny,2,\r\n")
        assert rows == [_NameCount("x", 1), _NameCount("y", 2)]

    @pytest.mark.parametrize(
        ("body", "error"),
        [
            (b"Name,Count,\r\nx,1\r\n", "row 1 has 2 fields, not the header's 3"),
            (b"Name,Count,\r\nx,1,z\r\n", "the line's closing separator is followed by 'z'.*: row 1"),
            (b"Name,Count\r\nx,1\r\n", "not specified in schema|missing"),
        ],
        ids=["a row not closed", "a cell past the closing separator", "a header not closed"],
    )
    def test_a_line_not_closed_by_the_separator_raises(self, body: bytes, error: str) -> None:
        with pytest.raises(ValueError, match=error):
            _ClosedLines(_NameCount).rows(body)

    def test_a_row_may_close_with_the_separator(self) -> None:
        body = b"Name,Count\r\nx,1,\r\ny,2\r\n"
        assert _ClosedRows(_NameCount).rows(body) == [_NameCount("x", 1), _NameCount("y", 2)]

    def test_a_blank_column_is_dropped_and_an_unread_one_dropped_whatever_it_holds(self) -> None:
        body = b"Name,Filler,Count,Notes,Filler\r\nx,,1,a note,\r\n"
        assert _Fillers(_NameCount).rows(body) == [_NameCount("x", 1)]

    def test_a_blank_column_holding_a_value_raises(self) -> None:
        body = b"Name,Filler,Count,Notes,Filler\r\nx,,1,,\r\ny,,2,,v\r\n"
        with pytest.raises(ValueError, match="the 'Filler' column holds 'v', and it never holds a value: row 2"):
            _Fillers(_NameCount).rows(body)


class _TextCells(TextFile[_NameCount]):
    _blank_columns = ("Follow",)
    _unread = ("Notes",)

    @typing.override
    def _cells(self, body: bytes) -> pl.DataFrame:
        header, *rows = (line.split(",") for line in body.decode().splitlines())
        return pl.DataFrame(rows, schema=dict.fromkeys(header, pl.String), orient="row")


class TestATextTablesColumnsNoFieldReads:
    def test_a_blank_column_of_spaces_and_an_unread_one_are_dropped(self) -> None:
        assert _TextCells(_NameCount).rows(b"Name,Count,Notes,Follow\nx,1,a note,  \n") == [_NameCount("x", 1)]

    def test_a_blank_column_holding_a_value_raises(self) -> None:
        with pytest.raises(ValueError, match="the 'Follow' column holds 'y', and it never holds a value: row 1"):
            _TextCells(_NameCount).rows(b"Name,Count,Follow\nx,1,y\n")


class _Raw(msgspec.Struct, frozen=True, gc=False):
    label: Annotated[str, Label("Label")]
    n: Annotated[int, Label("N")]


class _Shaped(Reshaped[_NameCount]):
    @typing.override
    def _shape(self, frame: pl.DataFrame) -> pl.DataFrame:
        return frame.select(pl.col("label").str.to_uppercase().alias("name"), pl.col("n").alias("count"))


class TestAReshapedFile:
    def test_the_inner_readers_frame_is_shaped_into_the_rows(self) -> None:
        reader = _Shaped(_NameCount, CsvFile(_Raw))
        assert reader.rows(b"Label,N\r\nx,1\r\n") == [_NameCount("X", 1)]

    def test_a_frame_read_as_it_is_must_be_the_rows_fields(self) -> None:
        with pytest.raises(ValueError, match=r"the reader's columns \['label', 'n'\] are not _NameCount's fields"):
            Reshaped(_NameCount, CsvFile(_Raw)).rows(b"Label,N\r\nx,1\r\n")

    def test_the_inner_reader_checks_the_file(self) -> None:
        with pytest.raises(ValueError, match="row 1 has 1 fields, not the header's 2"):
            _Shaped(_NameCount, CsvFile(_Raw)).rows(b"Label,N\r\nx\r\n")


# --- A sheet's blanks, its headings and its header rows ---


class _Lead(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("Name")]
    count: Annotated[int, Label("Count")]
    day: Annotated[dt.date, Label("Day")]


class _Texts(msgspec.Struct, frozen=True, gc=False):
    text: Annotated[str | None, Label("Text")]
    count: Annotated[int | None, Label("Count")]


class _RepeatedRates(msgspec.Struct, frozen=True, gc=False):
    first: Annotated[float, Label("Rate")]
    second: Annotated[float, Label("Rate")]


class _TrimmedSheet(SheetFile[_Texts]):
    _trim = True


class _StrippedSheet(_TrimmedSheet):
    _strip = True


class TestAnAbsoluteColumn:
    def test_a_table_after_an_empty_column_is_typed_by_the_sheets_own_columns(self) -> None:
        body = _book([None, "Name", "Count", "Day"], [None, "a", 2, _D1])
        assert SheetFile(_Lead, sheet="S").rows(body) == [_Lead("a", 2, _D1)]


class TestATrimmedSheet:
    def test_a_cell_of_only_whitespace_is_empty_where_a_sheet_says_so(self) -> None:
        body = _book(["Text", "Count"], [" ", 1], ["a", "  "])
        assert _TrimmedSheet(_Texts, sheet="S").rows(body) == [_Texts(None, 1), _Texts("a", None)]
        with pytest.raises(ValueError, match="is not a whole number"):
            SheetFile(_Texts, sheet="S").rows(body)
        assert SheetFile(_Texts, sheet="S").rows(_book(["Text", "Count"], [" ", 1]))[0].text == " "

    def test_a_heading_a_sheet_pads_is_read_stripped_where_a_sheet_says_so(self) -> None:
        body = _book([" Text\u00a0", " Count "], ["a", 1])
        assert _StrippedSheet(_Texts, sheet="S").rows(body) == [_Texts("a", 1)]
        for refused in (_TrimmedSheet, SheetFile):
            with pytest.raises(ValueError, match="is not"):
                refused(_Texts, sheet="S").rows(body)

    def test_a_heading_a_sheet_writes_twice_is_twice(self) -> None:
        class Twice(SheetFile[_RepeatedRates]):
            _trim = True

        assert Twice(_RepeatedRates, sheet="S").rows(_book(["Rate", "Rate"], [1, 2])) == [_RepeatedRates(1.0, 2.0)]

    def test_the_header_is_the_sheets_own_row_not_the_first_row_with_a_cell(self) -> None:
        with pytest.raises(ValueError, match=r"the 'S' sheet's header \(row 1\) \['', ''\] is not"):
            _TrimmedSheet(_Texts, sheet="S").rows(_book([], ["Text", "Count"], ["a", 1]))

    def test_a_sheet_that_ends_before_its_header_raises(self) -> None:
        with pytest.raises(ValueError, match=r"the 'S' sheet ends at row [01], before its header row 3"):
            _TrimmedSheet(_Texts, sheet="S", header_row=3).rows(_book(["Text", "Count"]))

    def test_a_table_that_starts_after_cells_of_the_sheets_top_rows_is_refused(self) -> None:
        body = _book(["Title"], [None, "Text", "Count"], [None, "a", 1])
        with pytest.raises(ValueError, match=r"header row holds 3 columns, not the 2 loaded"):
            _TrimmedSheet(_Texts, sheet="S", header_row=2).rows(body)

    def test_the_rows_over_the_header_must_be_above_it(self) -> None:
        class Tall(SheetFile[_Texts]):
            _above = 2

        with pytest.raises(ValueError, match="the 2 rows over the header do not fit above its row 2"):
            Tall(_Texts, sheet="S", header_row=2)


class _SpacerSheet(LongFile[_Scattered], DatedSheet[_Scattered]):
    _empty_rows = False
    _scattered = True
    _spacers = True
    _trim = True
    _keys = re.compile(r".+")


class _NoLabel(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Label("")]
    region: str
    value: float


class _NoLabelSheet(LongFile[_NoLabel], DatedSheet[_NoLabel]):
    _empty_rows = False
    _scattered = True
    _spacers = True
    _keys = re.compile(r".+")


class TestALongSheetsHeadings:
    def test_a_spacer_is_a_column_with_no_heading_and_no_cell_where_a_sheet_says_so(self) -> None:
        body = _book(["Day", "East", None, "Last", "West"], [_D1, 1, None, _D2, 2])
        assert _SpacerSheet(_Scattered, sheet="S").rows(body) == [
            _Scattered(_D1, _D2, "East", 1.0),
            _Scattered(_D1, _D2, "West", 2.0),
        ]
        with pytest.raises(ValueError, match="are not keys"):
            _ScatteredSheet(_Scattered, sheet="S").rows(body)

    def test_an_empty_label_is_the_first_column_not_a_spacer(self) -> None:
        body = _book([None, "East", None, "West"], [_D1, 1, None, 2])
        assert _NoLabelSheet(_NoLabel, sheet="S").rows(body) == [
            _NoLabel(_D1, "East", 1.0),
            _NoLabel(_D1, "West", 2.0),
        ]

    def test_a_title_in_the_first_column_over_a_table_that_starts_in_the_second_is_refused(self) -> None:
        body = _book(["Title"], [None, "Day", "East", "Last", "West"], [None, _D1, 1, _D2, 2])
        with pytest.raises(ValueError, match=r"header row holds 5 columns, not the 4 loaded"):
            _SpacerSheet(_Scattered, sheet="S", header_row=2).rows(body)

    def test_a_heading_is_any_text_but_a_field_name(self) -> None:
        for heading in ("#", "\u0001", "row", "_Scattered"):
            body = _book(["Day", "Last", heading], [_D1, _D2, 1])
            assert _SpacerSheet(_Scattered, sheet="S").rows(body) == [_Scattered(_D1, _D2, heading, 1.0)]
        for field in ("value", "region", "day", "last"):
            with pytest.raises(ValueError, match="names no key, or a column twice"):
                _SpacerSheet(_Scattered, sheet="S").rows(_book(["Day", "Last", field, "East"], [_D1, _D2, 1, 2]))

    def test_a_heading_written_twice_is_twice_not_numbered(self) -> None:
        with pytest.raises(ValueError, match=r"names no key, or a column twice$") as caught:
            _SpacerSheet(_Scattered, sheet="S").rows(_book(["Day", "Last", "East", "East"], [_D1, _D2, 1, 2]))
        assert "'East', 'East'" in str(caught.value)

    def test_a_title_cell_beside_the_table_and_a_padded_heading_over_it_are_no_part_of_it(self) -> None:
        body = _book(
            [None, " Stocks ", None, "far"],
            ["Day", "East", "West"],
            [_D1, 1, 2],
        )
        sheet = _StackedSheet(_Stacked, sheet="S", header_row=2)
        assert sheet.rows(body) == [_Stacked(_D1, "Stocks", "East", 1.0), _Stacked(_D1, "Stocks", "West", 2.0)]

    def test_a_key_field_no_group_names_needs_headings_that_repeat(self) -> None:
        class NoGroups(LongFile[_Stacked], DatedSheet[_Stacked]):
            _above = 1
            _key_count = 2
            _keys = re.compile(r"[^\x1f]+\x1f[^\x1f]+")

        with pytest.raises(ValueError, match=r"the key fields \['group', 'series'\] are named by no group of"):
            NoGroups(_Stacked, sheet="S", header_row=2).rows(_book([None, "Stocks"], ["Day", "East"], [_D1, 1]))

    def test_a_group_matching_nothing_is_no_text_where_the_field_admits_none(self) -> None:
        class Maybe(msgspec.Struct, frozen=True, gc=False):
            day: Annotated[dt.date, Label("Day")]
            group: str | None
            series: str
            value: float

        class MaybeSheet(LongFile[Maybe], DatedSheet[Maybe]):
            _above = 1
            _key_count = 2
            _empty_rows = False
            _keys = re.compile(r"(?P<group>[^\x1f]*)\x1f(?P<series>[^\x1f]+)")

        class Needed(msgspec.Struct, frozen=True, gc=False):
            day: Annotated[dt.date, Label("Day")]
            group: str
            series: str
            value: float

        class NeededSheet(LongFile[Needed], DatedSheet[Needed]):
            _above = 1
            _key_count = 2
            _empty_rows = False
            _keys = MaybeSheet._keys

        body = _book([None, None, "Stocks"], ["Day", "East", "West"], [_D1, 1, 2])
        assert MaybeSheet(Maybe, sheet="S", header_row=2).rows(body) == [
            Maybe(_D1, None, "East", 1.0),
            Maybe(_D1, "Stocks", "West", 2.0),
        ]
        assert NeededSheet(Needed, sheet="S", header_row=2).rows(body)[0] == Needed(_D1, "", "East", 1.0)


class TestATableOfNoValues:
    class Quiet(_StackedSheet):
        _rows_required = False
        _bare_header_refused = True

    def test_dated_rows_with_no_value_are_an_empty_answer_and_no_rows_are_none(self) -> None:
        head: list[list[Cell]] = [[None, "Stocks"], ["Day", "East"]]
        sheet = self.Quiet(_Stacked, sheet="S", header_row=2)
        frame = sheet.frame(_book(*head, [_D1, None], [_D2, None]))
        assert frame.is_empty()
        assert frame.schema == {"day": pl.Date, "group": pl.String, "series": pl.String, "value": pl.Float64}
        for body in (_book(*head), _book(*head, ["a note"])):
            with pytest.raises(ValueError, match="a header and no rows"):
                sheet.frame(body)
        with pytest.raises(ValueError, match="a header and no rows"):
            _StackedSheet(_Stacked, sheet="S", header_row=2).frame(_book(*head, [_D1, None]))

    def test_a_bare_header_is_an_empty_answer_unless_the_format_refuses_it(self) -> None:
        class Open(LongFile[_Stacked], DatedSheet[_Stacked]):
            _above = 1
            _key_count = 2
            _rows_required = False
            _keys = re.compile(r"(?P<group>[^\x1f]*)\x1f(?P<series>[^\x1f]+)")

        head: list[list[Cell]] = [[None, "Stocks"], ["Day", "East"]]
        assert Open(_Stacked, sheet="S", header_row=2).frame(_book(*head)).is_empty()

    def test_a_table_with_no_key_column_is_empty_where_none_is_required_and_refused_otherwise(self) -> None:
        body = _book(["Stocks", None], ["Day", None], [_D1, None])
        assert self.Quiet(_Stacked, sheet="S", header_row=2).frame(body).schema == {
            "day": pl.Date,
            "group": pl.String,
            "series": pl.String,
            "value": pl.Float64,
        }
        with pytest.raises(ValueError, match="names no key, or a column twice"):
            _StackedSheet(_Stacked, sheet="S", header_row=2).frame(body)


class TestASectionedCsvsFirstByte:
    def test_a_line_before_the_first_header_is_no_part_of_a_table(self) -> None:
        with pytest.raises(ValueError, match=r"the body has 1 tables, at bytes \[\d+\], not 1 from the first byte"):
            _Tables(_Cell).rows(b'junk\r\n"LINE","Week"\r\n"a","1"\r\n')


class TestARepeatedHeadingInACsv:
    def test_a_heading_that_looks_like_a_repeats_name_is_its_own(self) -> None:
        real = b'"LINE","Change_duplicated_0","Week"\r\n"a","2","3"\r\n'
        assert [(row.heading, row.value) for row in _Tables(_Cell).rows(real)] == [
            ("Change_duplicated_0", 2.0),
            ("Week", 3.0),
        ]
        repeated = b'"LINE","Change","Change"\r\n"a","2","3"\r\n'
        assert [(row.column, row.heading) for row in _Tables(_Cell).rows(repeated)] == [(1, "Change"), (2, "Change")]

    def test_a_heading_that_is_a_repeats_name_beside_the_repeat_is_refused_by_polars(self) -> None:
        body = b'"LINE","Change","Change_duplicated_0","Change"\r\n"a","1","2","3"\r\n'
        with pytest.raises(ValueError, match="de-duplication"):
            _Tables(_Cell).rows(body)

    def test_a_long_csv_that_may_not_repeat_a_heading_reads_polars_names_for_a_repeat(self) -> None:
        seen: list[str] = []

        class Pairs(CsvFile[_CodeCount]):
            @typing.override
            def _layout(self, header: Sequence[str]) -> Sequence[_Column]:
                return [_Column(f"c{at}", cell, str, None, None, None, ()) for at, cell in enumerate(header)]

            @typing.override
            def _read(self, body: bytes) -> pl.DataFrame:
                seen.extend(self._table(body).columns)
                return pl.DataFrame({"code": ["a"], "count": [1]})

        Pairs(_CodeCount).frame(b"A,A,B\r\n1,2,3\r\n")
        assert seen == ["A", "A_duplicated_0", "B"]

    def test_a_heading_that_holds_a_line_ending_is_one_heading(self) -> None:
        body = b'"LINE","Per\ncent","Week"\r\n"a","2","3"\r\n'
        assert [row.heading for row in _Tables(_Cell).rows(body)] == ["Per\ncent", "Week"]

    def test_a_heading_with_escaped_quotes_is_read_unescaped(self) -> None:
        body = b'"LINE","A ""q"" B","C"\r\n"a","1","2"\r\n'
        assert [row.heading for row in _Tables(_Cell).rows(body)] == ['A "q" B', "C"]

    def test_a_leading_heading_holding_a_unit_separator_is_refused_not_a_missing_column(self) -> None:
        class Plain(LongFile[_Cell], CsvFile[_Cell]):
            _keys = _Tables._keys
            _key_count = 2
            _repeats = True

        for body in (b"X\x1fLINE,Week\r\na,1\r\n", b"LINE,A\x1fB\r\na,1\r\n"):
            with pytest.raises(ValueError, match=r"holds a U\+001F"):
                Plain(_Cell).rows(body)

    def test_a_heading_holding_a_nul_is_refused(self) -> None:
        for heading in ("A\x00B", "\x00A", "A\x00\x000"):
            body = f'"LINE","A","{heading}"\r\n"a","1","2"\r\n'.encode()
            with pytest.raises(ValueError, match="holds a NUL"):
                _Tables(_Cell).rows(body)
        with pytest.raises(ValueError, match="holds a NUL"):
            _SpacerSheet(_Scattered, sheet="S").rows(_book(["Day", "Last", "E\x00ast"], [_D1, _D2, 1]))

    def test_empty_headings_are_each_a_heading(self) -> None:
        for body in (b'"LINE","A","",""\r\n"a","1","2","3"\r\n', b'"LINE","A",,\r\n"a","1","2","3"\r\n'):
            assert [(row.column, row.heading, row.value) for row in _Tables(_Cell).rows(body)] == [
                (1, "A", 1.0),
                (2, "", 2.0),
                (3, "", 3.0),
            ]

    def test_a_heading_that_is_a_field_name_or_the_row_index_is_a_refusal_or_a_heading_not_a_crash(self) -> None:
        for heading, read in (("#", True), ("value", False), ("column", False), ("heading", False)):
            body = f'"LINE","{heading}"\r\n"a","2"\r\n'.encode()
            if read:
                assert [row.heading for row in _Tables(_Cell).rows(body)] == [heading]
            else:
                with pytest.raises(ValueError, match="names no key, or a column twice"):
                    _Tables(_Cell).rows(body)


class _TitledSheet(TitledSheet[_Sheet]):
    _titles = (re.compile(r"Rates, \d{4}"), re.compile(""))


class TestATitledSheet:
    def test_each_row_above_its_header_is_one_of_its_titles(self) -> None:
        body = _book(["Rates, 2026"], [], _HEADER, _LINE)
        assert [row.name for row in _TitledSheet(_Sheet, sheet="S", header_row=3).rows(body)] == ["a"]

    def test_a_row_is_its_cells_joined_by_tabs_to_its_last_filled_one(self) -> None:
        class Spread(TitledSheet[_Sheet]):
            _titles = (re.compile("Rates\t\tin percent"),)

        body = _book(["Rates", None, "in percent", None], _HEADER, _LINE)
        assert [row.name for row in Spread(_Sheet, sheet="S", header_row=2).rows(body)] == ["a"]

    @pytest.mark.parametrize(
        ("body", "header_row", "message"),
        [
            pytest.param(
                _book(["Rates, 26"], [], _HEADER, _LINE),
                3,
                "the 'S' sheet's row 1 'Rates, 26' is not its title 'Rates, ",
                id="another-title",
            ),
            pytest.param(
                _book(["Rates, 2026"], [], ["a note"], _HEADER, _LINE),
                4,
                "the 'S' sheet has 3 rows above its header, not its 2 titles",
                id="another-count",
            ),
        ],
    )
    def test_rows_that_are_not_its_titles_raise(self, body: bytes, header_row: int, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            _TitledSheet(_Sheet, sheet="S", header_row=header_row).rows(body)


class TestAPreambleSheet:
    def test_its_header_is_the_first_row_whose_first_cell_is_its_first_label(self) -> None:
        sheet = PreambleSheet(_Sheet, sheet="S")
        short = _book(["A banner"], _HEADER, _LINE)
        long = _book(["A banner"], [None, "a link"], [], ["Name of the series"], _HEADER, _LINE, _with(Name="b"))
        # One reader reads both, the header found in each.
        assert [row.name for row in sheet.rows(long)] == ["a", "b"]
        assert [row.name for row in sheet.rows(short)] == ["a"]
        assert sheet.frame(short).height == 1

    def test_an_error_names_the_sheets_own_row(self) -> None:
        body = _book(["A banner"], [], _HEADER, _LINE, _with(Count="many"))
        with pytest.raises(ValueError, match="row 5"):
            PreambleSheet(_Sheet, sheet="S").rows(body)

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            pytest.param(_book(["A banner"], _LINE), "has no row whose first cell is 'Name'", id="no-header"),
            pytest.param(_book([]), "has no row whose first cell is 'Name'", id="empty-sheet"),
            pytest.param(b"<html>maintenance</html>", "the body is not a workbook", id="not-a-workbook"),
        ],
    )
    def test_a_body_without_its_header_raises(self, body: bytes, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            PreambleSheet(_Sheet, sheet="S").rows(body)


class TestATableOpeningPastColumnA:
    def test_each_column_takes_its_fields_type(self) -> None:
        """A dtype is keyed by the sheet's column, so a table opening at ``B`` is typed by its own columns."""
        frame = SheetFile(_Sheet, sheet="S").frame(_book([None, *_HEADER], [None, *_LINE]))
        assert frame.schema == pl.Schema(
            {
                "name": pl.String,
                "count": pl.Int64,
                "rate": pl.Float64,
                "open": pl.Boolean,
                "day": pl.Date,
                "written": pl.Date,
                "stamp": pl.Datetime("us"),
            }
        )


class TestAnOnlySheet:
    def test_it_reads_the_one_sheet_whatever_its_name(self) -> None:
        sheet = OnlySheet(_Sheet)
        assert [row.name for row in sheet.rows(_book(_HEADER, _LINE, sheet="RGDP"))] == ["a"]
        assert [row.name for row in sheet.rows(_book(_HEADER, _LINE, sheet="Median_Level"))] == ["a"]

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            pytest.param(
                workbook({"A": [_HEADER, _LINE], "B": []}), r"holds the sheets \['A', 'B'\], not one", id="two"
            ),
            pytest.param(b"<html>maintenance</html>", "the body is not a workbook", id="not-a-workbook"),
        ],
    )
    def test_a_workbook_of_another_count_raises(self, body: bytes, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            OnlySheet(_Sheet).rows(body)


class _Spaced(msgspec.Struct, frozen=True, gc=False):
    month: Annotated[dt.date, Label("Month"), Format("%Y-%m")]
    value: Annotated[float | None, Label("Value")]


class _SpacedSheet(SheetText[_Spaced]):
    """A table with an axis column the format does not read."""

    _unread = ("Axis",)


class TestASheetText:
    def test_its_table_is_its_header_and_every_filled_row_below_each_digit_kept(self) -> None:
        body = _book(["Month", "Axis", "Value"], ["2026-01", 0, 13442.008862616791], [], ["2026-02", 0.25, 1])
        assert _SpacedSheet(_Spaced, sheet="S").rows(body) == [
            _Spaced(dt.date(2026, 1, 1), 13442.008862616791),
            _Spaced(dt.date(2026, 2, 1), 1.0),
        ]

    def test_a_format_cuts_its_own_table_and_any_sheet_may_be_the_only_one(self) -> None:
        class Below(SheetText[_Spaced]):
            @typing.override
            def _table(self, grid: pl.DataFrame) -> pl.DataFrame:
                rows = grid.slice(2).select(grid.columns[:2])
                rows.columns = ["Month", "Value"]
                return rows

        body = _book(["A title"], [], ["2026-03", "n/a"], ["2026-04", 2], sheet="Anything")
        assert Below(_Spaced, null_values=("n/a",)).frame(body)["value"].to_list() == [None, 2.0]

    def test_it_reads_long(self) -> None:
        class ByTerm(msgspec.Struct, frozen=True, gc=False):
            day: Annotated[dt.date, Label("Day"), Format("%Y-%m-%d 00:00:00")]
            years: int
            rate: float | None

        class Long(LongFile[ByTerm], SheetText[ByTerm]):
            _keys = re.compile(r"(?P<key>\d+)Y")

        body = _book(["Day", "1Y", "2Y"], [dt.date(2026, 1, 2), 1.5, None])
        day = dt.date(2026, 1, 2)
        assert Long(ByTerm, sheet="S").rows(body) == [ByTerm(day, 1, 1.5), ByTerm(day, 2, None)]

    @pytest.mark.parametrize(
        ("body", "sheet", "message"),
        [
            pytest.param(_book(["Month", "Value"]), "S", "a header and no rows", id="no-rows"),
            pytest.param(
                _book(["Month", "Value"], ["2026-01", "x"]), "S", "the 'Value' cell 'x' is not a number", id="cell"
            ),
            pytest.param(
                _book(["Month", "Price"], ["2026-01", 1]), "S", r"the header \['Month', 'Price'\] is not", id="header"
            ),
            pytest.param(
                _book(["Month", "", "", "Value"], ["2026-01", 1, 2, 3]), "S", "leaves two cells empty", id="empty"
            ),
            pytest.param(workbook({"T": [["Month"]]}), "S", "no sheet 'S'", id="no-sheet"),
            pytest.param(
                _book(["Month", "Value", None], ["2026-01", 1, 2]), "S", "past its last label", id="past-header"
            ),
            pytest.param(
                workbook({"A": [], "B": []}), None, r"holds the sheets \['A', 'B'\], not one", id="two-sheets"
            ),
            pytest.param(b"<html>maintenance</html>", "S", "the body is not a workbook", id="not-a-workbook"),
            pytest.param(
                swapped(
                    _book(["Month"]),
                    "xl/worksheets/sheet1.xml",
                    b"<sheetData>",
                    b'<sheetData><row r="9"><c r="A9" t="s"><v>99</v></c></row>',
                ),
                "S",
                "the workbook's 'S' sheet will not load",
                id="sheet-will-not-load",
            ),
        ],
    )
    def test_a_table_that_is_not_its_own_raises(self, body: bytes, sheet: str | None, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            SheetText(_Spaced, sheet=sheet).rows(body)


class TestASheetGrid:
    def test_it_is_every_cell_as_text_each_digit_kept_a_blank_or_null_value_none(self) -> None:
        body = _book([], ["Month", " ", 13442.008862616791], ["n/a", 1, "x"], sheet="Only")
        assert sheet_grid(body, null_values=("n/a",)).rows() == [
            (None, None, None),
            ("Month", None, repr(13442.008862616791)),
            (None, "1", "x"),
        ]

    def test_it_reads_a_named_sheet_of_several(self) -> None:
        body = workbook({"A": [["a"]], "B": [["b"]]})
        assert sheet_grid(body, "B").to_dict(as_series=False) == {"0": ["b"]}

    @pytest.mark.parametrize(
        ("body", "sheet", "message"),
        [
            pytest.param(b"<html>maintenance</html>", None, "the body is not a workbook", id="not-a-workbook"),
            pytest.param(_book(["a"]), "T", r"has no sheet 'T'; its sheets are \['S'\]", id="no-sheet"),
            pytest.param(workbook({"A": [], "B": []}), None, r"holds the sheets \['A', 'B'\], not one", id="two"),
        ],
    )
    def test_a_body_without_the_sheet_raises(self, body: bytes, sheet: str | None, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            sheet_grid(body, sheet)


# --- A table cut from a grid of text cells: a sheet's or a delimited file's (GridFile) ---


class _Factor(msgspec.Struct, frozen=True, gc=False):
    month: Annotated[dt.date, Label(""), Format("%b-%Y")]
    group: str
    series: str
    value: float | None


class _FactorsCsv(LongFile[_Factor], CsvText[_Factor]):
    """G.17's seasonal factors: titles, then a group row over a row of labels, the dates' column headed by neither."""

    _above = 1
    _key_count = 2
    _keys = re.compile(r"(?P<group>[^\x1f]+)\x1f(?P<series>[^\x1f]+)")


class _FactorsSheet(LongFile[_Factor], SheetText[_Factor]):
    _above = 1
    _key_count = 2
    _keys = _FactorsCsv._keys


class _ForeignHolding(msgspec.Struct, frozen=True, gc=False):
    country: Annotated[str, Label("Country")]
    month: str
    year: int
    value: float


class _ForeignHoldings(LongFile[_ForeignHolding], CsvText[_ForeignHolding]):
    """TIC's major foreign holders: a month row over a year row, each line closed by a separator."""

    _above = 1
    _key_count = 2
    _keys = re.compile(r"(?P<month>[A-Z][a-z]{2})\x1f(?P<year>[0-9]{4})")


_SEASONAL_FACTORS: Final = (
    b"                ,Monthly Seasonal Factors for,,,\r\n"
    b",,,,\r\n"
    b"           ,Regular Seasonal Factors,,Weekday-basis Seasonal Factors,\r\n"
    b"           ,Autos  ,Total Trucks  ,Autos  ,Weekdays\r\n"
    b"Jan-1996,98.9,99.23,93.17,23\r\n"
    b"Feb-1996,104.64,,107.96,21\r\n"
)


class TestACsvText:
    def test_a_group_row_heads_each_column_to_its_right_under_the_headers_labels(self) -> None:
        jan, feb = dt.date(1996, 1, 1), dt.date(1996, 2, 1)
        regular, weekday = "Regular Seasonal Factors", "Weekday-basis Seasonal Factors"
        assert _FactorsCsv(_Factor, header_row=4).rows(_SEASONAL_FACTORS) == [
            _Factor(jan, regular, "Autos", 98.9),
            _Factor(jan, regular, "Total Trucks", 99.23),
            _Factor(jan, weekday, "Autos", 93.17),
            _Factor(jan, weekday, "Weekdays", 23.0),
            _Factor(feb, regular, "Autos", 104.64),
            _Factor(feb, regular, "Total Trucks", None),
            _Factor(feb, weekday, "Autos", 107.96),
            _Factor(feb, weekday, "Weekdays", 21.0),
        ]

    def test_a_sheet_of_the_same_cells_reads_as_its_csv(self) -> None:
        grid: list[list[Cell]] = [
            [cell.strip() or None for cell in line.split(",")] for line in _SEASONAL_FACTORS.decode().splitlines()
        ]
        body = _book(*grid)
        assert _FactorsSheet(_Factor, sheet="S", header_row=4).rows(body) == _FactorsCsv(_Factor, header_row=4).rows(
            _SEASONAL_FACTORS
        )

    def test_a_quoted_cell_keeps_its_separator_and_a_closing_separator_heads_nothing(self) -> None:
        body = b',Dec,Nov,\r\nCountry,2025,2025,\r\n"China, Mainland",683.5,683.9,\r\n\r\nJapan,1185.5,1202.7,\r\n'
        assert _ForeignHoldings(_ForeignHolding, header_row=2).rows(body) == [
            _ForeignHolding("China, Mainland", "Dec", 2025, 683.5),
            _ForeignHolding("China, Mainland", "Nov", 2025, 683.9),
            _ForeignHolding("Japan", "Dec", 2025, 1185.5),
            _ForeignHolding("Japan", "Nov", 2025, 1202.7),
        ]

    def test_three_rows_head_a_block_of_columns_and_a_heading_carries_to_the_headers_end(self) -> None:
        class Position(msgspec.Struct, frozen=True, gc=False):
            week: Annotated[str, Label("Week")]
            market: str
            category: str
            side: str
            value: int

        class Blocks(LongFile[Position], CsvText[Position]):
            """CFTC's large-trader file: a market over its categories over each one's buys and sells."""

            _above = 2
            _key_count = 3
            _keys = re.compile(r"(?P<market>[^\x1f]+)\x1f(?P<category>[^\x1f]+)\x1f(?P<side>[^\x1f]+)")

        body = b",Cocoa (CC),,,\nWeek,Producer,,Swap,\nWeek,Buys,Sells,Buys,Sells\n2026-01-06,1,2,3,4\n"
        assert [(row.category, row.side, row.value) for row in Blocks(Position, header_row=3).rows(body)] == [
            ("Producer", "Buys", 1),
            ("Producer", "Sells", 2),
            ("Swap", "Buys", 3),
            ("Swap", "Sells", 4),
        ]
        assert {row.market for row in Blocks(Position, header_row=3).rows(body)} == {"Cocoa (CC)"}

    def test_a_title_over_several_lines_is_one_cell_and_a_short_row_is_filled_with_none(self) -> None:
        class Fee(msgspec.Struct, frozen=True, gc=False):
            transactions: Annotated[str, Label("Transactions")]
            fee: Annotated[float | None, Label("Fee")]

        class Fees(CsvText[Fee]):
            """The Fed's same-day ACH review: a title over two lines, then its table."""

            @typing.override
            def _table(self, grid: pl.DataFrame) -> pl.DataFrame:
                assert grid.row(0)[1] == "SAME-DAY ACH ENTRY FEE \nYEAR 5 REVIEW"
                return super()._table(grid.select(grid.columns[1:]))

        body = b',"SAME-DAY ACH ENTRY FEE \nYEAR 5 REVIEW",\n,Transactions,Fee\n,"8,933 ",5.2\n,"9,100"\n'
        assert Fees(Fee, header_row=2).rows(body) == [Fee("8,933 ", 5.2), Fee("9,100", None)]

    def test_a_tab_separated_file_in_another_encoding_reads_through_its_transcoding(self) -> None:
        class Survey(msgspec.Struct, frozen=True, gc=False):
            country: Annotated[str, Label("Country")]
            period: str
            measure: str
            value: int | None

        class Surveys(Transcoded[Survey], LongFile[Survey], CsvText[Survey]):
            """TIC's holdings surveys: a TSV in latin-1, each survey's period over its band over its measures."""

            _encodings = ("latin-1",)
            _above = 2
            _key_count = 2
            _keys = re.compile(r"(?P<period>[^\x1f]+)\x1f[^\x1f]*\x1f(?P<measure>[^\x1f]+)")

        body = "\tJune 2025\t\n\tAll\t\nCountry\tTotal\tEquity\nCôte d'Ivoire\t 12 \t\n".encode("latin-1")
        assert Surveys(Survey, header_row=3, separator="\t").rows(body) == [
            Survey("Côte d'Ivoire", "June 2025", "Total", 12),
            Survey("Côte d'Ivoire", "June 2025", "Equity", None),
        ]

    def test_its_words_for_none_and_a_byte_order_mark_are_no_cells(self) -> None:
        body = b"\xef\xbb\xbfMonth,Value\n2026-01,n/a\n2026-02,  \n2026-03,4\n"
        assert CsvText(_Spaced, null_values=("n/a",)).frame(body)["value"].to_list() == [None, None, 4.0]

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            pytest.param(b"", "the body is empty", id="empty"),
            pytest.param(b" \r\n", "the body is empty", id="blank"),
            pytest.param(b"\xef\xbb\xbf", "the body is empty", id="byte-order-mark"),
            pytest.param(b"Month,Value\n2026-01,\xff\n", "the body is not UTF-8", id="not-utf-8"),
            pytest.param(b'Month,Value\n2026-01,"4\n', "the body is no delimited file", id="cut-in-a-quote"),
            pytest.param(b"Month,Value\n2026-01,4,5\n", "past its last label", id="past-header"),
            pytest.param(b"Month,Price\n2026-01,4\n", r"the header \['Month', 'Price'\] is not", id="header"),
        ],
    )
    def test_a_body_that_is_not_its_table_raises(self, body: bytes, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            CsvText(_Spaced).rows(body)

    def test_a_misquoted_file_reads_a_stray_quote_as_text(self) -> None:
        class Noted(CsvText[_Spaced]):
            _misquoted = True

            @typing.override
            def _table(self, grid: pl.DataFrame) -> pl.DataFrame:
                return super()._table(grid.head(3))

        body = b'Month,Value\n2026-01,"1.5"\n2026-02,4\n"ts." Holdings o","x\n'
        with pytest.raises(ValueError, match="the body is no delimited file"):
            CsvText(_Spaced).rows(body)
        assert Noted(_Spaced).rows(body) == [_Spaced(dt.date(2026, 1, 1), 1.5), _Spaced(dt.date(2026, 2, 1), 4.0)]


class TestAGridFile:
    def test_its_source_reads_the_grid_and_the_rows_over_the_header_must_fit_above_it(self) -> None:
        class Listed(GridFile[_Spaced]):
            @typing.override
            def _body_grid(self, body: bytes) -> pl.DataFrame:
                return pl.DataFrame({"0": ["Month", "2026-01"], "1": ["Value", body.decode()]})

        assert Listed(_Spaced).rows(b"3") == [_Spaced(dt.date(2026, 1, 1), 3.0)]

        class Tall(Listed):
            _above = 1

        with pytest.raises(ValueError, match="the 1 rows over the header do not fit above its row 1"):
            Tall(_Spaced)

    def test_two_columns_a_stacked_header_heads_alike_raise(self) -> None:
        with pytest.raises(ValueError, match="heads two columns alike"):
            _FactorsCsv(_Factor, header_row=2).rows(b",Stocks,,,\n,East,,,West\nJan-1996,1,,,2\n")

    def test_a_header_below_the_grids_end_is_no_header(self) -> None:
        with pytest.raises(ValueError, match=r"the header \[\] is not"):
            CsvText(_Spaced, header_row=9).rows(b"Month,Value\n2026-01,4\n")


class _EndedGrid(BlankEnded[_Spaced], CsvText[_Spaced]):
    pass


class _TitledGrid(BlankEnded[_Spaced], TitledGrid[_Spaced], CsvText[_Spaced]):
    _title = "II. Values"


class _BandGrid(BesideGrid[_Spaced], CsvText[_Spaced]):
    _band = (2, 4)


class TestAGridTableCut:
    def test_a_blank_ended_table_stops_at_its_first_blank_row(self) -> None:
        body = b"Month,Value\n2026-01,4\n,\nLast Updated: 9-30-2026,\n"
        assert _EndedGrid(_Spaced).rows(body) == [_Spaced(dt.date(2026, 1, 1), 4.0)]
        with pytest.raises(ValueError, match=r"the 'Month' cell 'Last Updated: 9-30-2026' is not a date"):
            CsvText(_Spaced).rows(body)

    def test_a_titled_table_is_the_first_filled_row_under_its_title_to_the_next_blank(self) -> None:
        body = b"Prose, with a comma\nII. Values\n\nMonth,Value\n2026-01,4\n\nIII. Notes\nx,y\n"
        assert _TitledGrid(_Spaced).rows(body) == [_Spaced(dt.date(2026, 1, 1), 4.0)]

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            pytest.param(b"II. Values,x\nMonth,Value\n", "no row is the title 'II. Values' alone", id="not-alone"),
            pytest.param(b"Month,Value\nII. Values\n\n", "the title 'II. Values' has no table below it", id="none"),
        ],
    )
    def test_a_titled_table_without_its_title_or_rows_raises(self, body: bytes, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            _TitledGrid(_Spaced).rows(body)

    def test_a_titled_header_needs_the_rows_over_it(self) -> None:
        class Stacked(_TitledGrid):
            _above = 2

        with pytest.raises(ValueError, match=r"the header in row 2 has not the 2 rows over it"):
            Stacked(_Spaced, header_row=3).rows(b"II. Values\nMonth,Value\n2026-01,4\n")

    def test_a_filled_header_is_the_first_row_about_as_filled_as_the_fullest(self) -> None:
        class Found(FilledHeader[_Spaced], CsvText[_Spaced]):
            _rows_required = False

        body = b"A report title,,\nMonth,Value\n2026-01,4\n"
        assert Found(_Spaced).rows(body) == [_Spaced(dt.date(2026, 1, 1), 4.0)]

        class Cell(msgspec.Struct, frozen=True, gc=False):
            sheet: str
            column: str
            value: str

        class Long(LongFile[Cell], FilledHeader[Cell], CsvText[Cell]):
            """A sheet of one filled column is a note: it holds no table."""

            _keys = re.compile(r".+")
            _given = types.MappingProxyType({"sheet": "S"})
            _rows_required = False

        assert Long(Cell).rows(b"No trades this month.\n") == []

    def test_a_band_reads_its_columns_and_leaves_the_table_beside_it(self) -> None:
        body = b"Code,x,Month,Value,Other\nA,,2026-01,4,9\nB,,2026-02,,\n"
        assert _BandGrid(_Spaced).rows(body) == [_Spaced(dt.date(2026, 1, 1), 4.0), _Spaced(dt.date(2026, 2, 1), None)]


def _record(kind: int, body: bytes) -> bytes:
    """Return a BIFF record: its kind and length, then *body*."""
    return struct.pack("<HH", kind, len(body)) + body


def _at(row: int, column: int, xf: int = 0) -> bytes:
    """Return a BIFF3 cell record's opening: its row, column and format (XF 1 a date's)."""
    return struct.pack("<HHH", row, column, xf)


def _label(row: int, column: int, text: str) -> bytes:
    return _record(0x0204, _at(row, column) + struct.pack("<H", len(text)) + text.encode("latin-1"))


def _number(row: int, column: int, number: float, xf: int = 0) -> bytes:
    return _record(0x0203, _at(row, column, xf) + struct.pack("<d", number))


def _boolerr(row: int, column: int, value: int, *, error: bool) -> bytes:
    return _record(0x0205, _at(row, column) + bytes([value, error]))


def _biff3(*cells: bytes) -> bytes:
    """Return a BIFF3 worksheet of *cells*: its BOF, a general format and a date's, an XF of each, then its end."""
    formats = b"".join(_record(0x001E, bytes([len(f)]) + f.encode()) for f in ("General", "mm/dd/yyyy"))
    xfs = b"".join(_record(0x0243, bytes([0, f]) + bytes(10)) for f in (0, 1))
    return _record(0x0209, struct.pack("<HHH", 0, 0x10, 0)) + formats + xfs + b"".join(cells) + _record(0x000A, b"")


class _Biff(msgspec.Struct, frozen=True, gc=False):
    when: Annotated[str, Label("When")]
    flag: Annotated[str, Label("Flag")]
    error: Annotated[str | None, Label("Error")]
    whole: Annotated[str, Label("Whole")]
    part: Annotated[str | None, Label("Part")]


#: A sheet from column B (A is empty): a date, a boolean, an error, a whole number and a fraction, then ``#N/A``.
_BIFF3 = _biff3(
    *(_label(0, column, label) for column, label in enumerate(["When", "Flag", "Error", "Whole", "Part"], 1)),
    _number(1, 1, 37803.0, xf=1),
    _boolerr(1, 2, 1, error=False),
    _boolerr(1, 3, 0x07, error=True),
    _number(1, 4, 4.0),
    _number(1, 5, 0.1),
    _label(2, 1, "2003-07-02"),
    _boolerr(2, 2, 0, error=False),
    _boolerr(2, 3, 0x2A, error=True),
    _number(2, 4, -3.0),
    _label(2, 5, "  "),
)


class TestABiffSheet:
    def test_each_cell_reads_as_fastexcel_writes_it_from_the_first_filled_column(self) -> None:
        assert BiffSheet(_Biff, null_values=("#N/A",)).rows(_BIFF3) == [
            _Biff("2003-07-01 00:00:00", "true", "#DIV/0!", "4", "0.1"),
            _Biff("2003-07-02", "false", None, "-3", None),
        ]

    @pytest.mark.parametrize(
        ("body", "sheet", "message"),
        [
            pytest.param(b"PK\x03\x04", None, "not a BIFF3 or BIFF4 workbook: it opens b'PK", id="not-biff"),
            pytest.param(b"", None, "not a BIFF3 or BIFF4 workbook", id="empty"),
            pytest.param(_BIFF3[:-7], None, "not a readable BIFF3 or BIFF4 workbook", id="cut"),
            pytest.param(_BIFF3, "Other", r"holds the sheets \['Sheet 1'\], not 'Other'", id="no-sheet"),
        ],
    )
    def test_a_body_that_is_not_its_workbook_raises(self, body: bytes, sheet: str | None, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            BiffSheet(_Biff, sheet=sheet).rows(body)


# --- A date written across two columns ---


class _ByMonthName(msgspec.Struct, frozen=True, gc=False):
    month: Annotated[dt.date, Label("Month"), Joined("Year"), Format("%B %Y")]
    value: Annotated[float, Label("Value")]


class _ByYearAndQuarter(msgspec.Struct, frozen=True, gc=False):
    quarter: Annotated[dt.date | None, Label("YEAR"), Joined("QUARTER", separator=":"), Format("%Y:%q")]
    value: Annotated[float, Label("V")]


class TestAJoinedField:
    def test_its_two_cells_are_read_as_one_date(self) -> None:
        assert CsvFile(_ByMonthName).rows(b"Month,Year,Value\nJanuary,2022,1.5\nMay,2022,2\n") == [
            _ByMonthName(dt.date(2022, 1, 1), 1.5),
            _ByMonthName(dt.date(2022, 5, 1), 2.0),
        ]
        assert CsvFile(_ByYearAndQuarter).rows(b"YEAR,QUARTER,V\n2026,3,1\n,,2\n") == [
            _ByYearAndQuarter(dt.date(2026, 7, 1), 1.0),
            _ByYearAndQuarter(None, 2.0),
        ]

    def test_a_sheet_reads_a_number_cell_as_its_text(self) -> None:
        body = _book(["Month", "Year", "Value"], ["March", 2022, 1.5])
        assert SheetFile(_ByMonthName, sheet="S").rows(body) == [_ByMonthName(dt.date(2022, 3, 1), 1.5)]

    def test_a_long_table_keeps_it_with_the_record(self) -> None:
        class Forecast(msgspec.Struct, frozen=True, gc=False):
            survey: Annotated[dt.date, Label("YEAR"), Joined("QUARTER"), Format("%Y %q")]
            horizon: int
            value: float | None

        class Forecasts(LongFile[Forecast], CsvFile[Forecast]):
            _keys = re.compile(r"RGDP(?P<key>\d)")

        assert Forecasts(Forecast).rows(b"YEAR,QUARTER,RGDP1,RGDP2\n2026,1,2.5,\n") == [
            Forecast(dt.date(2026, 1, 1), 1, 2.5),
            Forecast(dt.date(2026, 1, 1), 2, None),
        ]

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            pytest.param(
                b"Month,Year,Value\nJanuary,,1.5\n", "row 1 holds one of the 'Month' and 'Year' cells", id="half"
            ),
            pytest.param(
                b"Month,Year,Value\nJanvier,2022,1.5\n",
                "the 'Month' cell 'Janvier 2022' is not a date written %B %Y: row 1",
                id="not-a-month",
            ),
        ],
    )
    def test_cells_that_are_not_its_date_raise(self, body: bytes, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            CsvFile(_ByMonthName).rows(body)

    def test_a_joined_field_is_a_date_read_by_its_format(self) -> None:
        class Unformatted(msgspec.Struct, frozen=True, gc=False):
            month: Annotated[dt.date, Label("Month"), Joined("Year")]

        with pytest.raises(ValueError, match="is Joined, so it is a date or time read by its Format"):
            CsvFile(Unformatted)


# --- Format alternatives ---


class TestFormatAlternatives:
    def test_a_column_written_two_ways_reads_each_cell_by_the_one_it_is_written_in(self) -> None:
        class Either(msgspec.Struct, frozen=True, gc=False):
            day: Annotated[dt.date, Label("DAY"), Format("%m/%d/%Y", alternatives=("%-m/%-d/%Y",))]

        flat = CsvFile(Either)
        assert flat.rows(b"DAY\n01/14/2008\n1/14/2009\n") == [
            Either(dt.date(2008, 1, 14)),
            Either(dt.date(2009, 1, 14)),
        ]
        with pytest.raises(
            ValueError, match="the 'day' cell '1/05/2008' is not a date written %m/%d/%Y or %-m/%-d/%Y: row 1"
        ):
            flat.rows(b"DAY\n1/05/2008\n")


# --- XmlFile ---


class _Record(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("Name")]
    note: Annotated[str | None, Label("Note")]
    year: Annotated[int, Label("Year")]
    rate: Annotated[float | None, Label("Rate")]
    open: Annotated[bool, Label("Open")]
    day: Annotated[dt.date, Label("Day"), Format("%-m/%-d/%Y")]


_TABLE = XmlFile(_Record, root="Table", record="Row")


def _xml(*records: str, root: str = "Table") -> bytes:
    """Return a document of *root* holding *records*, each a record's inner XML."""
    return f"<{root}>{''.join(f'<Row>{r}</Row>' for r in records)}</{root}>".encode()


def _cells(**cells: str) -> str:
    """Return a record's elements, the defaults with *cells* replaced, in the struct's order."""
    values = {"Name": "a", "Note": "x", "Year": "2008", "Rate": "1.5", "Open": "true", "Day": "1/14/2008"} | cells
    return "".join(f"<{tag}>{text}</{tag}>" if text != "/" else f"<{tag}/>" for tag, text in values.items())


class TestAnXmlFile:
    def test_a_clean_table_reads_typed(self) -> None:
        body = b'\xef\xbb\xbf<?xml version="1.0"?>\r\n' + _xml(
            _cells(), _cells(Name="b &amp; c", Note="/", Year=" 2009 ", Rate=" ", Open="false")
        )
        assert _TABLE.rows(body) == [
            _Record("a", "x", 2008, 1.5, open=True, day=dt.date(2008, 1, 14)),
            _Record("b & c", None, 2009, None, open=False, day=dt.date(2008, 1, 14)),
        ]
        schema = _TABLE.frame(body).schema
        assert (schema["year"], schema["rate"], schema["open"], schema["day"]) == (
            pl.Int64,
            pl.Float64,
            pl.Boolean,
            pl.Date,
        )

    @pytest.mark.parametrize(
        ("cells", "message"),
        [
            ({"Year": "2008.5"}, "the 'Year' cell '2008.5' is not a whole number: row 1"),
            ({"Rate": "high"}, "the 'Rate' cell 'high' is not a number: row 1"),
            ({"Open": "yes"}, "the 'Open' cell 'yes' is not a true or false: row 1"),
            ({"Day": "2008-01-14"}, "the 'Day' cell '2008-01-14' is not a date written %-m/%-d/%Y: row 1"),
            ({"Name": " "}, r"the \['name'\] columns have an empty cell"),
        ],
        ids=["fraction", "text in float", "not a boolean", "another date form", "a blank required element"],
    )
    def test_an_element_not_its_fields_type_raises(self, cells: dict[str, str], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            _TABLE.rows(_xml(_cells(**cells)))

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (b" \n", "the body is empty"),
            (b"<Table><Row>", "the body is not well-formed XML"),
            (_xml(_cells(), root="Other"), "the document's root is <Other>, not <Table>"),
            (b"<Table><Line>" + _cells().encode() + b"</Line></Table>", r"record 1 is <Line> of \['Name'"),
            (
                _xml("<Note>x</Note><Name>a</Name>"),
                r"record 1 is <Row> of \['Note', 'Name'\], not <Row> of leaf elements",
            ),
            (_xml(_cells(Note="<b>x</b>")), "record 1 is <Row> of"),
            (b'<!DOCTYPE Table [<!ENTITY e "boom">]>' + _xml(_cells(Note="&e;")), "record 1 is <Row> of"),
            (b"<Table/>", "the <Table> root holds no <Row> record"),
        ],
        ids=[
            "empty",
            "cut",
            "another root",
            "another record",
            "other elements",
            "a nested element",
            "an entity",
            "no records",
        ],
    )
    def test_a_document_that_is_not_the_table_raises(self, body: bytes, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            _TABLE.rows(body)

    def test_a_date_field_needs_its_format(self) -> None:
        class Undated(msgspec.Struct, frozen=True, gc=False):
            day: Annotated[dt.date, Label("Day")]

        with pytest.raises(ValueError, match="the field 'day' needs the Format its text is written in"):
            XmlFile(Undated, root="Table", record="Row")


# --- NestedXml ---


class _Seat(msgspec.Struct, frozen=True, gc=False):
    district: Annotated[str, Label("../@district")]
    code: Annotated[str, Label("@code")]
    rank: Annotated[int, Label("@rank")]
    name: Annotated[str | None, Label("info/name")]
    state: Annotated[str, Label("info/state/@postal")]
    kind: Annotated[str, Label("local-name()")]
    sworn: Annotated[dt.date | None, Label("info/sworn/@date"), Format("%Y%m%d")]
    chair: Annotated[bool, Label("@chair")] = False


_SEATS = NestedXml(_Seat, root="House", record="seats/seat/*")
_SEAT = (
    '<committee code="AG" rank="1"><info><name>A</name><state postal="AK"/><sworn date="20250103"/></info></committee>'
)


def _house(*seats: str) -> bytes:
    """Return a House of *seats*, each a seat's inner XML, the first in district 1, the second in 2."""
    inner = "".join(f'<seat district="{n}">{seat}</seat>' for n, seat in enumerate(seats, 1))
    return f"<House><seats>{inner}</seats></House>".encode()


class TestANestedXml:
    def test_each_field_is_found_by_its_path_from_its_record(self) -> None:
        body = _house(
            _SEAT + '<subcommittee code="AG01" rank="2" chair="true"><info><state postal="AK"/></info></subcommittee>',
            _SEAT,
        )
        assert _SEATS.rows(body) == [
            _Seat("1", "AG", 1, "A", "AK", "committee", dt.date(2025, 1, 3)),
            _Seat("1", "AG01", 2, None, "AK", "subcommittee", None, chair=True),
            _Seat("2", "AG", 1, "A", "AK", "committee", dt.date(2025, 1, 3)),
        ]
        assert _SEATS.frame(body).schema["chair"] == pl.Boolean

    def test_its_prefixes_are_the_formats_namespaces(self) -> None:
        class Named(msgspec.Struct, frozen=True, gc=False):
            id: Annotated[str, Label("@id")]
            name: Annotated[str, Label("n:Name[@xml:lang='en']")]

        class _Sdmx(NestedXml[Named]):
            _namespaces: typing.ClassVar = {"m": "urn:m", "n": "urn:n"}

        body = (
            b'<m:Doc xmlns:m="urn:m" xmlns:n="urn:n"><m:Flows><n:Flow id="X"><n:Name xml:lang="de">x</n:Name>'
            b'<n:Name xml:lang="en">Ex</n:Name></n:Flow></m:Flows></m:Doc>'
        )
        assert _Sdmx(Named, root="m:Doc", record="m:Flows/n:Flow").rows(body) == [Named("X", "Ex")]

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (b"<House><seats/></House>", r"the <House> root holds no <seats/seat/\*> record"),
            (
                _house(_SEAT.replace("<name>A</name>", "<name>A</name><name>B</name>")),
                r"record 1's 'info/name' finds 2 nodes",
            ),
            (
                _house(_SEAT.replace("<name>A</name>", "<name><b>A</b></name>")),
                r"record 1's 'info/name' is <name> holding elements",
            ),
            (
                b'<!DOCTYPE House [<!ENTITY e "boom">]>' + _house(_SEAT.replace(">A<", ">&e;<")),
                r"record 1's 'info/name' is <name> holding elements",
            ),
            (_house(_SEAT.replace(' postal="AK"', "")), r"the \['state'\] columns have an empty cell"),
            (_house(_SEAT.replace('rank="1"', 'rank="first"')), r"the '@rank' cell 'first' is not a whole number"),
            (_house(_SEAT).replace(b"</House>", b""), "the body is not well-formed XML"),
        ],
        ids=[
            "no records",
            "two nodes",
            "a nested element",
            "an entity",
            "a required attribute missing",
            "not its type",
            "cut",
        ],
    )
    def test_a_document_that_is_not_the_table_raises(self, body: bytes, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            _SEATS.rows(body)

    def test_a_path_that_finds_no_text_raises(self) -> None:
        class Counted(msgspec.Struct, frozen=True, gc=False):
            count: Annotated[str, Label("count(*)")]

        with pytest.raises(ValueError, match=r"record 1's 'count\(\*\)' finds 0.0, which is no text"):
            NestedXml(Counted, root="T", record="R").rows(b"<T><R/></T>")

    def test_a_record_path_that_finds_no_elements_raises(self) -> None:
        with pytest.raises(ValueError, match=r"the record path 'seats/seat/@district' finds \['1'\], not elements"):
            NestedXml(_Seat, root="House", record="seats/seat/@district").rows(_house(_SEAT))

    def test_a_prefix_no_namespace_binds_raises(self) -> None:
        with pytest.raises(ValueError, match="a path will not evaluate on the document"):
            NestedXml(_Seat, root="House", record="x:seats").rows(_house(_SEAT))

    def test_a_path_that_is_no_xpath_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"the record path 'seats\[' or a field's Label .* is no XPath"):
            NestedXml(_Seat, root="House", record="seats[")


class TestAFieldsDefault:
    def test_a_row_takes_its_default_where_the_file_holds_no_value(self) -> None:
        class Defaulted(msgspec.Struct, frozen=True, gc=False):
            name: Annotated[str, Label("NAME")]
            open: Annotated[bool, Label("OPEN")] = False

        assert CsvFile(Defaulted).rows(b"NAME,OPEN\na,\nb,true\n") == [Defaulted("a"), Defaulted("b", open=True)]


class TestAFlagInASheetOrXml:
    def test_a_sheets_flag_cells_read_by_their_words(self) -> None:
        sheet = SheetFile(_Flagged, sheet="S")
        assert sheet.rows(workbook({"S": [["NAME", "OPEN"], ["a", "Y"], ["b", "N"]]})) == [
            _Flagged("a", open=True),
            _Flagged("b", open=False),
        ]
        with pytest.raises(ValueError, match="the 'OPEN' cell 'yes' is not 'Y' or 'N': row 2"):
            sheet.rows(workbook({"S": [["NAME", "OPEN"], ["a", "yes"]]}))

    def test_an_xml_elements_flag_reads_by_its_words(self) -> None:
        table = XmlFile(_Flagged, root="T", record="R")
        assert table.rows(b"<T><R><NAME>a</NAME><OPEN>N</OPEN></R></T>") == [_Flagged("a", open=False)]


class _Fixed(msgspec.Struct, frozen=True, gc=False):
    symbol: Annotated[str, Label("SYM"), Span(0, 4)]
    count: Annotated[int, Label("COUNT"), Span(5, 10)]
    rate: Annotated[float | None, Label("RATE"), Span(10, 15)]
    day: Annotated[dt.date, Label("DAY"), Format("%Y%m%d"), Span(15, 23)]
    open: Annotated[bool, Label("O"), Flag("Y", "N"), Span(23, 24)]


_FIXED_HEADER = b"SYM  COUNTRATE DAY     O\n"


class _Headless(FixedWidthFile[_Fixed]):
    _header = False


class _Dates(msgspec.Struct, frozen=True, gc=False):
    as_of: Annotated[dt.date, Format("%m%d%Y")]
    symbol: Annotated[str, Span(0, 5)]
    count: Annotated[int, Span(5, 14)]


class _Titled(FixedWidthFile[_Dates]):
    """A title line, a date, written once above a table of no header (OCC's volume files)."""

    _header = False
    _stated = frozenset({"as_of"})

    @typing.override
    def _table(self, lines: pl.Series) -> tuple[pl.Series, dict[str, str]]:
        return lines.slice(1), {"as_of": lines[0].strip()}


class _GroupedFixed(Thousands[_Fixed], FixedWidthFile[_Fixed]):
    """A fixed-width table whose numbers are grouped in thousands."""


class TestAFixedWidthFile:
    def test_its_numbers_may_be_grouped_in_thousands(self) -> None:
        body = _FIXED_HEADER + b"AB   1,234 1.5 20261106Y\n"
        assert _GroupedFixed(_Fixed).rows(body)[0].count == 1234
        with pytest.raises(ValueError, match="the 'COUNT' cell '12,34' is not a number grouped in thousands: row 1"):
            _GroupedFixed(_Fixed).rows(_FIXED_HEADER + b"AB   12,34 1.5 20261106Y\n")

    def test_each_cell_is_its_span_stripped_and_typed(self) -> None:
        body = _FIXED_HEADER + b"AB   00012 1.5 20261106Y\nABCD     7     20261109N\n"
        flat = FixedWidthFile(_Fixed)
        frame = flat.frame(body)
        assert frame.dtypes == [pl.String, pl.Int64, pl.Float64, pl.Date, pl.Boolean]
        assert flat.rows(body) == [
            _Fixed("AB", 12, 1.5, dt.date(2026, 11, 6), open=True),
            _Fixed("ABCD", 7, None, dt.date(2026, 11, 9), open=False),
        ]

    def test_columns_may_touch(self) -> None:
        assert msgspec.structs.astuple(FixedWidthFile(_Fixed).rows(_FIXED_HEADER + b"ABCD 12345-2.0520261106Y\r\n")[0])[
            1:3
        ] == (
            12345,
            -2.05,
        )

    @pytest.mark.parametrize(
        ("body", "error"),
        [
            (b"SYM  COUNTRATE DAY     O\nAB   00012 1.5 20261106Y \n", r"row 1 'AB   00012 1.5 20261106Y ' is not 24"),
            (b"SYM  COUNTRATE DAY     O\nAB   1 1.5 20261106Y\n", "row 1 .* is not 24 characters"),
            (b"SYM  COUNTRATE DAY     O\nAB  x00012 1.5 20261106Y\n", "row 1 .* spaces between its columns"),
            (b"SYM  COUNTRATE DAY     O\nAB   00012 1.5 20261106Y\n\n", "row 2 '' is not 24"),
            (b"SYM  COUNTRATE DAY      \nAB   00012 1.5 20261106Y\n", r"the header .* is not the labels \['SYM'"),
            (b"SYM COUNTRATE DAY     O\nAB   00012 1.5 20261106Y\n", "the header .* is not 24 characters"),
            (b"SYM  COUNTRATE DAY     O\nAB   0001x 1.5 20261106Y\n", "the 'COUNT' cell '0001x' is not a whole number"),
            (b"SYM  COUNTRATE DAY     O\nAB   00012 1.5 20261131Y\n", "the 'DAY' cell '20261131' is not a date"),
            (b"SYM  COUNTRATE DAY     O\nAB   00012 1.5 20261106y\n", "the 'O' cell 'y' is not 'Y' or 'N': row 1"),
            (
                b"SYM  COUNTRATE DAY     O\n     00012 1.5 20261106Y\n",
                r"\['symbol'\] columns have an empty cell.*row 1",
            ),
            (b"SYM  COUNTRATE DAY     O\nAB   00012 nan 20261106Y\n", "hold NaN or an infinity"),
            (_FIXED_HEADER, "the body is a header and no rows"),
            (b"  \n", "the body is empty"),
        ],
    )
    def test_a_body_that_is_not_the_table_raises(self, body: bytes, error: str) -> None:
        with pytest.raises(ValueError, match=error):
            FixedWidthFile(_Fixed).rows(body)

    def test_a_file_with_no_header_reads_every_line_and_its_fields_need_no_label(self) -> None:
        assert _Headless(_Fixed).rows(b"AB   00012 1.5 20261106Y\n")[0].count == 12
        with pytest.raises(ValueError, match="the 'COUNT' cell 'COUNT' is not a whole number: row 1"):
            _Headless(_Fixed).rows(_FIXED_HEADER)

    def test_a_stated_field_is_read_from_above_the_table_for_every_row(self) -> None:
        rows = _Titled(_Dates).rows(b"  11062026\nA    000000012\nZYME 000000008\n")
        assert rows == [_Dates(dt.date(2026, 11, 6), "A", 12), _Dates(dt.date(2026, 11, 6), "ZYME", 8)]
        with pytest.raises(ValueError, match="the 'as_of' cell '13062026' is not a date"):
            _Titled(_Dates).rows(b"13062026\nA    000000012\n")

    def test_the_final_line_ending_is_checked(self) -> None:
        with pytest.raises(ValueError, match="it was cut"):
            FixedWidthFile(_Fixed, final_line_ending=True).rows(_FIXED_HEADER + b"AB   00012 1.5 20261106Y")

    @pytest.mark.parametrize(
        ("flat", "row", "error"),
        [
            (FixedWidthFile, _Row, "the field 'name' needs a Span"),
            (_Headless, _Dates, "the field 'as_of' needs a Span"),
            (_Titled, _Fixed, r"the format states \['as_of'\], which are no fields of _Fixed"),
        ],
    )
    def test_a_layout_with_a_field_out_of_place_raises(
        self, flat: type[FixedWidthFile[typing.Any]], row: type[msgspec.Struct], error: str
    ) -> None:
        with pytest.raises(ValueError, match=error):
            flat(row)

    def test_spans_out_of_order_overlapping_or_empty_raise(self) -> None:
        class Overlapping(msgspec.Struct):
            a: Annotated[str, Label("A"), Span(0, 4)]
            b: Annotated[str, Label("B"), Span(3, 6)]

        class Empty(msgspec.Struct):
            a: Annotated[str, Label("A"), Span(2, 2)]

        for row in (Overlapping, Empty):
            with pytest.raises(ValueError, match="not one or more, in order"):
                FixedWidthFile(row)

    def test_a_date_needs_its_format(self) -> None:
        class Undated(msgspec.Struct):
            day: Annotated[dt.date, Label("DAY"), Span(0, 8)]

        with pytest.raises(ValueError, match="needs the Format"):
            FixedWidthFile(Undated)


# --- ReportFile ---


class _Sale(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Format("%m/%d/%Y")]
    region: str
    store: str
    units: int
    price: float | None


class _Sales(ReportFile[_Sale]):
    """A printed sales report: a title stating its day, a two-line header, regions over their stores, page breaks."""

    _line = re.compile(r" (?P<store>\S+(?: \S+)*) +(?P<units>-?[0-9][0-9,]*) +(?P<price>\S+) *")
    _header = (re.compile(r" STORE +UNITS +UNIT *"), re.compile(r" +SOLD +PRICE *"))
    _framing = (
        re.compile(r"SALES REPORT +DAY (?P<day>\d\d/\d\d/\d{4}) +PAGE +\d+"),
        re.compile(r"REGION (?P<region>[A-Z]+)"),
        re.compile(r"-+"),
    )


_SALES = b"""SALES REPORT   DAY 11/06/2026   PAGE 1

 STORE      UNITS   UNIT
             SOLD  PRICE
-------------------------
REGION NORTH
 MAIN ST       12   1.50
 MILL           3   n.a.
SALES REPORT   DAY 11/06/2026   PAGE 2
 STORE      UNITS   UNIT
             SOLD  PRICE
REGION SOUTH
 PIER           7   2.25
"""


class _Ended(_Sales):
    _end = re.compile(r" +\*+ END OF REPORT \*+ *")


class _GroupedSales(Thousands[_Sale], _Sales):
    """Its units grouped in thousands (``1,200``)."""


class _MaybeEmptySales(_Sales):
    _rows_required = False
    _framing = (*_Sales._framing, re.compile(r"NO SALES"))


class _CountedSales(_Sales):
    """Its last line counts its stores, checked against the rows read."""

    _end = re.compile(r"STORES +(\d+)")

    @typing.override
    def _check_lines(self, read: typing.Sequence[tuple[int, re.Match[str]]]) -> None:
        super()._check_lines(read)
        if (rows := sum(match.re is self._line for _, match in read)) != int(read[-1][1][1]):
            msg = f"the report counts {read[-1][1][1]} stores, and holds {rows}"
            raise ValueError(msg)


class TestAReportFile:
    def test_rows_are_read_among_its_framing_each_stated_field_carried_down(self) -> None:
        sales = _Sales(_Sale, null_values=("n.a.",))
        assert sales.rows(_SALES) == [
            _Sale(dt.date(2026, 11, 6), "NORTH", "MAIN ST", 12, 1.5),
            _Sale(dt.date(2026, 11, 6), "NORTH", "MILL", 3, None),
            _Sale(dt.date(2026, 11, 6), "SOUTH", "PIER", 7, 2.25),
        ]
        assert sales.frame(_SALES).dtypes == [pl.Date, pl.String, pl.String, pl.Int64, pl.Float64]

    @pytest.mark.parametrize(
        ("body", "error"),
        [
            (_SALES.replace(b" MILL           3", b" MILL           x"), r"line 8 ' MILL +x +n.a.' is neither a row"),
            (_SALES.replace(b"SOLD  PRICE\n---", b"SOLD  PRICE\n=="), "line 5 '==.*' is neither a row nor a line"),
            (_SALES.replace(b" STORE      UNITS   UNIT\n", b"", 1), "no column header"),
            (_SALES.replace(b"REGION NORTH\n", b""), r"\['region'\] columns have an empty cell.*row 1"),
            (_SALES.replace(b"   n.a.", b"   cash"), "the 'price' cell 'cash' is not a number: row 2"),
            (_SALES.replace(b"11/06/2026   PAGE 1", b"11/31/2026   PAGE 1"), "the 'day' cell '11/31/2026'"),
            (_SALES.split(b" MAIN")[0], "the report holds no rows"),
            (b" \n\n", "the body is empty"),
        ],
    )
    def test_a_body_that_is_not_the_report_raises(self, body: bytes, error: str) -> None:
        with pytest.raises(ValueError, match=error):
            _Sales(_Sale, null_values=("n.a.",)).rows(body)

    def test_a_report_that_writes_its_end_must_end_with_it(self) -> None:
        ended = _SALES + b"  ***** END OF REPORT *****\n\n"
        assert len(_Ended(_Sale, null_values=("n.a.",)).rows(ended)) == 3
        with pytest.raises(ValueError, match=r"the report ends with ' PIER .*': it was cut"):
            _Ended(_Sale, null_values=("n.a.",)).rows(_SALES)

    def test_a_report_that_may_hold_no_rows_reads_none(self) -> None:
        empty = _SALES.split(b"REGION NORTH")[0] + b"NO SALES\n"
        assert _MaybeEmptySales(_Sale).rows(empty) == []
        with pytest.raises(ValueError, match="no column header"):
            _MaybeEmptySales(_Sale).rows(b"SALES REPORT   DAY 11/06/2026   PAGE 1\nNO SALES\n")

    def test_a_format_checks_its_lines(self) -> None:
        assert len(_CountedSales(_Sale, null_values=("n.a.",)).rows(_SALES + b"STORES 3\n")) == 3
        with pytest.raises(ValueError, match="the report counts 4 stores, and holds 3"):
            _CountedSales(_Sale, null_values=("n.a.",)).rows(_SALES + b"STORES 4\n")

    def test_the_final_line_ending_is_checked(self) -> None:
        with pytest.raises(ValueError, match="always ends its last line"):
            _Sales(_Sale, null_values=("n.a.",), final_line_ending=True).rows(_SALES.rstrip(b"\n"))

    def test_thousands_cleans_a_reports_numbers(self) -> None:
        body = _SALES.replace(b"MAIN ST       12", b"MAIN ST    1,200")
        assert _GroupedSales(_Sale, null_values=("n.a.",)).rows(body)[0].units == 1200
        with pytest.raises(ValueError, match="the 'units' cell '12,00' is not a number grouped in thousands: row 1"):
            _GroupedSales(_Sale, null_values=("n.a.",)).rows(_SALES.replace(b"MAIN ST       12", b"MAIN ST    12,00"))

    def test_a_layout_whose_groups_are_not_its_fields_raises(self) -> None:
        class Stray(_Sales):
            _framing = (*_Sales._framing, re.compile(r"CLERK (?P<clerk>\w+)"))

        class Both(_Sales):
            _framing = (*_Sales._framing, re.compile(r"STORE (?P<store>\w+)"))

        with pytest.raises(ValueError, match=r"the format's groups \['clerk'\] name no field of _Sale"):
            Stray(_Sale)
        with pytest.raises(ValueError, match=r"the fields \['store'\] are both a row's cells and stated"):
            Both(_Sale)


class _Shares(msgspec.Struct, frozen=True, gc=False):
    holder: str
    month: Annotated[dt.date, Format("%Y-%m")]
    value: float
    share: float | None


class _Held(LongReport[_Shares]):
    """A block a year: its months over a holder's value and share for each."""

    _key = "month"
    _line = re.compile(r"(?P<holder>[A-Z][a-z]+) +(?P<value>[-0-9. ]+?) *")
    _framing = (re.compile(r"HOLDER +(?P<month>\S.*?) *"),)


_HELD = b"""HOLDER  2026-02  2026-01
Japan   1.5 10   2.5 20
Peru    3.5 30   4.5 -

HOLDER  2025-12
Japan   5.5 50
"""


class TestALongReport:
    def test_each_key_is_a_row_its_block_header_naming_the_keys(self) -> None:
        rows = _Held(_Shares, null_values=("-",)).rows(_HELD)
        assert rows == [
            _Shares("Japan", dt.date(2026, 2, 1), 1.5, 10.0),
            _Shares("Japan", dt.date(2026, 1, 1), 2.5, 20.0),
            _Shares("Peru", dt.date(2026, 2, 1), 3.5, 30.0),
            _Shares("Peru", dt.date(2026, 1, 1), 4.5, None),
            _Shares("Japan", dt.date(2025, 12, 1), 5.5, 50.0),
        ]

    def test_a_row_of_another_count_of_values_raises(self) -> None:
        with pytest.raises(ValueError, match="row 2 holds 3 values, not 4: 2 for each key above it"):
            _Held(_Shares, null_values=("-",)).rows(_HELD.replace(b"4.5 -", b"4.5"))
        with pytest.raises(ValueError, match="row 1 holds 2 values, not 0"):
            _Held(_Shares).rows(b"Japan   5.5 50\n")


class _Holding(msgspec.Struct, frozen=True, gc=False):
    sector: Annotated[str, Label("SECTOR")]
    name: Annotated[str, Label("NAME")]
    value: Annotated[float, Label("VALUE")]


class _Sectors(Carried[_Holding], CsvFile[_Holding]):
    _carried = ("sector",)


class TestCarried:
    def test_a_group_printed_on_its_first_row_is_carried_down(self) -> None:
        rows = _Sectors(_Holding).rows(b"SECTOR,NAME,VALUE\nBanks,a,1\n,b,2\nOil,c,3\n,d,4\n")
        assert [(row.sector, row.name) for row in rows] == [("Banks", "a"), ("Banks", "b"), ("Oil", "c"), ("Oil", "d")]

    def test_a_first_row_without_its_group_or_a_carried_stray_raises(self) -> None:
        with pytest.raises(ValueError, match=r"\['sector'\] columns have an empty cell.*row 1"):
            _Sectors(_Holding).rows(b"SECTOR,NAME,VALUE\n,a,1\n")

        class Stray(_Sectors):
            _carried = ("region",)

        with pytest.raises(ValueError, match=r"carries \['region'\] down, which are no fields of _Holding"):
            Stray(_Holding).rows(b"SECTOR,NAME,VALUE\nBanks,a,1\n")


_CSV = b"NAME,VALUE,NOTE\na,1.5,x\n"


def _zip(**members: bytes) -> bytes:
    """Return a zip of *members*, each name to its bytes, stored (not deflated)."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return out.getvalue()


class _ZippedCsv(ZippedFile[_Row], CsvFile[_Row]):
    _member = re.compile(r"(?:.*/)?table\.csv")


class _ZippedBoth(ZippedFile[_Row], _Both):
    _member = re.compile(r"table\.csv")


class _Paired(ZippedFile[_Record], XmlFile[_Record]):
    _member = re.compile(r"[^/]*\.xml")
    _members = 2


class TestAZippedFile:
    def test_its_member_is_read_by_the_reader_it_is_composed_with(self) -> None:
        assert _ZippedCsv(_Row).rows(_zip(**{"readme.txt": b"", "2026/table.csv": _CSV})) == [_Row("a", 1.5, "x")]
        assert _ZippedBoth(_Row).rows(_zip(**{"table.csv": b"NAME,VALUE\na,1.5\n1\n"})) == [_Row("a", 1.5, None)]
        body = _zip(**{"t.txt": b"not read", "t.xml": _xml(_cells())})
        assert _Paired(_Record, root="Table", record="Row").frame(body).height == 1

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (_CSV, r"^the body is not a zip: "),
            (_zip(**{"t.xml": _xml(_cells())}), r"^the zip holds \['t\.xml'\], not 2 members$"),
            (
                _zip(**{"t.txt": b"", "t.csv": b""}),
                r"^the zip holds 0 members matching \S+, not one: \['t\.txt', 't\.csv'\]$",
            ),
            (
                _zip(a=b"", **{"t.xml": b"", "u.xml": b""}),
                r"^the zip holds \['a', 't\.xml', 'u\.xml'\], not 2 members$",
            ),
            (_zip(**{"t.xml": b"", "u.xml": b""}), r"holds 2 members matching \S+, not one: \['t\.xml', 'u\.xml'\]$"),
            (
                _zip(**{"t.txt": b"", "t.xml": _xml(_cells())}).replace(b"<Name>a<", b"<Name>b<"),
                r"^the zip's 't\.xml' will not inflate: Bad CRC",
            ),
        ],
        ids=["no zip", "another count", "no member", "a third member", "several members", "a damaged member"],
    )
    def test_a_zip_that_is_not_the_file_raises(self, body: bytes, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            _Paired(_Record, root="Table", record="Row").frame(body)

    def test_a_member_past_the_inflate_cap_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("flatread.archives._INFLATED_MAX", len(_CSV) - 1)
        with pytest.raises(ValueError, match=r"^the zip's 'table\.csv' inflates to \d+ bytes, over \d+$"):
            _ZippedCsv(_Row).rows(_zip(**{"table.csv": _CSV}))


class _Daily(msgspec.Struct, frozen=True, gc=False):
    """A day's row of a month's zip: the day its file's name states, then its file's columns."""

    day: Annotated[dt.date, Format("%Y%m%d")]
    name: Annotated[str, Label("NAME")]
    value: Annotated[float, Label("VALUE")]


class _Month(ZippedFiles[_Daily], CsvFile[_Daily]):
    _member = re.compile(r"day(?P<day>\d{8})\.csv")

    @typing.override
    def _part(self, name: re.Match[str], part: pl.DataFrame) -> pl.DataFrame:
        if part.is_empty():
            msg = f"{name[0]} holds no rows"
            raise ValueError(msg)
        return part


class _Days(ZippedFiles[_Daily], CsvFile[_Daily]):
    _member = re.compile(r"day(?P<day>\d{8})\.csv")


class TestZippedFiles:
    def test_a_part_is_kept_as_read_where_a_format_checks_none(self) -> None:
        body = _zip(**{"day20261105.csv": b"NAME,VALUE\n", "day20261106.csv": b"NAME,VALUE\nb,2\n"})
        assert _Days(_Daily).rows(body) == [_Daily(dt.date(2026, 11, 6), "b", 2.0)]

    def test_every_member_is_read_in_name_order_its_name_stating_a_field(self) -> None:
        body = _zip(**{"day20261106.csv": b"NAME,VALUE\nb,2\nc,3\n", "day20261105.csv": b"NAME,VALUE\na,1\n"})
        assert _Month(_Daily).rows(body) == [
            _Daily(dt.date(2026, 11, 5), "a", 1.0),
            _Daily(dt.date(2026, 11, 6), "b", 2.0),
            _Daily(dt.date(2026, 11, 6), "c", 3.0),
        ]

    def test_a_field_written_across_two_columns_is_joined(self) -> None:
        class Stamped(msgspec.Struct, frozen=True, gc=False):
            day: Annotated[dt.date, Format("%Y%m%d")]
            stamp: Annotated[dt.datetime, Label("Date"), Format("%Y-%m-%d %H:%M"), Joined("Time")]

        class StampedMonth(ZippedFiles[Stamped], CsvFile[Stamped]):
            _member = re.compile(r"day(?P<day>\d{8})\.csv")

        body = _zip(**{"day20261105.csv": b"Date,Time\n2026-11-05,09:30\n"})
        assert StampedMonth(Stamped).rows(body) == [Stamped(dt.date(2026, 11, 5), dt.datetime(2026, 11, 5, 9, 30))]

    def test_a_given_field_is_stated_on_every_row(self) -> None:
        class Coded(msgspec.Struct, frozen=True, gc=False):
            day: Annotated[dt.date, Format("%Y%m%d")]
            code: str
            name: Annotated[str, Label("NAME")]

        class CodedMonth(ZippedFiles[Coded], CsvFile[Coded]):
            _member = re.compile(r"day(?P<day>\d{8})\.csv")

            def __init__(self) -> None:
                self._given = {"code": "H8"}
                super().__init__(Coded)

        body = _zip(**{"day20261105.csv": b"NAME\na\n"})
        assert CodedMonth().rows(body) == [Coded(dt.date(2026, 11, 5), "H8", "a")]

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (_zip(), r"^the zip holds \[\], not one or more members, each matching"),
            (_zip(**{"day20261105.csv": _CSV, "notes.txt": b""}), r"holds \['day20261105\.csv', 'notes\.txt'\], not"),
            (
                _zip(**{"day20261305.csv": b"NAME,VALUE\na,1\n"}),
                r"^the member 'day20261305\.csv' states its rows' the 'day' cell '20261305' is not a date",
            ),
            (_zip(**{"day20261105.csv": b"NAME,VALUE\n"}), r"^day20261105\.csv holds no rows$"),
            (_zip(**{"day20261105.csv": b"NAME,VALUE\na,x\n"}), r"could not parse `x`"),
            (_zip(**{"day20261105.csv": b"NAME,VAL\na,1\n"}), r"column names not specified in schema"),
        ],
        ids=[
            "no member",
            "another member",
            "a name that is no day",
            "a part refused",
            "a member not the file",
            "a header",
        ],
    )
    def test_a_zip_that_is_not_the_tables_parts_raises(self, body: bytes, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            _Month(_Daily).frame(body)


class _Long(msgspec.Struct, frozen=True, gc=False):
    """A wide table's record (a name and a day) and one of its columns, read long."""

    name: Annotated[str, Label("NAME")]
    day: Annotated[dt.date, Label("DAY"), Format("%Y-%m-%d")]
    horizon: int
    value: float | None


class _LongCsv(LongFile[_Long], CsvFile[_Long]):
    _keys = re.compile(r"H(?P<key>\d+)")


class _LongSheet(LongFile[_Long], SheetFile[_Long]):
    _keys = re.compile(r"Horizon (?P<key>\d+)")


class _ZippedLong(LongFile[_Long], ZippedFile[_Long], CsvFile[_Long]):
    _keys = re.compile(r"H(?P<key>\d+)")
    _member = re.compile(r"t\.csv")


class _Monthly(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("NAME")]
    month: Annotated[dt.date, Format("%Y-%m")]
    value: float


class _MonthlyCsv(LongFile[_Monthly], CsvFile[_Monthly]):
    _keys = re.compile(r"\d{4}-\d{2}")


class _Maturity(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Label("DAY"), Format("%Y-%m-%d")]
    months: int
    value: float


class _MaturityCsv(LongFile[_Maturity], CsvFile[_Maturity]):
    """A key computed from its header: ``03MO`` is 3 months, ``10YR`` 120."""

    _keys = re.compile(r"(?P<count>\d{2})(?P<unit>MO|YR)")

    @typing.override
    def _key(self, match: re.Match[str]) -> str:
        return str(int(match["count"]) * (1 if match["unit"] == "MO" else 12))


_WIDE = b"NAME,DAY,H2,H5\na,2026-01-02,1.5,\nb,2026-01-03,2,3.25\n"


class TestALongFile:
    def test_a_wide_csv_reads_a_row_per_cell_record_by_record_an_empty_cell_none(self) -> None:
        assert _LongCsv(_Long).rows(_WIDE) == [
            _Long("a", dt.date(2026, 1, 2), 2, 1.5),
            _Long("a", dt.date(2026, 1, 2), 5, None),
            _Long("b", dt.date(2026, 1, 3), 2, 2.0),
            _Long("b", dt.date(2026, 1, 3), 5, 3.25),
        ]
        assert _LongCsv(_Long).frame(_WIDE).schema == {
            "name": pl.String,
            "day": pl.Date,
            "horizon": pl.Int64,
            "value": pl.Float64,
        }

    def test_with_empty_rows_off_an_empty_cell_is_no_row_and_the_value_may_be_required(self) -> None:
        class Filled(msgspec.Struct, frozen=True, gc=False):
            name: Annotated[str, Label("NAME")]
            day: Annotated[dt.date, Label("DAY"), Format("%Y-%m-%d")]
            horizon: int
            value: float

        class FilledCsv(LongFile[Filled], CsvFile[Filled]):
            _keys = re.compile(r"H(?P<key>\d+)")
            _empty_rows = False

        assert FilledCsv(Filled).rows(_WIDE) == [
            Filled("a", dt.date(2026, 1, 2), 2, 1.5),
            Filled("b", dt.date(2026, 1, 3), 2, 2.0),
            Filled("b", dt.date(2026, 1, 3), 5, 3.25),
        ]

    def test_a_wide_sheet_reads_the_same_its_rows_the_sheets_own(self) -> None:
        body = _book(
            ["NAME", "DAY", "Horizon 2", "Horizon 5"], ["a", "2026-01-02", 1.5, None], ["b", "2026-01-03", 2, 3.25]
        )
        assert _LongSheet(_Long, sheet="S").rows(body) == _LongCsv(_Long).rows(_WIDE)
        bad = _book(["NAME", "DAY", "Horizon 2"], ["a", "2026-01-02", 1.5], ["b", "2026-01-03", "x"])
        with pytest.raises(ValueError, match=r"^the 'Horizon 2' cell 'x' is not a number: row 3$"):
            _LongSheet(_Long, sheet="S").rows(bad)

    def test_it_composes_with_a_container(self) -> None:
        assert _ZippedLong(_Long).rows(_zip(**{"t.csv": _WIDE})) == _LongCsv(_Long).rows(_WIDE)

    def test_a_date_key_is_its_format_and_a_key_may_be_computed(self) -> None:
        assert _MonthlyCsv(_Monthly).rows(b"NAME,2026-01,2026-02\na,1,2\n") == [
            _Monthly("a", dt.date(2026, 1, 1), 1.0),
            _Monthly("a", dt.date(2026, 2, 1), 2.0),
        ]
        months = _MaturityCsv(_Maturity).frame(b"DAY,03MO,10YR\n2026-01-02,1,2\n")["months"]
        assert months.to_list() == [3, 120]

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (
                b"DAY,NAME,H2\n2026-01-02,a,1\n",
                r"^the header opens \['DAY', 'NAME'\], not the record's \['NAME', 'DAY'\]$",
            ),
            (b"NAME,DAY,H2,notes\na,2026-01-02,1,x\n", r"^the header's columns \['notes'\] are not keys, which match "),
            (b"NAME,DAY\na,2026-01-02\n", r"names no key, or a column twice$"),
            (b"NAME,DAY,H2,H2\na,2026-01-02,1,2\n", r"names no key, or a column twice$"),
            (b"NAME,DAY,H2,H02\na,2026-01-02,1,2\n", r"name keys \['2', '02'\], not each one horizon once$"),
            (b"NAME,DAY,H2\na,2026-01-02,x\n", r"could not parse `x` as dtype `f64` at column 'H2'"),
        ],
        ids=["leading labels", "a stray column", "no key", "a header twice", "a key twice", "a cell not a number"],
    )
    def test_a_table_that_is_not_the_layout_raises(self, body: bytes, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            _LongCsv(_Long).rows(body)

    def test_a_header_may_name_no_key_where_the_key_field_admits_none(self) -> None:
        class Spread(msgspec.Struct, frozen=True, gc=False):
            name: Annotated[str, Label("NAME")]
            horizon: str | None
            value: float

        class SpreadCsv(LongFile[Spread], CsvFile[Spread]):
            _keys = re.compile(r"[XY](?:_(\w+))?")

            @typing.override
            def _key(self, match: re.Match[str]) -> str | None:
                return None if match[1] is None else str(match[1])

        class Named(msgspec.Struct, frozen=True, gc=False):
            name: Annotated[str, Label("NAME")]
            horizon: str
            value: float

        class NamedCsv(LongFile[Named], CsvFile[Named]):
            _keys = SpreadCsv._keys
            _key = SpreadCsv._key

        assert SpreadCsv(Spread).rows(b"NAME,X,Y_6M\na,1,2\n") == [Spread("a", None, 1.0), Spread("a", "6M", 2.0)]
        with pytest.raises(ValueError, match=r"name keys \[None, None\], not each one horizon once$"):
            SpreadCsv(Spread).rows(b"NAME,X,Y\na,1,2\n")
        with pytest.raises(ValueError, match=r"name keys \[None, '6M'\], not each one horizon once$"):
            NamedCsv(Named).rows(b"NAME,X,Y_6M\na,1,2\n")

    def test_a_key_that_is_not_its_type_raises(self) -> None:
        with pytest.raises(ValueError, match=r"name keys \['2026-13'\], not each a month: "):
            _MonthlyCsv(_Monthly).rows(b"NAME,2026-13\na,1\n")

    def test_an_empty_required_value_names_the_tables_row_and_header(self) -> None:
        body = b"NAME,2026-01,2026-02\na,1,2\nb,3,\n"
        with pytest.raises(ValueError, match=r"^the \['2026-02'\] columns have an empty cell, .*: row 2$"):
            _MonthlyCsv(_Monthly).rows(body)
        with pytest.raises(ValueError, match=r"^the \['2026-01'\] columns hold NaN or an infinity, .*: row 1$"):
            _MonthlyCsv(_Monthly).rows(b"NAME,2026-01\na,inf\n")

    def test_a_value_field_takes_no_format_or_flag(self) -> None:
        class Dated(msgspec.Struct, frozen=True, gc=False):
            name: Annotated[str, Label("NAME")]
            horizon: int
            value: Annotated[dt.date, Format("%Y-%m-%d")]

        class DatedCsv(LongFile[Dated], CsvFile[Dated]):
            _keys = re.compile(r"\d+")

        with pytest.raises(ValueError, match=r"^the value field 'value' takes no Format or Flag$"):
            DatedCsv(Dated)


# --- Cell shapes: PaddedCsv, ThousandsCsv, a quarter's Format ---


class _Counts(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Label("DAY"), Format("%m/%d/%Y")]
    name: Annotated[str, Label("NAME")]
    count: Annotated[int | None, Label("COUNT")]
    rate: Annotated[float, Label("RATE")]


class _Padded(PaddedCsv[_Counts]):
    pass


class _Grouped(ThousandsCsv[_Counts]):
    pass


class _PaddedGrouped(ThousandsCsv[_Counts], PaddedCsv[_Counts]):
    pass


class _PaddedLong(LongFile[_Long], PaddedCsv[_Long]):
    _keys = re.compile(r"H(?P<key>\d+)")


class TestAPaddedCsv:
    def test_each_cell_s_spaces_are_stripped_before_it_is_typed(self) -> None:
        body = b"DAY,NAME,COUNT,RATE\n10/02/2019 , CHI , 794943, 1.03 \n10/03/2019,NYC,   ,2\n"
        assert _Padded(_Counts).rows(body) == [
            _Counts(dt.date(2019, 10, 2), "CHI", 794943, 1.03),
            _Counts(dt.date(2019, 10, 3), "NYC", None, 2.0),
        ]
        assert _Padded(_Counts).frame(body).schema == pl.Schema(
            {"day": pl.Date, "name": pl.String, "count": pl.Int64, "rate": pl.Float64}
        )

    def test_a_quoted_cell_is_stripped_inside_its_quotes(self) -> None:
        body = b'DAY,NAME,COUNT,RATE\n10/02/2019,"CHI  ",1,"2.5 "\n'
        assert _Padded(_Counts).rows(body) == [_Counts(dt.date(2019, 10, 2), "CHI", 1, 2.5)]

    def test_a_cell_that_is_not_its_type_raises_naming_its_row(self) -> None:
        with pytest.raises(ValueError, match=r"the 'COUNT' cell '1\.5' is not a whole number: row 2"):
            _Padded(_Counts).rows(b"DAY,NAME,COUNT,RATE\n10/02/2019,A,1,1\n10/02/2019,B, 1.5,1\n")

    def test_a_cell_of_spaces_where_every_cell_holds_a_value_raises(self) -> None:
        with pytest.raises(ValueError, match=r"\['rate'\] columns have an empty cell"):
            _Padded(_Counts).rows(b"DAY,NAME,COUNT,RATE\n10/02/2019,A,1,  \n")

    def test_a_long_table_s_values_are_stripped_too(self) -> None:
        assert _PaddedLong(_Long).rows(b"NAME,DAY,H1,H2\n x ,2026-01-02 , 1 ,\n") == [
            _Long("x", dt.date(2026, 1, 2), 1, 1.0),
            _Long("x", dt.date(2026, 1, 2), 2, None),
        ]


class TestAThousandsCsv:
    def test_a_number_grouped_in_thousands_reads_as_its_number(self) -> None:
        body = b'DAY,NAME,COUNT,RATE\n10/02/2019,"A,B","1,782,319","1,234.5"\n10/03/2019,C,12,-0.5\n'
        assert _Grouped(_Counts).rows(body) == [
            _Counts(dt.date(2019, 10, 2), "A,B", 1782319, 1234.5),
            _Counts(dt.date(2019, 10, 3), "C", 12, -0.5),
        ]

    def test_a_separated_file_needs_no_quotes(self) -> None:
        flat = _Grouped(_Counts, separator="\t")
        assert flat.rows(b"DAY\tNAME\tCOUNT\tRATE\n10/02/2019\tA\t1,000\t1\n") == [
            _Counts(dt.date(2019, 10, 2), "A", 1000, 1.0)
        ]

    @pytest.mark.parametrize("cell", ["1,78", "12,3456", ",123", "1,000,"])
    def test_a_number_not_grouped_in_thousands_raises(self, cell: str) -> None:
        body = f'DAY,NAME,COUNT,RATE\n10/02/2019,A,1,"{cell}"\n'.encode()
        with pytest.raises(ValueError, match=f"the 'RATE' cell '{cell}' is not a number grouped in thousands: row 1"):
            _Grouped(_Counts).rows(body)

    def test_it_composes_with_padding(self) -> None:
        body = b'DAY,NAME,COUNT,RATE\n10/02/2019, A ," 1,000 ", 2,500\n'
        with pytest.raises(ValueError, match="row 1 has 5 fields"):
            _PaddedGrouped(_Counts).rows(body)
        body = b'DAY,NAME,COUNT,RATE\n10/02/2019, A ," 1,000 ","2,500 "\n'
        assert _PaddedGrouped(_Counts).rows(body) == [_Counts(dt.date(2019, 10, 2), "A", 1000, 2500.0)]


class _PaddedMonth(msgspec.Struct, frozen=True, gc=False):
    month: Annotated[dt.date, Label("Month"), Format("%Ym%-m")]
    name: Annotated[str | None, Label("Name")]
    value: Annotated[float, Label("Value")]


class TestAPaddedSheet:
    def test_its_text_cells_are_stripped_before_they_are_typed(self) -> None:
        body = workbook({"S": [["Month", "Name", "Value"], [" 1988m1", " a ", 1.5], ["1988m10", "  ", 2]]})
        assert PaddedSheet(_PaddedMonth, sheet="S").rows(body) == [
            _PaddedMonth(dt.date(1988, 1, 1), "a", 1.5),
            _PaddedMonth(dt.date(1988, 10, 1), None, 2.0),
        ]

    def test_a_plain_sheet_refuses_the_padding(self) -> None:
        body = workbook({"S": [["Month", "Name", "Value"], [" 1988m1", "a", 1.5]]})
        with pytest.raises(ValueError, match="the 'Month' cell ' 1988m1' is not a date written %Ym%-m: row 2"):
            SheetFile(_PaddedMonth, sheet="S").rows(body)

    def test_a_cell_stripped_to_a_word_for_none_holds_no_value(self) -> None:
        body = workbook({"S": [["Month", "Name", "Value"], ["1988m1", "  NA", 1.5]]})
        assert PaddedSheet(_PaddedMonth, sheet="S", null_values=["NA"]).rows(body)[0].name is None


class _Rates(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Label("Day")]
    count: Annotated[int, Label("Count")]
    share: Annotated[float | None, Label("Share")]


class _PaddedNumbers(PaddedSheet[_Rates], NumberTextSheet[_Rates]):
    pass


class TestANumberTextSheet:
    def test_a_number_written_as_text_reads_as_its_number_beside_number_cells(self) -> None:
        body = workbook(
            {"S": [["Day", "Count", "Share"], [dt.date(2026, 1, 1), " 2", " 1.03"], [dt.date(2026, 1, 2), 3, 0.6]]}
        )
        assert _PaddedNumbers(_Rates, sheet="S", null_values=["NA"]).rows(body) == [
            _Rates(dt.date(2026, 1, 1), 2, 1.03),
            _Rates(dt.date(2026, 1, 2), 3, 0.6),
        ]

    @pytest.mark.parametrize(
        ("count", "share", "message"),
        [
            (" 2.5", 1.0, "the 'Count' cell '2.5' is not a whole number: row 2"),
            (2, "x", "the 'Share' cell 'x' is not a number: row 2"),
            (2, "NaN", r"\['share'\] columns hold NaN or an infinity"),
        ],
    )
    def test_a_text_cell_that_is_not_its_number_raises(self, count: Cell, share: Cell, message: str) -> None:
        body = workbook({"S": [["Day", "Count", "Share"], [dt.date(2026, 1, 1), count, share]]})
        with pytest.raises(ValueError, match=message):
            _PaddedNumbers(_Rates, sheet="S").rows(body)

    @pytest.mark.parametrize("count", [" 1e3", " 1.0", " 1.", " inf", " nan", " 1e30", " 99999999999999999999"])
    def test_an_int_field_takes_text_that_is_a_whole_number_and_nothing_else(self, count: str) -> None:
        body = workbook({"S": [["Day", "Count", "Share"], [dt.date(2026, 1, 1), count, 1.0]]})
        with pytest.raises(ValueError, match=r"the 'Count' cell '.*' is not a whole number: row 2"):
            _PaddedNumbers(_Rates, sheet="S").rows(body)

    def test_an_int_text_above_two_to_the_53_keeps_every_digit(self) -> None:
        body = workbook({"S": [["Day", "Count", "Share"], [dt.date(2026, 1, 1), " 9007199254740993", 1.0]]})
        assert _PaddedNumbers(_Rates, sheet="S").rows(body)[0].count == 9007199254740993

    def test_a_float_field_refuses_an_infinity_written_as_text(self) -> None:
        body = workbook({"S": [["Day", "Count", "Share"], [dt.date(2026, 1, 1), 2, " inf"]]})
        with pytest.raises(ValueError, match=r"\['share'\] columns hold NaN or an infinity"):
            _PaddedNumbers(_Rates, sheet="S").rows(body)

    def test_a_float_field_stripped_to_a_word_for_none_holds_no_value(self) -> None:
        body = workbook({"S": [["Day", "Count", "Share"], [dt.date(2026, 1, 1), 2, "  NA"]]})
        assert PaddedSheet(_Rates, sheet="S", null_values=["NA"]).rows(body)[0].share is None

    def test_a_padded_sheet_alone_refuses_a_number_written_as_padded_text(self) -> None:
        """Fastexcel's typed load reads ``'1.03'`` as its number, and ``' 1.03'`` as none."""
        body = workbook({"S": [["Day", "Count", "Share"], [dt.date(2026, 1, 1), 2, " 1.03"]]})
        with pytest.raises(ValueError, match=r"the 'Share' cell '1\.03' is not a number: row 2"):
            PaddedSheet(_Rates, sheet="S").rows(body)


class _NotesBeside(SheetFile[_Rates]):
    _unread = ("", "notes")


class _NotesBetween(PaddedSheet[_Rates], NumberTextSheet[_Rates]):
    _unread = ("notes",)


class TestUnreadColumns:
    def test_an_unread_column_in_the_middle_before_a_number_text_column_keeps_each_columns_type(self) -> None:
        body = workbook(
            {
                "S": [
                    ["Day", "notes", "Count", "Share"],
                    [dt.date(2026, 1, 1), "prose", " 2", " 1.03"],
                    [dt.date(2026, 1, 2), None, 3, 0.6],
                ]
            }
        )
        assert _NotesBetween(_Rates, sheet="S").rows(body) == [
            _Rates(dt.date(2026, 1, 1), 2, 1.03),
            _Rates(dt.date(2026, 1, 2), 3, 0.6),
        ]

    def test_a_column_no_field_reads_is_dropped_wherever_it_stands(self) -> None:
        rows = [["", "Day", "Count", "Share", "notes"], [1, dt.date(2026, 1, 1), 2, 1.5, "Seasonally adjusted."]]
        assert _NotesBeside(_Rates, sheet="S").rows(
            workbook({"S": [*rows, [2, dt.date(2026, 1, 2), 3, None, None]]})
        ) == [
            _Rates(dt.date(2026, 1, 1), 2, 1.5),
            _Rates(dt.date(2026, 1, 2), 3, None),
        ]

    def test_an_unread_column_before_typed_columns_in_a_table_that_opens_in_column_b_keeps_each_columns_type(
        self,
    ) -> None:
        body = workbook(
            {
                "S": [
                    [None, "Day", "notes", "Count", "Share"],
                    [None, dt.date(2026, 1, 1), "prose", 2, 1.5],
                    [None, dt.date(2026, 1, 2), None, 3, None],
                ]
            }
        )
        assert _NotesBetween(_Rates, sheet="S").rows(body) == [
            _Rates(dt.date(2026, 1, 1), 2, 1.5),
            _Rates(dt.date(2026, 1, 2), 3, None),
        ]

    def test_a_plain_sheet_refuses_the_column(self) -> None:
        body = workbook({"S": [["Day", "Count", "Share", "notes"], [dt.date(2026, 1, 1), 2, 1.5, "prose"]]})
        with pytest.raises(ValueError, match=r"header \(row 1\) \['Day', 'Count', 'Share', 'notes'\] is not"):
            SheetFile(_Rates, sheet="S").rows(body)

    def test_an_unread_column_in_the_middle_of_a_trimmed_stripped_sheet_with_gaps(self) -> None:
        class Trimmed(SheetFile[_Rates]):
            _unread = ("notes",)
            _trim = True
            _strip = True
            _gaps = True

        body = workbook(
            {
                "S": [
                    ["Day", " notes ", "Count", "Share"],
                    [dt.date(2026, 1, 1), "prose", 2, 1.5],
                    [None, " ", None, None],
                    [dt.date(2026, 1, 2), None, 3, None],
                ]
            }
        )
        assert Trimmed(_Rates, sheet="S").rows(body) == [
            _Rates(dt.date(2026, 1, 1), 2, 1.5),
            _Rates(dt.date(2026, 1, 2), 3, None),
        ]


class _Pair(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("Name")]
    value: Annotated[float, Label("Value")]


class _Offset(BesideSheet[_Pair]):
    _columns_at = (1, 3)


class _Listed(BesideSheet[_Pair], NotedSheet[_Pair]):
    pass


class TestABesideSheet:
    def test_only_the_tables_columns_are_read(self) -> None:
        body = workbook({"S": [["Name", "Value", None, "Note"], ["a", 1, None, "a note"], ["b", 2]]})
        assert BesideSheet(_Pair, sheet="S").rows(body) == [_Pair("a", 1.0), _Pair("b", 2.0)]

    def test_a_plain_sheet_refuses_the_cells_beside(self) -> None:
        body = workbook({"S": [["Name", "Value", None, "Note"], ["a", 1, None, "a note"]]})
        with pytest.raises(ValueError, match="sheet's header"):
            SheetFile(_Pair, sheet="S").rows(body)

    def test_its_columns_may_start_right_of_others_and_skip_a_spacer(self) -> None:
        body = workbook({"S": [["Code", "Name", None, "Value"], ["X@Y", "a", None, 1]]})
        assert _Offset(_Pair, sheet="S").rows(body) == [_Pair("a", 1.0)]

    def test_a_list_beside_it_that_runs_longer_needs_the_table_to_end(self) -> None:
        body = workbook({"S": [["Name", "Value", None, "List"], ["a", 1, None, "x"], [None, None, None, "y"]]})
        with pytest.raises(ValueError, match="row 3 is empty"):
            BesideSheet(_Pair, sheet="S").rows(body)
        assert _Listed(_Pair, sheet="S").rows(body) == [_Pair("a", 1.0)]

    def test_columns_that_are_not_one_a_field_are_another_header(self) -> None:
        class Short(BesideSheet[_Pair]):
            _columns_at = (0,)

        with pytest.raises(ValueError, match=r"header \(row 1\) \['Name'\] is not \['Name', 'Value'\]"):
            Short(_Pair, sheet="S").rows(workbook({"S": [["Name", "Value"], ["a", 1]]}))

    def test_a_sheet_without_the_tables_columns_raises(self) -> None:
        with pytest.raises(ValueError, match=r"'S' sheet has no column \[1, 3\] of its table"):
            _Offset(_Pair, sheet="S").rows(workbook({"S": [["Code", "Name"], ["X@Y", "a"]]}))

    def test_columns_that_are_not_one_a_field_are_a_bug(self) -> None:
        class Short(BesideSheet[_Pair]):
            _columns_at = (0,)

        with pytest.raises(ValueError, match=r"the 'S' sheet's header \(row 1\) \['Name'\] is not \['Name', 'Value'\]"):
            Short(_Pair, sheet="S").rows(workbook({"S": [["Name", "Value"], ["a", 1]]}))


class _Mapped(msgspec.Struct, frozen=True, gc=False):
    code: str
    ticker: str | None


class _Headerless(SheetFile[_Mapped]):
    _header = False


class TestAHeaderlessSheet:
    def test_its_rows_are_read_by_position_from_under_header_row(self) -> None:
        body = workbook({"S": [[None], ["A@B", "A_B"], ["C@D", None]]})
        assert _Headerless(_Mapped, sheet="S", header_row=1).rows(body) == [_Mapped("A@B", "A_B"), _Mapped("C@D", None)]

    def test_from_the_first_row_with_header_row_zero(self) -> None:
        body = workbook({"S": [["A@B", "A_B"]]})
        assert _Headerless(_Mapped, sheet="S", header_row=0).rows(body) == [_Mapped("A@B", "A_B")]

    def test_a_sheet_whose_last_column_is_empty_all_down_reads_it_as_none(self) -> None:
        body = workbook({"S": [["A@B"], ["C@D"]]})
        assert _Headerless(_Mapped, sheet="S", header_row=0).rows(body) == [_Mapped("A@B", None), _Mapped("C@D", None)]

    def test_a_sheet_wider_than_its_table_raises(self) -> None:
        body = workbook({"S": [["A@B", "A_B", "extra"]]})
        with pytest.raises(ValueError, match="table is 3 columns wide, not 2"):
            _Headerless(_Mapped, sheet="S", header_row=0).rows(body)

    def test_a_row_of_another_width_raises(self) -> None:
        body = workbook({"S": [["A@B", "A_B", "extra"]]})
        with pytest.raises(ValueError, match="table is 3 columns wide, not 2"):
            _Headerless(_Mapped, sheet="S", header_row=0).rows(body)


class _Twice(msgspec.Struct, frozen=True, gc=False):
    first: Annotated[int, Label("N")]
    second: Annotated[int | None, Label("N")]


class TestARepeatedLabelInAPaddedCsv:
    def test_each_column_is_stripped_and_typed_by_its_place(self) -> None:
        assert PaddedCsv(_Twice).rows(b"N,N\n 1 , 2\n3,\n") == [_Twice(1, 2), _Twice(3, None)]


class _Quarterly(msgspec.Struct, frozen=True, gc=False):
    quarter: Annotated[dt.date, Label("QUARTER"), Format("%YQ%q", alternatives=("%Y:Q%q", "%Y: Q%q", "%Y:%q"))]
    value: Annotated[float, Label("VALUE")]


class TestAQuarterFormat:
    def test_a_quarter_reads_as_its_first_day_in_each_way_it_is_written(self) -> None:
        body = b"QUARTER,VALUE\n2026Q1,1\n2022:Q4,2\n2026: Q2,3\n1979:3,4\n"
        assert [row.quarter for row in CsvFile(_Quarterly).rows(body)] == [
            dt.date(2026, 1, 1),
            dt.date(2022, 10, 1),
            dt.date(2026, 4, 1),
            dt.date(1979, 7, 1),
        ]

    def test_a_quarter_s_text_may_hold_more_around_it(self) -> None:
        class Padded(msgspec.Struct, frozen=True, gc=False):
            quarter: Annotated[dt.date, Label("Q"), Format("%Y:0%q")]

        assert CsvFile(Padded).rows(b"Q\n1960:01\n1960:04\n") == [
            Padded(dt.date(1960, 1, 1)),
            Padded(dt.date(1960, 10, 1)),
        ]

    @pytest.mark.parametrize("cell", ["2026Q5", "2026Q0", "26Q1", "2026Q1 ", "2026-Q1", "2026q1"])
    def test_a_cell_written_otherwise_raises(self, cell: str) -> None:
        with pytest.raises(ValueError, match=re.escape(f"the 'quarter' cell '{cell}' is not a date written %YQ%q")):
            CsvFile(_Quarterly).rows(f"QUARTER,VALUE\n{cell},1\n".encode())

    def test_a_quarter_is_a_key_a_long_table_reads(self) -> None:
        class ByQuarter(msgspec.Struct, frozen=True, gc=False):
            day: Annotated[dt.date, Label("DAY"), Format("%Y-%m-%d")]
            quarter: Annotated[dt.date, Format("%YQ%q")]
            value: float | None

        class ByQuarterCsv(LongFile[ByQuarter], CsvFile[ByQuarter]):
            _keys = re.compile(r"[0-9]{4}Q[1-4]")

        assert ByQuarterCsv(ByQuarter).rows(b"DAY,2026Q1,2026Q2\n2026-01-05,1,\n") == [
            ByQuarter(dt.date(2026, 1, 5), dt.date(2026, 1, 1), 1.0),
            ByQuarter(dt.date(2026, 1, 5), dt.date(2026, 4, 1), None),
        ]

    def test_a_quarter_in_an_xml_table_too(self) -> None:
        class Quarter(msgspec.Struct, frozen=True, gc=False):
            quarter: Annotated[dt.date, Label("q"), Format("%YQ%q")]

        assert XmlFile(Quarter, root="t", record="r").rows(b"<t><r><q>2026Q3</q></r></t>") == [
            Quarter(dt.date(2026, 7, 1))
        ]

    @pytest.mark.parametrize(
        "written", [Format("%YQ%q %m"), Format("%Y", alternatives=("%Y%yQ%q",)), Format("%Y-%Y"), Format("%m")]
    )
    def test_a_period_format_with_another_directive_is_refused(self, written: Format) -> None:
        class Odd(msgspec.Struct, frozen=True, gc=False):
            quarter: Annotated[dt.date, Label("Q"), written]

        with pytest.raises(ValueError, match="writes no day, so it writes a period"):
            CsvFile(Odd)

    def test_a_quarter_format_on_a_time_is_refused(self) -> None:
        class Odd(msgspec.Struct, frozen=True, gc=False):
            quarter: Annotated[dt.datetime, Label("Q"), Format("%YQ%q")]

        with pytest.raises(ValueError, match="is no date, so its Format '%YQ%q' writes no quarter"):
            CsvFile(Odd)


class TestAPeriodFormat:
    @pytest.mark.parametrize(
        ("written", "cells", "days"),
        [
            ("%Ym%-m", ["2011m9", "2011m10"], [dt.date(2011, 9, 1), dt.date(2011, 10, 1)]),
            ("%Y%m", ["198803"], [dt.date(1988, 3, 1)]),
            ("%b-%Y", ["Sep-2026"], [dt.date(2026, 9, 1)]),
            ("%Y", ["1999"], [dt.date(1999, 1, 1)]),
        ],
    )
    def test_a_date_without_a_day_reads_as_its_period_s_first_day(
        self, written: str, cells: list[str], days: list[dt.date]
    ) -> None:
        class Month(msgspec.Struct, frozen=True, gc=False):
            month: Annotated[dt.date, Label("M"), Format(written)]

        body = "\n".join(["M", *cells, ""]).encode()
        assert CsvFile(Month).rows(body) == [Month(day) for day in days]

    @pytest.mark.parametrize(("written", "cell"), [("%Ym%-m", "2011m09"), ("%Y%m", "19883"), ("%Ym%-m", "2011m13")])
    def test_a_period_written_otherwise_raises(self, written: str, cell: str) -> None:
        class Month(msgspec.Struct, frozen=True, gc=False):
            month: Annotated[dt.date, Label("M"), Format(written)]

        with pytest.raises(ValueError, match=f"the 'month' cell '{cell}' is not a date written"):
            CsvFile(Month).rows(f"M\n{cell}\n".encode())


class _TickerDay(msgspec.Struct, frozen=True, gc=False):
    ticker: Annotated[str, Label("Ticker")]
    date: Annotated[dt.date, Format("%Y-%m-%d")]
    value: float


class _TickerSheet(LongFile[_TickerDay], CellHeaderSheet[_TickerDay]):
    _keys = re.compile(r"(?P<key>\d{4}-\d{2}-\d{2}) 00:00:00")
    _empty_rows = False
    _gaps = True


class _Rowed(msgspec.Struct, frozen=True, gc=False):
    row: int
    ticker: Annotated[str | None, Label("Ticker")]
    date: Annotated[dt.date, Format("%Y-%m-%d")]
    value: float


class _RowedSheet(LongFile[_Rowed], CellHeaderSheet[_Rowed]):
    _keys = _TickerSheet._keys
    _empty_rows = False
    _numbered = "row"


_JAN, _FEB = dt.date(2026, 1, 31), dt.date(2026, 2, 28)


class TestACellHeaderSheetReadLong:
    def test_its_date_cells_are_the_keys_and_blank_rows_and_headings_are_no_rows(self) -> None:
        body = workbook({"S": [["Ticker", _JAN, _FEB], ["A", 1, None], [None], ["Heading"], ["B", 2, 3]]})
        assert _TickerSheet(_TickerDay, sheet="S").rows(body) == [
            _TickerDay("A", _JAN, 1.0),
            _TickerDay("B", _JAN, 2.0),
            _TickerDay("B", _FEB, 3.0),
        ]

    def test_fastexcel_names_a_date_header_nothing(self) -> None:
        body = workbook({"S": [["Ticker", _JAN], ["A", 1]]})
        with pytest.raises(ValueError, match=r"the header's columns \[''\] are not keys"):
            _PlainLong(_TickerDay, sheet="S").rows(body)

    def test_a_blank_row_inside_a_plain_sheet_still_raises(self) -> None:
        body = workbook({"S": [["Ticker", _JAN], ["A", 1], [None], ["B", 2]]})
        with pytest.raises(ValueError, match="row 3 is empty"):
            _RowedSheet(_Rowed, sheet="S").rows(body)

    def test_a_record_with_values_still_needs_its_fields(self) -> None:
        body = workbook({"S": [["Ticker", _JAN], [None, 1]]})
        with pytest.raises(ValueError, match=r"\['ticker'\] columns have an empty cell.*row 2"):
            _TickerSheet(_TickerDay, sheet="S").rows(body)

    def test_a_numbered_record_holds_its_own_row(self) -> None:
        body = workbook({"S": [["Ticker", _JAN, _FEB], ["A", 1, None], [None, 2, 3]]})
        assert _RowedSheet(_Rowed, sheet="S").rows(body) == [
            _Rowed(2, "A", _JAN, 1.0),
            _Rowed(3, None, _JAN, 2.0),
            _Rowed(3, None, _FEB, 3.0),
        ]


class _GappedRows(_RowedSheet):
    _gaps = True


class TestANumberedSheet:
    def test_a_row_after_a_gap_keeps_its_own_sheet_row(self) -> None:
        body = workbook({"S": [["Ticker", _JAN], ["A", 1], [None], [None, 2]]})
        assert _GappedRows(_Rowed, sheet="S").rows(body) == [_Rowed(2, "A", _JAN, 1.0), _Rowed(4, None, _JAN, 2.0)]

    def test_a_plain_numbered_sheet_reads_each_rows_number_first(self) -> None:
        body = workbook({"S": [["Name"], ["a"], ["b"]]})
        assert _NumberedNames(_NumberedName, sheet="S", header_row=1).rows(body) == [
            _NumberedName(2, "a"),
            _NumberedName(3, "b"),
        ]


class _NumberedName(msgspec.Struct, frozen=True, gc=False):
    row: int
    name: Annotated[str, Label("Name")]


class _NumberedNames(SheetFile[_NumberedName]):
    _numbered = "row"


class _PlainLong(LongFile[_TickerDay], SheetFile[_TickerDay]):
    _keys = _TickerSheet._keys
    _empty_rows = False


class _Offset3(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Label("Day")]
    count: Annotated[int, Label("Count")]
    rate: Annotated[float, Label("Rate")]


class TestASheetStartingRightOfColumnA:
    def test_each_column_is_loaded_as_its_fields_type(self) -> None:
        """Fastexcel keys a load's types by the sheet's column, so a table from ``C`` is typed by its own columns."""
        body = workbook({"S": [[None, None, "Day", "Count", "Rate"], [None, None, dt.date(2026, 1, 2), 2, 1.5]]})
        assert SheetFile(_Offset3, sheet="S").rows(body) == [_Offset3(dt.date(2026, 1, 2), 2, 1.5)]


# --- FoundTable, FoundCsv, FoundSheet, ZippedOrBare ---


class _Found(msgspec.Struct, frozen=True, gc=False):
    section: str | None
    year: int | None
    kind: Annotated[str | None, Label("type", alternatives=("kind",))]
    note: str | None


class _FoundCsv(FoundCsv[_Found]):
    _nulls = ("NULL",)
    _section = "Section:"
    _stated = "section"
    _as_written = ("section",)

    @typing.override
    def _heading(self, cell: str) -> str:
        return cell.lower()


class _CountedCsv(_FoundCsv):
    _row_count = True


class TestAFoundCsv:
    def _rows(self, body: bytes) -> list[_Found]:
        return _FoundCsv(_Found, key="year").rows(body)

    def test_the_header_is_found_below_any_title_lines_and_blank_and_rule_lines_are_skipped(self) -> None:
        body = b"Title,,\nAs of,2026,\n\nYEAR,Kind,Note\n-----,---,\n,,\n2026,a,x\n   \n2025,NULL,\n"
        assert self._rows(body) == [_Found(None, 2026, "a", "x"), _Found(None, 2025, None, None)]

    def test_a_short_row_and_a_field_the_header_lacks_are_null_and_no_rows_is_empty(self) -> None:
        assert self._rows(b"Year,Type\n2026\n") == [_Found(None, 2026, None, None)]
        assert self._rows(b"Year,Type\n") == []

    def test_a_cell_is_text_as_written_and_a_whole_number_is_typed(self) -> None:
        assert self._rows(b'Year,Note\n 2026 ,"a, b"\n') == [_Found(None, 2026, None, "a, b")]
        with pytest.raises(ValueError, match=r"'x' is not a whole number"):
            self._rows(b"Year\nx\n")

    def test_an_unlabeled_column_must_be_empty_but_for_a_leading_row_count(self) -> None:
        assert self._rows(b"Year,,Note\n2026,,x\n") == [_Found(None, 2026, None, "x")]
        for body in (b"Year,,Note\n2026,y,x\n", b",Year,Note\n1,2026,x\n", b"Year\n2026,y\n"):
            with pytest.raises(ValueError, match=r"line 2 holds a value under no label"):
                self._rows(body)

    def test_a_leading_unlabeled_column_of_whole_numbers_is_a_row_count_only_where_the_format_says_so(self) -> None:
        counted = _CountedCsv(_Found, key="year")
        assert counted.rows(b",Year,Note\n1,2026,x\n") == [_Found(None, 2026, None, "x")]
        with pytest.raises(ValueError, match=r"line 2 holds a value under no label"):
            counted.rows(b",Year\nx,2026\n")

    def test_a_header_that_names_a_stranger_a_field_twice_or_no_key_is_refused(self) -> None:
        for body, message in (
            (b"Year,Bogus\n1,2\n", r"does not declare: \['Bogus'\]"),
            (b"Year,Type,Kind\n1,2,3\n", r"names one field twice"),
            (b"Type,Note\na,b\n", r"no header naming year"),
        ):
            with pytest.raises(ValueError, match=message):
                self._rows(body)

    def test_a_section_line_states_its_field_as_written_for_the_rows_below(self) -> None:
        body = b"Year,Note\nSection: FOIA\n1,a\nSection:  \n2,b\n3,c\n"
        assert [row.section for row in self._rows(body)] == ["FOIA", "", ""]
        assert self._rows(b"Year\n1\n")[0].section is None

    def test_latin_1_is_read_where_utf_8_fails_and_a_byte_order_mark_is_dropped(self) -> None:
        assert self._rows(b"Year,Note\n1,caf\xe9\n")[0].note == "caf\xe9"
        assert self._rows(codecs.BOM_UTF8 + b"Year,Note\n1,x\n")[0].year == 1


class TestAFoundSheet:
    def test_a_workbook_is_read_by_its_first_sheet_past_its_title_rows(self) -> None:
        body = _book(["Title"], [], ["year", "note", None, "type"], [2026, "x", None, "a"], [None, None, None, None])
        assert FoundSheet(_Found, key="year").rows(body) == [_Found(None, 2026, "a", "x")]

    def test_a_body_that_is_no_workbook_raises(self) -> None:
        with pytest.raises(ValueError, match=r"not a workbook"):
            FoundSheet(_Found, key="year").rows(b"Year\n1\n")


class _Name(msgspec.Struct, frozen=True, gc=False):
    name: str


class _FoundNames(ZippedOrBare[_Name], FoundCsv[_Name]):
    _member = re.compile(r"(?i).+\.csv")
    _members = 1

    @typing.override
    def _inner(self, name: str, member: bytes) -> pl.DataFrame:
        return super()._inner(name, member).with_columns(pl.lit(name).alias("name"))


class TestAZippedOrBare:
    def test_a_zip_with_a_matching_member_is_read_by_the_member_and_its_name(self) -> None:
        assert _FoundNames(_Name, key="name").rows(_zip(**{"A.CSV": b"name\nx\n"})) == [_Name("A.CSV")]

    def test_a_body_without_one_is_read_whole_as_a_member_named_nothing(self) -> None:
        reader = _FoundNames(_Name, key="name")
        assert reader.rows(b"name\nx\n") == [_Name("")]

    def test_a_zip_of_more_members_than_the_format_holds_raises(self) -> None:
        with pytest.raises(ValueError, match=r"not 1 members"):
            _FoundNames(_Name, key="name").rows(_zip(**{"a.csv": b"name\n", "b.csv": b"name\n"}))


class _Wide(msgspec.Struct, frozen=True, gc=False):
    id: str | None
    column: str
    value: str


class _WideCsv(LongFile[_Wide], FoundCsv[_Wide]):
    _open = True
    _nulls = ("[NULL]",)
    _as_written = ("id",)
    _keys = re.compile(r"(?s).+")
    _empty_rows = False

    @typing.override
    def _heading(self, cell: str) -> str:
        return cell.lower()

    @typing.override
    def _cells(self, body: bytes) -> pl.DataFrame:
        wide = super()._cells(body)
        return wide.select(pl.col("ID").replace("", None).alias("id"), pl.all())


class TestAFoundTableReadLong:
    def test_every_header_cell_names_a_column_and_each_filled_cell_is_a_row(self) -> None:
        body = b"Title\nID, A ,,B\n7,1,skip,[NULL]\n,  ,,2\n"
        assert _WideCsv(_Wide, key="id").rows(body) == [
            _Wide("7", "ID", "7"),
            _Wide("7", "A", "1"),
            _Wide(None, "B", "2"),
        ]


class TestAnOpenFoundTable:
    def test_a_header_cell_twice_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"names one field twice"):
            _WideCsv(_Wide, key="id").rows(b"ID,A,A\n1,2,3\n")


class _Names(msgspec.Struct, frozen=True, gc=False):
    name: str | None


class _Text(ReportFile[_Names]):
    _rows_required = False
    _line = re.compile(r"(?P<name>[a-z]+)")
    _header = (re.compile(r"NAME"), re.compile(r"-+"))
    _skip_blank = False
    _text_lines = True
    _preamble = True


class _Plain(_Text):
    _skip_blank = True
    _text_lines = False
    _preamble = False


class TestAReportFileReadAsText:
    def _rows(self, body: bytes) -> list[str | None]:
        return [row.name for row in _Text(_Names).rows(body)]

    def test_lines_above_the_header_are_a_preamble_whatever_they_hold(self) -> None:
        assert self._rows(b"title\nx\nNAME\n----\na\nb\n") == ["a", "b"]
        with pytest.raises(ValueError, match=r"holds no run of the header's lines"):
            self._rows(b"NAME\na\n")

    def test_a_blank_line_is_a_line_like_any_other_unless_the_format_skips_them(self) -> None:
        with pytest.raises(ValueError, match=r"line 4 '' is neither a row"):
            self._rows(b"NAME\n----\na\n\nb\n")
        assert [row.name for row in _Plain(_Names).rows(b"NAME\n----\na\n\nb\n")] == ["a", "b"]

    def test_lines_end_as_str_splitlines_ends_them_and_latin_1_is_read_where_utf_8_fails(self) -> None:
        assert self._rows(b"NAME\n----\na\x0cb\n") == ["a", "b"]
        assert self._rows(b"caf\xe9\nNAME\n----\nx\n") == ["x"]
        with pytest.raises(ValueError, match=r"line 3 'a\\x0cb' is neither a row"):
            _Plain(_Names).rows(b"NAME\n----\na\x0cb\n")


class TestAPeriodsEnd:
    @pytest.mark.parametrize(
        ("written", "cell", "day"),
        [
            ("%Y:Q%q", "2026:Q2", dt.date(2026, 6, 30)),
            ("%Y-%m", "2024-02", dt.date(2024, 2, 29)),
            ("%b %Y", "Jan 2012", dt.date(2012, 1, 31)),
            ("%Y", "1999", dt.date(1999, 12, 31)),
            ("%b-%y", "Dec-28", dt.date(2028, 12, 31)),
        ],
    )
    def test_an_ending_period_reads_as_its_last_day(self, written: str, cell: str, day: dt.date) -> None:
        class Ended(msgspec.Struct, frozen=True, gc=False):
            day: Annotated[dt.date, Label("D"), Format(written, end=True)]

        assert CsvFile(Ended).rows(f"D\n{cell}\n".encode()) == [Ended(day)]

    def test_a_two_digit_year_is_strptime_s(self) -> None:
        class Month(msgspec.Struct, frozen=True, gc=False):
            month: Annotated[dt.date, Label("M"), Format("%b-%y")]

        assert CsvFile(Month).rows(b"M\nJan-68\nJan-69\n") == [Month(dt.date(2068, 1, 1)), Month(dt.date(1969, 1, 1))]

    def test_a_day_has_no_end(self) -> None:
        class Day(msgspec.Struct, frozen=True, gc=False):
            day: Annotated[dt.date, Label("D"), Format("%Y-%m-%d", end=True)]

        with pytest.raises(ValueError, match=r"is no period, so its Format '%Y-%m-%d' has no end"):
            CsvFile(Day)


class _Coded(msgspec.Struct, frozen=True, gc=False):
    table: str
    name: Annotated[str, Label("NAME")]
    value: Annotated[float, Label("VALUE")]


class _CodedCsv(CsvFile[_Coded]):
    def __init__(self, table: str) -> None:
        self._given = {"table": table}
        super().__init__(_Coded)


class TestAGivenField:
    def test_every_row_holds_it_and_it_takes_no_column(self) -> None:
        assert _CodedCsv("F1").rows(b"NAME,VALUE\na,1\nb,2\n") == [_Coded("F1", "a", 1.0), _Coded("F1", "b", 2.0)]
        assert _CodedCsv("F1").frame(b"NAME,VALUE\na,1\n").columns == ["table", "name", "value"]

    def test_it_is_typed_as_its_field_none_too(self) -> None:
        class Given(msgspec.Struct, frozen=True, gc=False):
            group: str | None
            count: int
            name: Annotated[str, Label("NAME")]

        class GivenCsv(CsvFile[Given]):
            def __init__(self) -> None:
                self._given = {"group": None, "count": 3}
                super().__init__(Given)

        frame = GivenCsv().frame(b"NAME\na\n")
        assert frame.schema == {"group": pl.String, "count": pl.Int64, "name": pl.String}
        assert frame.rows() == [(None, 3, "a")]

    def test_a_given_field_the_struct_lacks_raises(self) -> None:
        class Stray(CsvFile[_Row]):
            _given = types.MappingProxyType({"table": "F1"})

        with pytest.raises(ValueError, match=r"gives \['table'\], which are no fields of _Row"):
            Stray(_Row)

    def test_a_long_table_takes_one_too(self) -> None:
        class CodedLong(msgspec.Struct, frozen=True, gc=False):
            table: str
            name: Annotated[str, Label("NAME")]
            series: str
            value: float

        class Long(LongFile[CodedLong], CsvFile[CodedLong]):
            _keys = re.compile(r"S[0-9]")

            def __init__(self) -> None:
                self._given = {"table": "F1"}
                super().__init__(CodedLong)

        assert Long().rows(b"NAME,S1,S2\na,1,2\n") == [CodedLong("F1", "a", "S1", 1.0), CodedLong("F1", "a", "S2", 2.0)]


class TestAPreambleCsv:
    def test_the_header_is_the_first_line_opening_with_the_first_label(self) -> None:
        body = b'"A title, quoted",,\n\nNotes:\n"NAME","VALUE","NOTE"\na,1,x\n'
        assert PreambleCsv(_Row).rows(body) == [_Row("a", 1.0, "x")]
        assert PreambleCsv(_Row).rows(b"NAME,VALUE,NOTE\na,1,x\n") == [_Row("a", 1.0, "x")]

    def test_a_body_with_no_header_raises(self) -> None:
        with pytest.raises(ValueError, match=r"^no line opens with the header's first label 'NAME'$"):
            PreambleCsv(_Row).rows(b"title\nVALUE,NAME\n")


class TestALongFileByKey:
    def test_rows_come_key_by_key(self) -> None:
        class ByKey(LongFile[_Long], CsvFile[_Long]):
            _keys = re.compile(r"H(?P<key>\d+)")
            _by_key = True

        assert [(row.name, row.horizon) for row in ByKey(_Long).rows(_WIDE)] == [("a", 2), ("b", 2), ("a", 5), ("b", 5)]


class _Position(msgspec.Struct, frozen=True, gc=False):
    country: Annotated[str, Label("country")]
    year: int
    measure: str
    value: int


class _Positions(LongFile[_Position], CsvFile[_Position]):
    _keys = re.compile(r"(?P<measure>[a-z]+)_(?P<year>[0-9]{4})")
    _key_count = 2


class TestALongFileOfSeveralKeys:
    def test_each_key_field_is_its_group(self) -> None:
        assert _Positions(_Position).rows(b"country,mv_2003,mvn_2003,mv_2004\nUS,1,2,3\n") == [
            _Position("US", 2003, "mv", 1),
            _Position("US", 2003, "mvn", 2),
            _Position("US", 2004, "mv", 3),
        ]

    def test_a_pair_named_twice_raises(self) -> None:
        class Padded(_Positions):
            _keys = re.compile(r"(?P<measure>[a-z]+)_(?P<year>[0-9]+)")

        with pytest.raises(ValueError, match=r"name keys \[.*\], not each one year, measure once$"):
            Padded(_Position).rows(b"country,mv_2003,mv_02003\nUS,1,2\n")

    def test_a_key_not_of_its_field_raises(self) -> None:
        class Dated(msgspec.Struct, frozen=True, gc=False):
            country: Annotated[str, Label("country")]
            month: Annotated[dt.date, Format("%Y-%m")]
            measure: str
            value: int

        class DatedHoldings(LongFile[Dated], CsvFile[Dated]):
            _keys = re.compile(r"(?P<measure>[a-z]+)_(?P<month>[0-9]{4}-[0-9]{2})")
            _key_count = 2

        with pytest.raises(ValueError, match=r"name keys \['2026-13'\], not each a month: "):
            DatedHoldings(Dated).rows(b"country,mv_2026-13\nUS,1\n")


class TestAShape:
    class _Period(msgspec.Struct, frozen=True, gc=False):
        period: Annotated[str, Label("P"), Shape(r"[0-9]{4}(?::Q[1-4])?")]

    def test_a_cell_of_its_shape_reads_as_written(self) -> None:
        assert CsvFile(self._Period).rows(b"P\n2026:Q2\n1946\n") == [self._Period("2026:Q2"), self._Period("1946")]

    def test_a_cell_of_another_shape_raises_naming_its_row(self) -> None:
        with pytest.raises(ValueError, match=r"^the 'P' cell '2026-06' is not of its shape .*: row 2$"):
            CsvFile(self._Period).rows(b"P\n2026:Q2\n2026-06\n")

    def test_only_text_takes_one(self) -> None:
        class Number(msgspec.Struct, frozen=True, gc=False):
            value: Annotated[int, Label("V"), Shape("[0-9]+")]

        with pytest.raises(ValueError, match=r"is no text, so it takes no Shape"):
            CsvFile(Number)


class _Answer(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Label("")]
    question: str
    answer: str
    share: float


class _Answers(GroupedSheet[_Answer]):
    _groups_row = 1
    _empty_rows = False


class _Tagged(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Label("Day")]
    note: Annotated[str | None, Label("Note")]
    component: str
    value: float


class _TaggedSheet(LongFile[_Tagged], SheetFile[_Tagged]):
    _keys = re.compile(r"[a-z]")
    _beside = ("note",)


class TestAGroupedSheet:
    def test_each_filled_cell_is_a_row_under_its_heading_and_label(self) -> None:
        body = workbook(
            {"S": [["a link", "One", None, None, "Two"], [None, "a", "b", None, "a"], [_JAN, 1, None, None, 3]]}
        )
        assert _Answers(_Answer, sheet="S", header_row=2).rows(body) == [
            _Answer(_JAN, "One", "a", 1.0),
            _Answer(_JAN, "Two", "a", 3.0),
        ]

    def test_a_label_repeated_under_another_heading_is_its_own_key(self) -> None:
        body = workbook({"S": [[None, "One", "Two"], [None, "a", "a"], [_JAN, 1, 2]]})
        assert [row.question for row in _Answers(_Answer, sheet="S", header_row=2).rows(body)] == ["One", "Two"]

    def test_a_key_left_of_every_heading_raises(self) -> None:
        body = workbook({"S": [[None, None, "One"], [None, "a", "b"], [_JAN, 1, 2]]})
        with pytest.raises(ValueError, match=r"the header's columns \['a'\] are not keys"):
            _Answers(_Answer, sheet="S", header_row=2).rows(body)


class TestARecordColumnBesideTheKeys:
    def test_it_is_read_wherever_it_stands_among_the_keys(self) -> None:
        body = workbook({"S": [["Day", "a", "Note", "b"], [_JAN, 1, "first", 2]]})
        assert _TaggedSheet(_Tagged, sheet="S").rows(body) == [
            _Tagged(_JAN, "first", "a", 1.0),
            _Tagged(_JAN, "first", "b", 2.0),
        ]

    def test_a_header_without_it_reads_it_as_none(self) -> None:
        body = workbook({"S": [["Day", "a"], [_JAN, 1]]})
        assert _TaggedSheet(_Tagged, sheet="S").rows(body) == [_Tagged(_JAN, None, "a", 1.0)]


class _CellStated(msgspec.Struct, frozen=True, gc=False):
    as_of: Annotated[dt.date, fields.Cell("B1")]
    name: Annotated[str, Label("Name")]
    count: Annotated[int, Label("Count")]
    note: Annotated[str | None, fields.Cell("D9")]


class TestACellField:
    def test_one_cell_states_it_for_every_row_typed_as_its_field(self) -> None:
        body = workbook({"S": [["As of", _JAN], ["Name", "Count"], ["a", 1], ["b", 2]]})
        assert SheetFile(_CellStated, sheet="S", header_row=2).rows(body) == [
            _CellStated(_JAN, "a", 1, None),
            _CellStated(_JAN, "b", 2, None),
        ]

    def test_a_cell_that_is_not_its_type_raises_naming_the_cell(self) -> None:
        body = workbook({"S": [["As of", "soon"], ["Name", "Count"], ["a", 1]]})
        with pytest.raises(ValueError, match=r"cell 'as_of' is not its as_of: .*'soon' is not a date cell"):
            SheetFile(_CellStated, sheet="S", header_row=2).rows(body)

    def test_a_required_cell_that_is_empty_raises(self) -> None:
        body = workbook({"S": [["As of"], ["Name", "Count"], ["a", 1]]})
        with pytest.raises(ValueError, match=r"\['as_of'\] columns have an empty cell"):
            SheetFile(_CellStated, sheet="S", header_row=2).rows(body)

    def test_a_date_cell_past_the_sheets_columns_is_empty(self) -> None:
        class Far(msgspec.Struct, frozen=True, gc=False):
            as_of: Annotated[dt.date | None, fields.Cell("Z1")]
            name: Annotated[str, Label("Name")]

        assert SheetFile(Far, sheet="S", header_row=2).rows(workbook({"S": [["As of"], ["Name"], ["a"]]})) == [
            Far(None, "a")
        ]

    def test_a_csv_takes_no_cell(self) -> None:
        with pytest.raises(ValueError, match="is a Cell's, which only a sheet's reader reads"):
            CsvFile(_CellStated)

    def test_an_address_excel_does_not_write_is_refused(self) -> None:
        class Bad(msgspec.Struct):
            day: Annotated[dt.date, fields.Cell("1A")]

        with pytest.raises(ValueError, match="'1A' is no cell"):
            SheetFile(Bad, sheet="S")


class _Lined(msgspec.Struct, frozen=True, gc=False):
    row: int
    variable: str
    value: str


class _LinedCsv(LongFile[_Lined], CsvFile[_Lined]):
    _keys = re.compile(r".+")
    _empty_rows = False
    _numbered = "row"


def test_a_numbered_csv_row_holds_its_own_line() -> None:
    assert _LinedCsv(_Lined).rows(b"id,paid\r\n101,Yes\r\n102,\r\n") == [
        _Lined(2, "id", "101"),
        _Lined(2, "paid", "Yes"),
        _Lined(3, "id", "102"),
    ]


class _Listing(msgspec.Struct, frozen=True, gc=False):
    code: Annotated[str, Label("Code")]
    count: Annotated[int, Label("Count")]


class _ListedBeside(BesideSheet[_Listing], NotedSheet[_Listing]):
    _columns_at = (4, 5)


class TestABesideSheetOfLists:
    #: A table of names in columns A and B, and the one read in E and F beside it, which runs longer.
    BODY = _book(
        ["Name", "Rate", None, None, "Code", "Count"],
        ["x", 1.5, None, None, "AA", 1],
        ["y", 2.5, None, None, "BB", 2],
        [None, None, None, None, "CC", 3],
        [None, None, None, None, None, None],
        ["A note", None, None, None, None, None],
    )

    def test_a_table_that_starts_past_column_a_reads_with_its_columns_types(self) -> None:
        frame = _ListedBeside(_Listing, sheet="S").frame(self.BODY)
        assert frame.rows() == [("AA", 1), ("BB", 2), ("CC", 3)]
        assert frame.schema["count"] == pl.Int64

    def test_the_tables_beside_it_are_not_read(self) -> None:
        class Named(msgspec.Struct, frozen=True, gc=False):
            name: Annotated[str, Label("Name")]
            rate: Annotated[float | None, Label("Rate")]

        class Beside(BesideSheet[Named], NotedSheet[Named]):
            _columns_at = (0, 1)

        assert Beside(Named, sheet="S").rows(self.BODY) == [Named("x", 1.5), Named("y", 2.5)]

    def test_the_header_must_be_the_columns_labels(self) -> None:
        body = _book(["A", "B", None, None, "Code", "Total"], ["x", 1, None, None, "AA", 1])
        with pytest.raises(ValueError, match=r"the 'S' sheet's header \(row 1\) .* is not \['Code', 'Count'\]"):
            _ListedBeside(_Listing, sheet="S").rows(body)

    def test_a_sheet_without_the_columns_raises(self) -> None:
        with pytest.raises(ValueError, match=r"the 'S' sheet has no column \[4, 5\] of its table"):
            _ListedBeside(_Listing, sheet="S").rows(_book(["Code", "Count"], ["AA", 1]))

    def test_columns_that_are_not_one_per_field_raise(self) -> None:
        class Short(BesideSheet[_Listing]):
            _columns_at = (4,)

        with pytest.raises(ValueError, match=r"the 'S' sheet's header \(row 1\) \['Code'\] is not \['Code', 'Count'\]"):
            Short(_Listing, sheet="S").rows(self.BODY)

    def test_without_columns_it_reads_the_first_ones(self) -> None:
        class Plain(BesideSheet[_Listing]):
            pass

        assert Plain(_Listing, sheet="S").rows(_book(["Code", "Count"], ["AA", 1])) == [_Listing("AA", 1)]

    def test_the_columns_are_counted_from_column_a_of_the_sheet(self) -> None:
        shifted = _book([None, "Code", "Count"], [None, "AA", 1])

        class Plain(BesideSheet[_Listing]):
            _columns_at = (1, 2)

        assert Plain(_Listing, sheet="S").rows(shifted) == [_Listing("AA", 1)]
