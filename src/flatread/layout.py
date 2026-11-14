"""A clean wide table read long, a row per cell, by the reader it is composed with.

:class:`LongFile` reads a clean wide table long: a CSV, a sheet or a text table (an HTML one) whose first columns are
the record's and whose every other column is one per key (a date, a series, a maturity, a horizon), read as a row per
cell. It is composed with its reader the same way (``class _ZlbSheet(LongFile[ZlbRow], SheetFile[ZlbRow])``): each
reader asks :meth:`~File._layout` for the columns a body's header lays out, the struct's own unless a long table's,
and :class:`LongFile` lays out the record's columns and one per key header, each read as the value field, then unpivots.
A key may be written over several header rows (``_key_count``, a key field per row, each a group of ``_keys``), a header
cell with no text may head a spacer column that holds nothing (``_spacers``), the record's columns may be found by
label among the keys (``_scattered``), a heading may repeat, its column's place a key too (``_repeats``: a CSV's
``Difference`` under each week), and a format whose table of rows with no value is an answer sets ``_rows_required``
false. A heading may be any text but a field's name: the long frame's own columns are named by characters no heading
holds. Its rows come record by record, or key by key (``_by_key``: each series' run whole). A record field may stand
among the keys (``_beside``, found by its label), or hold the record's own row where no label tells two apart
(``_numbered``, a sheet's row). It imports no reader of a format: a reader that stacks its header over several rows
says so (``File._joined_header``).
"""

import re
import typing
from collections.abc import Collection, Sequence

import msgspec
import polars as pl

from flatread.core import _DTYPES, _NUL, _UNIT, File, _Column, _first, _none_dtype, _typed
from flatread.errors import EmptyBodyError, LayoutError, ReadError

__all__ = ["LongFile"]


class LongFile[RowT: msgspec.Struct](File[RowT]):
    r"""A clean wide table read long, a row per cell, by the reader the format lists after this class.

    The row struct is the long row: its leading fields are the record's columns, each labeled as the reader's are, and
    its last two the key and the value, which name no column. Every header after the leading labels fullmatches
    :attr:`_keys`; :meth:`_key` gives the key's text from the match, typed as the key field (a whole number, a date by
    its ``Format``), or ``None`` where the key field admits it, and no two headers give one key. A header that names
    several keys (``market_value_residence_2003``) fills the :attr:`_key_count` fields before the value, each from the
    :attr:`_keys` group of its name (``(?P<measure>[a-z_]+)_(?P<year>[0-9]{4})``). Each cell under a
    key's header is a row's value, read as the reader reads the value field's cells; an empty one is a row whose value
    is ``None``, where the field admits it, or no row when :attr:`_empty_rows` is false. Rows come record by record, a
    record's keys in header order. An error names the table's own row and header. A format subclasses it with its
    reader (``class _ZlbSheet(LongFile[ZlbRow], SheetFile[ZlbRow])``) and names :attr:`_keys`.

    A table whose records no label tells apart (GDPNow's rows without a ticker) names :attr:`_numbered`, a leading
    field without a column that holds each record's own row, which its reader fills (a sheet's row). A record field
    whose column stands among the keys, not before them (a date or a note beside the values), is one of
    :attr:`_beside`: read from the column its label heads, wherever it stands, or ``None`` where the header has none.
    """

    #: The regex every header after the record's labels fullmatches; a format's subclass (or its constructor) sets it.
    _keys: re.Pattern[str]
    #: Whether an empty cell is a row whose value is ``None``: a table whose blanks are cells it did not fill (a
    #: vintage that did not yet reach a day) sets it false, and an empty cell is no row.
    _empty_rows: typing.ClassVar[bool] = True
    #: How many fields before the value a header's key fills: one, from :meth:`_key`, or several, each the text of the
    #: group of :attr:`_keys` named for it (empty is ``None``, where the field admits it).
    _key_count: typing.ClassVar[int] = 1
    #: Whether a header cell with no text past the record's columns heads a spacer, a column holding nothing: not a
    #: key, and refused only if it holds a cell.
    _spacers: typing.ClassVar[bool] = False
    #: Whether the record's columns are found by their label anywhere in the header, not leading it: a sheet whose
    #: ``Date Last Year`` sits among its regions. An empty label is the first column's.
    _scattered: typing.ClassVar[bool] = False
    #: Whether a table of a header and no rows is refused though :attr:`_rows_required` is false (a table of rows with
    #: no value is still an answer there): a format sets it for a sheet whose rows run to its end (EIA's).
    _bare_header_refused: typing.ClassVar[bool] = False
    #: Whether rows come key by key, each key's records in the table's order (a series' run whole), not record by
    #: record.
    _by_key: typing.ClassVar[bool] = False
    #: Whether the reader writes the header's key cells itself, their parts joined by U+001F (a GroupedSheet's group
    #: and label), so a cell holding one is a key's, not a stray.
    _split_keys: typing.ClassVar[bool] = False

    #: The record fields whose columns stand among the keys, each found by its label; absent, the field is ``None``.
    _beside: typing.ClassVar[Collection[str]] = ()

    @typing.override
    def _column(self, field: msgspec.structs.FieldInfo) -> _Column:
        column = super()._column(field)
        # shortcut: a value column is typed by the reader's dtype alone; a dated or flagged value needs its reader's
        # per-field conversion here too, once a table has one.
        if field.name == self._row.__struct_fields__[-1] and (column.written is not None or column.flag is not None):
            msg = f"the value field {field.name!r} takes no Format or Flag"
            raise LayoutError(msg)
        return column

    @typing.override
    def _label(self, name: str) -> str | None:
        # The keys and the value name no column: a reader lays out their templates under their names.
        fields = self._row.__struct_fields__
        return name if name in {*fields[-1 - self._key_count :], self._numbered} else super()._label(name)

    def _record(self) -> list[_Column]:
        """Return the record's leading columns: the fields before the keys, but the numbered, beside and Cell ones."""
        return [
            column
            for column in self._columns[: -1 - self._key_count]
            if column.field != self._numbered and column.field not in self._beside and column.cell is None
        ]

    def _key(self, match: re.Match[str]) -> str | None:
        """Return the text of the key a header names, from its *match*: its ``key`` group, or the whole header."""
        return str(match["key"] if "key" in match.re.groupindex else match[0])

    @typing.override
    def _layout(self, header: Sequence[str]) -> Sequence[_Column]:
        if any(_NUL in cell for cell in header):
            msg = f"the header {list(header)!r} holds a NUL, which no heading does"
            raise ReadError(msg)
        # A header over several rows (``above``) joins its cells by U+001F, as a reader that joins a cell's parts
        # itself does (``_split_keys``: a GroupedSheet's group and label); any other header holding one is no table's.
        joined = self._joined_header() or self._split_keys
        if not joined and any(_UNIT in cell for cell in header):
            msg = f"the header {list(header)!r} holds a U+001F, which only a header over several rows holds"
            raise ReadError(msg)
        index, value = self._record(), self._columns[-1]
        # A header written over more than one row ends in its label, which names the column.
        tails = [cell.rpartition(_UNIT)[2] for cell in header]
        if not self._scattered:
            if tails[: len(index)] != [column.label for column in index]:
                msg = (
                    f"the header opens {tails[: len(index)]!r}, not the record's {[column.label for column in index]!r}"
                )
                raise ReadError(msg)
            places = dict(enumerate(index))
        else:
            places = {}
            for column in index:
                # An empty label is the first column's, under an empty cell: a spacer's is empty too.
                found = [at for at, tail in enumerate(tails) if tail == column.label and (column.label or at == 0)]
                if len(found) != 1:
                    msg = f"the header {list(header)!r} has no one {column.label!r} column"
                    raise ReadError(msg)
                places[found[0]] = column
        if unnamed := [
            key.field
            for key in self._columns[-self._key_count - 1 : -1]
            if self._key_count > 1 and not self._repeats and key.field not in self._keys.groupindex
        ]:
            msg = f"the key fields {unnamed} are named by no group of {self._keys.pattern!r}, and no heading repeats"
            raise LayoutError(msg)
        beside = {column.label: column for column in self._columns if column.field in self._beside}
        keyed = [cell for at, cell in enumerate(header) if at not in places and cell.rpartition(_UNIT)[2] not in beside]
        spacers = [cell for cell in keyed if self._spacers and not cell.rpartition(_UNIT)[2]]
        if stray := [label for label in keyed if label not in spacers and not self._keys.fullmatch(label)]:
            msg = f"the header's columns {stray!r} are not keys, which match {self._keys.pattern!r}"
            raise ReadError(msg)
        named = [label for label in keyed if label not in spacers]
        clash = {column.field for column in self._columns} & set(named)
        if (not named and self._rows_required) or clash or (not self._repeats and len(set(named)) != len(named)):
            msg = f"the header {list(header)!r} names no key, or a column twice"
            raise ReadError(msg)
        # A spacer is named by its place, which no key's field is (a NUL); it holds nothing. A beside field stands
        # where its label does.
        return [
            places[at]._replace(label=cell)
            if at in places
            else beside[cell.rpartition(_UNIT)[2]]._replace(label=cell)
            if cell.rpartition(_UNIT)[2] in beside
            else value._replace(field=f"{_NUL}{at}", label=cell, scalar=str)
            if cell in spacers
            else value._replace(field=cell, label=cell)
            for at, cell in enumerate(header)
        ]

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        wide = super()._read(body)
        if wide.is_empty() and self._bare_header_refused:  # rows with no value are an answer, a bare header is not
            msg = "the body is a header and no rows"
            raise EmptyBodyError(msg)
        count = self._key_count
        keys, value = self._columns[-count - 1 : -1], self._columns[-1]
        # The record's fields in the struct's order: its number (the reader's, first), its leading columns and those
        # beside the keys, which a header may lack.
        record = [column.field for column in self._columns[: -1 - count]]
        for column in self._columns[: -1 - count]:
            if column.field not in wide.columns:
                wide = wide.with_columns(pl.lit(None, _none_dtype(column.scalar)).alias(column.field))
        stray = [name for name in wide.columns if name.startswith(_NUL) and not name.startswith(_NUL * 2)]
        if (
            stray
            and (at := _first(wide.select(pl.any_horizontal(pl.col(stray).is_not_null())).to_series(), self._row_one))
            is not None
        ):
            msg = f"a column with no heading holds a cell: row {at}"
            raise ReadError(msg)
        labels = [name for name in wide.columns if name not in record and name not in stray]
        # The headings as written: a repeat's name is its heading, then a NUL.
        written = [name.partition(_NUL)[0] for name in labels]
        # Checked wide, so an error names the table's row and header, not the long frame's row.
        required = [name for name in self._required if name in wide.columns]
        filled = self._empty_rows and value.field in self._required
        among = None if self._empty_rows else wide.select(pl.any_horizontal(pl.col(labels).is_not_null())).to_series()
        self._check_cells(wide, [*required, *labels] if filled else required, among)
        # Every label matches: the layout checked it.
        matches = [match for label in written if (match := self._keys.fullmatch(label))]
        if len(keys) == 1:
            texts = [[self._key(match) for match in matches]]
        else:
            texts = [
                [
                    self._key_text(match[key.field], key) if key.field in self._keys.groupindex else str(place)
                    for place, match in enumerate(matches, 1)
                ]
                for key in keys
            ]
        series = [pl.Series(key.field, text, pl.String) for key, text in zip(keys, texts, strict=True)]
        typed = []
        for key, text in zip(keys, series, strict=True):
            try:
                typed.append(_typed(key, text, "key"))
            except ValueError as e:
                msg = f"the headers {written!r} name keys {text.to_list()!r}, not each a {key.field}: {e}"
                raise ReadError(msg) from e
        if not labels:  # a table whose columns hold no key: no value either
            return (
                wide.select(record)
                .clear()
                .with_columns(*(key.clear() for key in typed), pl.Series(value.field, [], _DTYPES[value.scalar]))
            )
        for key, text, one in zip(keys, series, typed, strict=True):
            if one.null_count() and key.field in self._required:
                msg = f"the headers {written!r} name keys {text.to_list()!r}, not each one {key.field} once"
                raise ReadError(msg)
        names = pl.DataFrame(typed)
        if names.n_unique() != len(names):
            shown = texts[0] if count == 1 else [match.groupdict() for match in matches]
            msg = (
                f"the headers {written!r} name keys {shown!r}, not each one {', '.join(key.field for key in keys)} once"
            )
            raise ReadError(msg)
        # A heading can be any text but one with a NUL: the row's number and the heading's name are two of those.
        row, heading = _NUL, _NUL * 2
        unpivoted = wide.with_row_index(row).unpivot(
            labels, index=[row, *record], variable_name=heading, value_name=value.field
        )
        # unpivot lays the cells out key by key
        long = (
            (unpivoted if self._by_key else unpivoted.sort(row, maintain_order=True))
            .join(names.with_columns(pl.Series(heading, labels)), on=heading, how="left", maintain_order="left")
            .select(*record, *(key.field for key in keys), value.field)
        )
        return long if self._empty_rows else long.drop_nulls(value.field)

    def _key_text(self, text: str | None, key: _Column) -> str | None:
        """Return the text a key's group of :attr:`_keys` matched: empty is no text where the key field admits none."""
        return text if bool(text) or key.field in self._required else None
