"""An XML table read natively into its row struct's fields, through one hardened parser, :func:`parse_xml`.

Both are text tables (:class:`~flatread.core.TextFile`):

* :class:`XmlFile`, a clean XML table: a root element whose children are records, each a run of leaf elements, the
  struct's labels in order (an element's tag is its label). It is parsed by :func:`parse_xml` (entities left
  unexpanded, no network, a cut body refused). A format's ``_namespaces`` binds the
  prefixes its names write (``mes:Structure``).
* :class:`NestedXml`, a document whose records nest below its root (SDMX's code lists, the House's seats): the record
  is an XPath from the root, and each field's ``Label`` an XPath from its record to one node, an attribute
  (``@id``), a descendant's text (``info/name``) or an ancestor's, every record's under it (``../@comcode``).
"""

import types
import typing
from collections.abc import Mapping

import msgspec
import polars as pl
from lxml import etree

from flatread.core import TextFile
from flatread.errors import EmptyBodyError, UnknownLayoutError

__all__ = ["NestedXml", "XmlFile", "parse_xml"]


def parse_xml(body: bytes) -> etree._Element:
    """Parse *body* as one well-formed XML document, strictly and hardened, returning its root.

    An empty or whitespace-only body raises :class:`~flatread.errors.EmptyBodyError`, and one that is not
    well-formed XML :class:`~flatread.errors.UnknownLayoutError`. The parser is built per call: construction
    measured 0.42 us against a multi-megabyte download, so sharing one buys nothing. Three of its four flags are lxml
    6.1's defaults, kept explicit so the security posture reads here rather than depending on a version's defaults:

    * ``resolve_entities=False`` (lxml's default is ``'internal'``) is **load-bearing**: lxml still resolves internally
      declared entities by default, so a declared ``&lol;`` would expand into element text. This is the billion-laughs
      guard. (The external-file vector was CVE-2026-41066, fixed in lxml 6.1, hence the dependency's floor.)
    * ``recover=False``, load-bearing too: a cut-off body raises rather than yielding the elements before the cut.
    * ``no_network=True``.
    * ``huge_tree=False``: the flag that would *disable* libxml2's expansion limits stays off.
    """
    if not body.strip():
        msg = "the body is empty"
        raise EmptyBodyError(msg)
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, recover=False)
    try:
        return etree.XML(body, parser)
    except etree.XMLSyntaxError as exc:
        msg = f"the body is not well-formed XML: {exc}"
        raise UnknownLayoutError(msg) from exc


class XmlFile[RowT: msgspec.Struct](TextFile[RowT]):
    """A clean XML table: a root of records, each a run of leaf elements its row struct's labels name, in order."""

    #: The prefixes the format's names and paths write, each bound to its namespace (``{"str": "http://..."}``).
    _namespaces: typing.ClassVar[Mapping[str, str]] = types.MappingProxyType[str, str]({})

    def __init__(self, row: type[RowT], *, root: str, record: str) -> None:
        """Lay out a table of *row*: the document's root element *root*, each of its children a *record* element.

        Each field names its record's element with a :class:`~flatread.fields.Label`; a date or time field
        gives the :class:`~flatread.fields.Format` its text is written in. A name may carry a prefix of
        ``_namespaces`` (``mes:Structure``). Raises what :class:`TextFile` refuses.
        """
        super().__init__(row)
        self._root = self._qualified(root)
        self._record = record

    def _qualified(self, name: str) -> str:
        """Return *name* as lxml writes it: ``{namespace}local`` for a prefixed ``prefix:local``."""
        prefix, colon, local = name.rpartition(":")
        return f"{{{self._namespaces[prefix]}}}{local}" if colon else name

    def _document(self, body: bytes) -> etree._Element:
        """Parse *body* into its root element, hardened, and check the root is the format's, or raise ValueError."""
        # shortcut: the whole tree is held, about 15-20 times the body; fine for tables of a few MB, a large one would
        # want lxml's iterparse, clearing each record as it is read.
        root = parse_xml(body)
        if root.tag != self._root:
            msg = f"the document's root is <{root.tag}>, not <{self._root}>"
            raise ValueError(msg)
        return root

    def _rows(self, root: etree._Element) -> list[list[str | None]]:
        """Return each record's cells under *root*, in order, one per label: the root's children, each its leaves."""
        tag = self._qualified(self._record)
        cells: list[list[str | None]] = []
        for n, record in enumerate(root, 1):
            tags = [child.tag for child in record]
            if record.tag != tag or tags != self._labels or any(len(child) for child in record):
                msg = f"record {n} is <{record.tag}> of {tags!r}, not <{tag}> of leaf elements {self._labels!r}"
                raise ValueError(msg)
            cells.append([child.text for child in record])
        return cells

    @typing.override
    def _cells(self, body: bytes) -> pl.DataFrame:
        root = self._document(body)
        cells = self._rows(root)
        if not cells:
            msg = f"the <{self._root}> root holds no <{self._record}> record"
            raise ValueError(msg)
        return pl.DataFrame(cells, schema=dict.fromkeys(self._labels, pl.String), orient="row")


class NestedXml[RowT: msgspec.Struct](XmlFile[RowT]):
    """An XML document whose records nest below its root, each field found by an XPath from its record.

    *record* is the XPath from the root to every record (``members/member``), and each field's ``Label`` the XPath
    from its record to one node: an element, read as its text, or an attribute (``@id``, ``state/@postal-code``). A
    path may climb to an ancestor, whose value is then every record's under it (``../@comcode``), or be an expression
    of text (``local-name()``, the record's own tag). A path finding nothing is no value; one finding two nodes, or an
    element holding elements (an unexpanded entity is one), raises. Paths write the prefixes of ``_namespaces``, or of
    :meth:`_namespaces_of` where a format's bodies bind them otherwise (SEC's ``edgar:``, once ``http``).
    """

    def __init__(self, row: type[RowT], *, root: str, record: str) -> None:
        """Lay out *row*'s records at the XPath *record* below the root element *root* (see the class).

        Raises ``ValueError`` for a path that is no XPath, or what :class:`XmlFile` refuses.
        """
        super().__init__(row, root=root, record=record)
        self._compiled: dict[tuple[tuple[str, str], ...], tuple[etree.XPath, list[etree.XPath]]] = {}
        self._paths(self._namespaces)

    def _namespaces_of(self, root: etree._Element) -> Mapping[str, str]:
        """Return the prefixes a body whose root is *root* is read with: the format's ``_namespaces``."""
        del root
        return self._namespaces

    def _paths(self, namespaces: Mapping[str, str]) -> tuple[etree.XPath, list[etree.XPath]]:
        """Return the record's XPath and each field's, compiled once for *namespaces*."""
        key = tuple(sorted(namespaces.items()))
        if key not in self._compiled:
            try:
                self._compiled[key] = (
                    etree.XPath(self._record, namespaces=namespaces, smart_strings=False),
                    [etree.XPath(label, namespaces=namespaces, smart_strings=False) for label in self._labels],
                )
            except etree.XPathSyntaxError as e:
                msg = f"the record path {self._record!r} or a field's Label {self._labels!r} is no XPath: {e}"
                raise ValueError(msg) from e
        return self._compiled[key]

    @typing.override
    def _rows(self, root: etree._Element) -> list[list[str | None]]:
        record_path, field_paths = self._paths(self._namespaces_of(root))
        try:
            found = record_path(root)
            records = [record for record in found if etree.iselement(record)] if isinstance(found, list) else []
            if not isinstance(found, list) or len(records) != len(found):
                msg = f"the record path {self._record!r} finds {found!r}, not elements"
                raise ValueError(msg)
            return [
                [_found(path(record), label, n) for path, label in zip(field_paths, self._labels, strict=True)]
                for n, record in enumerate(records, 1)
            ]
        except etree.XPathEvalError as e:  # a prefix the namespaces do not bind
            msg = f"a path will not evaluate on the document: {e}"
            raise ValueError(msg) from e


def _found(found: object, label: str, n: int) -> str | None:
    """Return the text an XPath *label* found in record *n*: an attribute's, a leaf element's, an expression's."""
    node: object = found
    if isinstance(found, list):
        if len(found) > 1:
            msg = f"record {n}'s {label!r} finds {len(found)} nodes, not one"
            raise ValueError(msg)
        node = found[0] if found else None
    if etree.iselement(node):
        if len(node):
            msg = f"record {n}'s {label!r} is <{node.tag}> holding elements, not text"
            raise ValueError(msg)
        return node.findtext(".")  # its own text, as the stubs type it ("" for none)
    if node is None or isinstance(node, str):
        return node
    msg = f"record {n}'s {label!r} finds {node!r}, which is no text"
    raise ValueError(msg)
