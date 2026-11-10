"""Fixed-width text: each field at the columns the provider's spec gives it, sliced natively in polars.

``columns`` maps each field to its first and last character, 1-based and inclusive, as a provider's record layout
writes them. Every line that is not blank must match ``record``, or ``skip`` (a header or control record, such as
OCC's ``H`` record), or it raises naming the line. Cells are sliced as written, so a padded field takes ``strip``.
"""

from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar, override

import polars as pl

from flatread.core import Plan, Text
from flatread.errors import EncodingError, RaggedRowError, TruncatedFileError, UnknownLayoutError
from flatread.layout import Labels, Layout

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["FixedWidth"]


class FixedWidth(Layout):
    """A file of fixed-width records."""

    columns: ClassVar[Mapping[str, tuple[int, int]]] = MappingProxyType[str, tuple[int, int]]({})
    record: ClassVar[str] = r".*"
    skip: ClassVar[str | None] = None
    final_line_ending: ClassVar[bool] = True

    # A base extends its parent's settings; its fields are named by position, so it has no labels.
    _SETTINGS: ClassVar[frozenset[str]] = (Layout._SETTINGS - {"labels", "variants"}) | {  # noqa: SLF001
        "columns", "record", "skip", "final_line_ending",
    }  # fmt: skip

    @classmethod
    @override
    def _labels(cls) -> Labels:
        return [(name, name) for name in cls.columns]

    @classmethod
    @override
    def _compile(cls, row: type) -> Plan:
        for name, (start, end) in cls.columns.items():
            if not 1 <= start <= end:
                msg = f"{cls.__name__}: the columns {(start, end)!r} of {name!r} are not a 1-based first and last"
                raise TypeError(msg)
        for pattern in (cls.record, cls.skip):
            if pattern is not None:
                pl.Series([""]).str.contains(pattern)
        return super()._compile(row)  # ty: ignore[invalid-argument-type]

    @classmethod
    @override
    def read_text(cls, body: bytes) -> Text:
        try:
            text = body.decode()
        except UnicodeDecodeError as exc:
            msg = f"the body is not UTF-8: {exc}"
            raise EncodingError(msg) from exc
        if cls.final_line_ending and not text.endswith("\n"):
            msg = f"the file ends mid-line, with {text.rsplit(chr(10), 1)[-1][:200]!r}, so it may have been cut off"
            raise TruncatedFileError(msg)
        lines = (
            pl.Series("line", [text.removeprefix("﻿")]).str.split("\n").explode().str.strip_suffix("\r")
            .to_frame().with_row_index("number", offset=1).filter(pl.col("line") != "")
        )  # fmt: skip
        if cls.skip is not None:
            lines = lines.filter(~pl.col("line").str.contains(f"^(?:{cls.skip})$"))
        if (bad := lines.filter(~pl.col("line").str.contains(f"^(?:{cls.record})$"))).height:
            number, line = bad.row(0)[0], bad.row(0)[1]
            msg = f"line {number} is not a record this layout reads: {line[:200]!r}"
            raise UnknownLayoutError(msg)
        width = max(end for _, end in cls.columns.values())
        if (short := lines.filter(pl.col("line").str.len_chars() < width)).height:
            number, line = short.row(0)
            msg = f"line {number} is {len(line)} characters, short of the record's {width}: {line[:200]!r}"
            raise RaggedRowError(msg)
        numbers = lines["number"]
        frame = lines.select(
            pl.col("line").str.slice(start - 1, end - start + 1).alias(name)
            for name, (start, end) in cls.columns.items()
        )
        return Text(frame, lambda i: f"line {numbers[i]}")
