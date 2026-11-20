"""Wall-clock timings of ``Csv._whole``, the pure-Python CSV fallback, as a Markdown table.

Run from the repository root: ``PYTHONPATH=. uv run python benchmarks/whole_speed.py``. The root must be on the path
because the bodies come from ``benchmarks/test_speed.py``, which imports ``tests.support``. Each case warms up once,
then reports the best of 20 runs. The script uses only APIs that stay the same across the ``_whole`` port, so one run
on each commit gives the before and after columns.
"""

import io
import platform
import subprocess
import sys
import time
from typing import TYPE_CHECKING

from benchmarks.test_speed import _CSV, _QUOTED, NEXT_HPI_MISQUOTED, next_whole
from flatread import next as fr

if TYPE_CHECKING:
    from collections.abc import Callable

_MISQUOTED = fr.Csv(misquoted=True)


def _grid(body: bytes) -> object:
    return _MISQUOTED.grid(io.BytesIO(body), fr.ReadContext(body))


CASES: dict[str, tuple[Callable[[bytes], object], bytes]] = {
    "`Csv(misquoted=True).grid`, `_CSV` (3.1 MB)": (_grid, _CSV),
    "`Csv(misquoted=True).grid`, `_QUOTED` (10k rows)": (_grid, _QUOTED),
    "`Csv()._whole(text, body)`, `_CSV` (private-path micro-benchmark)": (next_whole, _CSV),
    '`Reader(Hpi, Csv(misquoted=True), null_values=["."]).frame`, `_CSV`': (NEXT_HPI_MISQUOTED.frame, _CSV),
}


def _best_ms(read: Callable[[bytes], object], body: bytes, runs: int = 20) -> float:
    read(body)
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        read(body)
        times.append(time.perf_counter() - start)
    return min(times) * 1000


def _run(*command: str) -> str:
    return subprocess.run(command, capture_output=True, text=True, check=False).stdout.strip()  # noqa: S603


def main() -> None:
    cpu = (sys.platform == "darwin" and _run("/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string")) or (
        platform.processor() or platform.machine()
    )
    sha = _run("git", "rev-parse", "--short", "HEAD")
    lines = [
        f"`{sha}` · Python {platform.python_version()} · {platform.platform()} · {cpu}",
        "",
        "| case | best ms |",
        "| --- | ---: |",
        *(f"| {case} | {_best_ms(read, body):.1f} |" for case, (read, body) in CASES.items()),
    ]
    print("\n".join(lines))  # noqa: T201 (the script's report)


if __name__ == "__main__":
    main()
