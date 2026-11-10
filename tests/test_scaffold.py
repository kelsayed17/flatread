import datetime as dt
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

import flatread
from flatread.scaffold import field_names, scaffold

if TYPE_CHECKING:
    import pathlib

SAMPLE = b"Date,GDP Growth (%),Rate,Code,Quarter\n2026-11-09,1.5,.,A,2024Q1\n2026-11-10,2,3.25,B,2024Q2\n"


def run(source: str) -> dict[str, object]:
    namespace: dict[str, object] = {}
    exec(compile(source, "<scaffold>", "exec"), namespace)  # noqa: S102  # the scaffold's own output
    return namespace


def test_scaffold_csv_output_compiles_and_reads_its_own_sample() -> None:
    source = scaffold(SAMPLE, format="csv", name="Growth")
    namespace = run(source)
    layout = namespace["Growth"]
    assert isinstance(layout, type)
    assert issubclass(layout, flatread.Csv)
    frame = layout.frame(SAMPLE)
    assert frame.columns == ["date", "gdp_growth", "rate", "code", "quarter"]
    assert frame.row(0) == (dt.date(2026, 11, 9), 1.5, None, "A", dt.date(2024, 1, 1))
    assert "rate: float | None" in source
    assert 'missing = frozenset({"", "."})' in source


def test_a_pipe_and_a_json_sample_scaffold() -> None:
    pipe = scaffold(b"Symbol|Shares\nAAPL|100\n", format="pipe", name="Holdings")
    assert run(pipe)["Holdings"].frame(b"Symbol|Shares\nAAPL|100\n").row(0) == ("AAPL", 100)  # ty: ignore[unresolved-attribute]
    body = b'{"data": [{"sym": "A", "px": 1.5, "n": 3}, {"sym": "B", "px": null, "n": 4}]}'
    source = scaffold(body, format="json", name="Quotes", records="data")
    assert run(source)["Quotes"].frame(body).rows() == [("A", 1.5, 3), ("B", None, 4)]  # ty: ignore[unresolved-attribute]


def test_names_are_snake_case_and_unique() -> None:
    assert field_names(["Date", "GDP Growth (%)", "1st Rate", "Filler", "Filler", "class", ""]) == [
        "date",
        "gdp_growth",
        "n_1st_rate",
        "filler",
        "filler_2",
        "class_",
        "column",
    ]


def test_cli_prints_the_source(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "rates.csv"
    path.write_bytes(SAMPLE)
    done = subprocess.run(  # noqa: S603  # the interpreter running the suite, on a file it wrote
        [sys.executable, "-m", "flatread", "scaffold", str(path), "--format", "csv"],
        capture_output=True,
        check=True,
        text=True,
    )
    assert "class Rates(flatread.Csv):" in done.stdout


def test_an_unknown_format_raises() -> None:
    with pytest.raises(ValueError, match="fixed"):
        scaffold(SAMPLE, format="fixed", name="X")  # ty: ignore[invalid-argument-type]
