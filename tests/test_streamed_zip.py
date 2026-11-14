"""Tests for :class:`flatread.archives.StreamedZip`: one member of a zip, read forward as it arrives."""

import io
import re
import zipfile
from typing import TYPE_CHECKING, override

import pytest

from flatread.archives import StreamedZip

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_CSV = re.compile(r"(?:.*/)?x\.csv")


class _Alone(StreamedZip):
    _alone = True


class _Nested(StreamedZip):
    _nested = True


class _Unseekable(io.BytesIO):
    """A stream zipfile cannot seek back in, so it writes each member's CRC and sizes in a data descriptor."""

    @override
    def seekable(self) -> bool:
        return False


def _zip(*members: tuple[str, bytes, int], described: bool = False) -> bytes:
    out = _Unseekable() if described else io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data, method in members:
            archive.writestr(name, data, compress_type=method)
    return out.getvalue()


async def _chunks(body: bytes, size: int) -> AsyncIterator[bytes]:
    for at in range(0, len(body), size):
        yield b""  # an empty chunk is no end
        yield body[at : at + size]


async def _read(body: bytes, zip_: StreamedZip | None = None, size: int = 7) -> bytes:
    return b"".join([text async for text in (zip_ or StreamedZip(_CSV)).inflated(_chunks(body, size))])


_TEXT = b"a,b\n1,2\n" * 50
_D, _S = zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED


@pytest.mark.parametrize("described", [False, True], ids=["sized", "described"])
@pytest.mark.parametrize("method", [_D, _S], ids=["deflated", "stored"])
async def test_the_member_is_found_past_the_members_before_it(method: int, *, described: bool) -> None:
    body = _zip(
        ("a.txt", b"skip" * 99, _S), ("b.txt", b"skip" * 99, _D), ("dir/x.csv", _TEXT, method), described=described
    )
    assert await _read(body) == _TEXT


async def test_a_descriptor_without_its_signature_reads() -> None:
    """APPNOTE 4.3.9.3: a writer may leave out a descriptor's signature; a forward read needs no offsets."""
    body = _zip(("a.txt", b"skip" * 99, _D), ("x.csv", _TEXT, _D), described=True)
    assert await _read(body.replace(b"PK\x07\x08", b"")) == _TEXT


async def test_a_zip_stored_in_the_zip_is_read_in_its_place() -> None:
    inner = _zip(("x.csv", _TEXT, _D))
    assert await _read(_zip(("readme.txt", b"hi", _D), ("amex.zip", inner, _S)), _Nested(_CSV)) == _TEXT


async def test_a_member_that_inflates_a_thousandfold_arrives_in_steps() -> None:
    body = _zip(("x.csv", bytes(8 << 20), _D))
    steps = [len(text) async for text in StreamedZip(_CSV).inflated(_chunks(body, 1 << 16))]
    assert (sum(steps), max(steps)) == (8 << 20, 1 << 20)


async def test_the_member_alone_reads() -> None:
    assert await _read(_zip(("x.csv", _TEXT, _D)), _Alone(_CSV)) == _TEXT


def _damaged(body: bytes, at: int, value: bytes) -> bytes:
    return body[:at] + value + body[at + len(value) :]


@pytest.mark.parametrize(
    ("body", "zip_", "says"),
    [
        (b"<html>", None, "the body is not a zip"),
        (b"", None, "the body is not a zip"),
        (_zip(("y.csv", _TEXT, _D)), None, r"holds no member matching \(\?:\.\*/\)\?x\\\.csv"),
        (_zip(("x.csv", _TEXT, zipfile.ZIP_BZIP2)), None, "compressed by method 12, which is not read"),
        (_damaged(_zip(("x.csv", _TEXT, _S)), 14, bytes(4)), None, "does not match its CRC-32 and size"),
        (_damaged(_zip(("x.csv", _TEXT, _D)), 22, bytes(4)), None, "inflates past the 0 bytes it states"),
        (_damaged(_zip(("x.csv", _TEXT, _D)), 24, b"\x01"), None, "does not match its CRC-32 and size"),
        (_damaged(_zip(("x.csv", _TEXT, _D)), 30 + 5, b"\xff" * 8), None, "will not inflate"),
        (_zip(("x.csv", _TEXT, _D))[:40], None, "the zip was cut: it ends inside a member"),
        (_zip(("x.csv", _TEXT, _S))[:60], None, "the zip was cut: it ends inside a member"),
        (_zip(("x.csv", b"x", _S))[: 30 + 5 + 1], _Alone(_CSV), "the zip was cut: it ends after its member"),
        (_zip(("x.csv", b"x", _S))[: 30 + 5 + 1 + 2], _Alone(_CSV), "the zip was cut: it ends after its member"),
        (_zip(("a.txt", b"x", _S))[: 30 + 5 + 1], None, "the zip was cut: it ends after a member"),
        (_zip(("x.csv", _TEXT, _D), ("y.csv", b"", _D)), _Alone(_CSV), "the zip holds more than its one member"),
    ],
    ids=[
        "not-zip",
        "empty",
        "no-member",
        "bzip2",
        "stored-crc",
        "size-past",
        "size-short",
        "bad-deflate",
        "cut-deflated",
        "cut-stored",
        "cut-after-alone",
        "cut-inside-central",
        "cut-between",
        "two-members",
    ],
)
async def test_a_zip_that_is_not_the_member_raises(body: bytes, zip_: StreamedZip | None, says: str) -> None:
    with pytest.raises(ValueError, match=says):
        await _read(body, zip_)
