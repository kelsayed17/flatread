# Changelog

Each release's changes, newest first, and what a reader declared for the release before must change. Releases before
6.0.0 have no entry here; their tags and commit messages describe them. What a patch and a minor release may change is
in the README, under "Versions".

## Unreleased

`flatread.next` reads a CSV's cells as 7.x's `CsvFile` reads them, where it read them as a text table's:

- A number cell padded with whitespace (` 5`, `5 `, a tab) is refused, naming its cell and row, where it was trimmed;
  a `Padded()` cleaner reads it, as 7.x's `PaddedCsv` did. A text cell of spaces is that text, not no value. Both the
  typed fast path and the grid read this way, where polars' parser took ` 5` as 5: an unquoted body where a cell may
  open with a space or a tab is read as text and its numbers cast, and a quoted body's row check gives way to the grid
  on such a cell. That check's pattern is larger, so a quoted CSV of about 1,000 columns or more (was about 3,000) is
  read by the grid.
- A float cell that underflows (`1e-400`, which polars reads as `0.0`) is refused in every format, where the fast
  path and the grid read `0.0`.
- A body opening with two byte order marks raises `EncodingError`, where polars dropped the second too.
- `Csv(closing="always" | "sometimes")` reads a separator closing every line, or some, as 7.x's `_closing_separator`
  did; a line without it under `"always"` raises `RaggedRowError`, naming the line. `Headers(blank=[""])` stood in for
  it, and took a blank column anywhere in the header.

A `flatread.next` sheet's date-and-time or time field may name no `Format`, as a 7.x `SheetFile`'s may: it reads the
sheet's date cells alone, and refuses a text cell, naming it. A text format's such field still needs its `Format`.
A sheet's number cell is read as 7.x reads it in two more places: under a date field with a `Format` of its own, its
digits are text in that format (`198803` for `%Y%m`), and one that is a word for none (a stored `NaN` under
`null_values=["NaN"]`) holds none, where both were refused.

`Sheet(text_numbers=True)` reads a sheet that writes its numbers as text as well as in number cells, as 7.x's
`TextNumbersSheet` does: a number field's text cell is its text's number, strictly (an int's digits after an optional
`-`, of 64 bits, digit for digit; a float's a plain decimal, no exponent, `nan` or `inf`), and any other text refused,
naming it. With `cells=[Padded()]` it reads what 7.x's `PaddedSheet` and `TextNumbersSheet` read together.

`Noted(header_row=..., ends="bare" | "undated")` finds a table with notes under it, as 7.x's `NotedSheet` and
`DatedSheet` do: its rows end at the first holding nothing past its first column, or at the first whose first cell is
no date cell (a sheet's alone), and a cell past the first column among the notes below raises, naming its row.

`Header(header_row=...)` drops the rows above a header row unread, as 7.x's `header_row` does. `Beside(at=..., table=...)` reads a sheet's table with more beside it, as 7.x's `BesideSheet` does: only the table's
columns are kept (the first, one a field, or those `at` names), and `table` finds the table in them, so
`Beside(table=Noted())` reads a table with notes under it and more beside it.

In the `flatread` readers: `TitledCsv` refuses a blank line below its titles, where polars skipped it before the header.
In both, a body of CRLF lines that ends in a CR alone raises `TruncatedFileError`: it was cut between the CR and the
LF, and `final_line_ending=True` took the CR for a whole line ending.

## 0.5.0

Numbering restarts at 0.5.0. Releases 1.0.0 through 7.2.0 came out within one week while the API was still being
found, so their numbers record that search, not stability. flatread stays at 0.x until its API holds still, and any
0.x minor may change it. A version resolver orders 7.2.0 above 0.5.0, so pin a tag (`@v0.5.0`), not a range.
The tags v1.0.0 through v7.2.0 stay as they are.

The `flatread` readers read every file as 7.2.0 does but two, each below: a sheet's `int` field refuses a number cell
at or past 2\*\*53, and a workbook whose zip lists a name twice is refused. New:

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

Also in 0.5.0:

- `flatread.next` reads a header that is not the struct's labels in order: every table finder (`Header`, `Preamble`,
  `Titled`, `Trailer`) takes a `header=` rule. `Headers(lacking=...)` reads a file published under several headers,
  each the struct's labels in order with some left out, as `HeadersCsv` does; `Named(...)` one whose header names its
  columns in any order, as `NamedCsv` does, with `Label` alternatives, a `heading` that folds each cell, and fields
  `required` in every header. Both drop `unread` columns and check that `blank` ones hold nothing, which next lacked.
  A format whose headers follow a rule of their own implements `HeaderRule`.
- `flatread.next` cuts a table from an irregular grid, as `CsvText`, `SheetText` and `GridFile`'s pieces do:
  `Cut(header_row=..., above=..., title=..., filled=..., ends="blank", band=..., full_rows=...)`, one finder for a CSV
  and a sheet, which keeps a sheet's cell kinds. `Csv(misquoted=True)` reads a stray quote as text, as `CsvText`'s
  `_misquoted` does, and `Long` reads the record columns of a header over several rows by their last line.
- `flatread.next`'s `Long` takes `LongFile`'s switches: `key_count` (a header naming several keys, each from its
  group), `spacers`, `scattered` and `beside` record columns, `repeats` and `needs_rows`. A sheet's key is its header
  text typed as the key field, as `LongFile` types it; next refused an `int` key on a sheet before.
- `flatread.next` finds an export's header by its key field's words, as `FoundTable` (`FoundCsv`, `FoundSheet`) does:
  `Found(key=..., heading=..., open=..., section=..., section_field=..., numbered_rows=...)`. A finder or header rule
  that reads by name sets `by_name`, and only then may a `Label` name alternatives.
- `flatread.next` reads a report laid out for a page, as `ReportFile` and `KeyedReport` do: the `Text` format reads a
  file of lines, the `Report` finder its rows among header and framing lines (a format checks more by subclassing it
  and overriding `check`), and the `Keyed` shape a report's runs of values long, a row per key.
- `flatread.next` reads a label two fields share by place, as 7.x does (a header that writes `%` twice); a header
  read by name refuses one, which it cannot tell apart.
- `flatread.next` reads HTML pages, as `HtmlFile` and `HtmlRecords` do: the `Html` format reads a page's one table
  (`table=` its selector), and `HtmlRecords` records that are elements (`record=`, under `heading=` headings matching
  `heading_pattern`). Each cell is read by its field: a `Link` field's as its link's `href`, a field that is no text's
  as one text run, and fields sharing a cell each their own way. A format of its own subclasses either, overriding
  `tables`, `rows` (returning `HtmlRows`), `records`, `is_record` or `value`. A header cell no field reads is text,
  for a header rule to drop, or, with `unread_cells="value"`, one text run, for `Long` to type.

## 0.5.0

Numbering restarts at 0.5.0. Releases 1.0.0 through 7.2.0 came out within one week while the API was still being
found, so their numbers record that search, not stability. flatread stays at 0.x until its API holds still, and any
0.x minor may change it. A version resolver orders 7.2.0 above 0.5.0, so pin a tag (`@v0.5.0`), not a range.
The tags v1.0.0 through v7.2.0 stay as they are.

The `flatread` readers read every file as 7.2.0 does but two, each below: a sheet's `int` field refuses a number cell
at or past 2\*\*53, and a workbook whose zip lists a name twice is refused. New:

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

Also in 0.5.0:

- **A sheet's number cell in an `int` field must hold a whole number below 2\*\*53**, and one at or past it raises
  `ReadError`. A number cell is a double, so `9007199254740993` written in a workbook loaded as `9007199254740992`
  and read with no error; 7.2.0 read every whole double up to 2\*\*63. Excel holds 15 digits, so no workbook
  Excel writes holds such a number; a reader of one that does must read the column as text: a `str` field, marked
  `AsWritten()` in `flatread.next`. This holds for `SheetFile`, the sheet readers built on it, and `flatread.next`'s `Sheet`.
- `flatread.next` gains three stages the class-per-quirk readers had: `Thousands()`, a cleaner for numbers grouped in
  thousands (`ThousandsCsv`, `Thousands`); `Zip(member, bare=True)`, a file published zipped or bare
  (`ZippedOrBare`); and `Transcoded(encodings, eof=, lossy=, source=)`, a file in a legacy encoding, the body or a
  zip's member (`Transcoded`).
- `flatread.next` gains `Preamble(label=None)`, a table finder for a header below a preamble of any length, one stage
  for a CSV and a sheet (`PreambleCsv`, `PreambleSheet`). A CSV's blank first line is now the table finder's to
  refuse, not `Csv`'s, so a preamble may open with one; `Header()` refuses it as before.
- `flatread.next` reads no sheet number cell into a `str` field unless the field is marked `AsWritten()`: a ZIP code
  or CUSIP stored as a number has lost its leading zeros, so it is refused, not read as `2134`. `SheetFile` still
  reads such a cell as its number's text (design-review finding 1, left to 7.x as it is).
- `flatread.next`'s `Csv` reads a cell holding a vertical tab, a form feed, a line or paragraph separator, or a lone CR
  as one cell, as polars and `CsvFile` do. Before, it broke the row there and raised `RaggedRowError`.
- The README states the versioning policy (a "Versions" section): what a 0.x patch and minor release may change, and
  what 1.0.0 waits for.
- Property tests (`tests/test_properties.py`, hypothesis) read generated tables back exactly, and damaged ones and
  bytes of no format as rows or a `ReadError`, through the CSV, sheet, XML and HTML readers of both APIs, and hold
  the two CSV readers to the same rows. They found the two defects above.
- A workbook whose zip lists a member's name twice raises `CorruptArchiveError`, as every other zip a reader opens
  does. Before, a sheet reader opened it.
- Importing `flatread.pdf` no longer changes pdfminer for any other caller: outside a flatread read, its stream decoders
  are pdfminer's own again, logging included. The caps and the linear-time `decompress_corrupted` apply only during a
  flatread read.
- A `ReadError` carries `row`, `label` and `member` as attributes beside its message, each `None` where not known. A
  `flatread.next` reader sets all three wherever its message names them, numbering a row as its file does; the other
  readers set `row` and `label` on a cell's error, numbering rows as their messages do.
- A `LayoutError` raised inside a read now stays a `LayoutError` at the five places that turn a `ValueError` into a
  `ReadError` naming where. No read is known to reach them with one, so no reader is known to read differently.
- One test of a Rust panic (`flatread.core._panicked`) serves every reader. Before, the core, sheet and
  `flatread.next` readers each had their own.

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
