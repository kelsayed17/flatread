"""Tests for ``flatread.frames``: a row struct's frame schema, and a frame read back into typed rows."""

import datetime as dt
from itertools import starmap
from typing import Annotated, Literal, NewType

import msgspec
import polars as pl
import pytest

from flatread.cells import Column, UtcDatetime
from flatread.delimited import read_csv
from flatread.frames import frame_rows, frame_schema, typed_file, typed_rows

_Code = NewType("_Code", str)
_Wrapped = NewType("_Wrapped", _Code)


class _Fields(msgspec.Struct, frozen=True, gc=False):
    code: _Wrapped
    count: int
    price: float | None
    flag: bool
    day: dt.date | None
    side: Literal["S", "B"]
    stated: UtcDatetime | None
    unstated: dt.datetime


class _Unmapped(msgspec.Struct, frozen=True, gc=False):
    tags: list[str]


class _Either(msgspec.Struct, frozen=True, gc=False):
    key: int | str


def test_each_field_is_its_types_column() -> None:
    """A NewType is its supertype's column, however deep; ``X | None`` is ``X``'s; a ``Literal`` is an ``Enum``."""
    assert frame_schema(_Fields) == pl.Schema(
        {
            "code": pl.String,
            "count": pl.Int64,
            "price": pl.Float64,
            "flag": pl.Boolean,
            "day": pl.Date,
            "side": pl.Enum(["B", "S"]),
            "stated": pl.Datetime("us", "UTC"),
            "unstated": pl.Datetime("us"),
        }
    )


@pytest.mark.parametrize(
    ("row_type", "named"), [(_Unmapped, "list"), (_Either, r"int \| str")], ids=["a list", "a union of two"]
)
def test_a_field_with_no_column_type_raises(row_type: type[msgspec.Struct], named: str) -> None:
    with pytest.raises(TypeError, match="no frame column type for a field annotated " + named):
        frame_schema(row_type)


def test_a_utc_datetime_refuses_a_naive_moment() -> None:
    with pytest.raises(msgspec.ValidationError, match="timezone"):
        msgspec.convert({"at": "2026-01-02T03:04:05"}, msgspec.defstruct("At", [("at", UtcDatetime)]))


class _Row(msgspec.Struct, frozen=True, gc=False):
    day: dt.date
    at: UtcDatetime | None
    name: str
    value: float | None


def test_frame_rows_builds_the_rows_iter_rows_does() -> None:
    frame = pl.DataFrame(
        {
            "day": [dt.date(2026, 1, 2), dt.date(2026, 1, 5)],
            "at": [dt.datetime(2026, 1, 2, 16, tzinfo=dt.UTC), None],
            "name": ["a", "b"],
            "value": [1.5, None],
        },
        schema=frame_schema(_Row),
    )
    assert frame_rows(frame, _Row) == list(starmap(_Row, frame.iter_rows()))
    assert frame_rows(frame.clear(), _Row) == []


class _Threshold(msgspec.Struct, frozen=True, gc=False):
    symbol: Annotated[str, Column("Symbol")]
    flag: Annotated[Literal["Y", "N"], Column("Flag")]
    filler_1: Annotated[str, Column("Filler")]
    filler_2: Annotated[str, Column("Filler")]
    note: Annotated[str | None, Column(("Note", "Remark"), missing=("",), optional=True)]


_BODY = b"Symbol|Flag|Filler|Filler|Note\nACYQ|Y|||x\nZZZ|N|a|b|\n"
_ROWS = [_Threshold("ACYQ", "Y", "", "", "x"), _Threshold("ZZZ", "N", "a", "b", None)]


def test_a_csv_frame_reads_back_into_its_typed_rows_by_label() -> None:
    assert typed_rows(read_csv(_BODY, _Threshold, separator="|"), _Threshold) == _ROWS


def test_a_sheets_repeat_names_and_an_alternate_label_read_back_too() -> None:
    frame = pl.DataFrame({"Symbol": ["A"], "Flag": ["Y"], "Filler": ["x"], "Filler_1": ["y"], "Remark": ["r"]})
    assert typed_rows(frame, _Threshold) == [_Threshold("A", "Y", "x", "y", "r")]


def test_an_optional_column_the_file_left_out_reads_back_as_none() -> None:
    frame = read_csv(b"Symbol|Flag|Filler|Filler\nA|Y|x|y\n", _Threshold, separator="|")
    assert typed_rows(frame, _Threshold) == [_Threshold("A", "Y", "x", "y", None)]


def test_a_frame_without_a_required_column_cannot_read_back() -> None:
    with pytest.raises(ValueError, match=r"no column for _Threshold\.symbol \('Symbol'\)"):
        typed_rows(pl.DataFrame({"Flag": ["Y"]}), _Threshold)


class _Priced(msgspec.Struct, frozen=True, gc=False):
    symbol: Annotated[str, Column("Symbol")]
    price: Annotated[float | None, Column("Price", missing=("",))]


def test_a_frame_without_a_column_that_may_be_none_but_is_not_optional_cannot_read_back() -> None:
    with pytest.raises(ValueError, match=r"no column for _Priced\.price \('Price'\)"):
        typed_rows(pl.DataFrame({"Symbol": ["A"]}), _Priced)


class _File(msgspec.Struct, frozen=True, gc=False):
    generated_at: dt.datetime
    rows: pl.DataFrame


class _TypedFile(msgspec.Struct, frozen=True, gc=False):
    generated_at: dt.datetime
    securities: list[_Threshold]


def test_a_file_struct_reads_back_into_its_typed_twin() -> None:
    file = _File(dt.datetime(2026, 7, 17, 22, 2, 1), read_csv(_BODY, _Threshold, separator="|"))
    assert typed_file(file, _TypedFile) == _TypedFile(dt.datetime(2026, 7, 17, 22, 2, 1), _ROWS)


def test_a_file_struct_of_no_frame_has_no_typed_twin() -> None:
    with pytest.raises(TypeError, match="holds 0 DataFrames, _TypedFile 1 row lists"):
        typed_file(_TypedFile(dt.datetime(2026, 1, 1), []), _TypedFile)
