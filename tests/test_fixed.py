import msgspec
import pytest

from flatread.errors import CellError, TruncatedFileError, UnknownLayoutError
from flatread.fixed import FixedWidth


class Volume(msgspec.Struct, frozen=True):
    symbol: str
    kind: str
    contracts: int


class OccVolume(FixedWidth):
    row = Volume
    columns = {"symbol": (2, 7), "kind": (8, 8), "contracts": (9, 16)}
    record = r"D.{15}"
    skip = r"H.*"
    strip = True


def test_columns_slice_by_spec_positions() -> None:
    body = b"H20261109 header\nDAAPL  C00001234\nDMSFT  P00000007\n"
    assert OccVolume.frame(body).rows() == [("AAPL", "C", 1234), ("MSFT", "P", 7)]


def test_a_line_matching_neither_record_nor_skip_raises() -> None:
    with pytest.raises(UnknownLayoutError, match=r"line 2 .*'XAAPL"):
        OccVolume.frame(b"DAAPL  C00001234\nXAAPL  C00001234\n")


def test_a_bad_cell_names_its_line() -> None:
    with pytest.raises(CellError, match="line 3: the 'contracts' cell '0000x234'"):
        OccVolume.frame(b"H\nDAAPL  C00001234\nDAAPL  C0000x234\n")


def test_a_cut_body_raises() -> None:
    with pytest.raises(TruncatedFileError):
        OccVolume.frame(b"DAAPL  C00001234")


class Reading(msgspec.Struct, frozen=True):
    station: str
    temp: float | None


def test_noaa_minus_9999_is_missing() -> None:
    class Noaa(FixedWidth):
        row = Reading
        columns = {"station": (1, 6), "temp": (7, 13)}
        record = r".{13}"
        strip = True
        missing = frozenset({"", "-9999"})

    frame = Noaa.frame(b"USW001   12.5\r\nUSW002  -9999\r\n")
    assert frame.rows() == [("USW001", 12.5), ("USW002", None)]


def test_columns_must_name_every_field_and_fit() -> None:
    with pytest.raises(TypeError, match="contracts"):

        class Missing(FixedWidth):
            row = Volume
            columns = {"symbol": (1, 6), "kind": (7, 7)}
            record = r".*"

    with pytest.raises(TypeError, match=r"\(5, 2\)"):

        class Backwards(FixedWidth):
            row = Volume
            columns = {"symbol": (5, 2), "kind": (7, 7), "contracts": (8, 9)}
            record = r".*"
