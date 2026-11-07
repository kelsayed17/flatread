"""A row struct's DataFrame schema, and a frame's typed rows.

* :func:`frame_schema` is a row struct's DataFrame schema, and :func:`frame_rows` turns a frame of its columns into
  typed rows, for a table that is no file (an HTML page's links, a JSON record list);
* :func:`typed_rows` and :func:`typed_file` read a file's frame, under its own labels, back into typed rows.
"""

from typing import get_args, get_origin

import msgspec
import polars as pl

from flatread.cells import column_specs, dtype_of, resolve_type

__all__ = ["frame_rows", "frame_schema", "typed_file", "typed_rows"]


def frame_schema(row_type: type[msgspec.Struct]) -> pl.Schema:
    """Return the DataFrame schema of *row_type*'s rows: one column per field, named and typed as the field is.

    A frame reader reads its file straight into this schema, so the frame's columns are the row struct's fields and the
    struct stays the one statement of the wire's shape. A NewType is its supertype's column (a ``NewType("Cusip",
    str)`` is a string), and ``X | None`` is ``X``'s.
    """
    return pl.Schema({field.name: _dtype(field.type) for field in msgspec.structs.fields(row_type)})


def frame_rows[RowT: msgspec.Struct](frame: pl.DataFrame, row_type: type[RowT]) -> list[RowT]:
    """Return *frame*'s rows, one *row_type* per row, in order.

    The frame's columns are *row_type*'s fields in order (:func:`frame_schema`), so a row's values build the struct
    positionally. Each column becomes Python values in one call and the rows are zipped from them, which measured 1.6
    times as fast as ``iter_rows`` over a million rows (2026-11).
    """
    return list(map(row_type, *(column.to_list() for column in frame.iter_columns()), strict=True))


def typed_rows[RowT: msgspec.Struct](frame: pl.DataFrame, row_type: type[RowT]) -> list[RowT]:
    """Return a file's frame as *row_type* rows: each column under its label read back into its field, in file order.

    The typed form of a frame read under the file's own labels: *row_type* declares each column (its
    :class:`~flatread.cells.Column`), so each field takes the frame column its label names, or one of its alternative
    labels. A label the row declares more than once takes polars' name for its repeat, in order (``Filler``, then
    ``Filler_duplicated_0`` from a delimited file or ``Filler_1`` from a sheet). A field whose column the file left out
    (an ``optional`` one) is ``None`` on every row. Any other field whose column the frame has not raises
    :class:`ValueError`, though it may be ``None``.
    """
    seen: dict[str, int] = {}
    columns: list[pl.Expr] = []
    for spec in column_specs(row_type):
        found = None
        for label in spec.labels:
            repeat = seen.get(label, 0)
            names = (label,) if repeat == 0 else (f"{label}_duplicated_{repeat - 1}", f"{label}_{repeat}")
            found = next((name for name in names if name in frame.columns), None)
            if found is not None:
                seen[label] = repeat + 1
                break
        if found is not None:
            columns.append(pl.col(found).alias(spec.name))
        elif spec.column.optional:
            columns.append(pl.lit(None).alias(spec.name))
        else:
            msg = f"the frame has no column for {row_type.__name__}.{spec.name} ({spec.labels[0]!r}): {frame.columns}"
            raise ValueError(msg)
    return frame_rows(frame.select(columns), row_type)


def typed_file[FileT: msgspec.Struct](file: msgspec.Struct, file_type: type[FileT]) -> FileT:
    """Return a file struct holding its rows as a DataFrame as *file_type*, which holds them as typed rows.

    *file_type*'s fields are *file*'s values by name, and its one ``list[Row]`` field is *file*'s one DataFrame,
    read back by :func:`typed_rows`: the typed twin of a file read frames-only.
    """
    fields = msgspec.structs.fields(file)
    frames = [field.name for field in fields if isinstance(getattr(file, field.name), pl.DataFrame)]
    lists = [
        (field.name, args[0])
        for field in msgspec.structs.fields(file_type)
        if get_origin(field.type) is list
        and (args := get_args(field.type))
        and isinstance(args[0], type)
        and issubclass(args[0], msgspec.Struct)
    ]
    if len(frames) != 1 or len(lists) != 1:
        held = f"{len(frames)} DataFrames, {file_type.__name__} {len(lists)} row lists"
        msg = f"{type(file).__name__} holds {held}: not one each"
        raise TypeError(msg)
    ((frame_name,), ((rows_name, row_type),)) = (frames, lists)
    values = {field.name: getattr(file, field.name) for field in fields if field.name != frame_name}
    return file_type(**values, **{rows_name: typed_rows(getattr(file, frame_name), row_type)})


def _dtype(annotation: object) -> pl.DataType:
    """Return the polars type of a field annotated *annotation*, as :func:`msgspec.inspect.type_info` reads it.

    A NewType is its supertype's column and ``X | None`` is ``X``'s; the column is the one a cell of the type reads
    into (:func:`~flatread.cells.dtype_of`).
    """
    try:
        resolved, _ = resolve_type(msgspec.inspect.type_info(annotation))
    except TypeError:
        resolved = None
    if resolved is not None and (dtype := dtype_of(resolved)) is not None:
        return dtype
    msg = f"no frame column type for a field annotated {annotation!r}"
    raise TypeError(msg)
