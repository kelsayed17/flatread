"""A ``LayoutError`` raised inside a read stays a ``LayoutError``: no ``except ValueError`` relabels it a ``ReadError``.

Both are ``ValueError``, so each place that turns a ``ValueError`` into a ``ReadError`` naming where it went wrong
lets a ``LayoutError`` through first. None of these places raises one today; each test makes the step it guards raise
one, as a reader declared wrong would.
"""

import datetime as dt
import io
import re
import zipfile
from typing import Annotated, NoReturn

import msgspec
import pytest

import flatread.archives
import flatread.fields
import flatread.layout
from flatread.archives import ZippedFiles
from flatread.delimited import CsvFile
from flatread.errors import LayoutError
from flatread.fields import Format, Label
from flatread.layout import LongFile
from flatread.pages import HtmlFile
from flatread.sheets import SheetFile
from tests.support.xlsx import workbook


def _misdeclared(*_: object) -> NoReturn:
    msg = "the reader is declared wrong"
    raise LayoutError(msg)


class _Monthly(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("NAME")]
    month: Annotated[dt.date, Format("%Y-%m")]
    value: float


class _MonthlyCsv(LongFile[_Monthly], CsvFile[_Monthly]):
    _keys = re.compile(r"\d{4}-\d{2}")


def test_a_long_files_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flatread.layout, "_typed", _misdeclared)
    with pytest.raises(LayoutError, match=r"^the reader is declared wrong$"):
        _MonthlyCsv(_Monthly).rows(b"NAME,2026-01\na,1\n")


class _Daily(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Format("%Y%m%d")]
    name: Annotated[str, Label("NAME")]


class _Month(ZippedFiles[_Daily], CsvFile[_Daily]):
    _member = re.compile(r"day(?P<day>\d{8})\.csv")


def test_a_zipped_members_stated_field(monkeypatch: pytest.MonkeyPatch) -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("day20261105.csv", b"NAME\na\n")
    monkeypatch.setattr(flatread.archives, "_stated", _misdeclared)
    with pytest.raises(LayoutError, match=r"^the reader is declared wrong$"):
        _Month(_Daily).rows(out.getvalue())


class _Quote(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("Name")]
    rate: Annotated[float | None, Label("Rate")]


def test_an_html_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(HtmlFile, "_value", _misdeclared)
    with pytest.raises(LayoutError, match=r"^the reader is declared wrong$"):
        HtmlFile(_Quote).rows(b"<table><tr><th>Name</th><th>Rate</th></tr><tr><td>A</td><td>1</td></tr></table>")


class _Dated(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Label("Day"), Format("%m/%d/%Y")]


class _StatedDay(msgspec.Struct, frozen=True, gc=False):
    day: Annotated[dt.date, Format("%m/%d/%Y"), flatread.fields.Cell("B1")]
    name: Annotated[str, Label("Name")]


def test_a_sheets_text_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SheetFile, "_value", _misdeclared)
    with pytest.raises(LayoutError, match=r"^the reader is declared wrong$"):
        SheetFile(_Dated, sheet="S").rows(workbook({"S": [["Day"], ["01/02/2026"]]}))


def test_a_sheets_stated_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SheetFile, "_value", _misdeclared)
    with pytest.raises(LayoutError, match=r"^the reader is declared wrong$"):
        SheetFile(_StatedDay, sheet="S", header_row=2).rows(workbook({"S": [[None, "01/02/2026"], ["Name"], ["a"]]}))
