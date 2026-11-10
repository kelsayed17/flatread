import io
import re
import zipfile

import msgspec
import pytest

from flatread.archives import pick, unzip
from flatread.delimited import Csv
from flatread.errors import CorruptArchiveError, UnknownLayoutError


def archive(**members: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def test_unzip_returns_every_member() -> None:
    assert unzip(archive(**{"a.csv": b"1", "dir/b.csv": b"2"})) == {"a.csv": b"1", "dir/b.csv": b"2"}


def test_member_by_name_regex_and_callable() -> None:
    names = ["readme.txt", "data_2026.csv", "notes.csv"]
    assert pick(names, "notes.csv") == "notes.csv"
    assert pick(names, re.compile(r"data_\d{4}\.csv")) == "data_2026.csv"
    assert pick(names, lambda name: name.startswith("read")) == "readme.txt"


def test_no_or_two_matches_raise_naming_the_members() -> None:
    with pytest.raises(UnknownLayoutError, match=r"no member.*'notes.csv'"):
        pick(["notes.csv"], "data.csv")
    with pytest.raises(UnknownLayoutError, match="2 members"):
        pick(["a.csv", "b.csv"], re.compile(r".*\.csv"))


def test_a_corrupt_archive_raises() -> None:
    with pytest.raises(CorruptArchiveError):
        unzip(b"PK\x03\x04 not really")
    with pytest.raises(CorruptArchiveError):
        unzip(archive(**{"a.csv": b"x" * 1000})[:-30])


class Pair(msgspec.Struct):
    a: int
    b: int


def test_a_layout_reads_a_member() -> None:
    class Inner(Csv):
        row = Pair
        labels = {"A": "a", "B": "b"}
        member = re.compile(r"table_\d+\.csv")

    body = archive(**{"readme.txt": b"hi", "table_1.csv": b"A,B\n1,2\n"})
    assert Inner.frame(body).row(0) == (1, 2)
