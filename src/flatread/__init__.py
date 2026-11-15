"""Read publishers' files strictly into typed rows: every cell its value, a missing-value token, or an error.

A reader is a msgspec row struct, whose fields say where each value comes from (:mod:`flatread.fields`), given to
the reader class for the file's shape. Every reader is a :class:`~flatread.core.File`, read two ways:
:meth:`~flatread.core.File.frame` (a polars DataFrame of the struct's fields) and :meth:`~flatread.core.File.rows`
(a list of the struct). A file's quirks are switches, class attributes a subclass sets, and the readers compose by
inheritance: ``class _Volumes(TrailerCsv[Row], HeadersCsv[Row])`` is a CSV under one of several headers, closed by a
trailer.

A module per format family, each importing only the ones below it (``pyproject.toml``'s layers contract), so a reader
loads the parsers of its own format alone; a CSV-only install loads no HTML, PDF, XML or workbook library.

* :mod:`~flatread.errors`: :class:`~flatread.errors.ReadError` for a bad body, :class:`~flatread.errors.LayoutError`
  for a reader declared wrong;
* :mod:`~flatread.fields`: a row field's markers (``Label``, ``Format``, ``Flag``, ``Span``, ...), with no polars;
* :mod:`~flatread.core`: ``File``, ``TextFile`` and the pieces every format shares (``Thousands``, ``Carried``,
  ``Reshaped``);
* :mod:`~flatread.grids`: a table cut from a grid of text cells, and the pieces that find and end it;
* :mod:`~flatread.layout`: ``LongFile``, a wide table read long;
* :mod:`~flatread.archives`: a file inside a zip (whole or streamed), or in a legacy encoding;
* :mod:`~flatread.found`: a table whose header is found by its labels;
* :mod:`~flatread.delimited`, :mod:`~flatread.fixed`, :mod:`~flatread.reports`: CSV and delimited text, fixed-width
  text, and tables laid out for print;
* :mod:`~flatread.sheets` (the ``excel`` extra): xlsx and xls workbooks;
* :mod:`~flatread.markup` and :mod:`~flatread.sdmx` (``xml``): XML tables and SDMX messages;
* :mod:`~flatread.pages` (``html``): HTML tables and records;
* :mod:`~flatread.pdf` (``pdf``): a PDF's text layer.
"""
