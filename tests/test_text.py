"""Tests for ``flatread.errors`` and ``frames`` — the frame schema of a row struct, a file's lines read both
ways, and the streams: an archive inflated and a file's lines read as their chunks arrive.

The archive and the errors are exercised through a real wire in ``tests/sec/test_decoders.py``; what is pinned here is
what no one wire shows: every field type :func:`frame_schema` maps, and that :func:`lines` and :func:`frame_lines` frame
a file alike, on the line rules a wire rarely hits. The streams are held to the whole-body readers: :func:`stream_zip`
to :mod:`zipfile` on archives :mod:`zipfile` writes (stored, deflated, with data descriptors, Zip64), cut at every byte
and with every byte inverted, and :func:`stream_lines` to :func:`lines` with its checks, however the chunks fall.
"""

import asyncio
import codecs
import datetime as dt
import io
import re
import zipfile
from dataclasses import dataclass
from itertools import starmap
from typing import TYPE_CHECKING, NewType, override

import msgspec
import polars as pl
import pytest
from flatread.archives import ZipChunk, stream_unzip, stream_zip, unzip, unzip_member, zip_members
from flatread.cells import UtcDatetime, is_utc_datetime
from flatread.errors import (
    CorruptArchiveError,
    EmptyBodyError,
    EncodingError,
    NotPublishedError,
    ReadError,
    TruncatedFileError,
    UnknownLayoutError,
    or_not_published,
)
from flatread.frames import frame_lines, frame_schema
from flatread.text import Line, batched, check_line_ending, decode_text, decode_utf8, lines, stream_lines

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Buffer, Callable

_Code = NewType("_Code", str)
_Wrapped = NewType("_Wrapped", _Code)


class _Row(msgspec.Struct, frozen=True, gc=False):
    code: _Wrapped
    count: int
    price: float | None
    flag: bool
    day: dt.date | None


class _Unmapped(msgspec.Struct, frozen=True, gc=False):
    tags: list[str]


class _Either(msgspec.Struct, frozen=True, gc=False):
    key: int | str


class _Moments(msgspec.Struct, frozen=True, gc=False):
    stated: UtcDatetime | None
    always: UtcDatetime
    unstated: dt.datetime | None


def test_a_utc_datetime_is_a_utc_column_and_a_plain_one_naive() -> None:
    assert frame_schema(_Moments) == pl.Schema(
        {"stated": pl.Datetime("us", "UTC"), "always": pl.Datetime("us", "UTC"), "unstated": pl.Datetime("us")}
    )


def test_a_utc_datetime_refuses_a_naive_moment() -> None:
    """Msgspec holds the annotation's ``tz=True``: a moment the file states in UTC cannot arrive without its zone."""
    moment = "2026-01-02T03:04:05"
    with pytest.raises(msgspec.ValidationError, match="timezone"):
        msgspec.convert({"stated": moment, "always": moment, "unstated": moment}, _Moments)


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [(UtcDatetime, True), (UtcDatetime | None, True), (dt.datetime, False), (dt.datetime | None, False)],
)
def test_is_utc_datetime_names_the_alias_alone_or_optional(annotation: object, *, expected: bool) -> None:
    assert is_utc_datetime(annotation) is expected


def test_each_field_is_its_types_column() -> None:
    """A NewType is its supertype's column, however deep, and ``X | None`` is ``X``'s."""
    assert frame_schema(_Row) == pl.Schema(
        {"code": pl.String, "count": pl.Int64, "price": pl.Float64, "flag": pl.Boolean, "day": pl.Date}
    )


def test_a_rows_values_fill_its_schemas_columns() -> None:
    row = _Row(code=_Wrapped(_Code("A")), count=2, price=None, flag=True, day=dt.date(2026, 1, 2))
    values = msgspec.structs.astuple(row)
    assert pl.DataFrame([values], schema=frame_schema(_Row), orient="row").row(0) == values


def test_a_field_with_no_column_type_raises() -> None:
    with pytest.raises(TypeError, match="no frame column type for a field annotated list"):
        frame_schema(_Unmapped)


def test_a_field_of_two_types_has_no_column_type() -> None:
    """Only ``X | None`` is ``X``'s column: a union of two scalars is neither's."""
    with pytest.raises(TypeError, match=r"no frame column type for a field annotated int \| str"):
        frame_schema(_Either)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a\rb\r\nc\r", [Line(1, "a\rb"), Line(2, "c")]),
        ("\n\nx\n\ny", [Line(3, "x"), Line(5, "y")]),
        ("x", [Line(1, "x")]),
    ],
    ids=["only a CR before an LF or ending the file ends a line", "blank lines count", "no final LF"],
)
def test_both_readers_frame_a_file_alike(text: str, expected: list[Line]) -> None:
    assert lines(text) == expected
    assert list(starmap(Line, frame_lines(text).select("number", "line").rows())) == expected


@pytest.mark.parametrize("read", [lines, frame_lines], ids=["lines", "frame_lines"])
def test_a_file_of_nothing_but_a_byte_order_mark_is_empty(read: Callable[[str], object]) -> None:
    with pytest.raises(EmptyBodyError, match="the file is empty"):
        read(decode_text(b"\xef\xbb\xbf"))


@pytest.mark.parametrize("read", [lines, frame_lines], ids=["lines", "frame_lines"])
def test_a_file_of_blank_lines_is_empty(read: Callable[[str], object]) -> None:
    with pytest.raises(EmptyBodyError, match="only blank lines"):
        read("\n\r\n\n")


def test_polars_cannot_frame_a_file_holding_every_control_character() -> None:
    every = "".join(chr(code) for code in range(32) if chr(code) not in "\n\r")

    with pytest.raises(UnknownLayoutError, match="every ASCII control character"):
        frame_lines(every)


def _two_files() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SUB.tsv", b"sub")
        archive.writestr("NUM.tsv", b"num")
    return buffer.getvalue()


def test_unzip_member_reads_the_named_file_of_several() -> None:
    assert unzip_member(_two_files(), "NUM.tsv") == b"num"


def test_unzip_member_names_the_files_an_archive_holds_when_it_lacks_one() -> None:
    with pytest.raises(UnknownLayoutError, match=r"holds no file 'TAG.tsv'; it holds \['SUB.tsv', 'NUM.tsv'\]"):
        unzip_member(_two_files(), "TAG.tsv")


def test_zip_members_lists_the_files_in_the_directorys_order() -> None:
    assert zip_members(_two_files()) == ["SUB.tsv", "NUM.tsv"]


#: A body that opens as a ZIP's local header and is nothing after it.
_NOT_AN_ARCHIVE = b"PK" + bytes([3, 4]) + b"not an archive"


@pytest.mark.parametrize(
    "read", [lambda body: unzip_member(body, "SUB.tsv"), zip_members], ids=["unzip_member", "zip_members"]
)
def test_a_body_that_opens_as_a_zip_but_is_none_is_corrupt(read: Callable[[bytes], object]) -> None:
    with pytest.raises(CorruptArchiveError, match="the archive cannot be read"):
        read(_NOT_AN_ARCHIVE)


#: Where a central header's flags sit after its signature: after the versions made by and needed.
_CENTRAL_FLAGS = 8


def test_unzip_member_refuses_an_encrypted_file_before_it_is_read() -> None:
    """A file's flags are read from its directory entry, so the encrypted bit is set there."""
    body = _two_files()
    at = body.index(b"PK" + bytes([1, 2]), body.index(b"PK" + bytes([1, 2])) + 1) + _CENTRAL_FLAGS
    encrypted = body[:at] + (body[at] | 0x1).to_bytes() + body[at + 1 :]
    with pytest.raises(CorruptArchiveError, match=r"its file 'NUM\.tsv' is encrypted"):
        unzip_member(encrypted, "NUM.tsv")


# --- The streams ------------------------------------------------------------------------------------------------------


async def _chunks(body: bytes, size: int) -> AsyncIterator[bytes]:
    for at in range(0, len(body), size):
        await asyncio.sleep(0)  # each chunk arrives on a turn of the loop of its own, as off a socket
        yield body[at : at + size]


async def _drain[T](items: AsyncIterator[T], into: list[T]) -> None:
    """Keep each item as it comes, so the ones before an error are kept: a comprehension would drop them."""
    async for item in items:
        into.append(item)


async def _pieces(body: bytes, size: int = 7) -> list[ZipChunk]:
    return [piece async for piece in stream_zip(_chunks(body, size))]


async def _files(body: bytes, size: int = 7) -> dict[str, bytes]:
    """Every file the stream inflates, joined, checking each file's pieces arrive together and end with a last one."""
    files: dict[str, bytes] = {}
    current: str | None = None
    for piece in await _pieces(body, size):
        assert current in {None, piece.name}, "a file's pieces arrive together"
        assert not (piece.last and piece.data), "the last piece only says the file ended"
        files[piece.name] = files.get(piece.name, b"") + piece.data
        current = None if piece.last else piece.name
    assert current is None, "every file ends with a last piece"
    return files


class _Unseekable(io.RawIOBase):
    """A sink zipfile cannot seek back into, so it writes each file's CRC-32 and sizes in a data descriptor after it."""

    def __init__(self) -> None:
        super().__init__()
        self.written = bytearray()

    @override
    def writable(self) -> bool:
        return True

    @override
    def write(self, b: Buffer, /) -> int:
        self.written += b
        return memoryview(b).nbytes


def _archive(
    files: dict[str, bytes], *, method: int = zipfile.ZIP_DEFLATED, descriptor: bool = False, zip64: bool = False
) -> bytes:
    """Write *files* with zipfile: deflated or stored, to a sink it can seek in or not, Zip64 or not."""
    sink = _Unseekable() if descriptor else io.BytesIO()
    with zipfile.ZipFile(sink, "w", method) as archive:
        for name, data in files.items():
            with archive.open(name, "w", force_zip64=zip64) as file:
                file.write(data)
    return bytes(sink.written) if isinstance(sink, _Unseekable) else sink.getvalue()


_TEXT = b"".join(b"%d,a line of text that deflates,%d\n" % (n, n * n) for n in range(400))
_FILES = {"one.csv": _TEXT, "dir/": b"", "two.txt": b"short", "empty": b""}


@pytest.mark.parametrize("method", [zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED], ids=["deflated", "stored"])
@pytest.mark.parametrize("size", [1, 7, 4096, 1 << 20], ids=["byte", "seven", "page", "whole"])
async def test_a_streamed_archive_inflates_to_the_files_zipfile_reads(method: int, size: int) -> None:
    body = _archive(_FILES, method=method)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        expected = {name: archive.read(name) for name in archive.namelist()}
    assert await _files(body, size) == expected == _FILES


@pytest.mark.parametrize("zip64", [False, True], ids=["classic", "zip64"])
async def test_a_file_whose_sizes_follow_it_in_a_descriptor_is_checked_against_it(*, zip64: bool) -> None:
    body = _archive({"one.csv": _TEXT, "two.txt": b"short"}, descriptor=True, zip64=zip64)
    assert body[6] & 0x8, "zipfile wrote a data descriptor"
    assert await _files(body) == {"one.csv": _TEXT, "two.txt": b"short"}


async def test_a_zip64_field_makes_the_descriptor_zip64_without_the_mark() -> None:
    """A writer deferring its sizes writes 0 in the local header, not 0xFFFFFFFF, by a Zip64 field (APPNOTE 4.3.9.1).

    zipfile writes the mark, so the header's sizes are zeroed here: the descriptor is still read as eight-byte sizes.
    """
    written = _archive({"one.csv": _TEXT}, descriptor=True, zip64=True)
    assert written[18:26] == b"\xff" * 8, "zipfile marked the local header's sizes as Zip64's"
    body = written[:18] + bytes(8) + written[26:]
    assert await _files(body) == {"one.csv": _TEXT}


async def test_a_zip64_file_reads_its_sizes_from_its_extra_field() -> None:
    body = _archive({"one.csv": _TEXT}, zip64=True)
    assert body[18:26] == b"\xff" * 8, "zipfile marked the local header's sizes as Zip64's"
    assert await _files(body) == {"one.csv": _TEXT}


async def test_empty_chunks_are_skipped() -> None:
    async def with_empties(body: bytes) -> AsyncIterator[bytes]:
        async for chunk in _chunks(body, 7):
            yield b""
            yield chunk

    body = _archive(_FILES)
    pieces = [piece async for piece in stream_zip(with_empties(body))]
    assert pieces == await _pieces(body)


async def test_a_directory_with_zip64_end_records_is_read_to_its_classic_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Zip64 end record and its locator are written past 65,535 files; zipfile's limit is lowered to write them."""
    monkeypatch.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 0)
    body = _archive({"one.csv": _TEXT, "two.txt": b"short"})
    assert b"PK" + bytes([6, 6]) in body, "zipfile wrote a Zip64 end record"
    assert b"PK" + bytes([6, 7]) in body, "zipfile wrote a Zip64 end locator"
    assert await _files(body) == {"one.csv": _TEXT, "two.txt": b"short"}


async def test_a_file_marking_its_sizes_zip64_without_a_zip64_field_is_corrupt() -> None:
    body = _archive({"one.txt": b"hello"})
    marked = body[:18] + b"\xff" * 8 + body[26:]
    with pytest.raises(CorruptArchiveError, match=r"'one\.txt' marks its sizes as Zip64's but has no Zip64 field"):
        await _pieces(marked)


async def test_a_large_file_is_yielded_in_pieces_of_at_most_a_mebibyte() -> None:
    data = bytes(range(256)) * (3 << 12)  # 3 MiB that deflate well
    pieces = await _pieces(_archive({"big.bin": data}), 1 << 16)
    assert max(len(piece.data) for piece in pieces) <= 1 << 20
    assert b"".join(piece.data for piece in pieces) == data


async def test_an_archive_with_no_file_streams_nothing() -> None:
    assert await _pieces(_archive({})) == []


async def test_an_empty_body_is_empty() -> None:
    with pytest.raises(EmptyBodyError, match="the body is empty"):
        await _pieces(b"")


async def test_a_body_that_is_not_a_zip_is_an_unknown_layout() -> None:
    with pytest.raises(UnknownLayoutError, match=r"not a ZIP archive: it opens b'<html>'"):
        await _pieces(b"<html>")


@pytest.mark.parametrize("method", [zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED], ids=["deflated", "stored"])
async def test_an_archive_cut_at_any_byte_is_corrupt(method: int) -> None:
    body = _archive({"one.csv": _TEXT[:300], "two.txt": b"short"}, method=method)
    for cut in range(1, len(body)):
        with pytest.raises(UnknownLayoutError if cut < 4 else CorruptArchiveError):
            await _pieces(body[:cut])


@pytest.mark.parametrize("descriptor", [False, True], ids=["sizes in the header", "sizes in a descriptor"])
async def test_an_archive_with_any_byte_inverted_reads_its_files_or_raises_a_decode_error(*, descriptor: bool) -> None:
    """A byte no check reads (a name, a time, the directory's copy of a size) may change; nothing else may pass."""
    body = _archive({"one.csv": _TEXT[:300], "two.txt": b"short"}, descriptor=descriptor)
    for at in range(len(body)):
        damaged = body[:at] + bytes([body[at] ^ 0xFF]) + body[at + 1 :]
        try:
            files = await _files(damaged)
        except ReadError:
            continue
        assert list(files.values()) == [_TEXT[:300], b"short"], at


async def test_a_damaged_deflate_stream_is_corrupt() -> None:
    body = bytearray(_archive({"one.csv": _TEXT}))
    body[40:60] = bytes(20)
    with pytest.raises(CorruptArchiveError, match="cannot be read"):
        await _pieces(bytes(body))


async def test_a_file_whose_bytes_do_not_match_its_crc_is_corrupt() -> None:
    body = bytearray(_archive({"one.txt": b"hello"}, method=zipfile.ZIP_STORED))
    body[30 + len("one.txt")] ^= 0x20  # the data's first byte: h becomes H
    with pytest.raises(CorruptArchiveError, match=r"'one.txt' reads as CRC-32 [0-9a-f]{8}, 5 bytes compressed"):
        await _pieces(bytes(body))


async def test_bytes_after_the_end_record_are_corrupt() -> None:
    with pytest.raises(CorruptArchiveError, match="bytes follow its end record"):
        await _pieces(_archive({"one.txt": b"hello"}) + b"x")


def _patched(body: bytes, at: int, value: int) -> bytes:
    """Rewrite the two-byte field at *at*: the first local header's flags (6) or method (8), or the end record's."""
    return body[:at] + value.to_bytes(2, "little") + body[at + 2 :]


async def test_an_encrypted_file_is_refused_before_it_is_read() -> None:
    with pytest.raises(CorruptArchiveError, match=r"'one\.txt' is encrypted"):
        await _pieces(_patched(_archive({"one.txt": b"hello"}), 6, 0x1))


async def test_a_file_compressed_another_way_is_refused_before_it_is_read() -> None:
    with pytest.raises(CorruptArchiveError, match="compressed with method 12, not stored or deflated"):
        await _pieces(_patched(_archive({"one.txt": b"hello"}), 8, zipfile.ZIP_BZIP2))


async def test_a_stored_file_whose_size_follows_it_cannot_be_streamed() -> None:
    body = _archive({"one.txt": b"hello"}, method=zipfile.ZIP_STORED, descriptor=True)
    with pytest.raises(CorruptArchiveError, match="gives its size only after its data"):
        await _pieces(body)


async def test_a_directory_that_counts_other_files_is_corrupt() -> None:
    body = _archive({"one.txt": b"hello"})
    end = body.rindex(b"PK" + bytes([5, 6]))
    with pytest.raises(CorruptArchiveError, match="lists 1 files and counts 2, but it holds 1"):
        await _pieces(_patched(body, end + 10, 2))


async def _unzipped(body: bytes) -> bytes:
    return b"".join([data async for data in stream_unzip(_chunks(body, 5))])


async def test_stream_unzip_yields_the_one_file() -> None:
    assert await _unzipped(_archive({"Z1.csv": _TEXT})) == _TEXT


async def test_stream_unzip_refuses_an_archive_of_no_file() -> None:
    with pytest.raises(UnknownLayoutError, match="the archive holds 0 files, not one"):
        await _unzipped(_archive({}))


def _same_name_twice() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("one.csv", b"first")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("one.csv", b"second")
    return buffer.getvalue()


@pytest.mark.parametrize("second", ["two.csv", "one.csv"], ids=["another name", "the same name"])
async def test_stream_unzip_refuses_a_second_file_when_it_begins(second: str) -> None:
    body = _same_name_twice() if second == "one.csv" else _archive({"one.csv": b"first", second: b"second"})
    seen: list[bytes] = []
    with pytest.raises(UnknownLayoutError, match=re.escape(f"holds more than one file: 'one.csv', then '{second}'")):
        await _drain(stream_unzip(_chunks(body, 5)), seen)
    assert b"".join(seen) == b"first"


async def _streamed_lines(body: bytes, size: int) -> list[Line]:
    return [line async for line in stream_lines(_chunks(body, size))]


def _whole_lines(body: bytes) -> list[Line]:
    """Return what the whole-body readers make of *body*: decode_utf8, lines, then check_line_ending."""
    text = decode_utf8(body)
    found = lines(text)
    check_line_ending(text, found[-1])
    return found


@pytest.mark.parametrize(
    "body",
    [
        b"a,b\r\n1,2\r\n",
        b"\xef\xbb\xbfa\n\n\nb\n",
        "café — \U0001f600\né\n".encode(),
        b"a\rb\r\nc\n",
        b"x\n\r\n\n",
    ],
    ids=[
        "CRLF",
        "a byte-order mark and blank lines",
        "multi-byte characters",
        "a CR inside a line",
        "blank lines last",
    ],
)
@pytest.mark.parametrize("size", [1, 2, 3, 64], ids=["byte", "two", "three", "whole"])
async def test_streamed_lines_are_the_whole_bodys_however_the_chunks_fall(body: bytes, size: int) -> None:
    assert await _streamed_lines(body, size) == _whole_lines(body)


@pytest.mark.parametrize(
    ("body", "error", "message"),
    [
        (b"", EmptyBodyError, "the file is empty"),
        (b"\xef\xbb\xbf", EmptyBodyError, "the file is empty"),
        (b"\n\r\n", EmptyBodyError, "only blank lines"),
        (b"a\nb", TruncatedFileError, "with 'b' on line 2"),
        (b"a\nb\r", TruncatedFileError, "with 'b' on line 2"),
        (b"a\n\r", TruncatedFileError, "with 'a' on line 1"),
        (b"a\n\xff\n", EncodingError, "not UTF-8"),
        (b"a\n\xc3", EncodingError, "not UTF-8"),
    ],
    ids=[
        "empty",
        "a byte-order mark alone",
        "blank",
        "cut",
        "cut after a CR",
        "a CR alone last",
        "bad byte",
        "cut mid",
    ],
)
@pytest.mark.parametrize("size", [1, 64], ids=["byte", "whole"])
async def test_a_stream_raises_what_the_whole_body_raises(
    body: bytes, error: type[ReadError], message: str, size: int
) -> None:
    with pytest.raises(error, match=message):
        _whole_lines(body)
    with pytest.raises(error, match=message):
        await _streamed_lines(body, size)


async def test_a_line_is_yielded_before_the_rest_of_the_stream_is_read() -> None:
    async def body() -> AsyncIterator[bytes]:
        await asyncio.sleep(0)
        yield b"first\nsec"
        raise AssertionError  # never reached: the first line comes out before the next chunk is asked for

    assert await anext(stream_lines(body())) == Line(1, "first")


async def _numbers(count: int, *, then: ReadError | None = None) -> AsyncIterator[int]:
    for number in range(count):
        await asyncio.sleep(0)
        yield number
    if then is not None:
        raise then


async def test_batches_are_the_size_asked_the_last_shorter() -> None:
    assert [batch async for batch in batched(_numbers(7), 3)] == [[0, 1, 2], [3, 4, 5], [6]]


async def test_a_batch_cut_by_an_error_comes_out_before_the_error() -> None:
    seen: list[list[int]] = []
    with pytest.raises(TruncatedFileError, match="cut"):
        await _drain(batched(_numbers(4, then=TruncatedFileError("cut")), 3), seen)
    assert seen == [[0, 1, 2], [3]]


async def test_a_batch_holds_at_least_one_item() -> None:
    with pytest.raises(ValueError, match="at least one item, not 0"):
        await anext(batched(_numbers(1), 0))


async def test_batches_that_fill_exactly_leave_no_short_one() -> None:
    assert [batch async for batch in batched(_numbers(6), 3)] == [[0, 1, 2], [3, 4, 5]]


async def test_an_error_before_any_item_yields_no_batch() -> None:
    seen: list[list[int]] = []
    with pytest.raises(TruncatedFileError, match="cut"):
        await _drain(batched(_numbers(0, then=TruncatedFileError("cut")), 3), seen)
    assert seen == []


# --- A whole archive's one file, and a file's text ------------------------------------------------------------------


def test_unzip_returns_the_one_file() -> None:
    assert unzip(_archive({"one.csv": _TEXT})) == _TEXT


def test_unzip_refuses_an_archive_of_two_files() -> None:
    with pytest.raises(UnknownLayoutError, match="holds 2 files, not one"):
        unzip(_archive({"one.csv": _TEXT, "two.txt": b"short"}))


@pytest.mark.parametrize(
    ("body", "error", "message"),
    [
        (b"", EmptyBodyError, "the body is empty"),
        (b"<html>not found</html>", UnknownLayoutError, "not a ZIP archive"),
        (b"PK" + bytes([3, 4]) + b"garbage", CorruptArchiveError, "cannot be read"),
    ],
    ids=["empty", "not a zip", "damaged"],
)
def test_unzip_refuses_what_is_not_a_readable_zip(body: bytes, error: type[Exception], message: str) -> None:
    with pytest.raises(error, match=message):
        unzip(body)


def test_unzip_refuses_a_file_compressed_another_way_before_reading_it() -> None:
    with pytest.raises(CorruptArchiveError, match="compressed with method 12, not stored or deflated"):
        unzip(_archive({"one.csv": _TEXT}, method=zipfile.ZIP_BZIP2))


@pytest.mark.parametrize(
    ("file", "text"),
    [
        ("café,1\n".encode(), "café,1\n"),
        (codecs.BOM_UTF8 + b"a,1\n", "a,1\n"),
        ("café,1\n".encode("latin-1"), "café,1\n"),
    ],
    ids=["utf-8", "a byte-order mark", "latin-1"],
)
def test_decode_text_reads_utf8_else_latin1(file: bytes, text: str) -> None:
    assert decode_text(file) == text


def test_a_file_holding_nul_is_framed_line_by_line_all_the_same() -> None:
    """NUL is the separator polars is given when a file lacks it; a file holding one gets another."""
    text = "a" + chr(0) + "b\nc\n"
    assert frame_lines(text).rows() == [(1, "a" + chr(0) + "b"), (2, "c")]


@dataclass(frozen=True)
class _Marker:
    """A caller's not-published marker, a struct of its own."""


def _refuses(body: bytes) -> str:
    if body == b"none":
        msg = "not published: no file"
        raise NotPublishedError(msg)
    if body == b"junk":
        msg = "not the file"
        raise UnknownLayoutError(msg)
    return body.decode()


class TestOrNotPublished:
    """``or_not_published`` turns only a publisher's "no file" answer into a value; every other error still raises."""

    def test_a_readable_body_decodes(self) -> None:
        assert or_not_published(_Marker, _refuses, b"rows") == "rows"

    def test_the_no_file_answer_is_the_marker(self) -> None:
        assert or_not_published(_Marker, _refuses, b"none") == _Marker()

    def test_any_other_error_still_raises(self) -> None:
        with pytest.raises(UnknownLayoutError, match=r"^not the file$"):
            or_not_published(_Marker, _refuses, b"junk")
