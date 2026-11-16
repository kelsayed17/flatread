"""A read raises a :class:`ReadError` for a bad body and a :class:`LayoutError` for a reader declared wrong.

The two are siblings, each a ``ValueError``, so a caller can catch bad data alone and still let a misdeclared reader
fail loudly. The suite's ``pytest.raises`` name the kind each error is; these pin the kinds a caller tells apart.
"""

import datetime as dt
import io
import re
import zipfile
from typing import Annotated

import msgspec
import pytest

from flatread.archives import ZippedFile
from flatread.delimited import CsvFile, TrailerCsv
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
from flatread.fields import Format, Label
from flatread.fixed import FixedWidthFile
from flatread.markup import XmlFile
from flatread.sheets import EverySheet, SheetFile
from tests.support.xlsx import workbook


class _Row(msgspec.Struct, frozen=True):
    name: Annotated[str, Label("Name")]
    value: Annotated[float, Label("Value")]


def test_both_kinds_are_value_errors_and_neither_is_the_other() -> None:
    assert issubclass(ReadError, ValueError)
    assert issubclass(LayoutError, ValueError)
    assert not issubclass(LayoutError, ReadError)
    assert not issubclass(ReadError, LayoutError)


@pytest.mark.parametrize("body", [b"", b"  \n", b"Name,Value\n"], ids=["no bytes", "whitespace", "a header alone"])
def test_a_body_with_nothing_to_read_is_empty(body: bytes) -> None:
    with pytest.raises(EmptyBodyError):
        CsvFile(_Row).rows(body)


def test_a_short_row_is_ragged() -> None:
    with pytest.raises(RaggedRowError, match="row 2 has 1 fields, not the header's 2"):
        CsvFile(_Row).rows(b"Name,Value\na,1\nb\n")


_KINDS = (TruncatedFileError, ControlTotalError, UnknownLayoutError, CorruptArchiveError, EncodingError)


@pytest.mark.parametrize("kind", _KINDS, ids=lambda kind: kind.__name__)
def test_each_kind_is_a_read_error_built_from_its_message_alone(kind: type[ReadError]) -> None:
    assert issubclass(kind, ReadError)
    assert not issubclass(kind, LayoutError)
    error = kind("the body is not its file")
    assert type(error)(str(error)).args == ("the body is not its file",)


def test_a_body_cut_short_is_truncated() -> None:
    with pytest.raises(TruncatedFileError, match="it was cut"):
        CsvFile(_Row, final_line_ending=True).rows(b"Name,Value\na,1")


class _Totaled(TrailerCsv[_Row]):
    _trailer = re.compile(r"(?P<count>\d+)")


def test_a_trailer_counting_other_rows_than_the_body_holds_is_a_control_total() -> None:
    assert _Totaled(_Row).rows(b"Name,Value\na,1\n1\n") == [_Row("a", 1.0)]
    with pytest.raises(ControlTotalError, match="the trailer counts 2 rows, and the body holds 1"):
        _Totaled(_Row).rows(b"Name,Value\na,1\n2\n")


def test_a_header_that_is_not_the_readers_is_an_unknown_layout() -> None:
    with pytest.raises(UnknownLayoutError, match="is not the labels"):
        CsvFile(_Row).rows(b"Other,Value\na,1\n")


class _Zipped(ZippedFile[_Row], CsvFile[_Row]):
    _member = re.compile(r"x\.csv")


def test_a_zip_member_whose_crc_fails_is_a_corrupt_archive() -> None:
    held = io.BytesIO()
    with zipfile.ZipFile(held, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("x.csv", "Name,Value\na,1\n")
    with pytest.raises(CorruptArchiveError, match="Bad CRC-32"):
        _Zipped(_Row).rows(held.getvalue().replace(b"a,1", b"b,1"))


@pytest.mark.parametrize("body", [b"Name,Value\n\xff,1\n", b"Na\xffme,Value\na,1\n"], ids=["a cell", "the header"])
def test_bytes_that_are_not_utf8_are_an_encoding_error(body: bytes) -> None:
    with pytest.raises(EncodingError, match="is not UTF-8"):
        CsvFile(_Row).rows(body)


def test_a_sheet_read_by_every_sheet_keeps_its_kind() -> None:
    class _Year(msgspec.Struct, frozen=True):
        year: int
        name: Annotated[str, Label("Name")]

    class _Inner(msgspec.Struct, frozen=True):
        name: Annotated[str, Label("Name")]

    reader = EverySheet(
        _Year, sheets=re.compile(r"Report (?P<year>\d{4})"), each=lambda sheet: SheetFile(_Inner, sheet=sheet)
    )
    with pytest.raises(UnknownLayoutError, match="the 'Report 2026' sheet: "):
        reader.rows(workbook({"Report 2025": [["Name"], ["a"]], "Report 2026": [["Other"], ["b"]]}))


def test_a_cell_that_is_not_its_type_is_a_read_error() -> None:
    with pytest.raises(ReadError, match="'x'"):
        CsvFile(_Row).rows(b"Name,Value\na,x\n")


def test_every_readers_empty_body_is_empty() -> None:
    class _Fixed(msgspec.Struct):
        name: Annotated[str, Label("N")]

    with pytest.raises(EmptyBodyError):
        XmlFile(_Row, root="rows", record="row").rows(b"")
    with pytest.raises(LayoutError, match="needs a Span"):
        FixedWidthFile(_Fixed)


def test_a_reader_declared_wrong_is_a_layout_error() -> None:
    class Undated(msgspec.Struct):
        day: Annotated[dt.date, Label("Day")]

    class Zoned(msgspec.Struct):
        day: Annotated[dt.date, Label("Day"), Format("%Y", time_zone="UTC")]

    for row in (Undated, Zoned):
        with pytest.raises(LayoutError):
            CsvFile(row)


class _Flagged(msgspec.Struct, frozen=True):
    name: Annotated[str, Label("Name")]
    held: Annotated[bool, Label("Held")]


@pytest.mark.parametrize("word", ["True", "TRUE", "1", "yes"])
def test_a_csv_boolean_is_true_or_false_exactly(word: str) -> None:
    assert CsvFile(_Flagged).rows(b"Name,Held\na,true\nb,false\n") == [
        _Flagged("a", held=True),
        _Flagged("b", held=False),
    ]
    with pytest.raises(ReadError, match=f"^the 'Held' cell '{word}' is not 'true' or 'false': row 2$"):
        CsvFile(_Flagged).rows(f"Name,Held\na,true\nb,{word}\n".encode())


class _Counted(msgspec.Struct, frozen=True):
    count: Annotated[int, Label("Count")]
    value: Annotated[float, Label("Value")]


def test_a_mistyped_csv_cell_is_named_by_its_row() -> None:
    with pytest.raises(ReadError, match=r"^the 'Value' cell '1\.5\.' is not a number: row 3$"):
        CsvFile(_Row).rows(b'Name,Value\na,1\n"b\nc",2\nd,1.5.\n')
    with pytest.raises(ReadError, match=r"^the 'Count' cell '1\.5' is not a whole number: row 2$"):
        CsvFile(_Counted).rows(b"Count,Value\n1,1\n1.5,2\n")
