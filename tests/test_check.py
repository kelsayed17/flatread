import datetime as dt

import msgspec
import pytest

from flatread.check import diff, report
from flatread.core import Erratum
from flatread.delimited import Csv


class Obs(msgspec.Struct, frozen=True):
    day: dt.date
    value: float
    count: int


class Observations(Csv):
    row = Obs
    labels = {"Date": "day", "Value": "value", "Count": "count"}
    key = ("day",)


def test_report_counts_every_bad_cell_per_column() -> None:
    body = b"Date,Value,Count\n2026-11-09,x,1\n2026-11-10,y,2\n2026-11-11,1,z\n2026-11-12,w,3\n2026-11-13,v,4\n"
    found = report(Observations, body)
    assert not found.ok
    assert found.structure is None
    assert found.cells["value"].count == 4
    assert found.cells["value"].first == (("line 2", "x"), ("line 3", "y"), ("line 5", "w"))
    assert found.cells["count"].count == 1
    assert "day" not in found.cells
    assert "Value: 4 bad cells" in str(found)


def test_a_clean_body_reports_ok() -> None:
    found = report(Observations, b"Date,Value,Count\n2026-11-09,1,1\n")
    assert found.ok
    assert str(found) == "ok: 1 rows"


def test_report_stops_at_a_structure_fault() -> None:
    found = report(Observations, b"Day,Value,Count\n2026-11-09,1,1\n")
    assert not found.ok
    assert found.structure is not None
    assert "the header" in found.structure
    assert found.cells == {}


def test_report_lists_errata_gone() -> None:
    class Fixed(Observations):
        errata = (
            Erratum("Value", "1,5", "1.5", "a comma for a point"),
            Erratum("Count", "two", "2", "a number in words"),
        )

    found = report(Fixed, b"Date,Value,Count\n2026-11-09,1.5,two\n")
    assert [e.why for e in found.errata_gone] == ["a comma for a point"]
    assert found.cells == {}
    assert not found.ok


OLD = b"Date,Value,Count\n2026-11-09,1,1\n2026-11-10,2,2\n2026-11-11,3,3\n"
NEW = b"Date,Value,Count\n2026-11-10,2.5,2\n2026-11-11,3,3\n2026-11-12,4,4\n"


def test_diff_finds_added_removed_changed_by_key() -> None:
    found = diff(Observations, OLD, NEW)
    assert found.added["day"].to_list() == [dt.date(2026, 11, 12)]
    assert found.removed["day"].to_list() == [dt.date(2026, 11, 9)]
    assert found.changed.rows() == [(dt.date(2026, 11, 10), "value", "2.0", "2.5")]
    assert found.header is None


def test_diff_names_a_header_change() -> None:
    class Both(Observations):
        variants = ({"Day": "day", "Value": "value", "Count": "count"},)

    found = diff(Both, OLD, NEW.replace(b"Date,", b"Day,"))
    assert found.header == (("Date", "Value", "Count"), ("Day", "Value", "Count"))


def test_diff_without_key_raises_type_error() -> None:
    class Keyless(Observations):
        key = ()

    with pytest.raises(TypeError, match="key"):
        diff(Keyless, OLD, NEW)
