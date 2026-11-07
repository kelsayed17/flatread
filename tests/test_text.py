"""Tests for ``flatread.text`` and ``flatread.errors``: a body's text, the lines a file writes about itself, and
``or_not_published``.
"""

import codecs
from dataclasses import dataclass

import pytest

from flatread.errors import EncodingError, NotPublishedError, TruncatedFileError, UnknownLayoutError, or_not_published
from flatread.text import decode_text, decode_utf8, split_lines


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


def test_decode_utf8_takes_off_a_byte_order_mark_and_refuses_any_other_encoding() -> None:
    assert decode_utf8(codecs.BOM_UTF8 + "é\n".encode()) == "é\n"
    with pytest.raises(EncodingError, match="the file is not UTF-8"):
        decode_utf8("é\n".encode("latin-1"))


@pytest.mark.parametrize(
    ("text", "head", "tail", "expected"),
    [
        ("H\na\nb\nT\n", 1, 1, (["H"], "a\nb", ["T"])),
        ("H\r\na\r\nT\r\n", 1, 1, (["H"], "a", ["T"])),
        ("\nH\n\na\n\nT\n\n", 1, 1, (["H"], "\na\n", ["T"])),
        ("a\nb\nT", 0, 1, ([], "a\nb", ["T"])),
        ("H\nT\n", 1, 1, (["H"], "", ["T"])),
        ("a\nb\n", 0, 0, ([], "a\nb\n", [])),
    ],
    ids=["a head and a tail", "CRLF", "blank lines are not counted", "no final line ending", "no rows", "neither"],
)
def test_split_lines_takes_the_files_own_lines_off_its_rows(
    text: str, head: int, tail: int, expected: tuple[list[str], str, list[str]]
) -> None:
    assert split_lines(text, head=head, tail=tail) == expected


def test_split_lines_refuses_a_file_of_fewer_lines_than_it_writes_about_itself() -> None:
    with pytest.raises(TruncatedFileError, match="the file has 1 lines, fewer than the 2"):
        split_lines("H\n\n", head=1, tail=1)


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
