"""flatread's next reader, a preview: a row struct and a pipeline of stage values, not a class hierarchy.

``Reader(Row, Csv(separator="|"), table=Trailer(...), shape=Long(...), cells=[Padded()], source=Zip(...))``: each
stage is one frozen value with one job, configured by keyword, and a stage of a format's own is a class implementing
its protocol (:class:`Source`, :class:`Format`, :class:`TableFinder`, :class:`Shape`, :class:`Cleaner`,
:class:`Records`), handed the read's :class:`ReadContext`. One :class:`Engine` types every format's table. Not yet
stable: this module's names may change in any 0.x release.
"""

from flatread.next._engine import AsWritten, Engine
from flatread.next._model import (
    Cells,
    Cleaner,
    Format,
    Grid,
    Kind,
    Lines,
    ReadContext,
    Records,
    Shape,
    Source,
    Stream,
    Table,
    TableFinder,
)
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
    Preamble,
    Sdmx,
    Sheet,
    Thousands,
    Titled,
    Trailer,
    Transcoded,
    Zip,
)

__all__ = [
    "AsWritten",
    "Cells",
    "Cleaner",
    "Csv",
    "Derived",
    "Engine",
    "Format",
    "Grid",
    "Header",
    "Kind",
    "Layout",
    "Lines",
    "Long",
    "Observations",
    "Padded",
    "Pdf",
    "Preamble",
    "ReadContext",
    "Reader",
    "Records",
    "Sdmx",
    "Shape",
    "Sheet",
    "Source",
    "Stream",
    "Table",
    "TableFinder",
    "Thousands",
    "Titled",
    "Trailer",
    "Transcoded",
    "Zip",
]
