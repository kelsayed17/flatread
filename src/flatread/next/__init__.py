"""flatread 8.0's reader, a prototype: a row struct and a pipeline of stage values, not a class hierarchy.

``Reader(Row, Csv(separator="|"), table=Trailer(...), shape=Long(...), cells=[Padded()])``: each stage is one frozen
value with one job, configured by keyword, and a stage of a format's own is a class implementing its protocol
(:class:`Format`, :class:`TableFinder`, :class:`Shape`, :class:`Cleaner`, :class:`Source`), handed the read's
:class:`ReadContext`. Not yet stable: this module's names may change in any release until 8.0.
"""

from flatread.next._model import Cleaner, Format, Grid, ReadContext, Records, Shape, Source, Table, TableFinder
from flatread.next._reader import Reader
from flatread.next._stages import (
    Csv,
    Derived,
    Header,
    Layout,
    Long,
    Observations,
    Padded,
    Pdf,
    Sdmx,
    Sheet,
    Titled,
    Trailer,
)

__all__ = [
    "Cleaner",
    "Csv",
    "Derived",
    "Format",
    "Grid",
    "Header",
    "Layout",
    "Long",
    "Observations",
    "Padded",
    "Pdf",
    "ReadContext",
    "Reader",
    "Records",
    "Sdmx",
    "Shape",
    "Sheet",
    "Source",
    "Table",
    "TableFinder",
    "Titled",
    "Trailer",
]
