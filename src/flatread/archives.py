"""A ZIP archive's files: read from a body held whole by :mod:`zipfile`, or inflated as its chunks arrive.

:func:`unzip` returns an archive's one file, :func:`unzip_member` one named file, and :func:`zip_members` the names;
only stored and deflated files are read. :func:`stream_zip` and :func:`stream_unzip` read an archive too large to hold
as it arrives, by ``stream_unzip``'s :func:`~stream_unzip.async_stream_unzip`. An archive that is damaged, cut off or
encrypted raises :class:`~flatread.errors.CorruptArchiveError`.
"""

import io
import struct
import zipfile
import zlib
from typing import TYPE_CHECKING, Final

from stream_unzip import NO_ENCRYPTION, UnzipError, async_stream_unzip

from flatread.errors import CorruptArchiveError, EmptyBodyError, ReadError, UnknownLayoutError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

__all__ = ["stream_unzip", "stream_zip", "unzip", "unzip_member", "zip_members"]

# A ZIP opens with a local file header or, when it holds no file, with its end record.
_ZIP_SIGNATURES: Final = (b"PK" + bytes([3, 4]), b"PK" + bytes([5, 6]))

# The compression methods an archive's file may use: stored, and deflated, which every archive measured uses. zipfile
# reads a file compressed with bzip2, LZMA or Zstandard through those libraries, whose errors for damaged data are
# their own (OSError, LZMAError, ZstdError), and one compressed any other way not at all.
_METHODS: Final = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})

# The general-purpose flag bit that marks a file encrypted. zipfile will not read one without a password.
_ENCRYPTED: Final = 0x1

# The end of central directory record (PKWARE's APPNOTE.TXT, 4.3.16), after its signature: disk numbers, the counts of
# files, the directory's size and offset, and the comment's length; a count of 0xFFFF defers to Zip64's record. The
# body's last bytes are kept for it, as many as a record with the longest comment takes.
_END: Final = _ZIP_SIGNATURES[1]
_END_RECORD: Final = struct.Struct("<HHHHIIH")
_UNKNOWN_COUNT: Final = 0xFFFF
_TAIL: Final = len(_END) + _END_RECORD.size + 0xFFFF

# What zipfile raises for a stored or deflated file it cannot read, as measured (2026-09): a structure cut
# off or damaged, or a checksum that does not match (BadZipFile); a version or flag it does not support
# (NotImplementedError); data that is not the deflate it claims to be (zlib.error); and a directory offset out of range
# (ValueError). A ReadError is a ValueError too, so each read lets its own through before it catches these. The
# measure: a deflated and a stored archive of one 6.5 KB file, cut at every byte, with each byte inverted and each bit
# flipped in turn, 86,710 bodies in all.
_UNREADABLE: Final = (zipfile.BadZipFile, NotImplementedError, zlib.error, ValueError)


def unzip(body: bytes) -> bytes:
    """Return the one file the archive holds.

    An empty body raises :class:`EmptyBodyError`. A body that is not a ZIP, told by its opening bytes, raises
    :class:`UnknownLayoutError`, and so does an archive holding other than one file. An archive that cannot be read
    raises :class:`CorruptArchiveError`: one that is damaged or was cut off, and one whose file is encrypted or
    compressed other than stored or deflated, which is refused before it is read.
    """
    _check_zip(body)
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            return _read_only_file(archive)
    except ReadError:  # the archive's own check, a ValueError too: never zipfile's
        raise
    except _UNREADABLE as exc:
        msg = f"the archive cannot be read: {exc}"
        raise CorruptArchiveError(msg) from exc


def unzip_member(body: bytes, name: str) -> bytes:
    """Return the file *name* from an archive that holds several, reading no other file in it.

    The errors are :func:`unzip`'s, with one difference: an archive that does not hold *name* raises
    :class:`UnknownLayoutError`, naming the files it does hold, and the other files are not checked, since none is
    read. *name* is matched exactly, directory and case included.
    """
    _check_zip(body)
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            return _read_member(archive, name)
    except ReadError:  # the archive's own check, a ValueError too: never zipfile's
        raise
    except _UNREADABLE as exc:
        msg = f"the archive cannot be read: {exc}"
        raise CorruptArchiveError(msg) from exc


def zip_members(body: bytes) -> list[str]:
    """Return the names of the files the archive holds, in its directory's order, reading none of them.

    :func:`unzip`'s errors for a body that is empty, is not a ZIP, or whose directory cannot be read.
    """
    _check_zip(body)
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            return archive.namelist()
    except ReadError:  # the archive's own check, a ValueError too: never zipfile's
        raise
    except _UNREADABLE as exc:
        msg = f"the archive cannot be read: {exc}"
        raise CorruptArchiveError(msg) from exc


def _read_only_file(archive: zipfile.ZipFile) -> bytes:
    """Read the open *archive*'s one file, after checking that it holds one and that the file can be read."""
    infos = archive.infolist()
    if len(infos) != 1:  # counted off the directory, before any file is inflated
        msg = f"the archive holds {len(infos)} files, not one"
        raise UnknownLayoutError(msg)
    _check_readable(infos[0])
    return archive.read(infos[0])


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    """Read the file *name* from the open *archive*, after checking that it holds it and that it can be read."""
    try:
        info = archive.getinfo(name)
    except KeyError:
        msg = f"the archive holds no file {name!r}; it holds {archive.namelist()}"
        raise UnknownLayoutError(msg) from None
    _check_readable(info)
    return archive.read(info)


def _check_zip(body: bytes) -> None:
    """Check that the body is not empty and opens as a ZIP, told by its opening bytes."""
    if not body:
        msg = "the body is empty"
        raise EmptyBodyError(msg)
    if not body.startswith(_ZIP_SIGNATURES):
        msg = f"the body is not a ZIP archive: it opens {body[:16]!r}"
        raise UnknownLayoutError(msg)


def _check_readable(info: zipfile.ZipInfo) -> None:
    """Check that the archive's file is neither encrypted nor compressed other than stored or deflated."""
    if info.flag_bits & _ENCRYPTED:
        msg = f"the archive cannot be read: its file {info.filename!r} is encrypted"
        raise CorruptArchiveError(msg)
    if info.compress_type not in _METHODS:
        msg = (
            f"the archive cannot be read: its file {info.filename!r} is compressed with method {info.compress_type}, "
            "not stored or deflated"
        )
        raise CorruptArchiveError(msg)


async def stream_zip(chunks: AsyncIterator[bytes]) -> AsyncIterator[tuple[str, AsyncIterator[bytes]]]:
    """Yield each of an archive's files as its chunks arrive: its name, and its bytes as they are inflated.

    A file's bytes are read, or drained, before the next file comes. An empty body raises :class:`EmptyBodyError` and
    one that does not open as a ZIP :class:`UnknownLayoutError`, as :func:`unzip` does; an archive that is damaged,
    cut off or encrypted raises :class:`CorruptArchiveError`, from the file it is found in. ``stream_unzip`` stops at
    the directory, so the end record is checked here: it must close the body and count the files read.
    """
    opening = b""
    async for chunk in chunks:
        if len(opening := opening + chunk) >= len(_END):
            break
    _check_zip(opening)
    tail = [b""]
    files = 0
    try:
        async for raw, _, data in async_stream_unzip(
            _kept(opening, chunks, tail), allowed_encryption_mechanisms=(NO_ENCRYPTION,)
        ):
            files += 1
            yield _name(raw), _inflated(data)
    except UnzipError as exc:
        raise _corrupt(exc) from exc
    _check_end(tail[0], files)


async def stream_unzip(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Inflate the one file the archive holds as its chunks arrive, yielding its bytes a piece at a time.

    :func:`stream_zip`'s errors; an archive holding no file raises :class:`UnknownLayoutError` once its end is read,
    and one holding a second raises it when the second begins, the first's bytes all yielded.
    """
    first: str | None = None
    async for name, data in stream_zip(chunks):
        if first is not None:
            msg = f"the archive holds more than one file: {first!r}, then {name!r}"
            raise UnknownLayoutError(msg)
        first = name
        async for piece in data:
            yield piece
    if first is None:
        msg = "the archive holds 0 files, not one"
        raise UnknownLayoutError(msg)


async def _kept(opening: bytes, chunks: AsyncIterator[bytes], tail: list[bytes]) -> AsyncIterator[bytes]:
    """Yield *opening* then *chunks*, keeping in *tail* the last bytes, where the end record and its comment are."""
    tail[0] = opening[-_TAIL:]
    yield opening
    async for chunk in chunks:
        tail[0] = (tail[0] + chunk)[-_TAIL:]
        yield chunk


def _check_end(tail: bytes, files: int) -> None:
    """Check that the body closes with an end record, its comment the last bytes, counting *files* files."""
    size = len(_END) + _END_RECORD.size
    ends = [at for at in range(len(tail) - size, -1, -1) if tail.startswith(_END, at)]  # a comment may hold one
    fits = [at for at in ends if at + size + _END_RECORD.unpack_from(tail, at + len(_END))[-1] == len(tail)]
    if not fits:
        msg = "the archive cannot be read: it does not close with its end record, so it was cut off or added to"
        raise CorruptArchiveError(msg)
    count = _END_RECORD.unpack_from(tail, fits[0] + len(_END))[3]
    if count not in {files, _UNKNOWN_COUNT}:
        msg = f"the archive cannot be read: its directory counts {count} files, but it holds {files}"
        raise CorruptArchiveError(msg)


async def _inflated(data: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    try:
        async for piece in data:
            yield piece
    except UnzipError as exc:
        raise _corrupt(exc) from exc


def _name(raw: bytes) -> str:
    """Return a file's name: UTF-8 when it is UTF-8, else CP437, as :mod:`zipfile` reads a name not flagged UTF-8."""
    try:
        return raw.decode()
    except UnicodeDecodeError:
        return raw.decode("cp437")


def _corrupt(exc: UnzipError) -> CorruptArchiveError:
    return CorruptArchiveError(f"the archive cannot be read: {type(exc).__name__} {exc}".rstrip())
