"""A file's text, and the lines a file writes about itself around its rows.

:func:`decode_utf8` and :func:`decode_text` turn a body into text; :func:`split_lines` takes the lines before and
after a file's rows (a generation stamp, a record count) off its body, for :func:`~flatread.delimited.read_csv` to read
the rest.
"""

import codecs
from typing import Final

from flatread.errors import EncodingError, TruncatedFileError

__all__ = ["decode_text", "decode_utf8", "split_lines"]

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


def split_lines(text: str, *, head: int = 0, tail: int = 0) -> tuple[list[str], str, list[str]]:
    """Return *text*'s first *head* and last *tail* non-empty lines, and the text between them.

    A line's ending (LF, or CRLF) is not part of it. A file of fewer non-empty lines raises
    :class:`~flatread.errors.TruncatedFileError`.
    """
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    filled = [at for at, line in enumerate(lines) if line]
    if len(filled) < head + tail:
        msg = f"the file has {len(filled)} lines, fewer than the {head + tail} it writes about itself"
        raise TruncatedFileError(msg)
    start = filled[head - 1] + 1 if head else 0
    end = filled[len(filled) - tail] if tail else len(lines)
    before = [lines[at] for at in filled[:head]]
    after = [lines[at] for at in filled[len(filled) - tail :]] if tail else []
    return before, "\n".join(lines[start:end]), after
