"""flatread's next reader, a preview: a row struct and a pipeline of stage values, not a class hierarchy.

``Reader(Row, Csv(separator="|"), table=Trailer(...), shape=Long(...), cells=[Padded()], source=Zip(...))``: each
stage is one frozen value with one job, configured by keyword, and a stage of a format's own is a class implementing
its protocol (:class:`Source`, :class:`Format`, :class:`TableFinder`, :class:`HeaderRule`, :class:`Shape`,
:class:`Cleaner`, :class:`Records`), handed the read's :class:`ReadContext`. One :class:`Engine` types every format's
table. A finder's header rule reads a header that is not the struct's labels in order (``Header(header=Named())``).
Not yet stable: this module's names may change in any 0.x release.
"""

from flatread.next._engine import AsWritten, Engine
from flatread.next._headers import Headers, Named
from flatread.next._model import (
    Cells,
    Cleaner,
    Format,
    Grid,
    HeaderRule,
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
from flatread.next._pages import Html, HtmlRecords, HtmlRows
from flatread.next._reader import Reader
from flatread.next._reports import Keyed, Report, Text
from flatread.next._stages import (
    Csv,
    Cut,
    Derived,
    Found,
    Header,
    Layout,
    Long,
    Noted,
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
    "Cut",
    "Derived",
    "Engine",
    "Format",
    "Found",
    "Grid",
    "Header",
    "HeaderRule",
    "Headers",
    "Html",
    "HtmlRecords",
    "HtmlRows",
    "Keyed",
    "Kind",
    "Layout",
    "Lines",
    "Long",
    "Named",
    "Noted",
    "Observations",
    "Padded",
    "Pdf",
    "Preamble",
    "ReadContext",
    "Reader",
    "Records",
    "Report",
    "Sdmx",
    "Shape",
    "Sheet",
    "Source",
    "Stream",
    "Table",
    "TableFinder",
    "Text",
    "Thousands",
    "Titled",
    "Trailer",
    "Transcoded",
    "Zip",
]
