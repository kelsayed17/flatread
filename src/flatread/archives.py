"""The containers a file is published in, each handing the file to the reader it is composed with.

:class:`ZippedFile` is one member of a zip, and :class:`ZippedOrBare` one member or the bare file; :class:`ZippedFiles`
a table published in parts, every member of a zip (a file a day of a month), read in name order and concatenated, a
field its members' names state on their rows. Each opens the body and hands the file's bytes to the reader it is
composed with, listed before it (``class _HouseZip(ZippedFile[RowT], XmlFile[RowT])``), so any reader reads a zipped
file. :class:`Transcoded` reads a file written in a legacy encoding (EIA's Windows-1252, with a DOS end-of-file byte)
as UTF-8 by the reader listed after it. :class:`StreamedZip` is one member of a zip arriving as a stream, its text
yielded as it inflates, for a file too large to hold. :func:`zipped_member` returns a zip's one member of a name as
bytes, for a format whose member is no table (a JSON document).
"""

import io
import re
import struct
import typing
import zipfile
import zlib
from collections.abc import AsyncIterator, Sequence
from typing import Final

import msgspec
import polars as pl

from flatread.core import File, _Column, _decoded, _second, _stated
from flatread.errors import ReadError

__all__ = ["StreamedZip", "Transcoded", "ZippedFile", "ZippedFiles", "ZippedOrBare", "zipped_member"]

#: The most bytes a zip member may inflate to: a body past it is refused, not read into memory.
_INFLATED_MAX: Final = 1 << 30


class Transcoded[RowT: msgspec.Struct](File[RowT]):
    """A file written in a legacy encoding (EIA's Windows-1252), read as UTF-8 by the reader the format lists after it.

    A format subclasses it with its reader and names :attr:`_encodings`; a body that none of them decodes raises
    ``ValueError``. :attr:`_eof` is a byte the file ends with after its last line (DOS's end-of-file mark), dropped.
    """

    #: The codecs the body may be written in, tried in order (SEC's: ``("utf-8", "latin-1")``); each format's subclass
    #: sets them. ``utf-16`` is tried only on a body that opens with its byte-order mark, as any even-length body
    #: decodes as UTF-16 without one.
    _encodings: typing.ClassVar[Sequence[str]]
    #: Whether a byte the last of :attr:`_encodings` does not decode is read as U+FFFD, not refused: a format whose
    #: prose may hold a stray byte (New York Fed's nowcast releases) sets it.
    _lossy: typing.ClassVar[bool] = False
    #: A byte the file may end with, after its last line, which is no part of it.
    _eof: typing.ClassVar[bytes] = b""

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        return super()._read(_decoded(body.removesuffix(self._eof), self._encodings, lossy=self._lossy).encode())


class ZippedFile[RowT: msgspec.Struct](File[RowT]):
    r"""A file published as one member of a zip, read by the reader the format lists after this class.

    A format subclasses it with its reader and names the member: :attr:`_member`, the regex the member's whole name
    matches, folders and all (``(?:.*/)?x\.csv`` finds ``x.csv`` in any folder), and :attr:`_members`, how many members
    the zip holds where the format fixes it. A body that is no zip, a zip of another count, no member or several
    matching, and a member that will not inflate raise ``ValueError``, naming the zip's members.
    """

    #: The regex the member's whole name matches; each format's subclass (or its constructor, SEC's) sets it.
    _member: re.Pattern[str]
    #: How many members the zip holds, where the format fixes it: ``None`` takes any number.
    _members: typing.ClassVar[int | None] = None

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        return self._inner(*zipped_member(body, self._member, self._members))

    def _inner(self, name: str, member: bytes) -> pl.DataFrame:
        """Read the member *name*, inflated: by the reader listed after this class, unless a format picks by *name*."""
        del name
        return super()._read(member)


class ZippedOrBare[RowT: msgspec.Struct](ZippedFile[RowT]):
    """A file published as the one member of a zip, or bare: a body with no member matching :attr:`_member` is the file.

    A body that is no zip, or a zip of other names (a workbook is a zip of its parts), is read whole, as a member
    named ``""``; format hooks as :class:`ZippedFile`'s.
    """

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                zipped = any(self._member.fullmatch(name) for name in archive.namelist())
        except zipfile.BadZipFile:
            zipped = False
        return super()._read(body) if zipped else self._inner("", body)


class ZippedFiles[RowT: msgspec.Struct](File[RowT]):
    r"""A table published as a zip of its parts (a file a day, a file a month), read by the reader listed after it.

    Every member's whole name matches :attr:`_member` (a member of another name is not the table's), and each is read
    as one file by the reader, in name order, the parts concatenated: ``class _Month(ZippedFiles[RowT],
    CsvFile[RowT])``. A group of :attr:`_member` named for a field states that field on its member's rows (the day a
    file's name writes), a field no column holds, which takes no ``Label``; its text is typed as the field (a date by
    its ``Format``). Only a reader that lays out its columns (:meth:`File._layout`: a CSV, a sheet) reads a member
    without the stated fields. :meth:`_part` checks a part against its name. A zip of no member, a member that will not
    inflate, and what :class:`ZippedFile` refuses raise ``ValueError``.
    """

    #: The regex every member's whole name matches; each format's subclass sets it.
    _member: typing.ClassVar[re.Pattern[str]]
    #: How many members the zip holds, where the format fixes it: ``None`` takes any number.
    _members: typing.ClassVar[int | None] = None

    @typing.override
    def _label(self, name: str) -> str | None:
        return name if name in self._member.groupindex else super()._label(name)

    @typing.override
    def _layout(self, header: Sequence[str]) -> Sequence[_Column] | None:
        columns = super()._layout(header) or self._columns
        return [column for column in columns if column.field not in self._member.groupindex]

    def _part(self, name: re.Match[str], part: pl.DataFrame) -> pl.DataFrame:
        """Return the *part* read from the member *name* matched: as read, unless a format checks it by its name."""
        del name
        return part

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        stated = [column for column in self._columns if column.field in self._member.groupindex]
        # a field read across two columns has the second before the join; a given field is no column yet
        order = [*self._fields, *(_second(field) for field in self._joined)]
        parts = []
        with _unzipped(body, self._members) as archive:
            names = archive.namelist()
            matched = sorted(filter(None, map(self._member.fullmatch, names)), key=lambda match: match.string)
            if not names or len(matched) != len(names):
                msg = f"the zip holds {names!r}, not one or more members, each matching {self._member.pattern}"
                raise ReadError(msg)
            for match in matched:
                part = super()._read(_inflated(archive, match.string))
                try:
                    given = [_stated(column, match[column.field], part.height, "cell") for column in stated]
                except ValueError as e:
                    msg = f"the member {match[0]!r} states its rows' {e}"
                    raise ReadError(msg) from e
                part = part.with_columns(given)
                parts.append(self._part(match, part.select(name for name in order if name in part.columns)))
        return pl.concat(parts)


def zipped_member(body: bytes, member: re.Pattern[str], members: int | None = None) -> tuple[str, bytes]:
    """Return the name and inflated bytes of the one member of the zip *body* whose whole name matches *member*.

    For a zip whose member is no table (treasury's ``cslt.zip`` of one ``cslt.json``); :class:`ZippedFile` reads a
    table's. *members*, :attr:`ZippedFile._members`. Raises ``ValueError`` as :class:`ZippedFile` does.
    """
    with _unzipped(body, members) as archive:
        names = archive.namelist()
        found = [name for name in names if member.fullmatch(name)]
        if len(found) != 1:
            msg = f"the zip holds {len(found)} members matching {member.pattern}, not one: {names!r}"
            raise ReadError(msg)
        return found[0], _inflated(archive, found[0])


def _unzipped(body: bytes, members: int | None) -> zipfile.ZipFile:
    """Open *body* as a zip of *members* members (any number for ``None``), or raise ValueError naming them."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        msg = f"the body is not a zip: {e}"
        raise ReadError(msg) from e
    if members is not None and len(names := archive.namelist()) != members:
        archive.close()
        msg = f"the zip holds {names!r}, not {members} members"
        raise ReadError(msg)
    return archive


def _inflated(archive: zipfile.ZipFile, name: str) -> bytes:
    """Return the zip's member *name* inflated, or raise ValueError for one past the cap or that will not inflate."""
    # zipfile inflates no more than the declared size, so checking it bounds the read
    if (size := archive.getinfo(name).file_size) > _INFLATED_MAX:
        msg = f"the zip's {name!r} inflates to {size} bytes, over {_INFLATED_MAX}"
        raise ReadError(msg)
    try:
        return archive.read(name)
    except (zipfile.BadZipFile, zlib.error, EOFError) as e:  # a damaged or cut deflate stream, a CRC that fails
        msg = f"the zip's {name!r} will not inflate: {e}"
        raise ReadError(msg) from e


#: The zip records a forward read meets (PKWARE's APPNOTE 4.3.7, 4.3.9, 4.3.12): a local file header (30 bytes, then the
#: name and the extra field), a data descriptor's optional signature, and a central directory header, past the members.
_LOCAL_HEADER: Final = struct.Struct("<IHHHHHIIIHH")
_LOCAL_SIGNATURE: Final = struct.pack("<I", 0x04034B50)
_DESCRIPTOR_SIGNATURE: Final = struct.pack("<I", 0x08074B50)
_CENTRAL_SIGNATURE: Final = struct.pack("<I", 0x02014B50)
_STORED: Final = 0
_DEFLATED: Final = 8
#: General-purpose flag bits: 3, the CRC-32 and sizes follow the member's data in a descriptor; 11, the name is UTF-8.
_DESCRIBED: Final = 0x08
_UTF8_NAME: Final = 0x800
#: The most text one inflate step yields, so a member that inflates a thousandfold still arrives in pieces.
_INFLATE_STEP: Final = 1 << 20


class _Forward:
    """A zip arriving as chunks, read forward only: exact reads, a peek, and a member's bytes as they arrive."""

    def __init__(self, chunks: AsyncIterator[bytes]) -> None:
        self._chunks = aiter(chunks)
        self._buffer = bytearray()

    async def _fill(self, size: int) -> bool:
        """Hold at least *size* bytes, unless the zip ends first."""
        while len(self._buffer) < size:
            if (chunk := await anext(self._chunks, None)) is None:
                return False
            self._buffer += chunk
        return True

    async def peek(self, size: int) -> bytes:
        """Return up to the next *size* bytes, leaving them unread: fewer only where the zip ends."""
        await self._fill(size)
        return bytes(self._buffer[:size])

    async def read(self, size: int) -> bytes:
        """Return the next *size* bytes; a zip that ends first was cut, and raises."""
        if not await self._fill(size):
            msg = "the zip was cut: it ends inside a member"
            raise ReadError(msg)
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    async def _arrived(self, most: int | None = None) -> bytes:
        """Return what is held, at most *most* bytes, waiting for a chunk if nothing is; the zip ending raises."""
        if not self._buffer and not await self._fill(1):
            msg = "the zip was cut: it ends inside a member"
            raise ReadError(msg)
        return await self.read(len(self._buffer) if most is None else min(most, len(self._buffer)))

    async def stored(self, size: int) -> AsyncIterator[bytes]:
        """Yield the next *size* bytes as they arrive (a member's as written), none held."""
        while size:
            data = await self._arrived(size)
            size -= len(data)
            yield data

    async def inflated(self) -> AsyncIterator[bytes]:
        """Yield a deflated member's text as it inflates, up to the member's end; what follows stays unread."""
        inflate = zlib.decompressobj(-zlib.MAX_WBITS)
        while not inflate.eof:
            data = inflate.unconsumed_tail or await self._arrived()
            try:
                text = inflate.decompress(data, _INFLATE_STEP)
            except zlib.error as e:
                msg = f"the zip's member will not inflate: {e}"
                raise ReadError(msg) from e
            if text:
                yield text
        self._buffer[:0] = inflate.unused_data

    async def descriptor(self) -> tuple[int, int]:
        """Read a data descriptor: an optional signature, then the CRC-32 and both sizes; return the CRC and text's."""
        signed = await self.peek(len(_DESCRIPTOR_SIGNATURE)) == _DESCRIPTOR_SIGNATURE
        descriptor = (await self.read(16 if signed else 12))[-12:]
        return int.from_bytes(descriptor[:4], "little"), int.from_bytes(descriptor[8:], "little")


class StreamedZip:
    r"""One member of a zip arriving as an async stream of chunks, its text yielded as it inflates, then checked.

    The zip is read forward by its local headers; ../../docs/examples/streamed_zip.txt walks FINRA's April
    2019 file record by record. A member before the one asked for is skipped as it arrives, by its size or (one written
    with a data descriptor) inflated to its end and dropped, so neither the zip nor any member is held, and the read
    stops at the member's end, so the rest is never fetched. The member's whole name, folders and all, matches the
    pattern given; it is stored or deflated, and its CRC-32 and size are checked once it ends (against its data
    descriptor, where it has one), so a damaged member raises after its text. A reader splits the text into what it
    reads (FINRA's line blocks) or joins it for a reader of whole bodies (SEC's CSVs). A body that is no zip, a zip cut
    short (a download stopped), a member another method compressed, and a zip with no member matching raise
    ``ValueError``.
    """

    #: Whether the zip holds the member alone: a member after it raises (FINRA's monthly files).
    _alone: typing.ClassVar[bool] = False
    #: Whether a zip STORED in the zip (a ``.zip`` member) is read in its place: the member is looked for in it.
    _nested: typing.ClassVar[bool] = False

    def __init__(self, member: re.Pattern[str]) -> None:
        r"""Read the member whose whole name matches *member*: ``(?:.*/)?x\.csv`` finds ``x.csv`` in any folder."""
        self._member = member

    async def inflated(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Yield the member's text as *chunks* arrive and it inflates, then check it; see the class."""
        async for text in self._found(_Forward(chunks), first=True):
            yield text

    async def _found(self, archive: _Forward, *, first: bool) -> AsyncIterator[bytes]:
        """Yield the member's text from *archive*, read from its first local header (or a later one, not *first*)."""
        while True:
            if not (header := await archive.peek(_LOCAL_HEADER.size)).startswith(_LOCAL_SIGNATURE):
                raise self._no_member(header, first=first)
            first = False
            _, _, flags, method, _, _, crc, size, inflated_size, name_length, extra_length = _LOCAL_HEADER.unpack(
                await archive.read(_LOCAL_HEADER.size)
            )
            name = (await archive.read(name_length)).decode("utf-8" if flags & _UTF8_NAME else "cp437")
            await archive.read(extra_length)
            described = bool(flags & _DESCRIBED)
            if method not in {_DEFLATED, _STORED} or (described and method != _DEFLATED):
                msg = f"the zip's {name!r} is compressed by method {method}, which is not read"
                raise ReadError(msg)
            if self._nested and method == _STORED and name.endswith(".zip"):  # a zip stored in the zip
                async for text in self._found(_Forward(archive.stored(size)), first=True):
                    yield text
                return
            wanted = self._member.fullmatch(name) is not None
            # A member not asked for is skipped by its size as written, unless only its descriptor states it.
            data = archive.inflated() if method == _DEFLATED and (wanted or described) else archive.stored(size)
            check = length = 0
            async for text in data:
                if wanted:
                    check, length = zlib.crc32(text, check), length + len(text)
                    if length > inflated_size and not described:  # raised before it is all held, not after
                        msg = f"the zip's {name!r} inflates past the {inflated_size} bytes it states: it is damaged"
                        raise ReadError(msg)
                    yield text
            if described:
                crc, inflated_size = await archive.descriptor()
            if wanted:
                break
        if (check, length) != (crc, inflated_size):
            msg = f"the zip's {name!r} does not match its CRC-32 and size: it is damaged"
            raise ReadError(msg)
        if self._alone and (after := await archive.peek(len(_CENTRAL_SIGNATURE))) != _CENTRAL_SIGNATURE:
            msg = (
                "the zip was cut: it ends after its member"
                if len(after) < len(_CENTRAL_SIGNATURE)
                else "the zip holds more than its one member"
            )
            raise ReadError(msg)

    def _no_member(self, header: bytes, *, first: bool) -> ReadError:
        """Return the error for *header*, a record that is no local header: the zip's *first*, or a later one."""
        if first:
            return ReadError("the body is not a zip: it does not open with a local file header")
        if len(header) < len(_CENTRAL_SIGNATURE):
            return ReadError("the zip was cut: it ends after a member")
        return ReadError(f"the zip holds no member matching {self._member.pattern}")  # the central directory
