# Changelog

Each release's changes, newest first, and what a reader declared for the release before must change. Releases before
6.0.0 have no entry here; their tags and commit messages describe them.

## 0.5.0

Numbering restarts at 0.5.0. Releases 1.0.0 through 7.2.0 came out within one week while the API was still being
found, so their numbers record that search, not stability. flatread stays at 0.x until its API holds still, and any
0.x minor may change it. A version resolver orders 7.2.0 above 0.5.0, so pin a tag (`@v0.5.0`), not a range.
The tags v1.0.0 through v7.2.0 stay as they are.

The `flatread` readers read every file exactly as 7.2.0 does. New:

- `flatread.next`, a preview: a reader is `Reader(Row, format, table=..., shape=..., cells=[...], source=...)`, a
  pipeline of frozen stage values instead of a class per quirk (README, "flatread.next (preview)"). Its formats are
  `Csv`, `Sheet`, `Layout` (fixed width), `Pdf` and `Sdmx`, and its stages are `Header`, `Titled`, `Trailer`, `Long`,
  `Observations`, `Derived`, `Padded` and `Zip`. One engine types every format's cells, and a reader checks msgspec
  `Meta` constraints itself.
- `flatread.next` numbers a row as its file does (a text file's line, a sheet's row), not from the first data row.
- Its fast paths: a CSV whose fields are all plain text, numbers or booleans is typed by polars directly, its rows
  counted by one regex over the body and checked against what polars read; a sheet of such fields loads once. Either
  path gives way to the general one on anything it cannot vouch for, so both read every body the general path does.
- A whole number past 64 bits in an int field is refused as a `ReadError`, where the general path raised polars'
  `InvalidOperationError`.

## 7.2.0

7.2 refuses, when a reader is built, declarations 7.1 read without a word, each reading a file otherwise than it
says. A reader 7.1 built may raise `LayoutError` now; none of these read its file as declared:

- A switch within two edits of one its pieces read (`_quotechar` for `_quote_char`), which 7.1 ignored. A format that
  keeps a value of its own under such a name annotates it in its class (`_header_alone: ClassVar[bool] = True`).
- A switch annotated as a boolean, a number, text or `Literal` values, set to none of them (`_closing_separator =
  "never"`, read as `None`; `_header = "no"`, a `TypeError` once read).
- A `CsvFile` separator or `_quote_char` that is not one character of one byte, ends a line, or is the other; a
  `CsvText` separator of more than one character.
- A field whose type names a msgspec `Meta` constraint (`ge`, `max_length`, `pattern`, `tz` and the rest), which
  `rows()` never checked: state a pattern as a `Shape`, values as a `Literal`. A `Meta` of descriptions alone is kept.
- A keyword-only struct (`kw_only=True`), whose `rows()` raised `TypeError`; a `Flag` whose two words are one, every
  cell read as false; a `_given` value not of its field's type (`3.7` for an `int`, read as `3`).

Three faults now raise the kind they are, each still a `ReadError`: an XML or SDMX body cut short is a
`TruncatedFileError`, XML bytes its encoding does not read an `EncodingError`, and a fixed-width line of another width
a `RaggedRowError`. Their messages are 7.1's.

## 7.1.0

7.1 adds five kinds of `ReadError`: `TruncatedFileError`, `ControlTotalError`, `UnknownLayoutError`,
`CorruptArchiveError` and `EncodingError`. Each is a `ReadError`, so code that catches one catches them, and the
messages are 7.0's. A `FoundCsv` cell past `csv`'s field size limit is a `ReadError`, where 7.0 let `csv.Error` out.

## 7.0.0

A reader declared for 6.0 reads the same files the same way, but for these:

- `HeadersCsv`, `NamedCsv`, `TrailerCsv`, `SpilledCsv` and `TitledCsv` have no constructor of their own: each takes
  `CsvFile`'s keywords, as before, and checks its switches in `File._check_switches()`. A misdeclared one raises the
  same `LayoutError` when built; where it has two faults, which one is named first may differ.
- A `NamedCsv`'s empty or mistyped cell is named by the header's label, as every other reader names it, not by its
  field.
- Blank handling is one switch per reader, in one vocabulary: `SheetFile._gaps = True` → `_blank_rows = "drop"`,
  `SheetFile._blank_rows = True` → `_blank_rows = "keep"` (the default is `"refuse"`); `ReportFile._skip_blank =
  False` → `_blank_lines = "read"` (the default is `"drop"`); `LongFile._empty_rows = False` → `_empty_cells = "drop"`
  (the default is `"keep"`); `LongFile._spacers` → `_spacer_columns`. An old name, or a value none of these, is a
  `LayoutError` when built, so a format not yet moved is not read otherwise.
- A `TitledSheet` title with a group (`count`, or one named for a field) is a `LayoutError` when built: a sheet's title
  states no field and counts no rows, which it ignored before. A `TitledSheet` with no `_titles` is one too.
- Seven readers and switches have plain names: `SheetsFile` → `EverySheet`, `CellHeaderSheet` →
  `TypedHeaderSheet`, `NumberTextSheet` → `TextNumbersSheet`, `OnlySheet` → `OneSheet`, `LongReport` → `KeyedReport`
  (it is no `LongFile`), `FoundTable._row_count` → `_numbered_rows`, `LongFile._bare_header_refused` →
  `_header_needs_rows`. The old names are gone.
- A name is a method on every piece that has it, or a value on every piece that has it, since pieces that compose
  share one class (`tests/test_switch_names.py` checks): `FixedWidthFile._stated` → `_outside_fields` (the fields
  the lines above or below its table state), `SdmxObservations._value` → `_value_label` (the observation's value
  attribute). The other six such names were internal. A switch set under its 6.0 name is a `LayoutError` when built.

## 6.0.0

6.0 makes 5.0's readers one public API. A reader declared for 5.0 reads the same files the same way, but for these:

- Every body error is a `ReadError`, every declaration error a `LayoutError`; `UnknownLayoutError` is gone.
- A CSV's boolean column is `true` or `false` exactly, as in every other reader (polars read `TRUE` and `tRuE` too).
- A CSV's mistyped cell, Flag or date error names the header's label and the row, not polars' message or the field.
- `GridFile`, `TitledGrid`, `FilledHeader`, `BlankEnded` and `BesideGrid` moved from `flatread.core` to
  `flatread.grids`, and `FoundSheet` from `flatread.found` to `flatread.sheets` (so `flatread.found` needs no extra).
- `HeadersCsv._headers`, each header's labels written out again, is `_headers_lacking`: each published header by the
  fields it lacks (`((), ("shares",))`), so a label is written once, in the struct. A lacked field must admit `None`.
- `NamedCsv._carried` is gone: a header must name the column of every field that holds a value on every row, which
  the struct says; `_required_fields` names any other field whose column every header names.
- The pieces compose in any order where they can (`HeadersCsv` before or after `PaddedCsv` or `TitledCsv`), and an
  order that cannot read is a `LayoutError` when built: list `LongFile` before `Carried`, and every piece before the
  CSV or fixed-width reader that ends the read. A field a trailer, title or zip member's name states is stated to
  every piece (`File._stated_fields()`), so `ZippedFiles` composes with `HeadersCsv`, `NamedCsv` and `LongFile`.
- A switch is checked when the reader is built (`File._check_switches()`): one left unset, a regex group naming no
  field, a `Shape` polars cannot compile, an unknown zone, a `Format` that writes what its field cannot hold, `%z`.
  A constructor that sets a switch (`_keys`, `_member`) sets it before calling `super().__init__()`.
- Stricter reads: an HTML number, date or boolean cell is one text run (`5.3<sup>1</sup>` raises, not `5.31`); a cut
  HTML page, a page's bytes that are not its declared charset, a title or trailer that is not UTF-8, and a `FoundCsv`
  line narrower than its header raise; a sheet's text cell in a number or boolean field raises; `%y` reads 69 as
  1969, as `strptime` does; `PaddedCsv` strips a cell before matching its tokens; `SectionedCsv` matches where a
  line begins; an SDMX-CSV's floats are finite but `obs_value`'s (`File._non_finite`).
- An error names the file's own row in every grid reader, and the header's label, not the field.
- `null_values=` is every text reader's constructor argument (XML, HTML, PDF, report, found, fixed-width).
- `SdmxData._member` is `_zipped_member` (it streams the member; `ZippedFile` composes too). `row_one` left
  `File.__init__`. The hooks' column, `flatread.core.Column`, is public.
- Renamed switches: `ReportFile._header` → `_header_lines`,
  `ReportFile._end` → `_last_line`; `LongReport._key` → `_key_field`; `HtmlFile._table` → `_table_selector`;
  `HtmlRecords._record`, `_heading`, `_stated` → `_record_selector`, `_heading_selector`, `_heading_pattern`;
  `FoundTable._section`, `_stated` → `_section_prefix`, `_section_field`; `FoundTable._nulls` → the `null_values=`
  constructor argument.
- `scalar_type`, `check_ending`, `text_sheet` and `SheetFile.frame_on` are private.
