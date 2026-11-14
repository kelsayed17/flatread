"""The refusals and paths the hardening left only a branch away from a test: each read or refused once here."""

import asyncio
import io
import re
import struct
import typing
import zipfile
from typing import Annotated

import msgspec
import polars as pl  # noqa: TC002 (the hooks' annotations, read at runtime)
import pytest

from flatread.archives import StreamedZip, Transcoded, ZippedFile, _zip64, zip_names, zipped_member
from flatread.core import Carried, Reshaped
from flatread.delimited import CsvFile, CsvText, HeadersCsv, NamedCsv, SectionedCsv
from flatread.errors import LayoutError, RaggedRowError, ReadError
from flatread.fields import Label, Span
from flatread.fixed import FixedWidthFile


class _Row(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("Name")]
    count: Annotated[int, Label("Count")]


class _Plain(msgspec.Struct, frozen=True, gc=False):
    name: str
    count: int


@pytest.mark.parametrize("reader", [HeadersCsv, NamedCsv])
def test_a_reader_by_header_with_no_header_line_is_a_layout_error(reader: type[CsvFile[_Row]]) -> None:
    class Headless(reader):  # ty: ignore[unsupported-base]
        _header = False

    with pytest.raises(LayoutError, match=r"reads its file by its header, and _header is false$"):
        Headless(_Row)


@pytest.mark.parametrize("section", [re.compile(r"(?i)name,count"), re.compile(r"NAME,COUNT", re.IGNORECASE)])
def test_a_sections_own_flags_hold_where_a_line_begins(section: re.Pattern[str]) -> None:
    class Sections(SectionedCsv[_Row]):
        _section = section

    body = b"Name,Count\na,1\nName,Count\nb,2\n"
    assert Sections(_Row, section=1, sections=2).rows(body) == [_Row("b", 2)]


def test_a_piece_listed_after_a_fixed_width_reader_is_a_layout_error() -> None:
    class Fixed(msgspec.Struct):
        name: Annotated[str, Label("N"), Span(0, 4)]

    class Wrong(FixedWidthFile[Fixed], Carried[Fixed]):
        _carried = ("name",)

    with pytest.raises(LayoutError, match=r"after its fixed-width reader, whose read never reaches them$"):
        Wrong(Fixed)


def test_a_carried_field_the_reader_reads_no_column_of_is_a_layout_error() -> None:
    class Dropping(Carried[_Plain], Reshaped[_Plain]):
        _carried = ("name",)

        @typing.override
        def _shape(self, frame: pl.DataFrame) -> pl.DataFrame:
            return frame.drop("name")

    assert Reshaped(_Plain, CsvFile(_Row)).rows(b"Name,Count\na,1\n") == [_Plain("a", 1)]
    with pytest.raises(LayoutError, match=r"^the format carries \['name'\] down, which its reader reads no column of$"):
        Dropping(_Plain, CsvFile(_Row)).rows(b"Name,Count\na,1\n")


def _streamed(body: bytes) -> bytes:
    async def chunks():  # noqa: ANN202 (an async generator of bytes)
        yield body

    async def read() -> bytes:
        return b"".join([text async for text in StreamedZip(re.compile(r"x\.csv")).inflated(chunks())])

    return asyncio.run(read())


def test_a_zip_cut_inside_a_local_header_raises() -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("x.csv", b"a\n1\n")
    with pytest.raises(ReadError, match=r"^the zip was cut: it ends inside a member$"):
        _streamed(out.getvalue()[:20])


def test_a_zip64_record_is_found_among_a_headers_extra_records() -> None:
    timestamp = struct.pack("<HH", 0x5455, 5) + b"\x01" + bytes(4)
    assert _zip64(timestamp + struct.pack("<HH", 1, 0))
    assert not _zip64(timestamp)
    assert not _zip64(b"")


def test_a_header_that_is_not_the_labels_is_named() -> None:
    with pytest.raises(ReadError, match=r"^the header \['NAME', 'COUNT'\] is not the labels \['Name', 'Count'\]$"):
        CsvFile(_Row).rows(b"NAME,COUNT\na,1\n")
    with pytest.raises(ReadError, match=r"^the header \['Name'\] is not the labels \['Name', 'Count'\]$"):
        CsvFile(_Row).rows(b"Name\na\n")


def test_a_zipped_or_transcoded_format_without_its_switch_is_a_layout_error() -> None:
    class Zipped(ZippedFile[_Row], CsvFile[_Row]):
        pass

    class Unencoded(Transcoded[_Row], CsvFile[_Row]):
        pass

    class Misencoded(Transcoded[_Row], CsvFile[_Row]):
        _encodings = ("no-such-codec",)

    with pytest.raises(LayoutError, match=r"^the Zipped format names no _member, the regex its member's whole name"):
        Zipped(_Row)
    with pytest.raises(LayoutError, match=r"^the Unencoded format names no _encodings, the codecs its body may be"):
        Unencoded(_Row)
    with pytest.raises(
        LayoutError, match=r"^the Misencoded format's encoding 'no-such-codec' is no codec Python reads$"
    ):
        Misencoded(_Row)


def test_a_zips_names_are_listed_and_a_member_read_by_its_exact_name() -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("t.xlsx", b"x")
        archive.writestr("t.csv", b"Name,Count\na,1\n")
    assert zip_names(out.getvalue()) == ["t.xlsx", "t.csv"]
    assert zipped_member(out.getvalue(), re.compile(re.escape("t.csv"))) == ("t.csv", b"Name,Count\na,1\n")
    with pytest.raises(ReadError, match=r"^the body is not a zip"):
        zip_names(b"not a zip")


class _Full(CsvText[_Row]):
    _full_rows = True


def test_a_csv_text_of_full_rows_refuses_a_narrower_one() -> None:
    assert _Full(_Row).rows(b"Name,Count\na,1\n\nb,2\n") == [_Row("a", 1), _Row("b", 2)]  # a blank line is no row
    assert CsvText(_Row).rows(b"Name,Count,\na,1,\nb,2\n") == [_Row("a", 1), _Row("b", 2)]
    with pytest.raises(RaggedRowError, match=r"^row 3 has 2 fields, not the widest row's 3$"):
        _Full(_Row).rows(b"Name,Count,\na,1,\nb,2\n")
