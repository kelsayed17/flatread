"""A read raises a :class:`ReadError` for a bad body and a :class:`LayoutError` for a reader declared wrong.

The two are siblings, each a ``ValueError``, so a caller can catch bad data alone and still let a misdeclared reader
fail loudly. The suite's ``pytest.raises`` name the kind each error is; these pin the kinds a caller tells apart.
"""

import datetime as dt
from typing import Annotated

import msgspec
import pytest

from flatread.delimited import CsvFile
from flatread.errors import EmptyBodyError, LayoutError, RaggedRowError, ReadError
from flatread.fields import Format, Label
from flatread.fixed import FixedWidthFile
from flatread.markup import XmlFile


class _Row(msgspec.Struct, frozen=True):
    name: Annotated[str, Label("Name")]
    value: Annotated[float, Label("Value")]


def test_both_kinds_are_value_errors_and_neither_is_the_other() -> None:
    assert issubclass(ReadError, ValueError)
    assert issubclass(LayoutError, ValueError)
    assert not issubclass(LayoutError, ReadError)
    assert not issubclass(ReadError, LayoutError)


@pytest.mark.parametrize("body", [b"", b"  \n", b"Name,Value\n"], ids=["no bytes", "whitespace", "a header alone"])
def test_a_body_with_nothing_to_read_is_empty(body: bytes) -> None:
    with pytest.raises(EmptyBodyError):
        CsvFile(_Row).rows(body)


def test_a_short_row_is_ragged() -> None:
    with pytest.raises(RaggedRowError, match="row 2 has 1 fields, not the header's 2"):
        CsvFile(_Row).rows(b"Name,Value\na,1\nb\n")


def test_a_cell_that_is_not_its_type_is_a_read_error() -> None:
    with pytest.raises(ReadError, match="'x'"):
        CsvFile(_Row).rows(b"Name,Value\na,x\n")


def test_every_readers_empty_body_is_empty() -> None:
    class _Fixed(msgspec.Struct):
        name: Annotated[str, Label("N")]

    with pytest.raises(EmptyBodyError):
        XmlFile(_Row, root="rows", record="row").rows(b"")
    with pytest.raises(LayoutError, match="needs a Span"):
        FixedWidthFile(_Fixed)


def test_a_reader_declared_wrong_is_a_layout_error() -> None:
    class Undated(msgspec.Struct):
        day: Annotated[dt.date, Label("Day")]

    class Zoned(msgspec.Struct):
        day: Annotated[dt.date, Label("Day"), Format("%Y", time_zone="UTC")]

    for row in (Undated, Zoned):
        with pytest.raises(LayoutError):
            CsvFile(row)


class _Flagged(msgspec.Struct, frozen=True):
    name: Annotated[str, Label("Name")]
    held: Annotated[bool, Label("Held")]


@pytest.mark.parametrize("word", ["True", "TRUE", "1", "yes"])
def test_a_csv_boolean_is_true_or_false_exactly(word: str) -> None:
    assert CsvFile(_Flagged).rows(b"Name,Held\na,true\nb,false\n") == [
        _Flagged("a", held=True),
        _Flagged("b", held=False),
    ]
    with pytest.raises(ReadError, match=f"^the 'Held' cell '{word}' is not 'true' or 'false': row 2$"):
        CsvFile(_Flagged).rows(f"Name,Held\na,true\nb,{word}\n".encode())


class _Counted(msgspec.Struct, frozen=True):
    count: Annotated[int, Label("Count")]
    value: Annotated[float, Label("Value")]


def test_a_mistyped_csv_cell_is_named_by_its_row() -> None:
    with pytest.raises(ReadError, match=r"^the 'Value' cell '1\.5\.' is not a number: row 3$"):
        CsvFile(_Row).rows(b'Name,Value\na,1\n"b\nc",2\nd,1.5.\n')
    with pytest.raises(ReadError, match=r"^the 'Count' cell '1\.5' is not a whole number: row 2$"):
        CsvFile(_Counted).rows(b"Count,Value\n1,1\n1.5,2\n")
