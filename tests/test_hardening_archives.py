"""Hostile zips and XML: each refusal a ``ReadError`` (``LayoutError`` for a reader declared wrong), nothing held."""

import io
import math
import re
import struct
import typing
import zipfile
import zlib
from typing import Annotated, ClassVar

import msgspec
import pytest

from flatread import archives, sdmx
from flatread.archives import StreamedZip, ZippedFile, ZippedFiles, ZippedOrBare, zipped_member
from flatread.delimited import CsvFile
from flatread.errors import LayoutError, ReadError
from flatread.fields import Label
from flatread.markup import NestedXml, XmlFile, parse_xml
from flatread.sdmx import SdmxCsv, SdmxObservations
from tests.test_sdmx import _GOOD, _TWO, _Data, _message, _Obs, _ObsOf, _one, _Zipped

if typing.TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from lxml import etree


class _Row(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[int, Label("a")]


class _One(ZippedFile[_Row], CsvFile[_Row]):
    _member = re.compile(r"x\.csv")


class _OneOrBare(ZippedOrBare[_Row], CsvFile[_Row]):
    _member = re.compile(r"x\.csv")


class _Parts(ZippedFiles[_Row], CsvFile[_Row]):
    _member = re.compile(r".*\.csv")


_CSV = b"a\n" + b"1\n" * 200


def _zip(*members: tuple[str, bytes], method: int = zipfile.ZIP_DEFLATED) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in members:
            archive.writestr(name, data, compress_type=method)
    return out.getvalue()


def _patched(body: bytes, local: int, central: int, value: Callable[[int], int]) -> bytes:
    """Return *body* with the 2-byte field at *local* in its local header and *central* in its central one changed."""
    patched = bytearray(body)
    for signature, at in ((b"PK\x03\x04", local), (b"PK\x01\x02", central)):
        where = patched.find(signature) + at
        struct.pack_into("<H", patched, where, value(struct.unpack_from("<H", patched, where)[0]))
    return bytes(patched)


def _bad_name() -> bytes:
    body = _patched(_zip(("xx.csv", _CSV)), 6, 8, lambda flags: flags | 0x800)  # its name UTF-8, by its flags
    return body.replace(b"xx.csv", b"\xff\xfe.csv")


def _damaged(method: int) -> bytes:
    body = bytearray(_zip(("x.csv", _CSV * 50), method=method))
    at = body.find(b"PK\x03\x04") + 30 + len("x.csv") + 9
    body[at : at + 6] = b"\x00" * 6
    return bytes(body)


def _offset_past_its_start() -> bytes:
    body = bytearray(_zip(("x.csv", _CSV)))
    end = body.find(b"PK\x05\x06")
    struct.pack_into("<I", body, end + 16, 0x7FFFFFFF)  # the central directory stated far past where it sits
    return bytes(body)


_HOSTILE = {
    "a name not its flag's UTF-8": _bad_name(),
    "an encrypted member": _patched(_zip(("x.csv", _CSV)), 6, 8, lambda flags: flags | 1),
    "method 6, which zipfile lacks": _patched(_zip(("x.csv", _CSV)), 8, 10, lambda _: 6),
    "a damaged bzip2 stream": _damaged(zipfile.ZIP_BZIP2),
    "a damaged LZMA stream": _damaged(zipfile.ZIP_LZMA),
    "a header offset before the body": _offset_past_its_start(),
}

_READERS: dict[str, Callable[[bytes], object]] = {
    "ZippedFile": _One(_Row).frame,
    "ZippedOrBare": _OneOrBare(_Row).frame,
    "ZippedFiles": _Parts(_Row).frame,
    "zipped_member": lambda body: zipped_member(body, re.compile(r"x\.csv")),
}


@pytest.mark.parametrize("reader", _READERS)
@pytest.mark.parametrize("body", _HOSTILE.values(), ids=_HOSTILE)
def test_a_zip_zipfile_fails_on_raises_read_error(reader: str, body: bytes) -> None:
    with pytest.raises(ReadError, match=r"^the (zip's 'x\.csv' will not inflate|body is (a )?(not )?a zip)"):
        _READERS[reader](body)


class _ZippedMessage(_ObsOf[_Obs]):
    _zipped_member = re.compile(r"x\.csv")  # the hostile bodies' member, its name aside


@pytest.mark.parametrize("body", _HOSTILE.values(), ids=_HOSTILE)
def test_a_zipped_message_zipfile_fails_on_raises_read_error(body: bytes) -> None:
    with pytest.raises(ReadError, match=r"^the (zip's 'x\.csv' will not inflate|body is not a zip)"):
        _ZippedMessage(_Obs, "D").frame(body)


@pytest.mark.parametrize("method", [zipfile.ZIP_LZMA, zipfile.ZIP_BZIP2, zipfile.ZIP_DEFLATED])
def test_a_zipped_message_damaged_past_its_start_raises_as_it_is_parsed(method: int) -> None:
    body = bytearray(_zip(("T.xml", _message(*[_one(f"S{n}".encode(), _TWO) for n in range(2000)])), method=method))
    start, end = 30 + len("T.xml"), body.find(b"PK\x01\x02")  # the member's data, between its header and the directory
    at = (start + end) // 2
    body[at:end] = b"\xff" * (end - at)  # its first half inflates and parses, its second will not inflate
    with pytest.raises(ReadError, match=r"^the zip's 'T.xml' will not inflate"):
        _Zipped(_Obs, "D").frame(bytes(body))


def _listed_twice(body: bytes) -> bytes:
    """Return the one-member zip *body* with its central directory listing its member twice."""
    start, end = body.find(b"PK\x01\x02"), body.find(b"PK\x05\x06")
    entry = body[start:end]
    tail = bytearray(body[end:])
    struct.pack_into("<HHII", tail, 8, 2, 2, 2 * len(entry), start)
    return body[:start] + entry * 2 + bytes(tail)


@pytest.mark.parametrize("reader", _READERS)
def test_a_zip_listing_a_name_twice_raises(reader: str) -> None:
    body = _listed_twice(_zip(("x.csv", _CSV)))
    with pytest.raises(ReadError, match=r"^the zip lists 'x\.csv' more than once$"):
        _READERS[reader](body)


def test_a_zipped_message_listing_its_name_twice_raises() -> None:
    with pytest.raises(ReadError, match=r"^the zip lists 'T\.xml' more than once$"):
        _Zipped(_Obs, "D").frame(_listed_twice(_zip(("T.xml", _message(_GOOD)))))


def test_a_zip_s_parts_inflating_past_the_cap_in_all_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _zip(("1.csv", _CSV), ("2.csv", _CSV))
    assert len(_Parts(_Row).rows(body)) == 400
    monkeypatch.setattr(archives, "_INFLATED_MAX", len(_CSV) + 1)  # each part under it, the two over
    with pytest.raises(ReadError, match=rf"^the zip's members inflate to {2 * len(_CSV)} bytes in all, over \d+$"):
        _Parts(_Row).frame(body)


# --- StreamedZip ----------------------------------------------------------------------------------------------------


async def _chunks(body: bytes) -> AsyncIterator[bytes]:
    for at in range(0, len(body), 7):
        yield body[at : at + 7]


async def _streamed(body: bytes, zip_: StreamedZip | None = None) -> bytes:
    return b"".join([text async for text in (zip_ or StreamedZip(re.compile(r"x\.csv"))).inflated(_chunks(body))])


def _local(name: str, data: bytes, *, crc: int | None = None) -> bytes:
    """Return a stored member's local header and data: all a forward read needs."""
    crc = zlib.crc32(data) if crc is None else crc
    encoded = name.encode()
    header = struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, 0, 0, 0, crc, len(data), len(data), len(encoded), 0)
    return header + encoded + data


class _Nested(StreamedZip):
    _nested = True


class _NestedAlone(_Nested):
    _alone = True


async def test_a_streamed_name_not_its_flag_s_utf8_raises() -> None:
    with pytest.raises(ReadError, match=r"^the zip's member name b'\\xff\\xfe\.csv' is not the UTF-8 its flags state$"):
        await _streamed(_bad_name())


@pytest.mark.parametrize("seekable", [True, False], ids=["sized", "described"])
@pytest.mark.parametrize("method", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED], ids=["stored", "deflated"])
async def test_a_zip64_member_raises_before_any_text(method: int, *, seekable: bool) -> None:
    class _Stream(io.BytesIO):
        @typing.override
        def seekable(self) -> bool:
            return seekable

    out = _Stream()
    with zipfile.ZipFile(out, "w", compression=method) as archive, archive.open("x.csv", "w", force_zip64=True) as f:
        f.write(_CSV)
    texts: list[bytes] = []
    with pytest.raises(ReadError, match=r"^the zip's 'x\.csv' is a zip64 member, which is not read$"):
        texts.extend([text async for text in StreamedZip(re.compile(r"x\.csv")).inflated(_chunks(out.getvalue()))])
    assert texts == []


async def test_a_zip_in_the_stored_zip_raises() -> None:
    inner = _local("x.csv", _CSV)
    assert await _streamed(_local("a.zip", inner), _Nested(re.compile(r"x\.csv"))) == _CSV
    deep = _local("a.zip", _local("b.zip", inner))
    with pytest.raises(ReadError, match=r"^the zip's 'b\.zip' is a zip in the zip stored in the zip, which is not"):
        await _streamed(deep, _Nested(re.compile(r"x\.csv")))
    with pytest.raises(ReadError, match=r"is a zip in the zip stored in the zip"):  # no recursion, however deep
        await _streamed(_deep(1000), _Nested(re.compile(r"x\.csv")))


def _deep(depth: int) -> bytes:
    body = _local("x.csv", b"a\n")
    for _ in range(depth):
        body = _local("a.zip", body)
    return body


async def test_the_stored_zip_s_crc_and_size_are_checked_once_it_is_read() -> None:
    inner = _zip(("x.csv", _CSV))
    with pytest.raises(ReadError, match=r"^the zip's 'a\.zip' does not match its CRC-32 and size: it is damaged$"):
        await _streamed(_local("a.zip", inner, crc=0), _Nested(re.compile(r"x\.csv")))
    whole = _zip(("a.zip", inner), method=zipfile.ZIP_STORED)
    assert await _streamed(whole, _NestedAlone(re.compile(r"x\.csv"))) == _CSV
    after = _zip(("a.zip", inner), ("b.txt", b"b"), method=zipfile.ZIP_STORED)
    with pytest.raises(ReadError, match=r"^the zip holds more than its one member$"):
        await _streamed(after, _NestedAlone(re.compile(r"x\.csv")))


# --- SDMX-ML data messages ------------------------------------------------------------------------------------------


def test_a_message_holding_too_much_outside_its_series_raises_as_it_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sdmx, "_LOOSE", 1 << 16)
    header = b"<message:Header>" + b"<h/>" * (1 << 16) + b"</message:Header>"
    body = _message(_GOOD).replace(b"><x:DataSet", b">" + header + b"<x:DataSet", 1)
    with pytest.raises(ReadError, match=r"^the body holds over 65536 bytes outside its series$"):
        _ObsOf(_Obs, "D").frame(body)
    stray = _message(b"<x:note>" + b"<h/>" * (1 << 16) + b"</x:note>", _GOOD)
    with pytest.raises(ReadError, match=r"^the body holds over 65536 bytes outside its series$"):
        _ObsOf(_Obs, "D").frame(stray)
    many = _message(*[_one(f"S{n}".encode(), _TWO) for n in range(2000)])  # far past it, all of it series
    assert len(many) > 1 << 18
    assert len(_ObsOf(_Obs, "D").rows(many)) == 4000


def test_a_stray_element_between_series_is_named_by_its_tag_alone() -> None:
    body = _message(b"<x:note>" + b"<h/>" * 1000 + b"</x:note>", _GOOD)
    with pytest.raises(
        ReadError, match=r"^in the D data set, the body holds <\{urn:x\}note> where a series should be$"
    ):
        _ObsOf(_Obs, "D").frame(body)


def test_the_data_set_read_may_follow_others() -> None:
    other = b'<x:DataSet id="E" xmlns:k="urn:k">' + _GOOD + b"</x:DataSet>"
    body = _message(_GOOD).replace(b"<x:DataSet", other * 3 + b"<x:DataSet", 1)
    assert [row.series_name for row in _ObsOf(_Obs, "D").rows(body)] == ["S1", "S1"]


def test_a_series_whose_prefix_no_namespace_binds_raises_read_error() -> None:
    body = _message(_GOOD).replace(b"<x:DataSet", b'<x:DataSet id="E">' + _GOOD + b"</x:DataSet><x:DataSet", 1)
    with pytest.raises(ReadError, match=r"^in the E data set, the body holds other than series$"):
        _ObsOf(_Obs, "D").frame(body)


class _Observations(_Data[_Obs], SdmxObservations[_Obs]):
    _statuses = ("A", "M")
    _absent = ("",)
    _date_format = "%Y-%m"


class _Member(ZippedFile[_Obs], _Observations):
    _member = re.compile(r"[^/]*\.xml")


def test_a_data_message_composes_with_zipped_file_as_well_as_streaming_its_own_member() -> None:
    body = _zip(("T.xml", _message(_GOOD)))
    assert _Member(_Obs, "D").rows(body) == _Zipped(_Obs, "D").rows(body)
    assert len(_Member(_Obs, "D").rows(body)) == 2


# --- SDMX-CSV -------------------------------------------------------------------------------------------------------


class _Priced(msgspec.Struct, frozen=True, gc=False):
    key: str
    obs_value: float | None
    price: float


@pytest.mark.parametrize("cell", [b"nan", b"inf", b"-inf", b"1e400"])
def test_only_obs_value_may_be_non_finite(cell: bytes) -> None:
    with pytest.raises(ReadError, match=r"^the \['PRICE'\] columns hold NaN or an infinity, which is no number"):
        SdmxCsv(_Priced).frame(b"KEY,OBS_VALUE,PRICE\r\nK,1.5," + cell + b"\r\n")
    rows = SdmxCsv(_Priced).rows(b"KEY,OBS_VALUE,PRICE\r\nK,NaN,1\r\nK,INF,2\r\n")
    assert rows[0].obs_value is not None
    assert math.isnan(rows[0].obs_value)
    assert rows[1].obs_value == math.inf


# --- XML ------------------------------------------------------------------------------------------------------------


class _Pair(msgspec.Struct, frozen=True, gc=False):
    a: Annotated[str, Label("a")]
    n: Annotated[int, Label("n")]


def test_an_unbound_prefix_is_a_layout_error_when_built() -> None:
    with pytest.raises(LayoutError, match=r"^the name 'mes:Structure' writes the prefix 'mes', which the format's"):
        XmlFile(_Pair, root="mes:Structure", record="r")
    with pytest.raises(LayoutError, match=r"writes the prefix 'x'"):
        XmlFile(_Pair, root="t", record="x:r")


_TABLE = XmlFile(_Pair, root="t", record="r")


@pytest.mark.parametrize(
    ("body", "where"),
    [
        (b"<t><r><a>x</a>junk<n>12</n></r></t>", r"^record 1 holds the text 'junk' beside its leaf elements$"),
        (b"<t><r>junk<a>x</a><n>12</n></r></t>", r"^record 1 holds the text 'junk'"),
        (b"<t><r><a>x</a><n>12</n></r>junk</t>", r"^record 1 holds the text 'junk'"),
        (b"<t>junk<r><a>x</a><n>12</n></r></t>", r"^the <t> root holds the text 'junk' before its first record$"),
    ],
)
def test_text_beside_a_record_or_its_leaves_raises(body: bytes, where: str) -> None:
    with pytest.raises(ReadError, match=where):
        _TABLE.frame(body)


def test_whitespace_beside_a_record_or_its_leaves_is_no_text() -> None:
    body = b"<t>\n  <r>\n    <a>x</a>\n    <n>12</n>\n  </r>\n</t>\n"
    assert _TABLE.rows(body) == [_Pair("x", 12)]


class _Id(msgspec.Struct, frozen=True, gc=False):
    id: Annotated[str, Label("@id")]


class _PerBody(NestedXml[_Id]):
    _namespaces: ClassVar = {"e": "urn:x"}

    @typing.override
    def _namespaces_of(self, root: etree._Element) -> dict[str, str]:
        return {"e": root.nsmap[None]}

    @typing.override
    def _document(self, body: bytes) -> etree._Element:  # any root namespace, for the test
        return parse_xml(body)


def test_a_nested_xml_reader_holds_nothing_from_one_read_to_the_next() -> None:
    reader = _PerBody(_Id, root="root", record="e:item")
    held = dict(vars(reader))
    for n in range(50):
        assert reader.rows(f'<root xmlns="urn:v{n}"><item id="{n}"/></root>'.encode()) == [_Id(str(n))]
    assert vars(reader) == held
    assert all(vars(reader)[name] is value for name, value in held.items())
