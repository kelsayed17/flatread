"""The shared file readers, a module per format family: a provider reads every file it serves through them.

Each module imports only the ones below it (``pyproject.toml``'s layers contract), so a provider loads the parsers of
its own formats alone: a CSV-only provider never imports scrapling, pdfminer, lxml, fastexcel or xlrd.

* :mod:`~flatread.fields`: a row struct field's markers (``Label``, ``Format``, ...), with no polars, so
  a ``models.py`` names them cheaply;
* :mod:`~flatread.core`: :class:`~flatread.core.File`, the base every reader composes on,
  its cell typing, its text tables (``TextFile``, ``GridFile``) and ``Reshaped``, an inner reader's frame reshaped;
* :mod:`~flatread.layout`: ``LongFile``, a wide table read long;
* :mod:`~flatread.archives`: a file inside a zip, or written in a legacy encoding (or in the first of
  several that decodes it);
* :mod:`~flatread.delimited`, :mod:`~flatread.sheets`,
  :mod:`~flatread.fixed` and :mod:`~flatread.markup`: CSV, workbook, fixed-width and XML
  tables;
* :mod:`~flatread.reports` and :mod:`~flatread.found`: a table laid out for a page, and
  one whose header is found by its labels;
* :mod:`~flatread.pages`, :mod:`~flatread.pdf` and :mod:`~flatread.sdmx`:
  HTML pages, PDF text layers and SDMX messages.
"""
