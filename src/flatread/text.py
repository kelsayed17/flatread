"""A file's text, and the lines a file writes about itself around its rows.

:func:`decode_utf8` and :func:`decode_text` turn a body into text; :func:`split_lines` takes the lines before and
after a file's rows (a generation stamp, a record count) off its body, for :func:`~flatread.delimited.read_csv` to read
the rest.
"""

import codecs
from typing import Final, NamedTuple

from flatread.errors import EncodingError, TruncatedFileError

__all__ = ["Split", "decode_text", "decode_utf8", "split_lines"]

_BOM: Final = codecs.BOM_UTF8


def decode_text(file: bytes) -> str:
    """Return the file's text: UTF-8 when it is UTF-8, else latin-1, which gives every byte a character.

    A byte-order mark that opens the file is not part of it.
    """
    file = file.removeprefix(_BOM)
    try:
        return file.decode()
    except UnicodeDecodeError:
        return file.decode("latin-1")


def decode_utf8(file: bytes) -> str:
    """Return the file's text, for a wire that is UTF-8 throughout; one that is not raises :class:`EncodingError`.

    A byte-order mark that opens the file is not part of it.
    """
    try:
        return file.removeprefix(_BOM).decode()
    except UnicodeDecodeError as exc:
        msg = f"the file is not UTF-8: {exc}"
        raise EncodingError(msg) from exc


class Split(NamedTuple):
    """A file's own lines before and after its rows, the text between them, and that text's first line in the file."""

    before: list[str]
    text: str
    after: list[str]
    first_line: int


def split_lines(text: str, *, head: int = 0, tail: int = 0) -> Split:
    """Return *text*'s first *head* and last *tail* non-empty lines, the text between them, and the line it starts on.

    A line's ending (LF, or CRLF) is not part of a head or tail line. The text between is *text*'s own, sliced from
    it, so a CRLF inside it (a quoted field's) is kept; only its last line's ending, before a tail, is not. The line
    number counts blank lines, so it is the one to give :func:`~flatread.delimited.read_csv` as *first_line*. A file
    of fewer non-empty lines raises :class:`~flatread.errors.TruncatedFileError`.
    """
    lines = text.split("\n")  # each line keeps its CR: the text between is joined from them as written
    filled = [at for at, line in enumerate(lines) if line not in {"", "\r"}]
    if len(filled) < head + tail:
        msg = f"the file has {len(filled)} lines, fewer than the {head + tail} it writes about itself"
        raise TruncatedFileError(msg)
    start = filled[head - 1] + 1 if head else 0
    end = filled[len(filled) - tail] if tail else len(lines)
    before = [lines[at].removesuffix("\r") for at in filled[:head]]
    after = [lines[at].removesuffix("\r") for at in filled[len(filled) - tail :]] if tail else []
    between = "\n".join(lines[start:end])
    return Split(before, between.removesuffix("\r") if end < len(lines) else between, after, start + 1)
