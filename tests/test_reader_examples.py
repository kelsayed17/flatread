"""``docs/examples/README.md`` shows each reader piece on a small excerpt of a real recorded file it reads.

These checks keep the README and its folder in step: every link in it resolves (an excerpt, a reader's module, another
section), and each excerpt has its section, whose block is the file as it is.
"""

import re
from pathlib import Path

_EXAMPLES = Path(__file__).resolve().parents[1] / "docs" / "examples"
_README = (_EXAMPLES / "README.md").read_text(encoding="utf-8")
#: A section: the excerpt's link opens its first line, then its notes, then its text in a fenced block.
_SHOWN = re.compile(r"^\[`([\w.-]+)`\]\(\1\).*?\n```\n(.*?)\n```$", re.MULTILINE | re.DOTALL)
_LINK = re.compile(r"\]\(([^)]+)\)")


def _anchor(heading: str) -> str:
    """Return the anchor GitHub gives a heading: lower case, punctuation dropped, spaces as hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def _text(body: bytes) -> str:
    """Return an excerpt as the README shows it: UTF-8 or else cp1252, a BOM and DOS's end-of-file byte dropped."""
    try:
        text = body.decode()
    except UnicodeDecodeError:
        text = body.decode("cp1252", errors="replace")
    # CRLF reads as LF, and a closing line end is the fence's.
    return text.removeprefix("﻿").replace("\x1a", "").replace("\r\n", "\n").rstrip("\n")


def test_every_link_in_the_readme_resolves() -> None:
    anchors = {_anchor(heading) for heading in re.findall(r"^#+ (.+)$", _README, re.MULTILINE)}
    links = _LINK.findall(_README)
    broken = [link for link in links if link.removeprefix("#") not in anchors and not (_EXAMPLES / link).is_file()]
    assert broken == []


def test_the_readme_shows_every_example_as_its_file_is() -> None:
    shown = dict(_SHOWN.findall(_README))
    files = sorted(path.name for path in _EXAMPLES.iterdir() if path.name != "README.md")
    assert sorted(shown) == files
    assert [name for name in files if shown[name] != _text((_EXAMPLES / name).read_bytes())] == []
