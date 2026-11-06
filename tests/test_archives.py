"""Tests for ``flatread.archives``: a ZIP archive's files, whole or streamed.

The streams are held to :mod:`zipfile` on archives it writes (stored, deflated, with data descriptors, Zip64), cut at
every byte and with every byte inverted.
"""

import asyncio
import io
import re
import zipfile
from typing import TYPE_CHECKING, override

import pytest
from flatread.archives import stream_unzip, stream_zip, unzip, unzip_member, zip_members
from flatread.errors import CorruptArchiveError, EmptyBodyError, ReadError, UnknownLayoutError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Buffer, Callable


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


# --- The streams ---------------------------------------------------------------------------------------------------


async def _chunks(body: bytes, size: int) -> AsyncIterator[bytes]:
    for at in range(0, len(body), size):
        await asyncio.sleep(0)  # each chunk arrives on a turn of the loop of its own, as off a socket
        yield body[at : at + size]


async def _drain[T](items: AsyncIterator[T], into: list[T]) -> None:
    """Keep each item as it comes, so the ones before an error are kept: a comprehension would drop them."""
    async for item in items:
        into.append(item)


async def _pieces(body: bytes, size: int = 7) -> list[tuple[str, list[bytes]]]:
    """Every file the stream inflates: its name and its pieces."""
    return [(name, [piece async for piece in data]) async for name, data in stream_zip(_chunks(body, size))]


async def _files(body: bytes, size: int = 7) -> dict[str, bytes]:
    return {name: b"".join(pieces) for name, pieces in await _pieces(body, size)}


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
    files = {name: b"".join([piece async for piece in data]) async for name, data in stream_zip(with_empties(body))}
    assert files == await _files(body)


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
    with pytest.raises(CorruptArchiveError, match="cannot be read"):
        await _pieces(marked)


async def test_a_large_file_is_yielded_in_pieces_of_at_most_a_mebibyte() -> None:
    data = bytes(range(256)) * (3 << 12)  # 3 MiB that deflate well
    ((_, pieces),) = await _pieces(_archive({"big.bin": data}), 1 << 16)
    assert max(len(piece) for piece in pieces) <= 1 << 20
    assert b"".join(pieces) == data


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
    with pytest.raises(CorruptArchiveError, match="CRC32IntegrityError"):
        await _pieces(bytes(body))


async def test_bytes_after_the_end_record_are_corrupt() -> None:
    with pytest.raises(CorruptArchiveError, match="does not close with its end record"):
        await _pieces(_archive({"one.txt": b"hello"}) + b"x")


def _patched(body: bytes, at: int, value: int) -> bytes:
    """Rewrite the two-byte field at *at*: the first local header's flags (6) or method (8), or the end record's."""
    return body[:at] + value.to_bytes(2, "little") + body[at + 2 :]


async def test_an_encrypted_file_is_refused_before_it_is_read() -> None:
    with pytest.raises(CorruptArchiveError, match="cannot be read"):
        await _pieces(_patched(_archive({"one.txt": b"hello"}), 6, 0x1))


async def test_a_file_compressed_otherwise_than_it_claims_is_corrupt() -> None:
    with pytest.raises(CorruptArchiveError, match="cannot be read"):
        await _pieces(_patched(_archive({"one.txt": b"hello"}), 8, zipfile.ZIP_BZIP2))


async def test_a_bzip2_file_streams() -> None:
    assert await _files(_archive({"one.csv": _TEXT}, method=zipfile.ZIP_BZIP2)) == {"one.csv": _TEXT}


async def test_a_stored_file_whose_size_follows_it_cannot_be_streamed() -> None:
    body = _archive({"one.txt": b"hello"}, method=zipfile.ZIP_STORED, descriptor=True)
    with pytest.raises(CorruptArchiveError, match="cannot be read"):
        await _pieces(body)


async def test_a_directory_that_counts_other_files_is_corrupt() -> None:
    body = _archive({"one.txt": b"hello"})
    end = body.rindex(b"PK" + bytes([5, 6]))
    with pytest.raises(CorruptArchiveError, match="counts 2 files, but it holds 1"):
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
