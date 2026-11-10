"""Time flatread v1 layouts against prism-clients' hand-written polars readers on the same recorded bodies.

Usage: ``python benchmarks/bench.py <prism-clients tree>``, run in prism-clients' environment with this checkout
installed (``uv run --frozen --with-editable <flatread> python benchmarks/bench.py <tree>`` from prism-clients). The
tree is an export of prism-clients' ``origin/main`` holding ``src/``, ``tests/support/`` and the cassettes named below;
it goes first on ``sys.path`` so the readers and the cassette helper import from it. Cassettes are never copied here.

Each case reads one body both ways, best of 7 (the machine is shared, so compare the ratio, not the times), and
checks the two frames ``.equals()``. Where a hand-written reader runs a provider check, the v1 layout runs the same
check in its ``check`` or ``cells`` hook. The datasets v1 cannot express without a hook that re-implements the format
are listed at the end as gaps, not timed.
"""

import pathlib
import re
import sys
import time
from typing import TYPE_CHECKING, override

TREE = pathlib.Path(sys.argv[1]).resolve()
sys.path[:0] = [str(TREE / "src"), str(TREE)]

import polars as pl  # noqa: E402
from prism.clients.atlanta_fed import decoders as atlanta  # noqa: E402
from prism.clients.atlanta_fed.models import HoamAreaRow, HoamCountyRow  # noqa: E402
from prism.clients.chicago_fed import decoders as chicago  # noqa: E402
from prism.clients.chicago_fed.models import ContributionRow  # noqa: E402
from prism.clients.frbsf import decoders as frbsf  # noqa: E402
from prism.clients.frbsf.models import RateDistributionRow, TermModelRow  # noqa: E402
from prism.clients.freddie_mac import decoders as freddie  # noqa: E402
from prism.clients.freddie_mac.models import HpiRow, PmmsRow  # noqa: E402
from tests.support.cassettes import endpoint_body  # noqa: E402

import flatread  # noqa: E402
from flatread import core  # noqa: E402
from flatread.errors import CheckError, UnknownLayoutError  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Callable

RUNS = 7

# --- Freddie Mac ----------------------------------------------------------------------------------------------------


class FreddieMac(flatread.Csv):
    missing = frozenset({"", "."})


class Fmhpi(FreddieMac):
    """``fmhpi_master_file.csv`` whole: ``freddie_mac.decoders._read_table(body, _FMHPI)``."""

    row = HpiRow
    labels = {
        "Year": "year", "Month": "month", "GEO_Type": "geo_type", "GEO_Name": "geo_name", "GEO_Code": "geo_code",
        "Index_NSA": "index_nsa", "Index_SA": "index_sa",
    }  # fmt: skip


_GEO_TYPES = {"US": False, "State": False, "CBSA": True}


class FmhpiStates(Fmhpi):
    """``_read_fmhpi(body, geo_type="State")``: the same three checks, then the filter."""

    where = pl.col("geo_type") == "State"

    @classmethod
    @override
    def check(cls, frame: pl.DataFrame) -> None:
        if unknown := sorted(set(frame["geo_type"]) - set(_GEO_TYPES)):
            msg = f"the file holds GEO_Type {unknown!r}, not one of {sorted(_GEO_TYPES)!r}"
            raise CheckError(msg)
        coded = pl.col("geo_type").replace_strict(_GEO_TYPES, return_dtype=pl.Boolean)
        if not (bad := frame.filter(coded != pl.col("geo_code").is_not_null())).is_empty():
            msg = f"a {bad['geo_type'][0]} row's GEO_Code is {bad['geo_code'][0]!r}"
            raise CheckError(msg)
        if not (bad := frame.filter(~pl.col("month").is_between(1, 12))).is_empty():
            msg = f"the month {bad['month'][0]} is not 1 to 12"
            raise CheckError(msg)


_DISCLAIMER = "Although Freddie Mac attempts to provide reliable"


class PmmsFullHistory(flatread.Sheet):
    """``historicalweeklydata.xlsx`` sheet ``Full History``: ``_read_pmms_full_history``.

    The headings are three rows a column; v1 reads one header row, so the labels are the last row's (the first two
    are not checked), and the disclaimer row directly under the weeks (no blank row between) is dropped by ``cells``.
    """

    row = PmmsRow
    sheet = "Full History"
    header_row = 6  # 0-based from A1: Excel row 7 (fastexcel skips from A1, not the used range)
    labels = (
        ("Week", "date"), ("FRM", "pmms30"), ("points", "pmms30p"), ("FRM", "pmms15"), ("points", "pmms15p"),
        ("ARM", "pmms51"), ("points", "pmms51p"), ("margin", "pmms51m"), ("spread", "pmms51spread"),
    )  # fmt: skip
    missing = frozenset({"", " "})  # pmms30p holds one space for 32 weeks of 1971

    @classmethod
    @override
    def cells(cls, frame: pl.DataFrame) -> pl.DataFrame:
        if frame.schema["Week"] != pl.String or frame.is_empty():
            return frame
        last = frame.row(-1)
        if last[0] is not None and last[0].startswith(_DISCLAIMER) and set(last[1:]) <= {None, ""}:
            return frame.head(-1)
        return frame


# --- Chicago Fed ----------------------------------------------------------------------------------------------------


class NfciContributions(flatread.Sheet):
    """``contributions-data-series-xlsx.xlsx`` sheet ``NFCI``, long: ``_read_contributions_nfci``.

    Not checked as the hand-written reader checks: that every header is a mnemonic of the ``Key`` sheet (v1 sees one
    sheet; ``names`` is a static regex), and that every cell is filled (v1's long read drops an empty cell).
    """

    row = ContributionRow
    sheet = "NFCI"
    labels = {"Date": "friday_of_week"}
    long = flatread.Long(name="mnemonic", value="contribution", names=r"\S+")
    dates = {"friday_of_week": "%m/%d/%Y"}
    key = ("friday_of_week", "mnemonic")  # the hand reader refuses a repeated header


# --- San Francisco Fed ----------------------------------------------------------------------------------------------

_MATURITY = re.compile(r"OBSYLD(\d{2})(MO|YR)")


class TermModelObservedYields(flatread.Sheet):
    """``FRBSF_Term_Model_Data.xlsx`` sheet ``Observed Treasury Yields``, long: ``frbsf._TERM_PREMIUM_OBSERVED_YIELDS``.

    ``Long`` puts the label as written in ``name``; the hand reader's ``key_of`` turns ``OBSYLD03MO`` into 3 months,
    so ``cells`` renames each melted column to its maturity first.
    """

    row = TermModelRow
    sheet = "Observed Treasury Yields"
    labels = {"DATE": "date"}
    long = flatread.Long(name="maturity_months", value="value", names=r"\d+")

    @classmethod
    @override
    def cells(cls, frame: pl.DataFrame) -> pl.DataFrame:
        renames = {}
        for column in frame.columns[1:]:
            if not (found := _MATURITY.fullmatch(column)):
                msg = f"the column {column!r} is not {_MATURITY.pattern!r}"
                raise UnknownLayoutError(msg)
            renames[column] = str(int(found[1]) * (1 if found[2] == "MO" else 12))
        return frame.rename(renames)


class RateDistributionHorizon2(flatread.Sheet):
    """``interest-rate-probability-distributions-data.xlsx`` sheet ``Horizon 2``: ``_RATE_DISTRIBUTION_HORIZONS[2]``."""

    row = RateDistributionRow
    sheet = "Horizon 2"
    labels = {
        "Date": "date", "2.5th percentile": "p2_5", "5th percentile": "p5", "15th percentile": "p15",
        "50th percentile": "p50", "85th percentile": "p85", "95th percentile": "p95", "97.5th percentile": "p97_5",
    }  # fmt: skip


# --- Atlanta Fed ----------------------------------------------------------------------------------------------------


class HoamCounty(flatread.Sheet):
    """``HOAM_County_Data.xlsx`` sheet ``Sheet1``: ``atlanta._HOAM_COUNTY``."""

    row = HoamCountyRow
    sheet = "Sheet1"
    labels = {
        "County": "name", "County FIP": "fips", "Affordability Metric": "affordability",
        "county_perc_mo": "housing_cost_share_of_median_household_income", "Month": "month",
    }  # fmt: skip
    dates = {"month": flatread.YEAR_MONTH}


class HoamCbsa(flatread.Sheet):
    """``HOAM_CBSA_Data.xlsx`` sheet ``Sheet1``: ``atlanta._HOAM_CBSA``."""

    row = HoamAreaRow
    sheet = "Sheet1"
    labels = {
        "CBSA_Name": "name", "CBSA_Code": "code", "Month": "month", "Affordability": "affordability",
        "Housing_Cost%_of_Med_HHIncome": "housing_cost_share_of_median_household_income",
    }  # fmt: skip
    dates = {"month": flatread.YEAR_MONTH}


# --- The corpus -----------------------------------------------------------------------------------------------------


def _body(provider: str, test: str, template: str) -> bytes:
    path = TREE / "tests" / provider / "cassettes" / "test_recorded" / f"test_file[{test}].yaml"
    return endpoint_body(path, template)


#: (name, cassette (provider, test, path template), hand-written reader, v1 layout)
CASES: list[tuple[str, tuple[str, str, str], Callable[[bytes], pl.DataFrame], type[flatread.Layout]]] = [
    (
        "Freddie Mac FMHPI, whole",
        ("freddie_mac", "fmhpi_master_file", "fmhpi_master_file.csv"),
        lambda body: freddie._read_table(body, freddie._FMHPI),  # noqa: SLF001
        Fmhpi,
    ),
    (
        "Freddie Mac FMHPI, states (checks + where)",
        ("freddie_mac", "fmhpi_master_file", "fmhpi_master_file.csv"),
        freddie._read_fmhpi_states,  # noqa: SLF001
        FmhpiStates,
    ),
    (
        "Freddie Mac PMMS Full History",
        ("freddie_mac", "pmms_historicalweeklydata", "historicalweeklydata.xlsx"),
        freddie._read_pmms_full_history,  # noqa: SLF001
        PmmsFullHistory,
    ),
    (
        "Chicago Fed NFCI contributions, NFCI (long)",
        ("chicago_fed", "get_nfci_contributions_data_series_xlsx", "contributions-data-series-xlsx.xlsx"),
        chicago._read_contributions_nfci,  # noqa: SLF001
        NfciContributions,
    ),
    (
        "SF Fed term model, Observed Treasury Yields (long)",
        ("frbsf", "get_frbsf_term_model_data", "FRBSF_Term_Model_Data.xlsx"),
        frbsf._frame_reader(frbsf._TERM_PREMIUM_OBSERVED_YIELDS),  # noqa: SLF001
        TermModelObservedYields,
    ),
    (
        "SF Fed rate distributions, Horizon 2",
        (
            "frbsf",
            "get_interest_rate_probability_distributions_data",
            "interest-rate-probability-distributions-data.xlsx",
        ),
        frbsf._frame_reader(frbsf._RATE_DISTRIBUTION_HORIZONS[2]),  # noqa: SLF001
        RateDistributionHorizon2,
    ),
    (
        "Atlanta Fed HOAM counties",
        ("atlanta_fed", "get_hoam_county_data", "HOAM_County_Data.xlsx"),
        lambda body: atlanta._read_sheet(body, atlanta._HOAM_COUNTY, "HOAM county workbook"),  # noqa: SLF001
        HoamCounty,
    ),
    (
        "Atlanta Fed HOAM metro areas",
        ("atlanta_fed", "get_hoam_cbsa_data", "HOAM_CBSA_Data.xlsx"),
        lambda body: atlanta._read_sheet(body, atlanta._HOAM_CBSA, "HOAM metro workbook"),  # noqa: SLF001
        HoamCbsa,
    ),
]

#: What v1 cannot express without a hook that re-implements the format; paths are prism-clients' ``origin/main``.
GAPS = [
    (
        "Chicago Fed CFNAI real-time vintages (cfnai-realtime-{1,2,3}-xlsx.xlsx, every vintage sheet)",
        "src/prism/clients/chicago_fed/decoders.py:1141-1189 (_vintages)",
        (
            "The reader derives three fields from each melted label: `column` (the label, stripped), `series` (the "
            "prefix CF/CF3/PI/..., mapped per sheet, :1104-1113, :1154-1155) and `vintage` (the label's month "
            "and year, with five misprinted labels fixed by sheet and position, :1120-1126). Since this benchmark "
            "first ran, `Long.names` fills fields from its named groups, so a `cells` hook that renames each "
            "vintage column to `<series>_<YYYY-MM>` (fixing the misprints by position) plus "
            "`Long(names=r'(?P<series>\\w+)_(?P<vintage>\\d{4}-\\d{2})')` and `dates={'vintage': YEAR_MONTH}` express "
            "it. Not ported here."
        ),
    ),
    (
        "Atlanta Fed China macroeconomy vintages (201603Release.zip, *_quarterly.csv; every vintage zip)",
        "src/prism/clients/atlanta_fed/decoders.py:2732-2778 (_read_vintage), :2693-2729",
        (
            "The date column is a decimal year (1949.25). The row fills TWO fields from it, `period` (the float) and "
            "`date` (its period's first day, checked to sit within 1e-6 of a 1/4 or 1/12 step, :2762-2770); a label "
            "fills one field, so `date` has no source. The member is picked by suffix with XLSX preferred over CSV and "
            "__MACOSX copies skipped (:2693-2705), which `member` only does as a callable; the header row is found by "
            "its first cell (:2725) and ragged CSV rows are padded (:2712-2715), which `Csv` refuses. Missing: a "
            "derived field (one label to two fields) and a decimal-period date format."
        ),
    ),
]


# --- Timing ---------------------------------------------------------------------------------------------------------


def best(read: Callable[[], object], runs: int = RUNS) -> tuple[float, object]:
    times, result = [], None
    for _ in range(runs):
        start = time.perf_counter()
        result = read()
        times.append(time.perf_counter() - start)
    return min(times) * 1000, result


def stages(layout: type[flatread.Layout], body: bytes, runs: int = 3) -> tuple[float, float]:
    """Best ms of ``read_text`` and of ``core.run`` on its text: where a v1 read spends its time."""
    plan, hooks = layout._plan, layout._hooks()  # noqa: SLF001
    text_ms, text = best(lambda: layout.read_text(layout._unwrap(body)), runs)  # noqa: SLF001
    run_ms, _ = best(lambda: core.run(plan, text, **hooks), runs)  # ty: ignore[invalid-argument-type]
    return text_ms, run_ms


def difference(hand: pl.DataFrame, v1: pl.DataFrame) -> str:
    """Say how two frames differ: columns, dtypes, rows, then the first differing value."""
    if hand.columns != v1.columns:
        return f"columns {hand.columns} vs {v1.columns}"
    if hand.dtypes != v1.dtypes:
        return "dtypes " + ", ".join(
            f"{c}: {a} vs {b}" for c, a, b in zip(hand.columns, hand.dtypes, v1.dtypes, strict=True) if a != b
        )
    if hand.height != v1.height:
        return f"rows {hand.height} vs {v1.height}"
    for column in hand.columns:
        unequal = ~hand[column].eq_missing(v1[column])
        if unequal.any():
            at = int(unequal.arg_true()[0])
            return f"{column} row {at}: {hand[column][at]!r} vs {v1[column][at]!r} ({int(unequal.sum())} rows differ)"
    return "equal"


def main() -> None:
    bodies: dict[tuple[str, str, str], bytes] = {}
    header = f"{'dataset':52} {'MB':>6} {'hand ms':>9} {'v1 ms':>9} {'v1/hand':>8} {'equals':>7}  v1 read_text + run ms"
    print(header)
    print("-" * len(header))
    notes = []
    for name, cassette, hand_read, layout in CASES:
        body = bodies.setdefault(cassette, _body(*cassette))
        hand_ms, hand = best(lambda: hand_read(body))  # noqa: B023  # called before the loop moves on
        v1_ms, v1 = best(lambda: layout.frame(body))  # noqa: B023
        assert isinstance(hand, pl.DataFrame) and isinstance(v1, pl.DataFrame)
        equal = hand.equals(v1)
        text_ms, run_ms = stages(layout, body)
        print(
            f"{name:52} {len(body) / 1e6:6.1f} {hand_ms:9.1f} {v1_ms:9.1f} {v1_ms / hand_ms:8.2f} {equal!s:>7}"
            f"  {text_ms:.1f} + {run_ms:.1f}"
        )
        if not equal:
            notes.append(f"{name}: {difference(hand, v1)}")
    for note in notes:
        print("DIFFERS", note)
    print("\nGaps: datasets v1 cannot express without a hook that re-implements the format (not timed)")
    for dataset, where, what in GAPS:
        print(f"\n* {dataset}\n  {where}\n  {what}")


if __name__ == "__main__":
    main()
