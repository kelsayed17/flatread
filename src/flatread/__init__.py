"""flatread: read the flat files publishers serve exactly, or fail loudly.

A file is declared once, by the msgspec structs its rows (and, where it states values of itself, the file) decode into,
and read into those typed rows or a polars DataFrame of the same columns. Every cell is its value, a token the column
declares stands for none, or an error naming the line, the column and the text: nothing is silently ``None``.

* :mod:`flatread.delimited`: a delimited text file, ``pl.read_csv`` then the cells (``read_csv``, ``stream_csv``).
* :mod:`flatread.cells`: ``Column``, the declaration on a row-struct field, and ``read_table``, the strict cell reader.
* :mod:`flatread.frames`: a row struct's frame schema, and a frame read back into typed rows.
* :mod:`flatread.text`: a body's text, and the lines a file writes about itself around its rows.
* :mod:`flatread.archives`: a ZIP archive's files, whole or streamed.
* :mod:`flatread.workbooks`: an Excel sheet, its every cell's exact text (the ``excel`` extra).
* :mod:`flatread.pdf`: a PDF's text in the order its pages draw it (the ``pdf`` extra).
* :mod:`flatread.errors`: :class:`~flatread.errors.ReadError`, a ``ValueError``, and the errors under it.

Import each name from its module: the package itself exports none.
"""

__all__: list[str] = []
