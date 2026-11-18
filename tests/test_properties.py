"""Properties over generated bodies: a read returns the rows written, or raises ``ReadError``, and nothing else.

The README promises that no other exception escapes a read. The other suites test that on bodies picked by hand; here
hypothesis generates them: tables written by Python's :mod:`csv` and as workbooks, read back exactly; those tables cut,
flipped and spliced; and bytes of no format at all. ``flatread.next`` and the 7.x readers are held to the same rows.
"""

import contextlib
import csv
import io
import math
import os
import re
import zipfile
from typing import TYPE_CHECKING, Annotated

import msgspec
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from flatread import next as fr
from flatread.delimited import CsvFile
from flatread.errors import ReadError
from flatread.fields import Label
from flatread.markup import XmlFile
from flatread.pages import HtmlFile
from flatread.sheets import SheetFile
from tests.support.xlsx import workbook

if TYPE_CHECKING:
    from collections.abc import Callable

# Each property reads 100 small bodies, the same ones on every run (derandomize), so CI is repeatable; set
# FLATREAD_PROPERTY_EXAMPLES to read more, each run drawing new ones.
_EXAMPLES = int(os.environ.get("FLATREAD_PROPERTY_EXAMPLES", "0"))
_SETTINGS = settings(
    max_examples=_EXAMPLES or 100,
    derandomize=not _EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


class _Row(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("Name")]
    count: Annotated[int | None, Label("Count")]
    share: Annotated[float | None, Label("Share")]
    held: Annotated[bool | None, Label("Held")]


_LABELS = ["Name", "Count", "Share", "Held"]

# Text a publisher writes in a cell: any printable character and a quote or separator among them, no line ending, no
# cell of spaces alone (a reader reads one as empty) and none with spaces around it (a sheet keeps them, CSV too).
_TEXT = st.text(st.characters(codec="utf-8", exclude_categories=("Cc", "Cs", "Zl", "Zp")), min_size=1, max_size=12)
_NAMES = _TEXT.filter(lambda text: text.strip() == text)
_ROWS = st.lists(
    st.builds(
        _Row,
        name=_NAMES,
        count=st.none() | st.integers(-(2**63), 2**63 - 1),
        share=st.none() | st.floats(allow_nan=False, allow_infinity=False, width=64),
        held=st.none() | st.booleans(),
    ),
    min_size=1,
    max_size=8,
)


def _csv(rows: list[_Row]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(_LABELS)
    for row in rows:
        writer.writerow(
            [
                row.name,
                "" if row.count is None else row.count,
                "" if row.share is None else repr(row.share),
                "" if row.held is None else str(row.held).lower(),
            ]
        )
    return out.getvalue().encode()


def _same(read: list[_Row], written: list[_Row]) -> bool:
    """Return whether *read* is *written*, a float the same float (``-0.0`` read as ``0.0`` too)."""
    return len(read) == len(written) and all(
        (a.name, a.count, a.held) == (b.name, b.count, b.held)
        and (a.share == b.share or (a.share is not None and b.share is not None and math.isclose(a.share, b.share)))
        for a, b in zip(read, written, strict=True)
    )


@_SETTINGS
@given(rows=_ROWS, order=st.permutations(range(len(_LABELS))))
def test_a_csv_named_in_any_order_reads_back_the_rows_written(rows: list[_Row], order: list[int]) -> None:
    # The same file, its columns in another order: Named reads it as the struct's.
    lines = list(csv.reader(io.StringIO(_csv(rows).decode())))
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows([line[at] for at in order] for line in lines)
    named = fr.Reader(_Row, fr.Csv(), table=fr.Header(header=fr.Named()))
    assert _same(named.rows(out.getvalue().encode()), rows)


_READERS: dict[str, Callable[[bytes], object]] = {
    "CsvFile": CsvFile(_Row).rows,
    "flatread.next Csv": fr.Reader(_Row, fr.Csv()).rows,
}


@pytest.mark.parametrize("reader", _READERS)
@_SETTINGS
@given(rows=_ROWS)
def test_a_csv_reads_back_the_rows_written(reader: str, rows: list[_Row]) -> None:
    assert _same(_READERS[reader](_csv(rows)), rows)  # ty: ignore[invalid-argument-type] (a list of _Row)


# A number cell is a double: a whole number past 2**53 is no number cell's (Excel holds 15 digits).
_SHEET_ROWS = _ROWS.filter(lambda rows: all(row.count is None or abs(row.count) < 2**53 for row in rows))


@_SETTINGS
@given(rows=_SHEET_ROWS)
def test_a_sheet_reads_back_the_rows_written(rows: list[_Row]) -> None:
    body = workbook({"S": [_LABELS, *([row.name, row.count, row.share, row.held] for row in rows)]})
    assert _same(SheetFile(_Row, sheet="S").rows(body), rows)
    assert _same(fr.Reader(_Row, fr.Sheet(sheet="S")).rows(body), rows)


def _damaged(body: bytes, data: st.DataObject) -> bytes:
    """Return *body* cut, a byte of it changed, or a run of other bytes spliced in, as hypothesis picks."""
    at = data.draw(st.integers(0, len(body)))
    damage = data.draw(st.sampled_from(["cut", "flip", "splice"]))
    if damage == "cut":
        return body[:at]
    if damage == "flip" and at < len(body):
        return body[:at] + bytes([body[at] ^ data.draw(st.integers(1, 255))]) + body[at + 1 :]
    return body[:at] + data.draw(st.binary(max_size=8)) + body[at:]


def _reads_or_refuses(read: Callable[[bytes], object], body: bytes) -> None:
    """Read *body*: rows or a ``ReadError``, and no other exception (a ``LayoutError`` is a reader's bug here)."""
    with contextlib.suppress(ReadError):
        read(body)


@pytest.mark.parametrize("reader", _READERS)
@_SETTINGS
@given(rows=_ROWS, data=st.data())
def test_a_damaged_csv_reads_or_raises_read_error(reader: str, rows: list[_Row], data: st.DataObject) -> None:
    _reads_or_refuses(_READERS[reader], _damaged(_csv(rows), data))


@_SETTINGS
@given(rows=_ROWS, data=st.data())
def test_both_csv_readers_read_a_damaged_csv_alike(rows: list[_Row], data: st.DataObject) -> None:
    body = _damaged(_csv(rows), data)
    outcomes = []
    for read in _READERS.values():
        try:
            outcomes.append(read(body))
        except ReadError:
            outcomes.append(None)
    assert outcomes[0] == outcomes[1]


_SHEETS: dict[str, Callable[[bytes], object]] = {
    "SheetFile": SheetFile(_Row, sheet="S").rows,
    "flatread.next Sheet": fr.Reader(_Row, fr.Sheet(sheet="S")).rows,
}


@pytest.mark.parametrize("reader", _SHEETS)
@_SETTINGS
@given(rows=_ROWS, data=st.data())
def test_a_damaged_workbook_reads_or_raises_read_error(reader: str, rows: list[_Row], data: st.DataObject) -> None:
    body = workbook({"S": [_LABELS, *([row.name, row.count, row.share, row.held] for row in rows)]})
    _reads_or_refuses(_SHEETS[reader], _damaged(body, data))


@pytest.mark.parametrize("reader", _SHEETS)
@_SETTINGS
@given(rows=_ROWS, data=st.data())
def test_a_workbook_with_a_damaged_part_reads_or_raises_read_error(
    reader: str, rows: list[_Row], data: st.DataObject
) -> None:
    # The zip opens; a part inside it is cut or changed, which calamine and the span scan read.
    body = workbook({"S": [_LABELS, *([row.name, row.count, row.share, row.held] for row in rows)]})
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    name = data.draw(st.sampled_from(sorted(parts)))
    parts[name] = _damaged(parts[name], data)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for part, content in parts.items():
            archive.writestr(part, content)
    _reads_or_refuses(_SHEETS[reader], out.getvalue())


class _Long(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("Name")]
    field: str
    value: str | None


_CUTS: dict[str, Callable[[bytes], object]] = {
    "flatread.next Cut": fr.Reader(_Row, fr.Csv(), table=fr.Cut(ends="blank", header=fr.Named())).rows,
    "flatread.next Cut, misquoted and found": fr.Reader(
        _Row, fr.Csv(misquoted=True), table=fr.Cut(filled=(2, 1), full_rows=True), empty=True
    ).rows,
    "flatread.next Found": fr.Reader(
        _Row, fr.Csv(), table=fr.Found(key="name", section="# ", section_field="held")
    ).rows,
    "flatread.next Found, open and read long": fr.Reader(
        _Long, fr.Csv(), table=fr.Found(key="name", open=True), shape=fr.Long(keys=re.compile(".+"))
    ).rows,
    "flatread.next Cut on a sheet": fr.Reader(
        _Row, fr.Sheet(sheet="S"), table=fr.Cut(header=fr.Headers(lacking=[(), ("held",)]))
    ).rows,
}


@pytest.mark.parametrize("reader", _CUTS)
@_SETTINGS
@given(rows=_ROWS, data=st.data())
def test_a_damaged_grid_reads_or_raises_read_error(reader: str, rows: list[_Row], data: st.DataObject) -> None:
    body = (
        _csv(rows)
        if "sheet" not in reader
        else workbook({"S": [_LABELS, *([row.name, row.count, row.share, row.held] for row in rows)]})
    )
    _reads_or_refuses(_CUTS[reader], _damaged(body, data))


class _Record(msgspec.Struct, frozen=True, gc=False):
    name: Annotated[str, Label("name")]
    count: Annotated[int | None, Label("count")]


class _Keyed(msgspec.Struct, frozen=True, gc=False):
    name: str
    year: int
    count: int | None


_ANY: dict[str, Callable[[bytes], object]] = {
    **_READERS,
    **_SHEETS,
    **_CUTS,
    "flatread.next Report": fr.Reader(
        _Record,
        fr.Text(),
        table=fr.Report(
            line=re.compile(r"(?P<name>\S+) +(?P<count>\S*)"),
            header_lines=[re.compile("NAME +COUNT")],
            framing=[re.compile("-+")],
        ),
        empty=True,
    ).rows,
    "flatread.next Report, keyed": fr.Reader(
        _Keyed,
        fr.Text(splitlines=True),
        table=fr.Report(line=re.compile(r"(?P<name>\S+) +(?P<count>.*)"), framing=[re.compile(r"NAME +(?P<year>.*)")]),
        shape=fr.Keyed(key_field="year"),
    ).rows,
    "XmlFile": XmlFile(_Record, root="t", record="r").rows,
    "HtmlFile": HtmlFile(_Record).rows,
}


@pytest.mark.parametrize("reader", _ANY)
@_SETTINGS
@given(body=st.binary(max_size=200) | _TEXT.map(str.encode))
def test_bytes_of_no_format_read_or_raise_read_error(reader: str, body: bytes) -> None:
    _reads_or_refuses(_ANY[reader], body)


@pytest.mark.parametrize("reader", ["XmlFile", "HtmlFile"])
@_SETTINGS
@given(names=st.lists(_NAMES, min_size=1, max_size=5), data=st.data())
def test_damaged_markup_reads_or_raises_read_error(reader: str, names: list[str], data: st.DataObject) -> None:
    escaped = [name.replace("&", "&amp;").replace("<", "&lt;") for name in names]
    if reader == "XmlFile":
        body = "<t>" + "".join(f"<r><name>{n}</name><count>1</count></r>" for n in escaped) + "</t>"
    else:
        rows = "".join(f"<tr><td>{n}</td><td>1</td></tr>" for n in escaped)
        body = f"<table><tr><th>name</th><th>count</th></tr>{rows}</table>"
    _reads_or_refuses(_ANY[reader], _damaged(body.encode(), data))
