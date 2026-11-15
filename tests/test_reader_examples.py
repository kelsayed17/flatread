"""``docs/examples/README.md`` shows each reader piece on a small excerpt of a real recorded file it reads.

These checks keep the README and its folder in step: every link in it resolves (an excerpt, a reader's module, another
section), each excerpt has its section, whose block is the file as it is, and every reader and switch a heading names
is one flatread has, so a rename cannot leave the README behind.
"""

import importlib
import inspect
import pkgutil
import re
from pathlib import Path

import flatread

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


#: Every public class of flatread's modules, by name.
_CLASSES = {
    name: value
    for module in pkgutil.iter_modules(flatread.__path__)
    for name, value in vars(importlib.import_module(f"flatread.{module.name}")).items()
    if inspect.isclass(value) and value.__module__.startswith("flatread.") and not name.startswith("_")
}


def _has(cls: type, name: str) -> bool:
    """Return whether *cls* has the switch or hook *name*: set, or declared for a format to set (``_trailer: ...``)."""
    return hasattr(cls, name) or any(name in inspect.get_annotations(base) for base in cls.__mro__)


def _named(heading: str) -> list[str]:
    """Return what a heading names that flatread must have, each ``Class``, ``Class._switch`` or ``Class(param=)``."""
    tokens = re.findall(r"`([^`]+)`", re.sub(r" \(`[^`]*`\)$", "", heading))  # a switch's value, ``(`None`)``, is none
    named: list[str] = []
    owner = ""
    for token in tokens:
        owner = token if not token.startswith("_") else owner
        named.append(token if not token.startswith("_") else f"{owner.partition('.')[0]}.{token}")
    return named


def test_every_reader_and_switch_a_heading_names_exists() -> None:
    missing = []
    for heading in re.findall(r"^### (.+)$", _README, re.MULTILINE):
        for name in _named(heading):
            cls_name = re.split(r"[.(]", name, maxsplit=1)[0]
            rest = name.removeprefix(cls_name)
            cls = _CLASSES.get(cls_name)
            known = cls is not None and (
                _has(cls, rest[1:])
                if rest.startswith(".")
                else rest.removeprefix("(").partition("=")[0] in inspect.signature(cls).parameters
                if rest.startswith("(")
                else not rest
            )
            if not known:
                missing.append(name)
    assert missing == []
