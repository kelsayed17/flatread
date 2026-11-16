"""A reader declared to read a file otherwise than it says is refused when built, and a fault keeps its kind.

Each case read a body without a word before 7.2: a misspelled switch, a switch set to no value its piece takes, a
msgspec constraint no reader checks, a keyword-only struct, a ``Flag`` whose words are one, a separator no reader
splits by. Then the error kinds a fixed-width row and an XML body raise, as a CSV's do.
"""

from typing import Annotated, ClassVar

import msgspec
import pytest

from flatread.delimited import CsvFile, CsvText
from flatread.errors import EncodingError, LayoutError, RaggedRowError, ReadError, TruncatedFileError
from flatread.fields import Flag, Label, Span
from flatread.fixed import FixedWidthFile
from flatread.markup import XmlFile


class _Row(msgspec.Struct, frozen=True):
    name: Annotated[str, Label("Name")]


class _Misspelt(CsvFile[_Row]):
    _quotechar = "'"


class _Swapped(CsvFile[_Row]):
    _headed = False


def test_a_misspelled_switch_is_refused_naming_the_switch_it_misspells() -> None:
    with pytest.raises(
        LayoutError,
        match=(
            r"^the _Misspelt format sets _quotechar, which none of its pieces reads: is it \['_quote_char'\]\?"
            r" \(annotate _quotechar in _Misspelt to keep a value of the format's own under that name\)$"
        ),
    ):
        _Misspelt(_Row)
    with pytest.raises(LayoutError, match=r"sets _headed, which none of its pieces reads: is it \['_header'\]\?"):
        _Swapped(_Row)


class _Annotated(CsvFile[_Row]):
    _quotechar: ClassVar[str] = "'"


class _OwnSetting(CsvFile[_Row]):
    _publisher = "Cboe"
    _QUOTED: ClassVar[bool] = True

    def _quotes(self) -> str:  # a method of the format's own is no switch, however it is named
        return self._publisher


def test_a_name_the_format_annotates_or_that_misspells_no_switch_is_its_own() -> None:
    assert _Annotated(_Row).rows(b"Name\na\n") == [_Row("a")]
    assert _OwnSetting(_Row).rows(b"Name\na\n") == [_Row("a")]


class _Sometimes(CsvFile[_Row]):
    _closing_separator = "never"  # ty: ignore[invalid-assignment]


class _Worded(CsvFile[_Row]):
    _header = "no"  # ty: ignore[invalid-assignment]


def test_a_switch_set_to_a_value_its_piece_does_not_take_is_refused() -> None:
    with pytest.raises(
        LayoutError,
        match=r"^the _Sometimes format's _closing_separator is 'never', not one of \['always', 'sometimes', None\]$",
    ):
        _Sometimes(_Row)
    with pytest.raises(LayoutError, match=r"^the _Worded format's _header is 'no', not a bool$"):
        _Worded(_Row)


class _Bounded(msgspec.Struct, frozen=True):
    value: Annotated[int, Label("Value"), msgspec.Meta(ge=0)]


class _BoundedOptional(msgspec.Struct, frozen=True):
    code: Annotated[str, Label("Code"), msgspec.Meta(max_length=2)] | None


class _Described(msgspec.Struct, frozen=True):
    value: Annotated[int, Label("Value"), msgspec.Meta(description="the count")]


def test_a_msgspec_constraint_no_reader_checks_is_refused() -> None:
    with pytest.raises(LayoutError, match=r"^the field _Bounded.value's msgspec Meta names \['ge'\], which a reader"):
        CsvFile(_Bounded)
    with pytest.raises(LayoutError, match=r"_BoundedOptional.code's msgspec Meta names \['max_length'\]"):
        CsvFile(_BoundedOptional)
    assert CsvFile(_Described).rows(b"Value\n3\n") == [_Described(3)]


class _KeywordOnly(msgspec.Struct, frozen=True, kw_only=True):
    name: Annotated[str, Label("Name")]


def test_a_keyword_only_struct_is_refused_when_built_not_when_read() -> None:
    with pytest.raises(LayoutError, match=r"^the fields \['name'\] of _KeywordOnly are keyword-only, and a reader"):
        CsvFile(_KeywordOnly)


class _OneWord(msgspec.Struct, frozen=True):
    ok: Annotated[bool, Label("OK"), Flag("Y", "Y")]


def test_a_flag_whose_words_are_one_is_refused() -> None:
    with pytest.raises(LayoutError, match=r"^the field 'ok''s Flag writes true and false both as 'Y', which tells"):
        CsvFile(_OneWord)


@pytest.mark.parametrize(
    ("separator", "message"),
    [
        ("||", r"^the CsvFile format's separator is '\|\|', not one character of one byte that ends no line$"),
        ("", r"^the CsvFile format's separator is '', not one character"),
        ("\n", r"^the CsvFile format's separator is '\\n', not one character"),
        ("é", r"^the CsvFile format's separator is 'é', not one character of one byte"),
        ('"', r"^the CsvFile format's separator '\"' is its _quote_char too, which tells no cell's end$"),
    ],
    ids=["two", "none", "a line ending", "two bytes", "the quote"],
)
def test_a_separator_no_csv_reader_splits_by_is_refused_when_built(separator: str, message: str) -> None:
    with pytest.raises(LayoutError, match=message):
        CsvFile(_Row, separator=separator)


class _TwoQuotes(CsvFile[_Row]):
    _quote_char = "''"


def test_a_quote_char_of_more_than_one_character_is_refused() -> None:
    with pytest.raises(LayoutError, match=r"^the _TwoQuotes format's _quote_char is \"''\", not one character"):
        _TwoQuotes(_Row)


def test_a_text_grid_takes_any_one_character_and_refuses_more() -> None:
    assert CsvText(_Row, separator="é").rows(b"Name\na\n") == [_Row("a")]
    with pytest.raises(LayoutError, match=r"^the CsvText format's separator is '\|\|', not one character that ends"):
        CsvText(_Row, separator="||")


class _Fixed(msgspec.Struct, frozen=True):
    name: Annotated[str, Label("Name"), Span(0, 4)]


def test_a_fixed_width_line_of_another_width_is_ragged() -> None:
    with pytest.raises(RaggedRowError, match=r"^row 2 'ab' is not 4 characters, spaces between its columns$"):
        FixedWidthFile(_Fixed).rows(b"Name\nabcd\nab\n")


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (b"<rows><row><Name>a</Name></row><row><Name>b", TruncatedFileError),
        (b"<rows><row><Name>a</Name></row></rows", TruncatedFileError),
        (b"<rows><row><Name>\xff</Name></row></rows>", EncodingError),
        (b'<?xml version="1.0" encoding="bogus"?><rows/>', EncodingError),
    ],
    ids=["inside an element", "at its last byte", "bytes", "a charset"],
)
def test_an_xml_body_cut_or_not_its_encoding_keeps_its_kind(body: bytes, kind: type[ReadError]) -> None:
    with pytest.raises(kind, match=r"^the body is not well-formed XML: "):
        XmlFile(_Row, root="rows", record="row").rows(body)


def test_any_other_malformed_xml_is_a_plain_read_error() -> None:
    with pytest.raises(ReadError, match=r"^the body is not well-formed XML: Opening and ending tag mismatch") as raised:
        XmlFile(_Row, root="rows", record="row").rows(b"<rows><row><Name>a</Other></row></rows>")
    assert type(raised.value) is ReadError


class _Coded(msgspec.Struct, frozen=True):
    code: int
    name: Annotated[str, Label("Name")]


class _Fractional(CsvFile[_Coded]):
    _given = {"code": 3.7}


class _Whole(CsvFile[_Coded]):
    _given = {"code": 3}


def test_a_given_value_not_of_its_fields_type_is_refused_not_coerced() -> None:
    with pytest.raises(LayoutError, match=r"^the format gives _Coded.code 3.7, which is no value of its type: "):
        _Fractional(_Coded)
    assert _Whole(_Coded).rows(b"Name\na\n") == [_Coded(3, "a")]
