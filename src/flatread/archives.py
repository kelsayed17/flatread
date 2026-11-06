"""A ZIP archive's files: read from a body held whole, or inflated as its chunks arrive.

:func:`unzip` returns an archive's one file, :func:`unzip_member` one named file, and :func:`zip_members` the names.
:func:`stream_zip` and :func:`stream_unzip` read an archive too large to hold as it arrives, holding no more than a
chunk or a piece of a file at once. Only stored and deflated files are read, the two methods real archives use; an
archive that is damaged, cut off, encrypted or compressed another way raises
:class:`~flatread.errors.CorruptArchiveError`.
"""

import io
import struct
import zipfile
import zlib
from typing import TYPE_CHECKING, Final, NamedTuple

from flatread.errors import CorruptArchiveError, EmptyBodyError, ReadError, UnknownLayoutError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

__all__ = ["ZipChunk", "stream_unzip", "stream_zip", "unzip", "unzip_member", "zip_members"]

# A ZIP opens with a local file header or, when it holds no file, with its end record.
_ZIP_SIGNATURES: Final = (b"PK" + bytes([3, 4]), b"PK" + bytes([5, 6]))

# The compression methods an archive's file may use: stored, and deflated, which every archive measured uses. zipfile
# reads a file compressed with bzip2, LZMA or Zstandard through those libraries, whose errors for damaged data are
# their own (OSError, LZMAError, ZstdError), and one compressed any other way not at all.
_METHODS: Final = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})

# The general-purpose flag bit that marks a file encrypted. zipfile will not read one without a password.
_ENCRYPTED: Final = 0x1

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


# --- Streams ----------------------------------------------------------------------------------------------------------
#
# A ZIP's records, by signature (PKWARE's APPNOTE.TXT, sections 4.3.7 to 4.3.16): a file's local header, its data
# descriptor, the central directory's headers, and the end records, Zip64's and the classic one.
_LOCAL_HEADER: Final = b"PK" + bytes([3, 4])
_DATA_DESCRIPTOR: Final = b"PK" + bytes([7, 8])
_CENTRAL_HEADER: Final = b"PK" + bytes([1, 2])
_ZIP64_END: Final = b"PK" + bytes([6, 6])
_ZIP64_LOCATOR: Final = b"PK" + bytes([6, 7])
_END: Final = b"PK" + bytes([5, 6])

# A local header after its signature: version, flags, method, time, date, CRC-32, compressed and uncompressed sizes,
# name and extra-field lengths. A central header after its signature, up to its name, and the end record likewise.
_LOCAL: Final = struct.Struct("<HHHHHIIIHH")
_CENTRAL: Final = struct.Struct("<HHHHHHIIIHHHHHII")
_END_RECORD: Final = struct.Struct("<HHHHIIH")
_ZIP64_LOCATOR_SIZE: Final = 16
# Where a central header's name, extra-field and comment lengths sit among its fields.
_CENTRAL_LENGTHS: Final = slice(9, 12)

# The general-purpose flag bits: the CRC-32 and sizes follow the data in a descriptor (bit 3), and the name is UTF-8
# (bit 11; else it is CP437, as zipfile reads it).
_HAS_DESCRIPTOR: Final = 0x8
_UTF8_NAME: Final = 0x800
# A size of 0xFFFFFFFF in a local header says its Zip64 extra field (tag 1) holds both, as eight bytes each; that field
# also makes a data descriptor's sizes eight bytes.
_ZIP64_MARK: Final = 0xFFFFFFFF
_ZIP64_TAG: Final = 1
_ZIP64_SIZES: Final = struct.Struct("<QQ")
_UNKNOWN_COUNT: Final = 0xFFFF

# How much compressed data is read at a time, and how much a file is inflated into at most at once: what bounds a
# stream's memory, whatever a chunk's or a file's size.
_READ: Final = 1 << 16
_INFLATE: Final = 1 << 20
# The window bits of a raw deflate stream, as a ZIP stores one: no zlib header or trailer.
_RAW_DEFLATE: Final = -15


class ZipChunk(NamedTuple):
    """A piece of one file of a streamed archive: the file's ``name``, inflated ``data``, and whether it is ``last``.

    A file's pieces arrive together and in order, and its last one, whose data is empty, says it ended whole: its
    CRC-32 and sizes checked.
    """

    name: str
    data: bytes
    last: bool


class _Bytes:
    """The chunks of a body, read as exactly so many bytes, or as whatever has arrived.

    What has arrived and is not yet taken is one ``bytearray``, taken from the front: CPython drops a ``bytearray``'s
    front without copying the rest, so reading a large chunk in small pieces stays linear.
    """

    def __init__(self, chunks: AsyncIterator[bytes]) -> None:
        self._chunks = chunks
        self._held = bytearray()
        self._ended = False

    async def _more(self) -> bool:
        """Hold the next non-empty chunk; ``False`` once the body has ended."""
        while not self._ended:
            try:
                chunk = await anext(self._chunks)
            except StopAsyncIteration:
                self._ended = True
                return False
            if chunk:
                self._held += chunk
                return True
        return False

    async def exactly(self, size: int, what: str) -> bytes:
        """Return the next *size* bytes; a body that ends first was cut off inside *what*."""
        while len(self._held) < size:
            if not await self._more():
                msg = f"the archive cannot be read: it ends inside {what}, so it was cut off"
                raise CorruptArchiveError(msg)
        return self._take(size)

    async def some(self, most: int) -> bytes:
        """Return up to *most* bytes of what has arrived, waiting for one if none has; ``b""`` at the body's end."""
        if not self._held and not await self._more():
            return b""
        return self._take(most)

    async def peek(self, size: int) -> bytes:
        """Return up to *size* bytes without taking them: fewer only when the body ends first."""
        while len(self._held) < size and await self._more():
            pass
        return bytes(self._held[:size])

    def give_back(self, data: bytes) -> None:
        """Put *data*, read past what was wanted, back in front of what is held."""
        self._held[:0] = data

    def _take(self, size: int) -> bytes:
        """Take the first *size* bytes held, or all of them when fewer are."""
        taken = bytes(self._held[:size])
        del self._held[:size]
        return taken

    async def ended(self) -> bool:
        """Whether every byte of the body has been taken."""
        return not self._held and not await self._more()


class _Header(NamedTuple):
    """What a file's local header says: its name, flags, method, CRC-32 and sizes, and whether they are Zip64's."""

    name: str
    flags: int
    method: int
    crc: int
    compressed: int
    size: int
    zip64: bool


async def stream_zip(chunks: AsyncIterator[bytes]) -> AsyncIterator[ZipChunk]:
    """Inflate an archive's files as its chunks arrive, yielding each file's pieces in turn (:class:`ZipChunk`).

    The archive is read front to back by its local headers, as a stream can be: each file inflated with
    :func:`zlib.decompressobj`, its CRC-32 and sizes checked against its header or, when the header defers them (flag
    bit 3), against the data descriptor after its data; then the central directory is read to its end record, whose
    count of files must match. Neither a file nor a chunk is held whole: a piece is at most 1 MiB. A directory entry is
    a file with no bytes.

    An empty body raises :class:`EmptyBodyError` and one that does not open as a ZIP :class:`UnknownLayoutError`, as
    :func:`unzip` does. An archive that cannot be read raises :class:`CorruptArchiveError`: one cut off anywhere before
    its end record or with bytes after it, a file whose deflate data is damaged or whose CRC-32 or sizes do not match,
    a file encrypted or compressed other than stored or deflated, a stored file whose sizes follow it (a stream cannot
    tell where it ends), a record that is none an archive holds there, and a directory that counts other than the files
    read.
    """
    body = _Bytes(chunks)
    opening = await body.peek(16)
    if not opening:
        msg = "the body is empty"
        raise EmptyBodyError(msg)
    if not opening.startswith(_ZIP_SIGNATURES):
        msg = f"the body is not a ZIP archive: it opens {opening!r}"
        raise UnknownLayoutError(msg)
    files = 0
    while (signature := await body.exactly(4, "a file's header or the archive's directory")) == _LOCAL_HEADER:
        header = await _local_header(body)
        async for piece in _file(body, header):
            yield piece
        files += 1
    body.give_back(signature)
    await _directory(body, files)


async def _local_header(body: _Bytes) -> _Header:
    """Read a file's local header, after its signature, and refuse a file that cannot be streamed."""
    _, flags, method, _, _, crc, compressed, size, name_length, extra_length = _LOCAL.unpack(
        await body.exactly(_LOCAL.size, "a file's header")
    )
    raw_name = await body.exactly(name_length, "a file's header")
    extra = await body.exactly(extra_length, "a file's header")
    name = raw_name.decode("utf-8" if flags & _UTF8_NAME else "cp437", errors="replace")
    if flags & _ENCRYPTED:
        msg = f"the archive cannot be read: its file {name!r} is encrypted"
        raise CorruptArchiveError(msg)
    if method not in _METHODS:
        msg = (
            f"the archive cannot be read: its file {name!r} is compressed with method {method}, not stored or deflated"
        )
        raise CorruptArchiveError(msg)
    # A Zip64 field makes the descriptor's sizes eight bytes, whatever the header's own sizes are (APPNOTE 4.3.9.1): a
    # writer that defers them writes 0 there, not the 0xFFFFFFFF mark, and may still describe a file past 4 GiB.
    field = _zip64_field(extra)
    zip64 = field is not None
    if _ZIP64_MARK in {compressed, size}:
        if field is None:
            msg = f"the archive cannot be read: its file {name!r} marks its sizes as Zip64's but has no Zip64 field"
            raise CorruptArchiveError(msg)
        size, compressed = field
    if flags & _HAS_DESCRIPTOR and method == zipfile.ZIP_STORED:
        msg = f"the archive cannot be streamed: its stored file {name!r} gives its size only after its data"
        raise CorruptArchiveError(msg)
    return _Header(name, flags, method, crc, compressed, size, zip64)


def _zip64_field(extra: bytes) -> tuple[int, int] | None:
    """Return the uncompressed and compressed sizes a local header's Zip64 extra field holds; ``None`` without one."""
    at = 0
    while at + 4 <= len(extra):
        tag, length = struct.unpack_from("<HH", extra, at)
        if tag == _ZIP64_TAG and length >= _ZIP64_SIZES.size:
            size, compressed = _ZIP64_SIZES.unpack_from(extra, at + 4)
            return int(size), int(compressed)
        at += 4 + length
    return None


async def _file(body: _Bytes, header: _Header) -> AsyncIterator[ZipChunk]:
    """Yield one file's pieces, inflated, and check its CRC-32 and sizes once it ends."""
    crc = size = 0
    cut = f"the archive cannot be read: it ends inside the file {header.name!r}, so it was cut off"
    if header.method == zipfile.ZIP_STORED:
        left = compressed = header.compressed
        while left:
            if not (data := await body.some(min(left, _READ))):
                raise CorruptArchiveError(cut)
            left -= len(data)
            crc, size = zlib.crc32(data, crc), size + len(data)
            yield ZipChunk(header.name, data, last=False)
    else:
        inflater = zlib.decompressobj(_RAW_DEFLATE)
        fed, pending, full = 0, b"", False
        while not inflater.eof:
            # More input only once the last is used up and nothing inflated is still waiting to come out.
            if not pending and not full:
                if not (pending := await body.some(_READ)):
                    raise CorruptArchiveError(cut)
                fed += len(pending)
            try:
                data = inflater.decompress(pending, _INFLATE)
            except zlib.error as exc:
                msg = f"the archive cannot be read: its file {header.name!r} is not the deflate data it claims: {exc}"
                raise CorruptArchiveError(msg) from exc
            pending, full = inflater.unconsumed_tail, len(data) == _INFLATE
            if data:
                crc, size = zlib.crc32(data, crc), size + len(data)
                yield ZipChunk(header.name, data, last=False)
        body.give_back(inflater.unused_data)
        compressed = fed - len(inflater.unused_data)
    declared = await _descriptor(body, header) if header.flags & _HAS_DESCRIPTOR else header
    if (crc, compressed, size) != (declared.crc, declared.compressed, declared.size):
        msg = (
            f"the archive cannot be read: its file {header.name!r} reads as CRC-32 {crc:08x}, {compressed} bytes "
            f"compressed and {size} inflated, where it declares {declared.crc:08x}, {declared.compressed} and "
            f"{declared.size}"
        )
        raise CorruptArchiveError(msg)
    yield ZipChunk(header.name, b"", last=True)


async def _descriptor(body: _Bytes, header: _Header) -> _Header:
    """Read the data descriptor after a file's data: its CRC-32 and sizes, after an optional signature."""
    what = f"the descriptor of the file {header.name!r}"
    if await body.peek(4) == _DATA_DESCRIPTOR:
        await body.exactly(4, what)
    layout = struct.Struct("<IQQ" if header.zip64 else "<III")
    crc, compressed, size = layout.unpack(await body.exactly(layout.size, what))
    return header._replace(crc=crc, compressed=compressed, size=size)


async def _directory(body: _Bytes, files: int) -> None:
    """Read the central directory to the end record, which must close the body and count *files* files."""
    what = "the archive's directory"
    listed = 0
    while (signature := await body.exactly(4, what)) == _CENTRAL_HEADER:
        lengths = _CENTRAL.unpack(await body.exactly(_CENTRAL.size, what))[_CENTRAL_LENGTHS]
        await body.exactly(sum(lengths), what)
        listed += 1
    if signature == _ZIP64_END:
        (length,) = struct.unpack("<Q", await body.exactly(8, what))
        await body.exactly(length, what)
        signature = await body.exactly(4, what)
    if signature == _ZIP64_LOCATOR:
        await body.exactly(_ZIP64_LOCATOR_SIZE, what)
        signature = await body.exactly(4, what)
    if signature != _END:
        msg = f"the archive cannot be read: {signature!r} stands where a file's header or its directory should"
        raise CorruptArchiveError(msg)
    *_, count, _, _, comment = _END_RECORD.unpack(await body.exactly(_END_RECORD.size, what))
    await body.exactly(comment, what)
    if not await body.ended():
        msg = "the archive cannot be read: bytes follow its end record"
        raise CorruptArchiveError(msg)
    if listed != files or count not in {files, _UNKNOWN_COUNT}:
        msg = f"the archive cannot be read: its directory lists {listed} files and counts {count}, but it holds {files}"
        raise CorruptArchiveError(msg)


async def stream_unzip(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Inflate the one file the archive holds as its chunks arrive, yielding its bytes a piece at a time.

    :func:`unzip`'s stream, with :func:`stream_zip`'s errors. An archive holding no file raises
    :class:`UnknownLayoutError` once its end is read, and one holding a second raises it when the second begins: by
    then the first file's bytes have all been yielded, and checked.
    """
    name: str | None = None
    ended = False
    async for piece in stream_zip(chunks):
        if ended or name not in {None, piece.name}:
            msg = f"the archive holds more than one file: {name!r}, then {piece.name!r}"
            raise UnknownLayoutError(msg)
        name, ended = piece.name, piece.last
        if piece.data:
            yield piece.data
    if name is None:
        msg = "the archive holds 0 files, not one"
        raise UnknownLayoutError(msg)
