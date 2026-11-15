"""``README.md``'s examples run as written: each Python block, in order, in one namespace.

A block's asserts are its expected values, and the ``# `` lines that close a block are what it prints.
"""

import contextlib
import io
import re
from pathlib import Path

_README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
_BLOCKS = re.findall(r"^```python\n(.*?)^```$", _README, re.MULTILINE | re.DOTALL)


def test_the_readme_has_examples() -> None:
    assert len(_BLOCKS) >= 2


def test_every_example_runs_and_prints_what_it_says() -> None:
    namespace: dict[str, object] = {"__name__": "readme"}
    for block in _BLOCKS:
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            exec(compile(block, "README.md", "exec"), namespace)  # noqa: S102 (the README's own code)
        said = re.findall(r"^# (.*)$", block.rstrip().rpartition("\n\n")[2], re.MULTILINE)
        assert printed.getvalue().splitlines() == said
