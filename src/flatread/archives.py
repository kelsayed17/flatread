"""The containers a file is published in, each handing the file to the reader it is composed with.

:class:`ZippedFile` is one member of a zip, and :class:`ZippedOrBare` one member or the bare file; :class:`ZippedFiles`
a table published in parts, every member of a zip (a file a day of a month), read in name order and concatenated, a
field its members' names state on their rows. Each opens the body and hands the file's bytes to the reader it is
composed with, listed after it (``class _HouseZip(ZippedFile[RowT], XmlFile[RowT])``), so any reader reads a zipped
file. :class:`Transcoded` reads a file written in a legacy encoding (EIA's Windows-1252, with a DOS end-of-file byte)
as UTF-8 by the reader listed after it. :class:`StreamedZip` is one member of a zip arriving as a stream, its text
yielded as it inflates, for a file too large to hold. :func:`zipped_member` returns a zip's one member of a name as
bytes, for a format whose member is no table (a JSON document).

Each zip read whole (all but :class:`StreamedZip`'s) raises ``ReadError`` for what :mod:`zipfile` refuses or fails on
(a name that is not the UTF-8 its flag claims, an encrypted member, a compression method or feature it lacks, a
damaged deflate, bzip2 or LZMA stream, a header pointing outside the body), for a central directory listing a name
twice, and for members inflating past :data:`_INFLATED_MAX` bytes in all; :class:`StreamedZip` raises it for what it
lists.
"""

import codecs
import collections
import io
import lzma
import re
import struct
import typing
import zipfile
import zlib
from collections.abc import AsyncIterator, Sequence
from typing import IO, Final

import msgspec
import polars as pl

from flatread.core import Column, File, _decoded, _second, _stated
from flatread.errors import LayoutError, ReadError

__all__ = ["StreamedZip", "Transcoded", "ZippedFile", "ZippedFiles", "ZippedOrBare", "zip_names", "zipped_member"]

#: The most bytes a body's zip members may inflate to, in all: a body past it is refused, not read into memory.
_INFLATED_MAX: Final = 1 << 30
#: What :mod:`zipfile` raises opening or inflating a damaged or unsupported zip, besides ``BadZipFile``: ``ValueError``
#: (a name that is not its flag's UTF-8, a header offset outside the body), ``RuntimeError`` (an encrypted member),
#: ``NotImplementedError`` (a method, version or flag it lacks), ``OSError`` (bzip2), ``LZMAError``, ``zlib.error`` and
#: ``EOFError`` (a cut stream).
_UNZIP_ERRORS: Final = (
    zipfile.BadZipFile,
    zlib.error,
    lzma.LZMAError,
    EOFError,
    OSError,
    RuntimeError,
    NotImplementedError,
    ValueError,
)


class Transcoded[RowT: msgspec.Struct](File[RowT]):
    """A file written in a legacy encoding (EIA's Windows-1252), read as UTF-8 by the reader the format lists after it.

    A format subclasses it with its reader and names :attr:`_encodings`; a body that none of them decodes raises
    ``ReadError``. :attr:`_eof` is a byte the file ends with after its last line (DOS's end-of-file mark), dropped.
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
    def _check_switches(self) -> None:
        super()._check_switches()
        encodings = getattr(self, "_encodings", None)
        if encodings is None or isinstance(encodings, (str, bytes)) or len(encodings) == 0:
            msg = f"the {type(self).__name__} format names no _encodings, the codecs its body may be written in"
            raise LayoutError(msg)
        for encoding in encodings:
            try:
                codecs.lookup(encoding)
            except LookupError as e:
                msg = f"the {type(self).__name__} format's encoding {encoding!r} is no codec Python reads"
                raise LayoutError(msg) from e

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        return super()._read(_decoded(body.removesuffix(self._eof), self._encodings, lossy=self._lossy).encode())


class ZippedFile[RowT: msgspec.Struct](File[RowT]):
    r"""A file published as one member of a zip, read by the reader the format lists after this class.

    A format subclasses it with its reader and names the member: :attr:`_member`, the regex the member's whole name
    matches, folders and all (``(?:.*/)?x\.csv`` finds ``x.csv`` in any folder), and :attr:`_members`, how many members
    the zip holds where the format fixes it. A body that is no zip, a zip of another count, no member or several
    matching, and what the module refuses of every zip (a name listed twice, a member that will not inflate or that
    inflates past :data:`_INFLATED_MAX`) raise ``ReadError``, naming the zip's members or the member.
    """

    #: The regex the member's whole name matches; each format's subclass (or its constructor, SEC's) sets it.
    _member: re.Pattern[str]
    #: How many members the zip holds, where the format fixes it: ``None`` takes any number.
    _members: typing.ClassVar[int | None] = None

    @typing.override
    def _check_switches(self) -> None:
        super()._check_switches()
        if not isinstance(getattr(self, "_member", None), re.Pattern):
            msg = f"the {type(self).__name__} format names no _member, the regex its member's whole name matches"
            raise LayoutError(msg)

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        return self._inner(*zipped_member(body, self._member, self._members))

    def _inner(self, name: str, member: bytes) -> pl.DataFrame:
        """Read the member *name*, inflated: by the reader listed after this class, unless a format picks by *name*."""
        del name
        return super()._read(member)


class ZippedOrBare[RowT: msgspec.Struct](ZippedFile[RowT]):
    """A file published as the one member of a zip, or bare: a body with no member matching :attr:`_member` is the file.

    A body that is no zip (``BadZipFile``), or a zip of other names (a workbook is a zip of its parts), is read whole,
    as a member named ``""``; a zip :mod:`zipfile` opens only to fail on (a name that is not its flag's UTF-8) raises
    ``ReadError``, and a zip with a member matching is read as :class:`ZippedFile` reads it. Format hooks as
    :class:`ZippedFile`'s.
    """

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                zipped = any(self._member.fullmatch(name) for name in archive.namelist())
        except zipfile.BadZipFile:
            zipped = False
        except _UNZIP_ERRORS as e:
            msg = f"the body is a zip that will not open: {e}"
            raise ReadError(msg) from e
        return super()._read(body) if zipped else self._inner("", body)


class ZippedFiles[RowT: msgspec.Struct](File[RowT]):
    r"""A table published as a zip of its parts (a file a day, a file a month), read by the reader listed after it.

    Every member's whole name matches :attr:`_member` (a member of another name is not the table's), and each is read
    as one file by the reader, in name order, the parts concatenated: ``class _Month(ZippedFiles[RowT],
    CsvFile[RowT])``. A group of :attr:`_member` named for a field states that field on its member's rows (the day a
    file's name writes), a field no column holds, which takes no ``Label``; its text is typed as the field (a date by
    its ``Format``). Only a reader that lays out its columns (:meth:`File._layout`: a CSV, a sheet) reads a member
    without the stated fields. :meth:`_part` checks a part against its name. A zip of no member, members inflating past
    :data:`_INFLATED_MAX` bytes in all, and what :class:`ZippedFile` refuses raise ``ReadError``.
    """

    #: The regex every member's whole name matches; each format's subclass sets it.
    _member: typing.ClassVar[re.Pattern[str]]
    #: How many members the zip holds, where the format fixes it: ``None`` takes any number.
    _members: typing.ClassVar[int | None] = None

    @typing.override
    def _stated_fields(self) -> frozenset[str]:
        if not isinstance(member := getattr(self, "_member", None), re.Pattern) or not isinstance(member.pattern, str):
            msg = f"the {type(self).__name__} format names no _member, the regex its members' names match (from str)"
            raise LayoutError(msg)
        if stray := member.groupindex.keys() - set(self._fields):
            msg = f"the _member groups {sorted(stray)} name no field of {self._row.__name__}"
            raise LayoutError(msg)
        return super()._stated_fields() | member.groupindex.keys()

    @typing.override
    def _check_switches(self) -> None:
        super()._check_switches()
        self._stated_fields()  # refuses an unset _member, or a group naming no field, whatever the reader

    @typing.override
    def _label(self, name: str) -> str | None:
        return name if name in self._stated_fields() else super()._label(name)

    @typing.override
    def _layout(self, header: Sequence[str]) -> Sequence[Column] | None:
        columns = super()._layout(header) or self._columns
        stated = self._stated_fields()
        return [column for column in columns if column.field not in stated]

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
            if (size := sum(archive.getinfo(match.string).file_size for match in matched)) > _INFLATED_MAX:
                msg = f"the zip's members inflate to {size} bytes in all, over {_INFLATED_MAX}"
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


def zip_names(body: bytes) -> list[str]:
    """Return the names of the zip *body*'s members, in the order its central directory lists them.

    The zip is opened as every reader here opens one: a body that is no zip, will not open, or lists a name twice
    raises ``ReadError``. For a format that picks its member by what the zip holds (the table's ``.xlsx``, else its
    ``.csv``), then reads it by its exact name: ``zipped_member(body, re.compile(re.escape(name)))``.
    """
    with _unzipped(body, None) as archive:
        return archive.namelist()


def zipped_member(body: bytes, member: re.Pattern[str], members: int | None = None) -> tuple[str, bytes]:
    """Return the name and inflated bytes of the one member of the zip *body* whose whole name matches *member*.

    For a zip whose member is no table (treasury's ``cslt.zip`` of one ``cslt.json``); :class:`ZippedFile` reads a
    table's. *members*, :attr:`ZippedFile._members`. Raises ``ReadError`` as :class:`ZippedFile` does.
    """
    with _unzipped(body, members) as archive:
        names = archive.namelist()
        found = [name for name in names if member.fullmatch(name)]
        if len(found) != 1:
            msg = f"the zip holds {len(found)} members matching {member.pattern}, not one: {names!r}"
            raise ReadError(msg)
        return found[0], _inflated(archive, found[0])


def _unzipped(body: bytes, members: int | None) -> zipfile.ZipFile:
    """Open *body* as a zip of *members* members (any number for ``None``), each name listed once, or raise ReadError.

    A name listed twice is refused: zipfile would read its one member once per listing, a zip bomb of central
    directory entries.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except _UNZIP_ERRORS as e:
        msg = f"the body is not a zip: {e}"
        raise ReadError(msg) from e
    names = archive.namelist()
    if len(set(names)) != len(names):
        archive.close()
        twice = next(name for name, count in collections.Counter(names).items() if count > 1)
        msg = f"the zip lists {twice!r} more than once"
        raise ReadError(msg)
    if members is not None and len(names) != members:
        archive.close()
        msg = f"the zip holds {names!r}, not {members} members"
        raise ReadError(msg)
    return archive


def _inflated(archive: zipfile.ZipFile, name: str) -> bytes:
    """Return the zip's member *name* inflated, or raise ReadError for one past the cap or that will not inflate."""
    # zipfile inflates no more than the declared size, so checking it bounds the read
    if (size := archive.getinfo(name).file_size) > _INFLATED_MAX:
        msg = f"the zip's {name!r} inflates to {size} bytes, over {_INFLATED_MAX}"
        raise ReadError(msg)
    try:
        return archive.read(name)
    except _UNZIP_ERRORS as e:  # a damaged or cut stream, a CRC that fails, a method or feature zipfile lacks
        msg = f"the zip's {name!r} will not inflate: {e}"
        raise ReadError(msg) from e


class _Inflating:
    """A zip member's stream whose reads raise ReadError naming it, for a member that will not inflate."""

    def __init__(self, name: str, stream: IO[bytes]) -> None:
        self._name = name
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        """Return up to *size* more bytes of the member, inflated (all of the rest for ``-1``)."""
        try:
            return self._stream.read(size)
        except _UNZIP_ERRORS as e:
            msg = f"the zip's {self._name!r} will not inflate: {e}"
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
#: A local header's size that a zip64 record states in its place; an extra field's record opens with its header ID
#: and its data's size.
_ZIP64: Final = 0xFFFFFFFF
_EXTRA_RECORD: Final = struct.Struct("<HH")
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

    async def local_header(self) -> tuple[str, int, int, int, int, bool]:
        """Read a local file header, its name and extra field: return its name, method, CRC-32 and sizes, and its flag.

        The flag is whether a data descriptor states the CRC-32 and sizes after the data instead. Raises ReadError
        for a member that is zip64, compressed by a method other than storing or deflating (or stored with a
        descriptor), or whose name is not the UTF-8 its flags state.
        """
        header = typing.cast("tuple[int, ...]", _LOCAL_HEADER.unpack(await self.read(_LOCAL_HEADER.size)))
        _, _, flags, method, _, _, crc, size, inflated_size, name_length, extra_length = header
        name = _named(await self.read(name_length), flags)
        if _ZIP64 in (size, inflated_size) or _zip64(await self.read(extra_length)):
            msg = f"the zip's {name!r} is a zip64 member, which is not read"
            raise ReadError(msg)
        described = bool(flags & _DESCRIBED)
        if method not in {_DEFLATED, _STORED} or (described and method != _DEFLATED):
            msg = f"the zip's {name!r} is compressed by method {method}, which is not read"
            raise ReadError(msg)
        return name, method, crc, size, inflated_size, described

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
    short (a download stopped), a member another method compressed, a zip64 member (sizes past 4 GiB, which are not
    read), a name that is not the UTF-8 its flag states, and a zip with no member matching raise ``ReadError``.
    """

    #: Whether the zip holds the member alone: a member after it raises (FINRA's monthly files). With :attr:`_nested`,
    #: both the zip and the zip stored in it hold their member alone.
    _alone: typing.ClassVar[bool] = False
    #: Whether a zip STORED in the zip (a ``.zip`` member) is read in its place: the member is looked for in it, then
    #: the rest of the stored zip is read to check its own CRC-32 and size. One level only: a ``.zip`` stored in the
    #: stored zip raises.
    _nested: typing.ClassVar[bool] = False

    def __init__(self, member: re.Pattern[str]) -> None:
        r"""Read the member whose whole name matches *member*: ``(?:.*/)?x\.csv`` finds ``x.csv`` in any folder."""
        self._member = member

    async def inflated(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Yield the member's text as *chunks* arrive and it inflates, then check it; see the class."""
        async for text in self._found(_Forward(chunks), first=True):
            yield text

    async def _found(self, archive: _Forward, *, first: bool, inside: bool = False) -> AsyncIterator[bytes]:
        """Yield the member's text from *archive*, read from its first local header (or a later one, not *first*).

        *inside*, *archive* is the zip stored in the zip, which may store none in turn.
        """
        while True:
            if not (header := await archive.peek(_LOCAL_HEADER.size)).startswith(_LOCAL_SIGNATURE):
                raise self._no_member(header, first=first)
            first = False
            name, method, crc, size, inflated_size, described = await archive.local_header()
            if self._nested and method == _STORED and name.endswith(".zip"):  # a zip stored in the zip
                stored = _Counted(archive.stored(size))
                async for text in self._unstored(stored, name, inside=inside):
                    yield text
                check, length, inflated_size = stored.crc, stored.length, size
                break
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

    async def _unstored(self, stored: _Counted, name: str, *, inside: bool) -> AsyncIterator[bytes]:
        """Yield the member's text from the zip *stored* in the zip as *name*, then read the rest of it, summed.

        *inside*, the zip *stored* is in is itself stored in the zip: one level of zips is read, not two.
        """
        if inside:
            msg = f"the zip's {name!r} is a zip in the zip stored in the zip, which is not read"
            raise ReadError(msg)
        async for text in self._found(_Forward(stored.chunks()), first=True, inside=True):
            yield text
        async for _ in stored.chunks():  # the rest of the stored zip, its CRC-32 checked over the whole
            pass

    def _no_member(self, header: bytes, *, first: bool) -> ReadError:
        """Return the error for *header*, a record that is no local header: the zip's *first*, or a later one."""
        if first:
            return ReadError("the body is not a zip: it does not open with a local file header")
        if len(header) < len(_CENTRAL_SIGNATURE):
            return ReadError("the zip was cut: it ends after a member")
        return ReadError(f"the zip holds no member matching {self._member.pattern}")  # the central directory


class _Counted:
    """A stored member's chunks, passed on as they arrive, their CRC-32 and length summed for the check at its end."""

    def __init__(self, chunks: AsyncIterator[bytes]) -> None:
        self._chunks = chunks
        self.crc = self.length = 0

    async def chunks(self) -> AsyncIterator[bytes]:
        """Yield the chunks not yet passed on, summing each."""
        async for data in self._chunks:
            self.crc, self.length = zlib.crc32(data, self.crc), self.length + len(data)
            yield data


def _named(name: bytes, flags: int) -> str:
    """Return a local header's *name* decoded: UTF-8 where its *flags* state it, else IBM's code page 437."""
    try:
        return name.decode("utf-8" if flags & _UTF8_NAME else "cp437")
    except UnicodeDecodeError as e:
        msg = f"the zip's member name {name!r} is not the UTF-8 its flags state"
        raise ReadError(msg) from e


def _zip64(extra: bytes) -> bool:
    """Whether a local header's *extra* field holds a zip64 record (header ID 1, APPNOTE 4.5.3)."""
    while len(extra) >= _EXTRA_RECORD.size:
        tag, size = _EXTRA_RECORD.unpack_from(extra)
        if tag == 1:
            return True
        extra = extra[_EXTRA_RECORD.size + size :]
    return False
