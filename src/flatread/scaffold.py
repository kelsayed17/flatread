"""A row struct and a layout class written from a sample body: a starting point to edit, never a file kept in sync.

Each label becomes a snake_case field. Each column's type is the narrowest every cell that is not missing reads as:
a whole number, a number, a date (ISO, ``%m/%d/%Y``, ``%Y%m%d`` or a quarter), or text. A column with a missing
token (``""``, ``.``, ``NA``, ``N/A``, ``ND``, ``-``, ``null``) is ``| None``, and the tokens seen become the layout's
``missing``. Fixed-width files are left out: their columns' positions cannot be read off a sample.
"""

import keyword
import re
from typing import Any, Final, Literal

import msgspec
import polars as pl

__all__ = ["field_names", "scaffold"]

type Format = Literal["csv", "pipe", "tsv", "sheet", "json"]

_TOKENS: Final = ("", ".", "NA", "N/A", "ND", "-", "null")
_DATES: Final = ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d")
_QUARTER: Final = r"^\d{4}:?Q[1-4]$"
_BASES: Final = {"csv": "Csv", "pipe": "Pipe", "tsv": "Tsv", "sheet": "Sheet", "json": "Json"}


def field_names(labels: list[str]) -> list[str]:
    """Return a snake_case field name for each label: unique, an identifier, and not a keyword."""
    names: list[str] = []
    for label in labels:
        name = re.sub(r"[^0-9a-z]+", "_", label.lower()).strip("_") or "column"
        if name[0].isdigit():
            name = f"n_{name}"
        if keyword.iskeyword(name):
            name += "_"
        base, n = name, 1
        while name in names:
            n += 1
            name = f"{base}_{n}"
        names.append(name)
    return names


def _kind(column: pl.Series) -> tuple[str, str | None, bool, set[str]]:
    """Return a column's Python type, its date format (or ``None``), whether it may be missing, and its tokens."""
    seen = {t for t in _TOKENS if (column == t).any() or (t == "" and column.is_null().any())}
    values = column.filter(column.is_not_null() & ~column.is_in(list(_TOKENS)))
    nullable = values.len() < column.len()
    if values.len() == 0:
        return "str", None, nullable, seen
    if values.str.to_integer(strict=False).is_not_null().all():
        return "int", None, nullable, seen
    if values.cast(pl.Float64, strict=False).is_not_null().all():
        return "float", None, nullable, seen
    for fmt in _DATES:
        parsed = values.str.to_date(fmt, strict=False)
        if (parsed.dt.strftime(fmt) == values).fill_null(value=False).all():  # an unread cell is null, not a match
            return "dt.date", fmt, nullable, seen
    if values.str.contains(_QUARTER).all():
        return "dt.date", "QUARTER", nullable, seen
    return "str", None, nullable, seen


def _text(body: bytes, fmt: Format, sheet: str | None, records: str | None) -> pl.DataFrame:
    """Return the sample's table, every cell as text, under its labels."""
    if fmt in {"csv", "pipe", "tsv"}:
        separator = {"csv": ",", "pipe": "|", "tsv": "\t"}[fmt]
        return pl.read_csv(
            body, separator=separator, infer_schema=False, quote_char=None if fmt == "pipe" else '"',
            empty_string_is_null=False,
        )  # fmt: skip
    if fmt == "sheet":
        import fastexcel  # noqa: PLC0415  # the excel extra, only for a sheet

        reader = fastexcel.read_excel(body)
        return reader.load_sheet(sheet or 0, dtypes="string").to_polars()
    if fmt == "json":
        value: Any = msgspec.json.decode(body)
        for key in records.split(".") if records is not None and records != "" else []:
            value = value[key.removesuffix("[]")]
        rows: list[dict[str, Any]] = value
        labels = list(dict.fromkeys(k for row in rows for k in row))
        return pl.DataFrame(
            {
                label: pl.Series(
                    [None if (v := row.get(label)) is None else (v if isinstance(v, str) else repr(v)) for row in rows],
                    dtype=pl.String,
                )
                for label in labels
            }
        )
    msg = f"no scaffold for the format {fmt!r}: it takes csv, pipe, tsv, sheet or json (fixed width has no labels)"
    raise ValueError(msg)


def scaffold(
    body: bytes,
    *,
    format: Format,  # noqa: A002  # the CLI's own word for it
    name: str,
    sheet: str | None = None,
    records: str | None = None,
) -> str:
    """Return Python source for a row struct ``<name>Row`` and a layout ``<name>`` that read *body*."""
    text = _text(body, format, sheet, records)
    names = field_names(text.columns)
    fields, dates, tokens = [], {}, {""}
    for label, field in zip(text.columns, names, strict=True):
        kind, date_format, nullable, seen = _kind(text[label])
        fields.append(f"    {field}: {kind}{' | None' if nullable else ''}")
        tokens |= seen
        if date_format is not None and date_format != "%Y-%m-%d":
            dates[field] = "flatread.QUARTER" if date_format == "QUARTER" else repr(date_format)
    lines = [
        "import datetime as dt",
        "",
        "import msgspec",
        "",
        "import flatread",
        "",
        "",
        f"class {name}Row(msgspec.Struct, frozen=True):",
        *fields,
        "",
        "",
        f"class {name}(flatread.{_BASES[format]}):",
        f"    row = {name}Row",
        "    labels = {",
        *(f"        {label!r}: {field!r}," for label, field in zip(text.columns, names, strict=True)),
        "    }",
    ]
    if tokens != {""}:
        lines.append(f"    missing = frozenset({{{', '.join(repr(t) for t in sorted(tokens, key=_TOKENS.index))}}})")
    if dates:
        lines.append("    dates = {" + ", ".join(f"{k!r}: {v}" for k, v in dates.items()) + "}")
    if format == "sheet" and sheet is not None:
        lines.append(f"    sheet = {sheet!r}")
    if format == "json":
        lines.append(f"    records = {records or ''!r}")
    return "\n".join(lines).replace("'", '"') + "\n"
