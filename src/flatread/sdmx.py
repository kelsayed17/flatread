"""SDMX messages read natively into their row struct's fields: one piece per message kind, each set by class attributes.

SDMX (Statistical Data and Metadata eXchange) publishes data and its structure as messages. Each kind is a piece a
format subclasses, setting its class attributes, and composes with the pieces of :mod:`flatread` (a zip's
member is ``class _Structure(ZippedFile, SdmxStructure)``):

* :class:`SdmxCsv`, an SDMX-CSV data message: a header, the key, the dimensions, ``TIME_PERIOD``, ``OBS_VALUE`` and
  the attributes, then a row per observation. Each column is its field upper-cased, a trailing ``ACTION`` (the last
  field, where the struct has it) may be there or not, and ``OBS_VALUE`` is a decimal, SDMX's name for a non-finite
  number (``NaN``, ``INF``, ``-INF``) or empty. A header alone is an empty answer, not a refusal.
* :class:`SdmxStructure`, a structure message, SDMX-ML 1.0 or 2.1 (``_version``, which binds its namespaces): one kind
  of structure as records, its codes, concepts or key families' components (1.0) or its dataflows (2.1), each field's
  XPath from its record on the struct (:class:`~flatread.markup.NestedXml`).
* :class:`SdmxSeries` and :class:`SdmxObservations`, an SDMX-ML 1.0 compact data message as two tables: one data
  set's series, a row each (their attributes, then their annotations' texts), and its observations, long (the series'
  key, then each observation's period, value and status). Its element names, its status code list and its no-value
  markers are a format's class attributes. The message is read with lxml's ``iterparse`` from a stream (a zip's member
  as it inflates, ``_member``), each series cleared once read, so a message of hundreds of MB is never held whole.

A data message is parsed hardened as every XML reader here is: no entity resolved, nothing fetched, no DTD loaded,
libxml2's size limits kept. Every part of a series must be one its format reads: its annotations, then observations,
each of its three attributes alone; a series holding anything else, a data set holding anything but series, a value
that is not a number, a status off its code list or a period not written in the format's date format raises
``ReadError``, naming the file.
"""

import contextlib
import io
import re
import types
import typing
import zipfile
import zlib
from collections.abc import Collection, Generator, Iterator, Mapping
from typing import IO, Final, Literal

import msgspec
import polars as pl
from lxml import etree

from flatread.archives import _INFLATED_MAX, _unzipped
from flatread.core import File
from flatread.delimited import HeadersCsv
from flatread.errors import LayoutError, ReadError
from flatread.markup import NestedXml

__all__ = ["SdmxCsv", "SdmxData", "SdmxObservations", "SdmxSeries", "SdmxStructure"]

type Version = Literal["1.0", "2.1"]
#: The kinds of structure a structure message lists, each read as one table.
type Structure = Literal["codes", "concepts", "components", "dataflows"]

#: Each version's namespaces, under the prefixes its specification's own examples write.
_NAMESPACES: Final[Mapping[Version, Mapping[str, str]]] = {
    "1.0": {
        "message": "http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message",
        "structure": "http://www.SDMX.org/resources/SDMXML/schemas/v1_0/structure",
        "common": "http://www.SDMX.org/resources/SDMXML/schemas/v1_0/common",
    },
    "2.1": {
        "mes": "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message",
        "str": "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure",
        "com": "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common",
    },
}
#: Each version's structure message: its root, and the XPath from it to each kind of structure's records.
_ROOTS: Final[Mapping[Version, str]] = {"1.0": "message:Structure", "2.1": "mes:Structure"}
_RECORDS: Final[Mapping[Version, Mapping[Structure, str]]] = {
    "1.0": {
        "codes": "message:CodeLists/structure:CodeList/structure:Code",
        "concepts": "message:Concepts/structure:Concept",
        "components": "message:KeyFamilies/structure:KeyFamily/structure:Components/*",
    },
    "2.1": {"dataflows": "mes:Structures/str:Dataflows/str:Dataflow"},
}


# --- SDMX-CSV -------------------------------------------------------------------------------------------------------

#: The last field of a struct whose file writes it only at times (an ``updatedAfter`` answer's ``ACTION``).
_ACTION: Final = "action"
_OBS_VALUE: Final = "OBS_VALUE"
#: SDMX's names for the non-finite numbers, which it writes as observations.
_NON_FINITE: Final = r"^(?:NaN|[+-]?INF)$"
#: A decimal, or a non-finite number by name.
_CSV_NUMBER: Final = r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$|" + _NON_FINITE


class SdmxCsv[RowT: msgspec.Struct](HeadersCsv[RowT]):
    """An SDMX-CSV data message: its labels its fields upper-cased, a trailing ``ACTION`` column only sometimes there.

    The body ends its last line, ``OBS_VALUE`` is checked as SDMX writes it (:meth:`_check`), a ``NaN`` or an infinity
    is a value, and a header alone is a table of no rows.
    """

    _non_finite = (_OBS_VALUE.lower(),)
    _rows_required = False

    def __init__(self, row: type[RowT]) -> None:
        """Lay out an SDMX-CSV of *row*, each field its column upper-cased, in the file's order.

        Raises ``LayoutError`` for a struct with no ``obs_value``, the column every SDMX-CSV holds.
        """
        if _OBS_VALUE.lower() not in row.__struct_fields__:
            msg = f"{row.__name__} has no obs_value field, which every SDMX-CSV holds"
            raise LayoutError(msg)
        # Without ACTION, where the struct ends with it: a file writes it only at times.
        self._headers_lacking = ((), (_ACTION,)) if row.__struct_fields__[-1] == _ACTION else ((),)
        super().__init__(row, final_line_ending=True)

    @typing.override
    def _label(self, name: str) -> str:
        return name.upper()

    @typing.override
    def _check(self, body: bytes) -> None:
        """Refuse an ``OBS_VALUE`` that is neither a decimal nor one of SDMX's names for a non-finite number.

        polars alone would also read ``inf``, ``nan``, a padded ``1.5 `` or an overflowing ``1e400``.
        """
        # HeadersCsv has checked the header and every row's width by now, so polars reads the column
        cells = pl.read_csv(body, columns=[_OBS_VALUE], infer_schema=False)[_OBS_VALUE]
        finite = cells.cast(pl.Float64, strict=False).is_finite().fill_null(value=False)
        unread = cells.is_not_null() & (~cells.str.contains(_CSV_NUMBER) | (~finite & ~cells.str.contains(_NON_FINITE)))
        if (wrong := unread.arg_true()).len():
            msg = f"the OBS_VALUE cell {cells[wrong[0]]!r} is not a number: row {wrong[0] + 1}"
            raise ReadError(msg)


# --- Structure messages ---------------------------------------------------------------------------------------------


class SdmxStructure[RowT: msgspec.Struct](NestedXml[RowT]):
    """A structure message's one kind of structure, a record each: ``_version`` binds the namespaces its paths write.

    Its paths write the prefixes of its version's specification (``message:``, ``structure:``, ``common:`` in 1.0;
    ``mes:``, ``str:``, ``com:`` in 2.1), each field's ``Label`` an XPath from its record.
    """

    #: The SDMX-ML version of the message; a format's subclass sets its own.
    _version: typing.ClassVar[Version] = "2.1"
    _namespaces: typing.ClassVar[Mapping[str, str]] = _NAMESPACES["2.1"]

    @typing.override
    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        cls._namespaces = _NAMESPACES[cls._version]

    def __init__(self, row: type[RowT], structure: Structure) -> None:
        """Lay out *row*'s records, each one of the message's *structure* (``codes``), as the version writes them.

        Raises ``LayoutError`` for a kind of structure the version's messages do not list, or what
        :class:`~flatread.markup.NestedXml` refuses.
        """
        records = _RECORDS[self._version]
        if structure not in records:
            msg = f"an SDMX-ML {self._version} structure message lists no {structure}, only {sorted(records)}"
            raise LayoutError(msg)
        super().__init__(row, root=_ROOTS[self._version], record=records[structure])


# --- Data messages --------------------------------------------------------------------------------------------------

#: How many observations are held as Python text before polars takes them: the memory a read holds beyond its frame.
_CHUNK: Final = 1 << 20


class SdmxData[RowT: msgspec.Struct](File[RowT]):
    """An SDMX-ML 1.0 compact data message's one data set, streamed: the series of each format's table.

    A format sets its element names, each a prefixed name that SDMX-ML 1.0's namespaces or its own ``_prefixes`` bind:
    ``_root``, ``_data_set`` (whose ``_data_set_id`` attribute names it), ``_observation`` and ``_annotations``, the
    element holding a series' annotations; a series is any element named ``_series`` in a data set, since a compact
    message writes each data set's series in its key family's own namespace. The first field of the row struct is the
    attribute naming each series (its key). With ``_member``, the body is a zip and the message its one member whose
    name matches it, read as it inflates.
    """

    #: The format's own prefixes, beside SDMX-ML 1.0's ``message:`` and ``common:`` (the Board's ``frb:``).
    _prefixes: typing.ClassVar[Mapping[str, str]] = types.MappingProxyType[str, str]({})
    #: The message's root element.
    _root: typing.ClassVar[str]
    #: A data set's element, and its attribute naming it.
    _data_set: typing.ClassVar[str]
    _data_set_id: typing.ClassVar[str] = "id"
    #: A series' element's local name, in whatever namespace its data set binds.
    _series: typing.ClassVar[str] = "Series"
    #: An observation's element, and the element holding a series' annotations, which precedes its observations.
    _observation: typing.ClassVar[str]
    _annotations: typing.ClassVar[str]
    #: The regex the zip's member's whole name matches, where the message is one (``[^/]*_data\.xml``).
    _member: typing.ClassVar[re.Pattern[str] | None] = None

    def __init__(self, row: type[RowT], data_set: str) -> None:
        """Read the data set *data_set* (its ``_data_set_id``, ``H41``) into rows of *row*, fields named as in SDMX."""
        super().__init__(row)
        self._namespaces = {**_NAMESPACES["1.0"], **self._prefixes}
        self._wanted = data_set
        self._key = self._columns[0].label
        # Each name's prefix checked bound now: an unbound one would fail every read as lxml's own error.
        self._root_tag, self._set_tag, self._notes, _ = map(
            self._tag, (self._root, self._data_set, self._annotations, self._observation)
        )
        self._count = etree.XPath(f"count({self._observation})", namespaces=self._namespaces)
        # Each a whole-series count: a predicate per observation costs libxml2 a second over H.4.1's 1.7M.
        self._junk = etree.XPath(
            f"count({self._observation}/node()) + count(text()[normalize-space()])", namespaces=self._namespaces
        )

    def _tag(self, name: str) -> str:
        """Return the prefixed *name* as lxml writes it, ``{namespace}local``, or raise for a prefix none binds."""
        prefix, _, local = name.partition(":")
        if prefix not in self._namespaces:
            msg = f"the element {name!r} writes a prefix the format's namespaces do not bind"
            raise LayoutError(msg)
        return f"{{{self._namespaces[prefix]}}}{local}"

    @typing.override
    def _label(self, name: str) -> str:
        return name.upper()

    @contextlib.contextmanager
    def _opened(self, body: bytes) -> Generator[tuple[str, IO[bytes]]]:
        """Open the message in *body*, named for errors: the body itself, or the zip's ``_member`` as it inflates."""
        if self._member is None:
            yield "the body", io.BytesIO(body)
            return
        with _unzipped(body, None) as archive:
            names = archive.namelist()
            found = [name for name in names if self._member.fullmatch(name)]
            if len(found) != 1:
                msg = f"the zip holds {len(found)} members matching {self._member.pattern}, not one: {names[:5]!r}"
                raise ReadError(msg)
            if (size := archive.getinfo(found[0]).file_size) > _INFLATED_MAX:
                msg = f"the zip's {found[0]!r} inflates to {size} bytes, over {_INFLATED_MAX}"
                raise ReadError(msg)
            try:
                with archive.open(found[0]) as member:
                    yield found[0], member
            except (zipfile.BadZipFile, zlib.error, EOFError) as e:  # a damaged header, a cut stream, a CRC that fails
                msg = f"the zip's {found[0]!r} will not inflate: {e}"
                raise ReadError(msg) from e

    def _each(self, body: bytes) -> Iterator[tuple[str, etree._Element]]:
        """Yield the message's name and each series of the data set read, every part checked; each is cleared after.

        Raises ``ReadError`` for a message that is not well-formed, whose root is not ``_root``, with a series
        outside a data set, a data set holding anything but series, a series holding anything but its annotations
        and observations, and for a message holding no series of the data set.
        """
        data_set, series_tag, root_tag = self._set_tag, f"{{*}}{self._series}", self._root_tag
        with self._opened(body) as (name, stream):
            sets: list[str] = []
            found = 0
            last: etree._Element | None = None
            parsed = etree.iterparse(
                stream,
                events=("start", "end"),
                tag=(data_set, series_tag),
                resolve_entities=False,
                no_network=True,
                load_dtd=False,
                huge_tree=False,
            )
            try:
                # iselement narrows lxml's untyped events; with the tags filtered, every one is an element.
                for event, element in ((event, element) for event, element in parsed if etree.iselement(element)):
                    if not sets and (root := element.getroottree().getroot().tag) != root_tag:
                        msg = f"the root of {name} is <{root}>, not <{root_tag}>"
                        raise ReadError(msg)
                    if element.tag == data_set:
                        if event == "start":
                            _between_sets(element, data_set, name, first=not sets)
                            sets.append(element.get(self._data_set_id, ""))
                        else:
                            _only_series(element, self._series, sets[-1], name)
                            last = element
                        continue
                    if event == "start":
                        continue
                    parent = element.getparent()
                    if parent is None or parent.tag != data_set:
                        msg = f"a <{element.tag}> of {name} sits outside any <{data_set}>"
                        raise ReadError(msg)
                    if sets[-1] == self._wanted:
                        if self._stray(element):
                            msg = (
                                f"the series {element.get(self._key)!r} of the {self._wanted} data set of {name} holds"
                                " other than its annotations and observations"
                            )
                            raise ReadError(msg)
                        found += 1
                        yield name, element
                    # clear(keep_tail=True), which lxml's stubs lack: the text after a series may be parsed already,
                    # and is checked as the series is dropped
                    tail = element.tail
                    element.clear()
                    element.tail = tail
                    _drop_read(element, parent, self._series, sets[-1], name)
            except etree.XMLSyntaxError as e:
                msg = f"{name} is not well-formed XML: {e}"
                raise ReadError(msg) from e
            self._ended(name, sets, last, found=found)

    def _ended(self, name: str, sets: list[str], last: etree._Element | None, *, found: int) -> None:
        """Refuse a whole message holding more after its last data set (*last*), or no series of the data set read."""
        if last is not None and (last.getnext() is not None or not _blank(last.tail)):
            msg = f"{name} holds other than data sets after its last"
            raise ReadError(msg)
        if self._wanted not in sets:
            msg = f"{name} holds no {self._wanted} data set; it holds {sets!r}"
            raise ReadError(msg)
        if not found:
            msg = f"the {self._wanted} data set of {name} holds no series"
            raise ReadError(msg)

    def _stray(self, series: etree._Element) -> bool:
        """Whether *series* holds other than its annotations, first and once, and its observations, each empty."""
        notes = series.find(self._notes)
        return bool(
            self._junk(series)
            or len(series) != typing.cast("float", self._count(series)) + (notes is not None)
            or (notes is not None and notes.getprevious() is not None)
        )


def _blank(text: str | None) -> bool:
    return text is None or not text.strip()


def _between_sets(data_set: etree._Element, tag: str, name: str, *, first: bool) -> None:
    """Refuse a data set that is not the root's child, or one after another with anything between them."""
    parent = data_set.getparent()
    before = data_set.getprevious()
    if (
        parent is None
        or parent.getparent() is not None
        or not (first or (before is not None and before.tag == tag and _blank(before.tail)))
    ):
        msg = f"{name} holds a data set somewhere other than after the last, in its root"
        raise ReadError(msg)


def _drop_read(series: etree._Element, parent: etree._Element, local: str, data_set: str, name: str) -> None:
    """Delete the series before *series* in its data set *parent*, each read and cleared: anything else raises."""
    if series.getprevious() is None and not _blank(parent.text):
        msg = f"in the {data_set} data set, {name} holds {parent.text[:200]!r} where a series should be"
        raise ReadError(msg)
    while (before := series.getprevious()) is not None:
        if not isinstance(before.tag, str) or etree.QName(before).localname != local or not _blank(before.tail):
            msg = f"in the {data_set} data set, {name} holds {etree.tostring(before)[:200]!r} where a series should be"
            raise ReadError(msg)
        del parent[0]


def _only_series(data_set: etree._Element, local: str, set_id: str, name: str) -> None:
    """Refuse a closed data set holding anything after its series, the last of which is still in it, cleared."""
    if not _blank(data_set.text) or any(
        not isinstance(child.tag, str) or etree.QName(child).localname != local or not _blank(child.tail)
        for child in data_set
    ):
        msg = f"in the {set_id} data set, {name} holds other than series"
        raise ReadError(msg)


class SdmxSeries[RowT: msgspec.Struct](SdmxData[RowT]):
    """A data set's series, a row each: its attributes, each a field upper-cased, then its annotations' texts.

    ``_described`` maps each field read from an annotation to its ``AnnotationType``, in the order the series writes
    them; every other field is an attribute, and each series must carry exactly those. A series' key is unique within
    its data set.
    """

    #: Each field read from an annotation, and the annotation's type; a format's subclass sets its own.
    _described: typing.ClassVar[Mapping[str, str]] = types.MappingProxyType[str, str]({})
    #: An annotation's element, and its type's and text's (SDMX-ML 1.0's).
    _annotation: typing.ClassVar[str] = "common:Annotation"
    _annotation_type: typing.ClassVar[str] = "common:AnnotationType"
    _annotation_text: typing.ClassVar[str] = "common:AnnotationText"

    def _descriptions(self, series: etree._Element) -> list[str] | None:
        """Return the texts of *series*' annotations, in ``_described``'s order; ``None`` where they are not those."""
        notes = series.find(self._notes)
        kinds = list(self._described.values())
        if notes is None or len(notes) != len(kinds) or not _blank(notes.text):
            return None
        annotation, kind_tag, text_tag = map(
            self._tag, (self._annotation, self._annotation_type, self._annotation_text)
        )
        texts: list[str] = []
        for note, kind in zip(notes, kinds, strict=True):
            parts = list(note)
            if (
                note.tag != annotation
                or [part.tag for part in parts] != [kind_tag, text_tag]
                or parts[0].text != kind
                or any(len(part) for part in parts)
                or not all(_blank(text) for text in (note.text, note.tail, *(part.tail for part in parts)))
            ):
                return None
            texts.append(parts[1].text or "")
        return texts

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        described = [column for column in self._columns if column.field in self._described]
        concepts = sorted(column.label for column in self._columns if column.field not in self._described)
        columns: dict[str, list[str]] = {field: [] for field in self._fields}
        name = ""
        for name, series in self._each(body):
            if sorted(series.attrib) != concepts:
                msg = (
                    f"the series {series.get(self._key)!r} of the {self._wanted} data set of {name} carries "
                    f"{sorted(series.attrib)!r}, not {concepts!r}"
                )
                raise ReadError(msg)
            for column in self._columns:
                if column.field not in self._described:
                    columns[column.field].append(series.attrib[column.label])
            if (texts := self._descriptions(series)) is None:
                msg = (
                    f"the series {series.get(self._key)!r} of the {self._wanted} data set of {name} lacks its "
                    f"annotations {list(self._described.values())!r}"
                )
                raise ReadError(msg)
            for column, text in zip(described, texts, strict=True):
                columns[column.field].append(text)
        frame = pl.DataFrame(columns, schema=dict.fromkeys(self._fields, pl.String))
        key = self._fields[0]
        if len(duplicated := frame.filter(pl.col(key).is_duplicated())[key]):
            msg = f"the {self._wanted} data set of {name} lists the series {duplicated[0]!r} twice"
            raise ReadError(msg)
        return frame


class SdmxObservations[RowT: msgspec.Struct](SdmxData[RowT]):
    """A data set's observations, long: each series' key, then each observation's period, value and status.

    The row struct's fields are the series' key, then the observation's three attributes, upper-cased: ``_period``
    (a date, written as ``_date_format``), ``_value`` (a float) and ``_status``, each in the order the struct lists
    them. Under ``_normal`` the value is a number (``_number``); under any other status of ``_statuses`` it is one
    of the format's no-value markers (``_absent``), read as ``None``.
    """

    _period: typing.ClassVar[str] = "TIME_PERIOD"
    _value: typing.ClassVar[str] = "OBS_VALUE"
    _status: typing.ClassVar[str] = "OBS_STATUS"
    #: The code list the statuses are of, and its codes; a format's subclass sets its own.
    _status_list: typing.ClassVar[str] = "CL_OBS_STATUS"
    _statuses: typing.ClassVar[Collection[str]]
    #: The status of an observation with a value: SDMX's code for a normal one.
    _normal: typing.ClassVar[str] = "A"
    #: The regex a value under ``_normal`` fullmatches, and the values the format writes under any other status.
    _number: typing.ClassVar[str] = r"-?[0-9]+(?:\.[0-9]+)?"
    _absent: typing.ClassVar[Collection[str]]
    #: The ``strptime`` format each period is written in, every cell exactly.
    _date_format: typing.ClassVar[str]

    def __init__(self, row: type[RowT], data_set: str) -> None:
        """Read the data set *data_set*'s observations into rows of *row*: its key, then the three attributes."""
        super().__init__(row, data_set)
        attributes = [column.label for column in self._columns[1:]]
        if sorted(attributes) != sorted((self._period, self._value, self._status)):
            msg = f"{row.__name__}'s fields after the key are {attributes}, not an observation's three attributes"
            raise LayoutError(msg)
        obs = self._observation
        self._attributes = [
            etree.XPath(obs + "/@" + label, namespaces=self._namespaces, smart_strings=False) for label in attributes
        ]
        self._attribute_count = etree.XPath("count(" + obs + "/@*)", namespaces=self._namespaces)

    def _bad(self, cells: pl.DataFrame, condition: pl.Expr, name: str, what: str) -> None:
        """Raise naming the first observation of *cells* where *condition* holds, if any."""
        if len(bad := cells.filter(condition)):
            row = bad.row(0, named=True)
            msg = (
                f"the {self._wanted} observation of {row[self._key]!r} on {row[self._period]!r} in {name} "
                f"(status {row[self._status]!r}, value {row[self._value]!r}) {what}"
            )
            raise ReadError(msg)

    @typing.override
    def _read(self, body: bytes) -> pl.DataFrame:
        labels = [column.label for column in self._columns]
        held: list[list[object]] = [[] for _ in labels]
        chunks: list[pl.DataFrame] = []
        name = ""
        for name, series in self._each(body):
            key = series.get(self._key)
            if key is None:
                msg = f"a series of the {self._wanted} data set of {name} carries no {self._key}"
                raise ReadError(msg)
            found = [cells for path in self._attributes if isinstance(cells := path(series), list)]
            # Three lists as long as the observations, and three attributes an observation in all: each holds its
            # three alone.
            count = len(series) - (series.find(self._notes) is not None)
            if self._attribute_count(series) != 3 * count or [len(cells) for cells in found] != [count] * 3:
                msg = (
                    f"an observation of the series {key!r} of the {self._wanted} data set of {name} carries other than"
                    f" {labels[1:]}"
                )
                raise ReadError(msg)
            held[0].extend([key] * count)
            for column, cells in zip(held[1:], found, strict=True):
                column.extend(cells)
            if len(held[0]) >= _CHUNK:
                chunks.append(
                    pl.DataFrame(dict(zip(labels, held, strict=True)), schema=dict.fromkeys(labels, pl.String))
                )
                held = [[] for _ in labels]
        chunks.append(pl.DataFrame(dict(zip(labels, held, strict=True)), schema=dict.fromkeys(labels, pl.String)))
        table = pl.concat(chunks)
        normal = pl.col(self._status) == self._normal
        period = pl.col(self._period).str.to_date(self._date_format, strict=False)
        self._bad(table, ~pl.col(self._status).is_in(self._statuses), name, f"has no {self._status_list} status")
        self._bad(
            table,
            period.is_null() | (period.dt.strftime(self._date_format) != pl.col(self._period)),
            name,
            f"is not a day written {self._date_format}",
        )
        self._bad(table, normal & ~pl.col(self._value).str.contains(f"^(?:{self._number})$"), name, "is not a number")
        self._bad(
            table, ~normal & ~pl.col(self._value).is_in(self._absent), name, "is not the format's no-value marker"
        )
        return table.select(
            pl.col(self._key),
            period.alias(self._period),
            pl.when(normal).then(pl.col(self._value)).cast(pl.Float64, strict=True).alias(self._value),
            pl.col(self._status),
        ).select(pl.col(column.label).alias(column.field) for column in self._columns)
