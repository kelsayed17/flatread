# Reader examples

Each section below is one reader, or one of its switches, shown on a short excerpt of a real file that needs it: the
excerpt's file and where it was recorded (a cassette in prism-clients, where every excerpt was taken), the reader that
reads it (linked to its module) and the prism-clients class that uses it, then the excerpt itself. Near-twins link
each other and say the difference in one line. [`README.md`](../../README.md) introduces the row struct, the markers
and how the readers compose.

The files keep the recording's bytes. Here each is shown as text: CRLF as LF, and a byte-order mark and DOS's
end-of-file byte dropped. Workbooks, PDFs and zips are text renderings: a sheet's rows are `<row>: cell | cell`,
numbered as the sheet numbers them, a text cell quoted where its type matters. `tests/test_reader_examples.py` checks
that every link here resolves, that each block matches its file, and that every reader and switch a heading names is
one flatread has.

## Delimited files

### `CsvFile`

[`csv_file.csv`](csv_file.csv), recorded in prism-clients' `tests/cboe/cassettes/test_recorded/test_body[VX_2014-01-22].yaml`, interaction 0. Read by [`CsvFile`](../../src/flatread/delimited.py), in cboe `_FuturesContractCsv`.

A clean CSV: one header line, then a row per line, CRLF.

```
Trade Date,Futures,Open,High,Low,Close,Settle,Change,Total Volume,EFP,Open Interest
2013-04-22,F (Jan 2014),19.6500,19.7000,19.5500,19.5500,0,0,4,0,4
2013-04-23,F (Jan 2014),19.7000,20.2000,19.2000,19.3500,0,0,363,0,303
2013-04-24,F (Jan 2014),19.1800,19.2500,19.1500,19.1500,0,0,20,0,322
```

### `CsvFile._quote_char` (`None`)

[`csv_file-quotechar.txt`](csv_file-quotechar.txt), recorded in prism-clients' `tests/federal_reserve/cassettes/test_recorded/test_file[iba_199703].yaml`, interaction 0. Read by [`CsvFile._quote_char`](../../src/flatread/delimited.py), in federal_reserve `_IbaOfficesTsv`.

A TSV whose 1997 FBO name holds an unpaired quote (`B.A., "RABOB`); read with `"` as the quote character, polars fails ("expected 102 rows, actual 103").

```
US_OFFICE_TYPE_CD	US_OFFICE_TYPE_DESC	US_OFFICE_NAME	US_OFFICE_LOCATION	US_OFFICE_STATE	US_OFFICE_DISTRICT	US_OFFICE_ASSETS	A_CODE	G_CODE	H_CODE	L_CODE	X_CODE	FBO_NAME	FBO_TYPE	FBO_TYPE_DESC	FBO_COUNTRY	TOP_NAME	TOP_TYPE	TOP_TYPE_DESC	TOP_COUNTRY
AGI	AGREEMENT CORP-INVESTMENT	BANK OF MONTREAL GLOBAL	CHICAGO, IL	IL	7	0	 	 	H	 	 	BANK OF MONTREAL	FBH	FOREIGN BANKING ORGANIZATION AS A BHC	CANADA	 	 	 	
AGI	AGREEMENT CORP-INVESTMENT	BNP MEXICO HOLD	NEW YORK, NY	NY	2	17	 	 	 	 	 	BANQUE NATIONALE DE PARIS	FBH	FOREIGN BANKING ORGANIZATION AS A BHC	FRANCE	 	 	 	
REP	REPRESENTATIVE OFFICE	RABOBANK NEDERLAND ATL REP OFF	ATLANTA, GA	GA	6	0	 	 	 	 	 	COOPERATIEVE CENTRALE RAIFFEISEN-BOERENLEENBANK B.A., "RABOB	FBO	FOREIGN BANKING ORGANIZATION	NETHERLANDS	 	 	 	
REP	REPRESENTATIVE OFFICE	RABOBANK NEDERLAND CHICAGO REP	CHICAGO, IL	IL	7	0	 	 	 	 	 	COOPERATIEVE CENTRALE RAIFFEISEN-BOERENLEENBANK B.A., "RABOB	FBO	FOREIGN BANKING ORGANIZATION	NETHERLANDS	 	 	 	
```

### `CsvFile._header` (`False`)

[`csv_file-header.txt`](csv_file-header.txt), recorded in prism-clients' `tests/occ/cassettes/test_recorded/test_endpoint[daily_delo_download].yaml`, interaction 0. Read by [`CsvFile._header`](../../src/flatread/delimited.py), in occ `_DailyDelo`.

OCC's daily listed-products directory: TAB-separated rows with no header line, read by position.

```
1AAL	AAL	American Airlines Group, Inc. (AMER/FLEX)	ABCIPX	000025000000	EF
1AAPL	AAPL	Apple Inc (AMER/FLEX)	ABCIPX	000025000000	EF
1ABEO	ABEO	ABEONA THERAPEUTICS INC. (AMER/FLEX)	ABCIPX	000025000000	EF
1ACAD	ACAD	ACADIA Pharmaceuticals Inc. (AMER/FLEX)	ABCIPX	000025000000	EF
```

### `CsvFile._closing_separator` (`"always"`)

[`csv_file-closingseparator.csv`](csv_file-closingseparator.csv), recorded in prism-clients' `tests/occ/cassettes/test_recorded/test_endpoint[stock_loan_bal_by_security].yaml`, interaction 0. Read by [`CsvFile._closing_separator`](../../src/flatread/delimited.py), in occ `_EndedCsv`.

Every line, the header too, ends with one comma more: read as a last column with an empty label that never holds a value. Unlike [`File._blank_columns`](#file_blank_columns), the closing cell has no label of its own.

```
Symbol,Market Loan-Loan Balance,Hedge-Loan Balance,
A     ,7142100.00,26296100.00,
AA    ,3390700.00,6095450.00,
AAAU  ,0.00,8692000.00,
```

### `CsvFile._closing_separator` (`"sometimes"`)

[`csv_file-closingseparator-sometimes.csv`](csv_file-closingseparator-sometimes.csv), recorded in prism-clients' `tests/cboe/cassettes/test_recorded/test_body[CFE_M05_VX].yaml`, interaction 0. Read by [`CsvFile._closing_separator`](../../src/flatread/delimited.py), in cboe `_FuturesArchiveCsv`.

From 2005-04-14 the rows end in an empty cell past the header's last: the comma closing a line is dropped where written. With `"always"`, the header's line ends with it too.

```
Trade Date,Futures,Open,High,Low,Close,Settle,Change,Total Volume,EFP,Open Interest
4/12/2005,M (Jun 05),146.9,147,144,144,142.7,-3.5,27,0,323
4/13/2005,M (Jun 05),144,144,143.7,144,145.1,2.4,110,0,328
04/14/2005,M (Jun 05),145.50,152.40,145.50,152.40,152.40,7.30,28,0,316,
04/15/2005,M (Jun 05),153.80,158.10,153.80,156.70,159.50,7.10,68,0,342,
```

### `File._blank_columns`

[`file-blankcolumns.csv`](file-blankcolumns.csv), recorded in prism-clients' `tests/nasdaqtrader/cassettes/test_recorded/test_dated[get_regsho_nasdaqth-20050113].yaml`, interaction 0. Read by [`File._blank_columns`](../../src/flatread/core.py), in nasdaqtrader `_ThresholdCsv` (`("Filler", "Filler")`).

Two `Filler` columns no field reads, empty on every row: each is checked empty, then dropped. A column [`File._unread`](#file_unread) drops may hold anything.

```
Symbol|Security Name|Market Category|Reg SHO Threshold Flag|Filler|Filler
AAII|AAIPHARMA INC|Q|Y||
ABFI|AMERN BUSINESS FINL SVCS INC|Q|Y||
ABSSF|AIRBOSS OF AMERICA CP NEW(F)|u|Y||
```

### `HeadersCsv`

[`headers_csv.txt`](headers_csv.txt), recorded in prism-clients' `tests/finra/cassettes/test_recorded/test_file[FNYX-20090803].yaml`, interaction 0. Read by [`HeadersCsv`](../../src/flatread/delimited.py), in finra `_ShortVolumeCsv`.

The pre-2011 header, without `ShortExemptVolume` (that field reads as null), then rows and the trailer count `3108`, which has no line ending. The count is the whole file's, so a read of the excerpt alone fails it.

```
Date|Symbol|ShortVolume|TotalVolume|Market
20090803|A|2400|22200|N
20090803|AA|50521|153112|N
20090803|AAI|2900|12100|N
3108
```

### `NamedCsv`

[`named_csv.csv`](named_csv.csv), recorded in prism-clients' `tests/cboe/cassettes/test_recorded/test_body[vixpc].yaml`, interaction 0. Read by [`NamedCsv`](../../src/flatread/delimited.py), in cboe `_VixPutCall` (`_PutCallCsv`).

The header writes the ratio second (`Date,VIX Put/Call Ratio,VIX Put Volume,VIX Call Volume,...`), so columns are matched by name, not by place.

```
Cboe Volume and Put/Call Ratio data is provided for informational purposes only. The volume data reflected below may be based upon preliminary reported volume rather than cleared volume. Cboe makes no guarantee as to the accuracy of the data. Your use of Cboe Volume and Put/Call Ratio data is subject to the Terms and Conditions of the Cboe Website.,,,,
Date,VIX Put/Call Ratio,VIX Put Volume,VIX Call Volume,Total VIX Options Volume
2/24/2006,1.18,5095,4328,9423
2/27/2006,0.01,71,6996,7067
```

### `NamedCsv._required_fields`

[`named_csv-carried.csv`](named_csv-carried.csv), recorded in prism-clients' `tests/federal_reserve/cassettes/test_recorded/test_file[sfos_202601].yaml`, interaction 0. Read by [`NamedCsv._required_fields`](../../src/flatread/delimited.py), in federal_reserve `_Sfos`.

The January 2026 survey's header is only the seven columns every header names; `agg_desc_2`, `agg_desc_3`, `dom` and `fore` are missing, so they read as null.

```
survey_date,question_no,question_desc,response_option,agg_type,agg_desc_1,all
'January 2026',Q3,"If the discount window is only open for parts of the day on Sundays and weekday holidays, what do you consider are the most important hours to request and receive loans?",8:00 a.m. ET to noon ET,count,,6
'January 2026',Q3,"If the discount window is only open for parts of the day on Sundays and weekday holidays, what do you consider are the most important hours to request and receive loans?",Noon ET to 4:00 p.m. ET,count,,9
'January 2026',Q3,"If the discount window is only open for parts of the day on Sundays and weekday holidays, what do you consider are the most important hours to request and receive loans?",4:00 p.m. ET to end of day for Fedwire Funds (7:00 p.m. ET),count,,11
```

### `TrailerCsv`

[`trailer_csv.txt`](trailer_csv.txt), recorded in prism-clients' `tests/cboe/cassettes/test_recorded/test_body[reg_sho_threshold_20261106].yaml`, interaction 0. Read by [`TrailerCsv`](../../src/flatread/delimited.py), in cboe `_ThresholdCsv`.

A `\ | ` table whose last line is the 14-digit generated-at stamp (`20261106030225`), read onto every row.

```
Symbol|CompanyName
AAOX|Tradr 2X Long AAOI Daily ETF
AMPU|Defiance Daily Target 2X Long AMPX ETF
APLX|Tradr 2X Long APLD Daily ETF
20261106030225
```

### `SectionedCsv`

[`sectioned_csv.csv`](sectioned_csv.csv), recorded in prism-clients' `tests/eia/cassettes/test_recorded/test_file[wpsr-table1.csv].yaml`, interaction 1. Read by [`SectionedCsv`](../../src/flatread/delimited.py), in eia `_Table`.

Two tables back to back, each opening with its own `"STUB_1",...` header of a different width (stocks, then supply).

```
"STUB_1","10/30/26","10/23/26","Difference","Percent Change","10/31/25","Difference","Percent Change"
"Crude Oil","691.944","693.208","-1.264","-0.200","830.763","-138.819","-16.700"
"Commercial (Excluding SPR)","409.760","410.625","-0.865","-0.200","421.168","-11.408","-2.700"
"Total Stocks (Excluding SPR)","1,204.374","1,212.556","-8.182","-0.700","1,269.378","-65.004","-5.100"
"STUB_1","STUB_2","10/30/26","10/23/26","Difference","10/31/25","Difference","10/30/26","10/31/25","Percent Change","10/30/26","10/31/25","Percent Change"
"Crude Oil Supply ","(1)     Domestic Production","14,003","13,992","11","13,651","352","13,736","13,640","0.7","13,746","13,463","2.1"
"Crude Oil Supply ","(2)        Alaska","487","482","5","436","51","487","430","13.1","434","419","3.4"
"Crude Oil Supply ","(3)        Lower 48","13,516","13,510","6","13,215","301","13,249","13,210","0.3","13,312","13,044","2.1"
```

### `TitledCsv`

[`titled_csv.csv`](titled_csv.csv), recorded in prism-clients' `tests/nasdaqtrader/cassettes/test_recorded/test_file[get_bxo_lmm].yaml`, interaction 0. Read by [`TitledCsv`](../../src/flatread/delimited.py), in nasdaqtrader `_BxoLmmCsv`.

Three known title lines (an as-of date, `Number of Issues - 3448` as the `count` group, a blank line), each matched by its regex, then the header. The count is the whole file's, so a read of the excerpt alone fails it.

Its twins: [`PreambleCsv`](#preamblecsv) drops any number of unknown lines up to the first opening with the first label, where this matches a known regex per line; [`FoundTable`](#foundtable) finds the header by its key field's words and places each column by its label's; [`TitledGrid`](#titledgrid) finds one table of several by the row that is its title alone.

```
,NOBO Option Symbols as of 11/09/2026
,Number of Issues - 3448

Symbol,Company Name, Lead Market Maker
A,"AGILENT TECH INC","WOLVERINE TRADING, LLC"
AA,"ALCOA CORP","IMC-CHICAGO, LLC"
AAAU,"Goldman Sachs Physical Gold ETF Shares","IMC-CHICAGO, LLC"
```

### `PreambleCsv`

[`preamble_csv.csv`](preamble_csv.csv), recorded in prism-clients' `tests/federal_reserve/cassettes/test_recorded/test_file[feds_note_FED_Note_Term_SOFR].yaml`, interaction 0. Read by [`PreambleCsv`](../../src/flatread/delimited.py), in federal_reserve `_Long` (`_Wide`, `FED_Note_Term_SOFR`).

A preamble of free-form lines (a disclaimer, empty `,,,,,,` lines, notes) above the line that opens with `DATE`, the first label. Source lines 2-4 are dropped: three quoted disclaimer paragraphs of 350-767 characters.

Its twins: [`TitledCsv`](#titledcsv) matches each line above the header against a known regex and may state a field from it; [`FoundTable`](#foundtable) finds the header by its key field's words, wherever it stands.

```
Disclaimer:,,,,,,
,,,,,,
,,,,,,
FORWARD_XM:  Forward-looking term rates derived from end-of-day SOFR futures prices,,,,,,
REALIZED_XM: Compound average term rates derived from realized daily SOFR rates,,,,,,
,,,,,,
,,,,,,
DATE,FORWARD_1M,FORWARD_3M,FORWARD_6M,REALIZED_1M,REALIZED_3M,REALIZED_6M
05-12-2021,0.02,0.03,0.03,,,
05-11-2021,0.02,0.03,0.04,,,
05-10-2021,0.02,0.03,0.04,,,
```

### `PaddedCsv`

[`padded_csv.csv`](padded_csv.csv), recorded in prism-clients' `tests/cboe/cassettes/test_recorded/test_body[spxpc].yaml`, interaction 0. Read by [`PaddedCsv`](../../src/flatread/delimited.py), in cboe `_SpxPutCall` (`_CboeCsv`).

From 2011-06-09 the rows pad each cell with a space (`06/09/2011, 1.43, 392746`), while the earlier rows above them do not.

```
Cboe Volume and Put/Call Ratio data is provided for informational purposes only. The volume data reflected below may be based upon preliminary reported volume rather than cleared volume. Cboe makes no guarantee as to the accuracy of the data. Your use of Cboe Volume and Put/Call Ratio data is subject to the Terms and Conditions of the Cboe Websites.,,,,
Date,SPX Put/Call Ratio,SPX Put Volume,SPX Call Volume,Total SPX Options Volume
7/6/2010,1.91,446308,233843,680151
6/8/2011,1.77,526274,296987,823261
06/09/2011, 1.43, 392746, 275428, 668174
06/10/2011, 1.77, 598930, 337467, 936397
```

### `Thousands`, `ThousandsCsv`

[`thousands_csv.txt`](thousands_csv.txt), recorded in prism-clients' `tests/federal_reserve/cassettes/test_recorded/test_file[iba_fboshr].yaml`, interaction 0. Read by [`Thousands`](../../src/flatread/core.py) and [`ThousandsCsv`](../../src/flatread/delimited.py), in federal_reserve `_IBA_SHARES` (a plain `ThousandsCsv`).

Amounts with thousands separators (`1,782,319`), each read as its number. The same excerpt: a TSV whose amounts are grouped in thousands.

```
QTREND	TYPE	ASSETS	ASSETS_PCT	C&I_LOANS	C&I_LOANS_PCT	TOTAL_LOANS	TOTAL_LOANS_PCT	DEPOSITS	DEPOSITS_PCT
1980 / 12	DOMESTIC OWNED BANKS	1,782,319	89.2	374,596	85.8	964,264	88.3	1,422,258	93.6
1980 / 12	FOREIGN OWNED BANKS	66,465	3.3	15,492	3.5	35,237	3.2	53,392	3.5
1980 / 12	U.S. BRANCHES AND AGENCIES	149,332	7.5	46,495	10.6	92,290	8.5	43,676	2.9
```

### `Carried`

[`carried.txt`](carried.txt), recorded in prism-clients' `tests/occ/cassettes/test_recorded/test_endpoint[onn_volume_download].yaml`, interaction 0. Read by [`Carried`](../../src/flatread/core.py), in occ `_OnnVolume`.

A group (`VIX`, `VX`) and its class are printed on the first line only; the lines below leave the first cell empty, and the value is carried down.

```
		H11/06/2026	11/06/2026
BBX   	BX    	K	1084	14.63	0	0.00	1084	14.63	0	0.00	1084	14.63
	TBX   	K	6324	85.37	0	0.00	6324	85.37	0	0.00	6324	85.37
VIX   	VX    	F	943406	98.02	53050	100.00	996456	98.12	12	100.00	996468	98.12
	VXM   	F	19062	1.98	0	0.00	19062	1.88	0	0.00	19062	1.88
VX    	UX1E  	F	2	9.09	0	0.00	2	9.09	0	0.00	2	9.09
	UX2A  	F	2	9.09	0	0.00	2	9.09	0	0.00	2	9.09
	UX2B  	F	4	18.18	0	0.00	4	18.18	0	0.00	4	18.18
	UX2E  	F	4	18.18	0	0.00	4	18.18	0	0.00	4	18.18
```

### `CsvText`

[`csv_text.csv`](csv_text.csv), recorded in prism-clients' `tests/treasury/cassettes/test_tic_s_tables/test_recorded[oilexp_sdata_hist_2003-2014.csv].yaml`, interaction 0. Read by [`CsvText`](../../src/flatread/delimited.py), in treasury `_TicCsv` (`_TIC_GROSS_TRANSACTIONS_CSV`).

A title, a units line, a band row (`<---Gross Purchases--->`) and four stacked header rows above `Country Name,Code,Month,...`, then a dashes line and monthly rows. It is a grid for `_above`.

```
FOREIGN PURCHASES AND SALES OF LONG-TERM DOMESTIC AND FOREIGN SECURITIES BY TYPE
All amounts in millions of dollars.
,,,<------------Gross Purchases by Foreigners------------>,,,,,,<--------------Gross Sales by Foreigners-------------->,,,,,
,,,U.S.,U.S.,,,,,U.S.,U.S.,,,,
,,End,Treasury,Gov't,U.S.,U.S.,,,Treasury,Gov't,U.S.,U.S.,,
,Country,of,Bonds &,Agency,Corp.,Corp.,Foreign,Foreign,Bonds &,Agency,Corp.,Corp.,Foreign,Foreign
Country Name,Code,Month,Notes,Bonds,Bonds,Stocks,Bonds,Stocks,Notes,Bonds,Bonds,Stocks,Bonds,Stocks
------------,----------,----------,---------,---------,---------,---------,---------,---------,---------,---------,---------,---------,---------,---------
Bahrain,40703,2013-01,0,0,1,31,51,10,20,0,20,40,49,12
Bahrain,40703,2013-02,19,3,5,116,22,0,31,4,13,103,22,16
Bahrain,40703,2013-03,51,0,4,63,77,1,51,5,3,84,87,17
```

### `CsvText._misquoted`

[`csv_text-misquoted.csv`](csv_text-misquoted.csv), recorded in prism-clients' `tests/treasury/cassettes/test_tic_securities/test_recorded[slt3d_globl].yaml`, lines 15-16 and 17247-17254. Read by [`CsvText._misquoted`](../../src/flatread/delimited.py), in treasury `_TicCsv` (`_SLT3D_GLOBL_CSV`).

Monthly rows quoted cleanly, then prose notes cut at fixed widths that misquote (`"ts." Holdings o"`): with `csv`'s `strict` on, the file fails (`',' expected after '"'`). Unlike [`CsvFile._quote_char`](#csvfile_quote_char-none), a clean quoted cell still holds its commas (`"5,367"`).

```
"Austria",10189,"2023-01","5,367","4,175","1,192"
"Austria",10189,"2022-12","6,034","4,413","1,621"
"Grand Total",99996,"2011-12","5,004,354","4,356,681","647,673"
"Grand Total",99996,"2011-09","4,912,116","4,276,989","635,127"
""
"Notes:"
"1/ Estimated foreign holdings of","U.S. Treasury m","arketable and n","on-marketable b","ills, bonds, an","d notes are rep","orted monthly u","nder the"
"Treasury International Capital","(TIC) reportin","g system. Holdi","ngs of Treasury","bonds and note","s are reported","on TIC Form SLT",","Aggregate"
"Holdings of Long-Term Securiti","es by U.S. and","Foreign Residen","ts." Holdings o","f short-term Tr","easury securiti","es are reported","on TIC form BL","-2,"
""Report of Customers' U.S. Dol","lar Liabilities","to Foreign Res","idents.""
```

### `BlankEnded`

[`blank_ended.csv`](blank_ended.csv), recorded in prism-clients' `tests/federal_reserve/cassettes/test_recorded/test_file[g17_mv_sales_sf].yaml`, interaction 0. Read by [`BlankEnded`](../../src/flatread/grids.py), in federal_reserve `_SeasonalFactors` (`mv_sales_sf`).

The months stop at a blank row; a `Last Updated:` note stands below it. The excerpt keeps the first two and last three months.

Its twins: [`GridFile`](../../src/flatread/grids.py) reads every filled row to the grid's end, skipping blank ones; [`NotedSheet`](#notedsheet) ends at its first row with nothing past column A, where this ends at a row with nothing at all.

```
                                                          ,Monthly Seasonal Factors for,,,,,
                                                               ,Motor Vehicle Sales,,,,,
,,,,,,
,,,,,,
           ,Domestic autos  ,Foreign autos  ,Domestic light trucks  ,Foreign light trucks  ,Domestic heavy trucks  ,Foreign heavy trucks
Jan-21,81.56,78.04,80.36,81.81,83.2,74.05
Feb-21,93.2,86,91.4,91.64,86.35,79.66
Oct-28,93.05,92.23,94.86,89.99,97.95,98.83
Nov-28,91.67,94.62,98.38,94.19,92.41,94.47
Dec-28,102.74,105.47,111.8,111.83,111.61,131.63
,,,,,,
Last Updated:  9-30-2026,,,,,,
```

### `TitledGrid`

[`titled_grid.csv`](titled_grid.csv), recorded in prism-clients' `tests/federal_reserve/cassettes/test_recorded/test_file[g17_seasadj_spec].yaml`, interaction 0. Read by [`TitledGrid`](../../src/flatread/grids.py), in federal_reserve `_SeasonalSpecs` and `_Holidays` (each with `BlankEnded`).

Prose, then two tables, each under a row that is its title alone (`I. Specifications for individual IP indexes`, `II. User-defined holiday variables`). The excerpt keeps two rows of the first table and one of the second.

Its twins: [`PreambleCsv`](#preamblecsv) finds the header by the struct's first label, and [`TitledCsv`](#titledcsv) matches every line above it; this finds one table of several by the title over it.

```
The Federal Reserve seasonally adjusts its Industrial Production
indexes at the level of its most disaggregate components and
constructs its seasonally adjusted aggregates as chain-weighted Fisher
indexes of the seasonally adjusted individual indexes.
I. Specifications for individual IP indexes
Code,Description,Source data type,Beginning Date,Days in workweek,Robust Detrend,Additive,ARIMA model,Holiday variables,AO dates,X13 specs
N1133,Logging  NAICS=1133,H,1972.01,7,FALSE,FALSE,(1 0 1)(1 0 1),"PWHLABOR_6, PWHLABOR_7, PWHEASTER_EMSATSUN, PWHEASTER_GF_SAT, PWHEASTER_GF_SUN, PWHTIMING_SAT_1, PWHTIMING_SAT_10, PWHTIMING_SAT_11, PWHTIMING_SAT_12, PWHTIMING_SAT_2, PWHTIMING_SAT_4, PWHTIMING_SAT_5, PWHTIMING_SAT_6, PWHTIMING_SAT_7, PWHTIMING_SAT_8, PWHTIMING_SAT_9, PWHJULY4FRIDAY",1979.04 1982.01 1987.09 1987.10 1988.08 1988.09 1991.01 1991.02 1991.03 1991.04 1991.05 2003.08 2004.09 2009.02 2009.03 2009.04 2009.05 2009.06 2009.07 2009.08 2009.09 2009.10 2009.11 2009.12 2010.01 2020.04 2020.05 2020.06 2020.07 2020.08,x11 {seasonalma=(s3x9)}
N21112T,Texas crude oil  NAICS=211120pt.,P,1972.01,7,FALSE,FALSE,(1 0 1)(1 0 1),EASTER,1983.12 2020.03 2020.04 2020.05 2020.06 2020.07 2020.08 2021.02 2022.02 2024.01 2024.06,


II. User-defined holiday variables

,CHINESENEWYEAR.WT,EASTER,MVPWHEASTER_EM_SUN,PWHEASTER_EMSATSUN,PWHEASTER_GF_SAT,PWHEASTER_GF_SUN,PWHJULY4FRIDAY,PWHLABOR_6,PWHLABOR_7,PWHTIMING_SAT_1,PWHTIMING_SAT_10,PWHTIMING_SAT_11,PWHTIMING_SAT_12,PWHTIMING_SAT_2,PWHTIMING_SAT_4,PWHTIMING_SAT_5,PWHTIMING_SAT_6,PWHTIMING_SAT_7,PWHTIMING_SAT_8,PWHTIMING_SAT_9
Jan1972,-1.7000,0.0000,0.0000,0.0000,0.0000,0.0000,0.0000,0.0000,0.0000,-0.4182,0.0000,0.0000,0.0000,0.0000,0.0000,0.0000,0.0000,0.0000,0.0000,0.0000
```

### `BesideGrid`

[`beside_grid.csv`](beside_grid.csv), recorded in prism-clients' `tests/federal_reserve/cassettes/test_recorded/test_file[payments_NACHA_Same_Day_Entry_Fee].yaml`, interaction 0. Read by [`BesideGrid`](../../src/flatread/grids.py), in federal_reserve `_SameDayAch` (columns B to D) and `_SameDayAchAdjustments` (F on).

Two fee reviews side by side, with an empty column between, their header cells holding line breaks. The excerpt keeps the first ten columns, written out again as CSV.

Its twin: [`BesideSheet._columns_at`](#besidesheet_columns_at) names each column a sheet loads; this cuts a band of grid columns, its end `None` for the grid's end, so a table that grows to the right keeps every column.

```
,,,,,,,,,
,,,,,,,,,
,"SAME-DAY ACH ENTRY FEE 
YEAR 5 REVIEW",,,,"SAME-DAY ACH ENTRY FEE 
YEAR 8 REVIEW",YEAR 8 ADJUSTMENT,,,
,Actual SDA Txns through March 2023 (Million),"% Variance
from
Projection","New Fee
(Cents)",, Actual SDA Transactions through March 2026 (million) ,"8,933 ","11,166 ","11,613 ","12,060 "
,,,,," % Variance from
Projection ",0%,25%,30%,35%
,,,,,,,,,
,"4,939",0%,5.2,,"Year 5 = 4,939",5.2,4.5,4.4,4.3
,"6,633",25%,4.4,,"Year 5 = 6,633",4.4,4.4,4.3,4.2
,"6,898",30%,4.3,,"Year 5 = 6,898",4.3,4.3,4.3,4.2
,,,,,,,,,
,,,,,,,,,
```

## Workbooks

### `SheetFile`

[`sheet_file.txt`](sheet_file.txt), recorded in prism-clients' `tests/atlanta_fed/cassettes/test_recorded/test_file[get_hoam_county_data].yaml`, interaction 0. Read by [`SheetFile`](../../src/flatread/sheets.py), in atlanta_fed `_CountySheet`.

A clean table: the header in row 1, then typed rows to the sheet's end (rows 2 to 392481).

Its twin: [`SheetText`](#sheettext) reads the whole sheet as a grid of text cells, numbers to their every digit, and cuts the table from it, where fastexcel here loads one typed table under the header, each column typed by its field.

```
# sheet: Sheet1
# columns: A | B | C | D | E
1: 'County' | 'County FIP' | 'Affordability Metric' | 'county_perc_mo' | 'Month'
2: 'Abbeville, SC' | 45001 | 86.719309216 | 0.345978313 | '2005-01'
3: 'Abbeville, SC' | 45001 | 85.608441226 | 0.350467779 | '2005-02'
4: 'Abbeville, SC' | 45001 | 88.118736722 | 0.340483777 | '2005-03'
5: 'Abbeville, SC' | 45001 | 89.422925204 | 0.335517992 | '2005-04'
392480: 'Yuma, AZ' | 4027 | 80.894889512 | 0.370888699 | '2026-07'
392481: 'Yuma, AZ' | 4027 | 81.851333889 | 0.366554812 | '2026-08'
```

### `SheetFile._header` (`False`)

[`sheet_file-header.txt`](sheet_file-header.txt), recorded in prism-clients' `tests/atlanta_fed/cassettes/test_recorded/test_file[get_gdpnow_gdp_tracking_model_data_and_forecasts].yaml`, interaction 0, sheet `Sheet1`. Read by [`SheetFile._header`](../../src/flatread/sheets.py), in atlanta_fed `_HeaderlessSheet`.

No header: row 1 is empty and rows 2 to 20 are read by position (GDPNow's ticker map).

```
# sheet: Sheet1
# columns: A | B | C
1:  |  |
2: 'GDPZ@USNA' | 'GDPZ_USNA' |
3: 'CZ@USNA' | 'CZ_USNA' | 'CZ_USNA'
4: 'FNSZ@USNA' | 'FNSZ_USNA' | 'FNSZ_USNA'
5: 'FNEZ@USNA' | 'FNEZ_USNA' | 'FNEZ_USNA'
6: 'FNPZ@USNA' | 'FNPZ_USNA' | 'FNPZ_USNA'
19: 'PTXNETH@USNA' | 'PTXNETH_USNA' | 'PTXNETH_USNA'
20: 'PTGH@USNA' | 'PTGH_USNA' | 'PTGH_USNA'
```

### `File._unread`

[`sheet_file-unread.txt`](sheet_file-unread.txt), recorded in prism-clients' `tests/frbsf/cassettes/test_recorded/test_file[get_cpi_contributors_data].yaml`, interaction 0, sheet `chart5_coreCPI_granularCont_MoM`. Read by [`SheetFile._unread`](../../src/flatread/sheets.py), in frbsf `_CategorySheet` (`_unread = ("", "notes")`).

Column A holds the sheet's own row numbers under an empty header cell, and BS holds the prose notes; neither is read.

```
# sheet: chart5_coreCPI_granularCont_MoM
# columns: A | B | C | D | BS
1:  | 'date' | 'Total: Core CPI MoM' | 'Core Goods: Alcoholic beverages at home' | 'notes'
2: '1' | 2013-01-01 00:00:00 | ' 0.217499' | ' 0.000543' | 'The "Total" column is in percent, all other columns in percentage points.'
3: '2' | 2013-02-01 00:00:00 | ' 0.146407' | ' 0.003192' | 'Seasonally adjusted.'
4: '3' | 2013-03-01 00:00:00 | ' 0.096745' | ' 0.001711' | 'Contributions might not add up to total inflation due to approximation errors.'
165: '164' | 2026-08-01 00:00:00 | ' 0.289796' | '-0.000008' | ''
166: '165' | 2026-09-01 00:00:00 | '' | ' 0.000164' | ''
```

### `SheetFile._gaps`

[`sheet_file-gaps.txt`](sheet_file-gaps.txt), recorded in prism-clients' `tests/cboe/cassettes/test_recorded/test_body[vxoarchive].yaml`, interaction 0. Read by [`SheetFile._gaps`](../../src/flatread/sheets.py), in cboe `_VxoArchiveSheet`.

Row 3536, between 1999-12-31 and 2000-01-03, is empty and gets dropped.

```
# sheet: Sheet1
# columns: A | B | C | D | E
2: 'CBOE S&P 100 VOLATILITY INDEX - VXO(sm)' |  |  |  |
3: 'Date' | 'Open' | 'High' | 'Low' | 'Close'
4: 1986-01-02 00:00:00 | 17.93 | 18.71 | 17.4 | 18.07
5: 1986-01-03 00:00:00 | 19.04 | 19.1 | 17.72 | 17.96
3534: 1999-12-30 00:00:00 | 24.57 | 25.96 | 24.39 | 25.94
3535: 1999-12-31 00:00:00 | 26.42 | 26.99 | 26.26 | 26.71
3536:  |  |  |  |
3537: 2000-01-03 00:00:00 | 24.88 | 28.05 | 24.58 | 24.69
3538: 2000-01-04 00:00:00 | 26.6 | 30.05 | 26.03 | 29.64
```

### `SheetFile._blank_rows`

[`sheet_file-blankrows.txt`](sheet_file-blankrows.txt), recorded in prism-clients' `tests/cftc/cassettes/test_recorded/test_swaps_report[2013-08-07].yaml`, interaction 0, sheet `9a`. Read by [`SheetFile._blank_rows`](../../src/flatread/sheets.py), in cftc `_SwapsSheet`.

Rows 10, 16 and 19 between the product groups are empty and stay rows with no values.

```
# sheet: 9a
# columns: A | B | C | D | E
1: 'Product' |  | 'Cleared**' | 'Uncleared' | 'TOTAL'
2: 'Swap*' | 'Basis*' | 7273421 | 14872063 | 22145484
3:  | 'FRA' | 44907371 | 17263736 | 62171107
8:  | 'VNS' | 18859 | '' | 18859
9:  | 'Zero Coupon' | 286007 | '' | 286007
10: '' |  |  |  |
11: 'With Optionality' | 'Callable Swap' | '' | 718890 | 718890
12:  | 'Cap/Floor' | '' | 10005210 | 10005210
15:  | 'Swaption' | '' | 29517077 | 29517077
16: '' |  |  |  |
17: 'Cross-Currency' | 'Exotic' | '' | 85521 | 85521
18:  | 'Swap' | '' | 16401697 | 16401697
19: '' |  |  |  |
20: 'TOTAL' |  | 207531819 | 224988781 | 432520600
21: 'Estimated gross notional amount outstanding, August 2 weekly snapshot, by product type and tenor (millions of USD), all participant types and currencies. Data are adjusted for double-counting of cleared trades to show a single exposure for each trade.' |  |  |  |
```

### `SheetFile._strip`

[`sheet_file-strip.txt`](sheet_file-strip.txt), recorded in prism-clients' `tests/cftc/cassettes/test_recorded/test_swaps_report[2015-01-07].yaml`, interaction 0, sheet `1`. Read by [`SheetFile._strip`](../../src/flatread/sheets.py), in cftc `_SwapsSheet`.

Header row 1 is date cells (A1 empty). `_strip` makes them read from the sheet's own row as text. No recorded header cell is padded.

```
# sheet: 1
# columns: A | B | C | D | E | F
1:  | 2014-11-28 00:00:00 | 2014-12-05 00:00:00 | 2014-12-12 00:00:00 | 2014-12-19 00:00:00 | 2014-12-26 00:00:00
2: 'Total Interest Rate*' | 340517887 | 341274579 | 338185540 | 320149856 | 311213412
3: 'Cleared**' | 224156103 | 223000308 | 222682596 | 207042391 | 199141100
4: 'Uncleared' | 116361784 | 118274271 | 115502944 | 113107465 | 112072312
19: 'Uncleared' | 'N/A' | 'N/A' | 'N/A' | 'N/A' | 'N/A'
20: 'TOTAL' | 400116976 | 401057877 | 397935701 | 379306948 | 370089649
21:  |  |  |  |  |
23: '* See Data Dictionary for asset class descriptions and Explanatory Notes for data sources.' |  |  |  |  |
24: '** See Explanatory Notes for cleared and uncleared descriptions.' |  |  |  |  |
```

### `SheetFile._above`

[`sheet_file-above.txt`](sheet_file-above.txt), recorded in prism-clients' `tests/eia/cassettes/test_recorded/test_file[wpsr-psw01.xls].yaml`, interaction 1, sheet `Data 1`. Read by [`SheetFile._above`](../../src/flatread/sheets.py), in eia `_Described`.

The `Sourcekey` row 2 sits over the description row 3: each column's header is its series id over its description.

```
# sheet: Data 1
# columns: A | B | C | D
1: 'Back to Contents' | 'Data 1: Petroleum Stocks' |  |
2: 'Sourcekey' | 'WCRSTUS1' | 'WCESTUS1' | 'WCSSTUS1'
3: 'Date' | 'Weekly U.S. Ending Stocks of Crude Oil  (Thousand Barrels)' | 'Weekly U.S. Ending Stocks excluding SPR of Crude Oil  (Thousand Barrels)' | 'Weekly U.S. Ending Stocks of Crude Oil in SPR  (Thousand Barrels)'
4: 1982-08-20 00:00:00 | 609219 | 338764 | 270455
5: 1982-08-27 00:00:00 | 608741 | 336138 | 272603
2303: 2026-10-23 00:00:00 | 693208 | 410625 | 282583
2304: 2026-10-30 00:00:00 | 691944 | 409760 | 282184
```

### `PaddedSheet`

[`padded_sheet.txt`](padded_sheet.txt), recorded in prism-clients' `tests/frbsf/cassettes/test_recorded/test_file[get_cyclical_acyclical_core_pce_data].yaml`, interaction 0, sheet `Data`. Read by [`PaddedSheet`](../../src/flatread/sheets.py), in frbsf `_CORE_PCE_CYCLICAL` (plain `PaddedSheet`).

The Stata months are text cells padded with a space (`' 1988m1'`); the figures are number cells.

```
# sheet: Data
# columns: A | B | C | D
1: 'time_month' | 'Cyclical core PCE inflation (y/y)' | 'Acyclical core PCE inflation (y/y)' | 'Cyclical core PCE contribution (y/y)'
2: ' 1988m1' | 4.03 | 3.83 | 1.54
3: ' 1988m2' | 3.83 | 3.96 | 1.46
4: ' 1988m3' | 3.81 | 4.17 | 1.45
465: ' 2026m8' | 3.1 | 3.07 | 1.2
466: ' 2026m9' | 3.1 | 3.23 | 1.2
```

### `BesideSheet`

[`beside_sheet.txt`](beside_sheet.txt), recorded in prism-clients' `tests/atlanta_fed/cassettes/test_recorded/test_file[get_labor_force_participation_dynamics_build_your_own_decomposition].yaml`, interaction 0, sheet `Data_raw_forinteractive`. Read by [`BesideSheet`](../../src/flatread/sheets.py), in atlanta_fed `_BesideNoted`.

The table is A:P (its first 16 columns, one per field). R:S are work columns beside it (`year.qtr`) that run on past its last row.

```
# sheet: Data_raw_forinteractive
# columns: A | B | C | D | E | F | G | H | I | J | K | L | M | N | O | P | Q | R | S
4:  |  |  |  |  |  |  |  |  |  | 'Percent of the Selected Population' |  |  |  |  |  |  |  |
5: 'year' | 'quarter' | 'breakdown' | 'Gender' | 'Age' | 'Race\Ethnicity' | 'Education' | 'Total' | 'Demographics' | 'Behavioral/cyclical' | 'Disability/illness' | 'In school' | 'Other' | 'Retirement' | 'Family responsibilities' | 'Shadow labor force' |  | 'string' | 'year.qtr'
6: 1998 | 1 | 'Contributions to Change in Total' | 'All' | '16-24' | 'All' | 'All' | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |  | '1998.1' | 1998.1
7: 1998 | 1 | 'Contributions to Change in Total' | 'All' | '16-24' | 'Hispanic' | 'All' | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |  | '1998.1' | 1998.1
45400: 2022 | 1 | 'Contributions to Change in Demographics' | 'Women' | 'All' | 'Black NH' | 'HS or some college' | -0.5089 | -0.0662 | -0.4427 | 0.208 | -0.2014 | -0.0147 | 0.0072 | -0.039 | -0.0261 |  | '2022.1' | 2022.1
45401: 2022 | 1 | 'Contributions to Change in Demographics' | 'Women' | 'All' | 'Hispanic' | 'Associate or higher' | 0.9138 | 0.01 | 0.9038 | 0.0029 | 0.0335 | -0.0015 | -0.049 | 0.0343 | -0.0103 |  | '2022.1' | 2022.1
45402:  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  | '.' | ''
45403:  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  | '.' | ''
```

### `BesideSheet._columns_at`

[`beside_sheet-columnsat.txt`](beside_sheet-columnsat.txt), recorded in prism-clients' `tests/atlanta_fed/cassettes/test_recorded/test_file[get_gdpnow_gdp_tracking_model_data_and_forecasts].yaml`, interaction 0, sheet `FactorCNFAIData`. Read by [`BesideSheet._columns_at`](../../src/flatread/sheets.py), in atlanta_fed `_FactorCfnaiSheet`.

The table is E:H (header row 5), right of a Haver download in A:D.

Its twin: [`BesideGrid`](#besidegrid) cuts a band of a grid's columns, its end `None` for the grid's end, where this names each column the sheet loads.

```
# sheet: FactorCNFAIData
# columns: A | B | C | D | E | F | G | H | I | J | K | L | M
1: '6703 12009' | '.excel_last' | 'FRBCNAI@SURVEYS' |  |  |  |  |  |  | 'Correlation of Factors' |  |  | 0.964928935
2: '.DESC' |  | 'Chicago Fed National Activity Index (+=Growth Above Trend)' |  |  |  |  |  |  |  |  |  |
3: '.T1' |  | 'Mar-1967' |  |  |  |  |  |  |  |  |  |
4: '.TN' |  | 'Jun-2021' |  |  |  |  |  |  |  |  |  |
5: '.LSOURCE' |  | 'Federal Reserve Bank of Chicago' |  |  | 'Chicago Fed National Activity Index (CFNAI)' | 'GDPNow Factor (Rescaled to CFNAI)' | 'Zero' |  |  |  |  |
6: '.DTLM' |  | 'Jul-22-2021 08:32' |  | 1967-01-01 00:00:00 |  |  | 0 |  |  |  |  |
7: '.FRQ' |  | 'Monthly' |  | 1967-02-01 00:00:00 |  |  | 0 |  |  |  |  |
8: '6703' | 1967-03-31 00:00:00 | -0.34 | -0.121321598 | 1967-03-01 00:00:00 | -0.34 | -0.121321598 | 0 |  |  |  |  |
9: '6704' | 1967-04-30 00:00:00 | 0.02 | 0.036399496 | 1967-04-01 00:00:00 | 0.02 | 0.036399496 | 0 |  |  |  |  |
649: '2008' | 2020-08-31 00:00:00 | 1.22 | 1.795797749 | 2020-08-01 00:00:00 | 1.22 | 1.795797749 | 0 |  |  |  |  |
650: '2009' | 2020-09-30 00:00:00 | 0.37 | 0.794019406 | 2020-09-01 00:00:00 | 0.37 | 0.794019406 | 0 |  |  |  |  |
```

### `CellHeaderSheet`

[`cell_header_sheet.txt`](cell_header_sheet.txt), recorded in prism-clients' `tests/atlanta_fed/cassettes/test_recorded/test_file[get_gdpnow_gdp_tracking_model_data_and_forecasts].yaml`, interaction 0, sheet `Factor`. Read by [`CellHeaderSheet`](../../src/flatread/sheets.py), in atlanta_fed `_DatedSheet`.

Header row 1 is date cells, one month per column, read as their text.

```
# sheet: Factor
# columns: A | CS | CT | CU | CV | CW | CX | AEF | AEG | AEH | AEI | AEJ
1:  | 1966-12-31 00:00:00 | 1967-01-31 00:00:00 | 1967-02-28 00:00:00 | 1967-03-31 00:00:00 | 1967-04-30 00:00:00 | 1967-05-31 00:00:00 | 2026-07-31 00:00:00 | 2026-08-31 00:00:00 | 2026-09-30 00:00:00 | 2026-10-31 00:00:00 | 2026-11-30 00:00:00
2: 'Actual' |  |  | -0.604902976 | -0.121321598 | 0.036399496 | -0.223547181 | -0.067247451 | 0.043425436 | 0.067224704 | 0.086850985 |
3: 'Forecast' |  |  |  |  |  |  |  |  |  | 0.086850985 | 0.015181867
4: 'Actual+Forecast' |  |  | -0.604902976 | -0.121321598 | 0.036399496 | -0.223547181 | -0.067247451 | 0.043425436 | 0.067224704 | 0.086850985 | 0.015181867
```

### `NumberTextSheet`

[`number_text_sheet.txt`](number_text_sheet.txt), recorded in prism-clients' `tests/frbsf/cassettes/test_recorded/test_file[get_cpi_contributors_data].yaml`, interaction 0, sheet `chart2_headlineCPI_contr_YoY`. Read by [`NumberTextSheet`](../../src/flatread/sheets.py), in frbsf `_ChartSheet`.

Every figure is a text cell, either padded (`' 1.67'`, `'   NA'`) or not (`'0.60'`); the notes column I is not read.

```
# sheet: chart2_headlineCPI_contr_YoY
# columns: A | B | C | D | E | F | G | H | I
1: 'date' | 'Total: Headline CPI, YoY' | 'Food and Energy' | 'Food' | 'Energy' | 'Core Goods' | 'Core Services ex. Shelter' | 'Shelter' | 'notes'
2: 1999-01-01 00:00:00 | ' 1.67' | '-0.15' | ' 0.36' | '-0.50' | ' 0.29' | '0.60' | ' 0.92' | 'The "Total" column is in percent, all other columns in percentage points.'
3: 1999-02-01 00:00:00 | ' 1.61' | '-0.01' | ' 0.38' | '-0.38' | ' 0.17' | '0.59' | ' 0.90' | 'Not seasonally adjusted.'
4: 1999-03-01 00:00:00 | ' 1.73' | ' 0.14' | ' 0.35' | '-0.21' | ' 0.13' | '0.60' | ' 0.91' | 'Contributions might not add up to total inflation due to approximation errors.'
333: 2026-08-01 00:00:00 | ' 3.40' | ' 1.41' | ' 0.36' | ' 1.04' | ' 0.13' | '0.75' | ' 1.08' | ''
334: 2026-09-01 00:00:00 | '   NA' | ' 1.65' | ' 0.34' | ' 1.31' | ' 0.14' | '0.77' | ' 1.14' | ''
```

### `NotedSheet`

[`noted_sheet.txt`](noted_sheet.txt), recorded in prism-clients' `tests/eia/cassettes/test_recorded/test_web_file[WPSR_ErrataSheet.xlsx].yaml`, interaction 0, sheet `Appendix E`. Read by [`NotedSheet`](../../src/flatread/sheets.py), in eia `_ERRATA` (plain `NotedSheet`).

The table ends at row 24, the first row with nothing past column A; the `Source:` line and the note below it are dropped.

Its twins: [`DatedSheet`](#datedsheet) runs while the first column holds dates, so a dated row of empty values stays a row, where this ends at the first row with nothing past the first column; [`BlankEnded`](#blankended) ends a grid's table at its first row with nothing at all.

```
# sheet: Appendix E
# columns: A | B | C | D | E | F | G | H | I
5: 'Tables' | 'Region' | 'Petroleum product' | 'Supply type' | 'Units' | 'Week ending' | 'Published' | 'Corrected' | 'Difference'
6: '6, 9' | 'Midwest (PADD 2)' | 'Propane/Propylene' | 'Stocks' | 'Million barrels' | 2023-11-10 00:00:00 | 26.947 | 26.419 | -0.528
7: '6, 9' | 'Midwest (PADD 2)' | 'Propane/Propylene' | 'Stocks' | 'Million barrels' | 2023-11-17 00:00:00 | 26.574 | 25.879 | -0.695
22: '1, 9' | 'U.S.' | 'Propane/Propylene' | 'Product Supplied' | 'Thousand barrels per day' | 2023-12-08 00:00:00 | 1200 | 1719 | 519
23: '1, 9' | 'U.S.' | 'Propane/Propylene' | 'Product Supplied' | 'Thousand barrels per day' | 2023-12-15 00:00:00 | 1231 | 1231 | 0
24: 'Source: U.S. Energy Information Administration' |  |  |  |  |  |  |  |
25: 'Note: Published PADD 2 propane/propylene stocks are overstated for weeks ending November 10, 2023, through December 15, 2023. The stock error also affects calculated quantities of PADD 2 and U.S. stock change and product supplied for propane/propylene, total petroleum, and other oils. U.S. propane/propylene stocks and product supplied for the impacted weeks are provided in this table for comparison.' |  |  |  |  |  |  |  |
```

### `DatedSheet`

[`dated_sheet.txt`](dated_sheet.txt), recorded in prism-clients' `tests/eia/cassettes/test_recorded/test_file[ngs-ngshistory.xls].yaml`, interaction 1, sheet `weekly_net_changes`. Read by [`DatedSheet`](../../src/flatread/sheets.py), in eia `_NetChanges` (an `_Dated`).

Row 8 is dated and every value in it is empty; it still counts as a row, and the dated rows run to the sheet's end.

Its twin: [`NotedSheet`](#notedsheet) ends at the first row with nothing past the first column, so a dated row of empty values would end it.

```
# sheet: weekly_net_changes
# columns: A | B | C | D | E | F | G | H | I | J
6: 'Weekly Net Changes (Bcf) AS OF NOVEMBER 5, 2026' |  |  |  |  |  |  |  |  |
7:  |  | 'East Region' | 'Midwest Region' | 'Mountain Region' | 'Pacific Region' | 'South Central Region' | 'Salt' | 'NonSalt' | 'Total Lower 48'
8: 2010-01-01 00:00:00 |  |  |  |  |  |  |  |  |
9: 2010-01-08 00:00:00 |  | -66 | -80 | -10 | -11 | -99 | -36 | -63 | -267
10: 2010-01-15 00:00:00 |  | -61 | -70 | -9 | -11 | -93 | -32 | -61 | -243
885: 2026-10-23 00:00:00 |  | 13 | 23 | 1 | 0 | 43 | 23 | 19 | 80
886: 2026-10-30 00:00:00 |  | 13 | 23 | 2 | -1 | 40 | 22 | 18 | 77
```

### `SheetsFile`

[`sheets_file.txt`](sheets_file.txt), recorded in prism-clients' `tests/eia/cassettes/test_recorded/test_file[ngs-ngsstats.xls].yaml`, interaction 0. Read by [`SheetsFile`](../../src/flatread/sheets.py), in eia `_Years`.

One sheet per year, each named `ngsstats YYYY (YYYY-YYYY)` (the last with a trailing space). Each is read as one table, and its year comes from the sheet name.

```
# sheets: 'ngsstats 2026 (2021-2025)', 'ngsstats 2025 (2020-2024)', 'ngsstats 2024 (2019-2023)', 'ngsstats 2023 (2018-2022)', 'ngsstats 2022 (2017-2021)', 'ngsstats 2021 (2016-2020)', 'ngsstats 2020 (2015-2019)', 'ngsstats 2019 (2014-2018)', 'ngsstats 2018 (2013-2017)', 'ngsstats 2017 (2012-2016)', 'ngsstats 2016 (2011-2015)', 'ngsstats 2015 (2010-2014) '
# sheet: ngsstats 2026 (2021-2025)
# columns: A | B | C | D
2:  | 5-Year Average (2021-2025) |  |
3: Report Date |  East | Midwest | Mountain
4: 2026-01-02 00:00:00 | 745 | 891 | 200
5: 2026-01-09 00:00:00 | 705 | 842 | 190
# sheet: ngsstats 2015 (2010-2014) 
2:  | 5-Year Average (2010-2014) |  |
3: Report Date |  East | Midwest | Mountain
4: 2015-01-02 00:00:00 | 755 | 867 | 187
5: 2015-01-09 00:00:00 | 706 | 807 | 178
```

### `TitledSheet`

[`titled_sheet.txt`](titled_sheet.txt), recorded in prism-clients' `tests/freddie_mac/cassettes/test_recorded/test_file[cofi_repl_consumer].yaml`, interaction 0, sheet `Monthly Index`. Read by [`TitledSheet`](../../src/flatread/sheets.py), in freddie_mac `_CofiReplacementSheet`.

One title row (B1, plus the link in L1) sits over the header in row 2; the table opens in column B.

Its twin: [`PreambleSheet`](#preamblesheet) skips any number of rows unread and finds the header by its first label, where this holds the header at a fixed row under titles it checks.

```
# sheet: Monthly Index
# columns: B | C | D | E | F | G | H | I | J | K | L
1: 'Enterprise 11th District COFI Replacement Index' |  |  |  |  |  |  |  |  |  | 'View Notice and Disclaimer'
2: 'Index Month' | 'Index Year' | 'Index Value' | 'Date of Publication' |  |  |  |  |  |  |
3: 'January' | 2022 | 0.00219 | 'February 28, 2022' |  |  |  |  |  |  |
4: 'February' | 2022 | 0.00242 | 'March 31, 2022' |  |  |  |  |  |  |
58: 'August' | 2026 | 0.02838 | 'September 30, 2026' |  |  |  |  |  |  |
59: 'September' | 2026 | 0.02898 | 'October 30, 2026' |  |  |  |  |  |  |
```

### `PreambleSheet`

[`preamble_sheet.txt`](preamble_sheet.txt), recorded in prism-clients' `tests/new_york_fed/cassettes/test_recorded/test_web_file[nowcast_data_2002_2021].yaml`, interaction 0, sheet `Forecasts By Quarter`. Read by [`PreambleSheet`](../../src/flatread/sheets.py), in new_york_fed `_ByQuarterSheet`.

Rows 1 to 13 are a banner, a link and prose, and are not read. The header is row 14, the first row whose first cell is `Forecast Date`.

Its twins: [`TitledSheet`](#titledsheet) holds the header at a fixed row and checks each row above it against a title regex; [`FilledHeader`](#filledheader) finds a grid's header by how full its row is, for titles and labels that change.

```
# sheet: Forecasts By Quarter
# columns: A | B | C | D | E
1:  |  |  |  |
2:  |  |  |  |
3:  | 'NEW YORK FED  ECONOMIC RESEARCH' |  |  |
4:  |  | 'https://www.newyorkfed.org/research' |  |
5:  |  |  |  |
6:  | 'New York Fed Staff Nowcast' |  |  |
7:  | '2002-present' |  |  |
8:  | 'View the Nowcasting Report.' |  |  |
9:  |  |  |  |
10:  | 'Forecasts for GDP growth in quarters 2016Q1 to present were made in real-time (denoted in blue shading).' |  |  |
11:  | 'Forecasts for GDP growth in quarters 2002Q1 through 2015Q4 are historical reconstructions based on real-time data (denoted in red shading).' |  |  |
12:  |  |  |  |
13:  |  |  |  |
14: 'Forecast Date' | '2002Q1' | '2002Q2' | '2002Q3' | '2002Q4'
15: 2001-11-23 00:00:00 | -1.2 |  |  |
16: 2001-11-30 00:00:00 | -1.24 |  |  |
1045: 2021-08-20 00:00:00 |  |  |  |
1046: 2021-08-27 00:00:00 |  |  |  |
```

### `OnlySheet`

[`only_sheet.txt`](only_sheet.txt), recorded in prism-clients' `tests/philadelphia_fed/cassettes/test_recorded/test_file[rtdsm_yngsub_qvqd].yaml`, interaction 0, sheet `YNGSUB`. Read by [`OnlySheet`](../../src/flatread/sheets.py), in philadelphia_fed `_RtdsmVintages`.

The workbook's one sheet is named after its variable: `DATE`, then one column per vintage.

```
# sheet: YNGSUB
# columns: A | B | C | D | AT | AU | AV
1: 'DATE' | 'YNGSUB15Q2' | 'YNGSUB15Q3' | 'YNGSUB15Q4' | 'YNGSUB26Q2' | 'YNGSUB26Q3' | 'YNGSUB26Q4'
2: '1947:Q1' | 0.5 | 0.5 | 0.5 | 0.5 | 0.5 | 0.5
3: '1947:Q2' | 0.4 | 0.4 | 0.4 | 0.4 | 0.4 | 0.4
319: '2026:Q2' | '#N/A' | '#N/A' | '#N/A' | '#N/A' | 128.4 | 147.9
320: '2026:Q3' | '#N/A' | '#N/A' | '#N/A' | '#N/A' | '#N/A' | 123.6
```

### `SheetText`

[`sheet_text.txt`](sheet_text.txt), recorded in prism-clients' `tests/philadelphia_fed/cassettes/test_recorded/test_file[gdpplus_chart].yaml`, interaction 0, sheet `Sheet1`. Read by [`SheetText`](../../src/flatread/sheets.py), in philadelphia_fed `_GdpplusChartSheet`.

The grid `SheetText` cuts: every cell as text, a number to its every digit (`8.900755344981803`, where fastexcel's text is `8.900755345`); `OBS_QUARTER2` is not read.

The grid alone is [`sheet_grid`](../../src/flatread/sheets.py)`(body, sheet)`, for a format that cuts it without a row struct (cftc's Swaps Report contents sheet, the FCM month's label rows).

Its twin: [`SheetFile`](#sheetfile) has fastexcel load one typed table under the header row, each column typed by its field.

```
# sheet: Sheet1
# columns: A | B | C | D | E | F | G
1: OBS_YEAR | OBS_QUARTER | OBS_QUARTER2 | RECBARS | GRGDP_DATA | GRGDI_DATA | GDPPLUS_DATA
2: 1960 | 1 | 0 | 0 | 8.900755344981803 | 8.804717386774286 | 4.904242937102352
3: 1960 | 2 | 0.25 | 1 | -2.1666672127537367 | -0.0683079551961896 | 1.272742881209802
4: 1960 | 3 | 0.5 | 1 | 1.961906522333834 | 0.022770614527445332 | -0.33367091295166285
267: 2026 | 2 | 0.25 | 0 | 2.1954601317048628 | 2.610526117855727 | 2.779661525619448
268: 2026 | 3 | 0.5 | 0 | 3.3146453068923165 |  | 3.035694670282166
```

### `FilledHeader`

[`filled_header.txt`](filled_header.txt), recorded in prism-clients' `tests/federal_reserve/cassettes/test_recorded/test_file[disclosures_talf-transaction-specific-disclosures-01-11-24].yaml`, interaction 0, sheet `TALF Material Investors`. Read by [`FilledHeader`](../../src/flatread/grids.py), in federal_reserve `_DisclosureSheet` (each sheet of `_Disclosures`, a `SheetsFile`).

The header is row 2, under a one-cell title: the first row filling at least two cells and at most three fewer than the fullest. Each release words its titles and labels its own way, so no label finds it. The one-cell note at the end is no record.

Its twins: [`TitledGrid`](#titledgrid) finds its header under a known title, and [`PreambleSheet`](#preamblesheet) by its first label; this finds it by the header's shape alone.

```
# sheet: TALF Material Investors
# columns: A | B | C | D | E
1: TERM ASSET-BACKED SECURITIES LOAN FACILITY TALF.BORROWER DATA: a.o. 12.31.2023* |  |  |  | 
2: Borrower Number | Subscription Date | Borrower | Material Investor(s) in the Borrower | Material Investor(s) State/Country of Incorporation or Formation
3: 1 | 06/17/2020 | MacKay Shields TALF 2.0 Opportunities Master Fund LP | Oxford University Press Group Pension Scheme | United Kingdom
4: 1 | 06/17/2020 | MacKay Shields TALF 2.0 Opportunities Master Fund LP | HF Fund LP | FL
5: 1 | 06/17/2020 | MacKay Shields TALF 2.0 Opportunities Master Fund LP | MacKay Shields TALF 2.0 Opportunities Feeder Fund (Cayman) LP | Cayman Islands
6: 1 | 06/17/2020 | MacKay Shields TALF 2.0 Opportunities Master Fund LP | MacKay Shields TALF 2.0 Opportunities Feeder Fund (Cayman-GBP Hedged) LP | Cayman Islands
374:  |  |  |  | 
375: *Note: This report includes information regarding material investors, as provided to the Federal Reserve by TALF Agents on or before the as of date for the report. This information may be updated or revised in future reports. |  |  |  | 
```

### `BiffSheet`

[`biff_sheet.txt`](biff_sheet.txt), recorded in prism-clients' `tests/philadelphia_fed/cassettes/test_recorded/test_file[spf_error_statistics_cpi].yaml`, interaction 0, sheet `Sheet 1`. Read by [`BiffSheet`](../../src/flatread/sheets.py), in philadelphia_fed `_SpfErrorForecasts` (`_SpfErrorStatistics`).

A BIFF3 workbook that calamine cannot open: the grid xlrd reads, numbers to every digit, error cells as `#N/A`.

```
# sheet: Sheet 1
# columns: A | B | C | D | E | F | G | AA | AB
1: Date | SPFfor_Step1 | SPFfor_Step2 | SPFfor_Step3 | SPFfor_Step4 | SPFfor_Step5 | IARfor_Step1 | Realiz1 | Realiz2
2: 1968:04 | #N/A | #N/A | #N/A | #N/A | #N/A | #N/A | #N/A | #N/A
3: 1969:01 | #N/A | #N/A | #N/A | #N/A | #N/A | #N/A | #N/A | #N/A
190: 2015:04 | 0.886 | 1.7993 | 1.9 | 1.9558 | 2 | 2.0499807736088758 | 0.2266510543175393 | 0.7686245127321856
191: 2016:01 | 0.4 | 1.8022 | 2 | 1.9425 | 2.1 | 1.7416730931509579 | -0.3121112832365269 | -0.3121112832365269
192: 2016:02 | 1.9 | 1.6 | 2.1375 | 2.1568 | 2.1 | 1.554504513101459 | 2.541901548961034 | 2.526483163265336
229: 2025:03 | 3.017 | 3.4846 | 2.6844 | 2.2451 | 2.3431 | 2.3433010665156715 | #N/A | #N/A
230: 2025:04 | #N/A | 3 | 3.1798 | 2.6859 | 2.2741 | #N/A | #N/A | #N/A
231: 2026:01 | #N/A | #N/A | 2.6079 | 2.9 | 2.6144 | #N/A | #N/A | #N/A
232: 2026:02 | #N/A | #N/A | #N/A | 2.55 | 2.6223 | #N/A | #N/A | #N/A
233: 2026:03 | #N/A | #N/A | #N/A | #N/A | 2.5 | #N/A | #N/A | #N/A
```

### `GroupedSheet` (and `_groups_row`)

[`grouped_sheet.txt`](grouped_sheet.txt), recorded in prism-clients' `tests/atlanta_fed/cassettes/test_recorded/test_file[get_business_inflation_expectations_bie_data].yaml`, interaction 0, sheet `Quarterly- Price Change`. Read by [`GroupedSheet`](../../src/flatread/sheets.py), in atlanta_fed `_Grouped3`.

The group headings in row 3 each head their own column and those to the right; the labels are in row 4, and G is a spacer with no label.

```
# sheet: Quarterly- Price Change
# columns: A | B | C | D | E | F | G | H | I | J | K
1: 'Quarterly Question:  Looking back, by about what percent did you change prices over the last 12 months?\n    	                                  Looking ahead, by about what percent do you expect to change prices over the next 12 months?' |  |  |  |  |  |  |  |  |  |
2: 'Note: This quarterly question officially started in February 2024. The results prior to that were results collected through our Special Questions Series.' |  |  |  |  |  |  |  |  |  |
3:  |  | 'Realization (Past 12 months, Percentage)' |  |  |  |  | 'Expectations (Next 12 months, Percentage)' |  |  |
4: 'Date' | 'N' | 'Mean' | 'Median' | 'Variance' | 'Mode' |  | 'Mean' | 'Median' | 'Variance' | 'Mode'
5: 2020-12-11 00:00:00 | 204 | 5.1 | 2 |  |  |  | 1.6 | 0 |  |
6: 2021-04-09 00:00:00 | 199 | 4.5 | 3 |  |  |  | 2.83 | 3 |  |
7: 2021-07-09 00:00:00 | 175 | 9.6 | 3 |  |  |  | 2.8 | 4 |  |
22: 'May 15, 2026' | 430 | 3.687057154 | 3 | 22.245830793 | 5 |  | 4.137887005 | 3 | 13.142631046 | 5
23: 2026-08-15 00:00:00 | 512 | 4.521911803 | 4 | 29.998912359 | 5 |  | 3.705411975 | 3 | 14.277803999 | 5
```

## Text, reports, markup and PDFs

### `TextFile`

[`text_file.txt`](text_file.txt), recorded in prism-clients' `tests/philadelphia_fed/cassettes/test_recorded/test_file[spf_release_dates].yaml` interaction 0. Read by [`TextFile`](../../src/flatread/core.py), in philadelphia_fed `_SpfReleaseDates`.

a text file no shared piece reads: title, note, header, a survey per line (a year's first names it, a later row TAB-laid), the `*`/`**` marks and their notes, which the format gathers into cells itself (lines 1-9, 141-144, 185-188, 191-196)

```
Deadline and Release Dates for the Survey of Professional Forecasters
True deadline and news release dates for surveys prior to 1990:Q2 are not known.


Survey         True Deadline Date       News Release Date

1990 Q2             8/23/90*            8/31/90*
     Q3             8/23/90             8/31/90
     Q4             11/22/90            11/28/90
2017 Q1             2/7/17              2/10/17
     Q2             5/9/17              5/12/17
     Q3		    8/8/17		8/11/17
     Q4		    11/7/17		11/13/17	

2026 Q1             3/2/26**            3/6/26** 
     Q2             5/12/26             5/15/26
     Q3             8/11/26             8/14/26

*The 1990Q2 survey was not taken in real time, because the Philadelphia Fed had not yet 
 taken over the survey. Forecasters were asked to provide dated forecasts from May 1990.

**The 2026Q1, 2019Q1, 2013Q4, and 1996Q1 surveys were all delayed because of federal government 
 shutdowns, which in turn delayed the release of government statistical data.
```

### `XmlFile`

[`xml_file.txt`](xml_file.txt), recorded in prism-clients' `tests/congress/cassettes/test_recorded/test_post_employment_notifications.yaml` interaction 0. Read by [`XmlFile`](../../src/flatread/markup.py), in congress `_HouseZip`.

the zip's XML member: root `<PostEmployment>`, each child an `<Employee>` of four leaf elements in field order; two records and the closing tag (CRLF, BOM dropped)

```
# members: PostEmployment.txt, PostEmployment.xml
<?xml version="1.0" encoding="utf-8"?>
<PostEmployment>
  <Employee>
    <EmployeeName>DARWELL, WENDY, D</EmployeeName>
    <OfficeName>COMMITTEE ON APPROPRIATIONS</OfficeName>
    <TerminationDate>11/30/2007</TerminationDate>
    <LobbyingEligibilityDate>11/30/2008</LobbyingEligibilityDate>
  </Employee>
  <Employee>
    <EmployeeName>DARWELL, WENDY, D</EmployeeName>
    <OfficeName>HINCHEY, MAURICE D.</OfficeName>
    <TerminationDate>12/01/2007</TerminationDate>
    <LobbyingEligibilityDate>12/01/2008</LobbyingEligibilityDate>
  </Employee>
</PostEmployment>
```

### `NestedXml`

[`nested_xml.xml`](nested_xml.xml), recorded in prism-clients' `tests/sec/cassettes/test_recorded/test_body[xbrlrss_2005_04].yaml` interaction 0. Read by [`NestedXml`](../../src/flatread/markup.py), in sec `_XbrlRss`.

records nested at `channel/item/edgar:xbrlFiling` (and its `edgar:xbrlFile`s, read from attributes), the `edgar:` prefix bound on the filing; one item, two of its seven files, closing tags kept

```
<?xml version="1.0" encoding="windows-1252"?>
<?xml-stylesheet type="text/xsl" href="/rss/styles/shared_xsl_stylesheet_v2.xml"?>
<rss version="2.0">
  <channel>
    <item>
      <title>EDGAR ONLINE INC (0001080224) (Filer)</title>
      <link>http://www.sec.gov/Archives/edgar/data/1080224/000127528705001434/0001275287-05-001434-index.htm</link>
      <description>8-K</description>
      <pubDate>Mon, 25 Apr 2005 15:15:09 EDT</pubDate>
      <edgar:xbrlFiling xmlns:edgar="http://www.sec.gov/Archives/edgar">
        <edgar:companyName>EDGAR ONLINE INC</edgar:companyName>
        <edgar:formType>8-K</edgar:formType>
        <edgar:filingDate>04/25/2005</edgar:filingDate>
        <edgar:cikNumber>0001080224</edgar:cikNumber>
        <edgar:accessionNumber>0001275287-05-001434</edgar:accessionNumber>
        <edgar:fileNumber>001-32194</edgar:fileNumber>
        <edgar:acceptanceDatetime>20050425151509</edgar:acceptanceDatetime>
        <edgar:period>20050425</edgar:period>
        <edgar:assistantDirector>2 &amp; 3</edgar:assistantDirector>
        <edgar:assignedSic>7389</edgar:assignedSic>
        <edgar:fiscalYearEnd>1204</edgar:fiscalYearEnd>
        <edgar:xbrlFiles>
          <edgar:xbrlFile edgar:sequence="1" edgar:file="eo2425.txt" edgar:type="8-K" edgar:size="5282" edgar:description="" edgar:url="http://www.sec.gov/Archives/edgar/data/1080224/000127528705001434/eo2425.txt" />
          <edgar:xbrlFile edgar:sequence="2" edgar:file="eo2425ex991.txt" edgar:type="EX-99.1" edgar:size="4469" edgar:description="" edgar:url="http://www.sec.gov/Archives/edgar/data/1080224/000127528705001434/eo2425ex991.txt" />
          <edgar:xbrlFile edgar:sequence="3" edgar:file="edgr-2004_10k.xml" edgar:type="EX-100.INS" edgar:size="25257" edgar:description="" edgar:url="http://www.sec.gov/Archives/edgar/data/1080224/000127528705001434/edgr-2004_10k.xml" />
        </edgar:xbrlFiles>
      </edgar:xbrlFiling>
    </item>
  </channel>
</rss>
```

### `FixedWidthFile`

[`fixed_width_file.txt`](fixed_width_file.txt), recorded in prism-clients' `tests/federal_reserve/cassettes/test_recorded/test_file[lbr_current].yaml` interaction 0. Read by [`FixedWidthFile`](../../src/flatread/fixed.py), in federal_reserve `_LbrLines`.

the large bank list: titles, a three-line header over a rule of dashes per column, banks at fixed places (a name wrapping onto the next line), the closing rule and summary (lines 1-14, 10256-10264)

```
U.S. DOMESTICALLY CHARTERED COMMERCIAL BANKS,
RANKED by CONSOLIDATED ASSETS
As of June 30, 2026

------------------------------------------------------------------------------------------------------------------------------------------------------------
Bank Name / Holding Co       Nat'lRank   Bank ID  Bank Headquarters     Charter    Consol     Domestic      Pct         Pct       Domestic   Foreign    IBF 
Name                                                                               Assets      Assets     Domestic   Cumulative   Branches   Branches       
                                                                                   (Mil $)     (Mil $)     Assets      Assets                               
--------------------------- ----------- --------- -------------------- --------- ----------- ----------- ---------- ------------ ---------- ---------- -----
JPMORGAN CHASE BK NA /           1       852218   COLUMBUS, OH            NAT     4,091,315   2,877,877      70          16         5137        31       Y  
JPMORGAN CHASE & CO                                                                                                                                         

BANK OF AMER NA / BANK OF        2       480228   CHARLOTTE, NC           NAT     2,654,645   2,488,356      94          27         3593        20       Y  
AMER CORP                                                                                                                                                   
EMIGRANT MERCANTILE BK /       3765      3268173  NEW YORK, NY            NMB         3           3         100         100          0          0        N  
NEW YORK PRIV B&TR CORP                                                                                                                                     
------------------------------------------------------------------------------------------------------------------------------------------------------------

 
Summary:

Total number of banks: 3765
Total assets (millions): Consolidated: $25,182,894 | Domestic: $22,528,407
```

### `FixedWidthFile._stated`

[`fixed_width_file-stated.txt`](fixed_width_file-stated.txt), recorded in prism-clients' `tests/occ/cassettes/test_recorded/test_endpoint[cont_volume_download].yaml` interaction 0. Read by [`FixedWidthFile._stated`](../../src/flatread/fixed.py), in occ `_VolumeFile`.

the title line writing the report date twice (`H11062026      11062026`), stated on every row, over packed fixed-width rows

```
        H11062026      11062026
A     A     A000000000000000000000000012OSTK20261120
A     A     A000000006000000000000000006OSTK20261218
A     A     A000000002000000000000000002OSTK20270115
A     A     P000000013000000000000000013OSTK20261120
```

### `FixedWidthFile._header` (`False`)

[`fixed_width_file-header.txt`](fixed_width_file-header.txt), recorded in prism-clients' `tests/occ/cassettes/test_recorded/test_endpoint[trade_volume_download].yaml` interaction 0. Read by [`FixedWidthFile._header`](../../src/flatread/fixed.py), in occ `_VolumeFile` (also federal_reserve `_LbrLines`).

no header line at all: rows straight under the title, cells run together, to the last line

```
        H11062026      11062026
A     A     A000000008000000000000000020OSTK
A     A     P000000081000000000000000081OSTK
A     A     Z000000047000000000000000047OSTK
A     A     B000000007000000000000000019OSTK
ZYME  ZYME  L000000029000000000000000029OSTK
```

### `ReportFile`

[`report_file.txt`](report_file.txt), recorded in prism-clients' `tests/noaa/cassettes/test_recorded/test_file[archives_heating_weekly_city].yaml` interaction 0. Read by [`ReportFile`](../../src/flatread/reports.py), in noaa `_SummaryReport`.

a page-laid report: titles stating the period and season start, a legend, a five-line column header, then row lines (first four and the last)

Its twin: [`LongReport`](#longreport) reads a row's last cells as a run of values, one per key a header line names, and gives a row per key, where this gives a record per row line.

```
  
                HEATING DEGREE DAY DATA WEEKLY SUMMARY
                CLIMATE PREDICTION CENTER-NCEP-NWS-NOAA
  
            LAST DATE OF DATA COLLECTION PERIOD IS NOV  1, 2025
            ACCUMULATIONS ARE FROM JUL  1, 2025 TO NOV  1, 2025
             -999 = NORMAL LESS THAN 100 OR RATIO INCALCULABLE
  
 STATE CITY        CALL WEEK  WEEK WEEK    CUM   CUM   CUM   CUM   CUM
                        TOTAL DEV  DEV     TOTAL DEV   DEV   DEV   DEV
                              FROM FROM          FROM  FROM  FROM  FROM
                              NORM L YR          NORM  L YR  NORM  L YR
                                                             PRCT  PRCT
  
 AL BIRMINGHAM      BHM   75    25   75      98   -54    47   -36    92
 AL HUNTSVILLE      HSV   82    21   82     123   -70    62   -36   102
 AL MOBILE          MOB   43    19   43      45   -12    29  -999  -999
 AL MONTGOMERY      MGM   68    33   68      88    -5    42  -999  -999
 WY WORLAND         WRL  178    14   18     669  -115    72   -15    12
```

### `ReportFile._preamble`

[`report_file-preamble.txt`](report_file-preamble.txt), recorded in prism-clients' `tests/sec/cassettes/test_recorded/test_body[full_index_master_1993q1].yaml` interaction 0. Read by [`ReportFile._preamble`](../../src/flatread/reports.py), in sec `_EdgarIndex`.

the whole 1993Q1 master index: free-form `Description:`... preamble and blank lines above the `CIK\ | ...` column line and its rule, then four filings (no final newline)

```
Description:           Master Index of EDGAR Dissemination Feed
Last Data Received:    March 31, 1993
Comments:              webmaster@sec.gov
Anonymous FTP:         ftp://ftp.sec.gov/edgar/
Cloud HTTP:            https://www.sec.gov/Archives/




CIK|Company Name|Form Type|Date Filed|Filename
--------------------------------------------------------------------------------
860585|RBS PARTNERS L P /CT|13FCONP|1993-02-11|edgar/data/860585/9999999997-04-035713.txt
880794|MERRILL LYNCH LIFE VARIABLE ANNUITY SEPARATE ACCOUNT B|NSAR-B|1993-02-26|edgar/data/880794/9999999997-05-050433.txt
926688|SMITH THOMAS W|13F-HR|1993-02-12|edgar/data/926688/9999999997-05-015654.txt
94673|STORAGE TECHNOLOGY CORP|CERTNYS|1993-02-24|edgar/data/94673/9999999997-05-037760.txt
```

### `ReportFile._last_line`

[`report_file-end.txt`](report_file-end.txt), recorded in prism-clients' `tests/occ/cassettes/test_recorded/test_endpoint[flex_reports_open_interest_equity].yaml` interaction 0. Read by [`ReportFile._last_line`](../../src/flatread/reports.py), in occ `_FlexOpenInterest` (via `_PrintReport`).

the report's last non-blank line `***** END OF REPORT *****` after class and grand totals (a cut file would lack it); its head kept too (lines 1-14, 26553-26563)

```
                                                                                                                                                
 THE OPTIONS CLEARING CORPORATION - CHICAGO, ILLINOIS                             SYSTEM DATE 11/06/26    TIME 21:44:33    PAGE     1           
 EQUITY FLEX OPEN INTEREST REPORT                                               ACTIVITY DATE 11/06/26    PROGRAM ID BV2C0210   V005            
                                                                                                                                                
 MARK PRICES NOTED ON THIS REPORT ARE DERIVED BY THE CORPORATION FROM FACTORS                                                                   
 AND DATA DEEMED PERTINENT WHENEVER APPROPRIATE CLOSING PRICES IN FLEX                                                                          
 OPTIONS ARE NOT AVAILABLE.                                                                                                                     
                                                                                                                                                
                   EXPIRATION  STRIKE        MARK     OPEN                                                                                      
      SYMBOL  P/C  MO DAY  YR  PRICE         PRICE    INTEREST                                                                                  
                                                                                                                                                
 American Airlines Group, Inc.                                                                                                                  
      1AAL     C   11 06 2026  00016 250      0.0000     10680                                                                                  
      1AAL     C   11 23 2026  00017 260      0.0078     10150                                                                                  
 State Street Utilities Select                                                                                                                  
      4XLU     C   11 20 2026  00040 000      3.2893     30977                                                                                  
      4XLU     C   11 20 2026  00044 000      0.3100     30978                                                                                  
                                                                                                                                                
                          ***  CLASS TOTALS  ***         61955                                                                                  
                                                                                                                                                
                                                                                                                                                
                       ***  GRAND TOTALS ***          68206805                                                                                  
                                                                                                                                                
                      ***** END OF REPORT *****                                                                                                 
                                                                                                                                                
```

### `LongReport`

[`long_report.txt`](long_report.txt), recorded in prism-clients' `tests/treasury/cassettes/test_tic_securities/test_recorded[major_foreign_holders].yaml` interaction 0. Read by [`LongReport`](../../src/flatread/reports.py), in treasury `_MajorForeignHolders`.

TIC table 5: titles, a `Country` header naming 13 months (the keys), each row a country then a run of 13 TAB-separated values read one row per month; notes below

Its twin: [`ReportFile`](#reportfile) reads each row line as one record, a named group per field.

```
Table 5: Major Foreign Holders of Treasury Securities													
Holdings at end of time period													
Billions of dollars													
Link: https://ticdata.treasury.gov/resource-center/data-chart-center/tic/Documents/slt_table5.txt													
													
Country	2026-08	2026-07	2026-06	2026-05	2026-04	2026-03	2026-02	2026-01	2025-12	2025-11	2025-10	2025-09	2025-08
Japan	1068.3	1103.9	1116.7	1143.1	1209.9	1191.6	1239.3	1225.3	1185.5	1202.7	1200.0	1189.3	1183.9
United Kingdom	1009.5	998.3	939.9	948.6	937.5	927.1	897.3	879.8	863.2	879.8	875.4	862.5	901.3
China, Mainland	590.7	618.0	633.4	659.3	651.1	653.3	694.2	695.3	684.4	683.9	687.7	699.2	699.7
All Other	1852.3	1842.7	1850.8	1860.2	1861.5	1828.5	1861.7	1843.9	1827.1	1843.5	1833.2	1797.9	1794.3
Grand Total	9331.7	9248.4	9298.5	9368.5	9350.1	9352.4	9487.3	9289.1	9270.5	9350.4	9231.3	9238.2	9249.1
													
Notes:													
The data in this table are collected primarily from U.S.-based custodians and broker-dealers. Since U.S. securities held in													
```

### `Transcoded`

[`transcoded.csv`](transcoded.csv), recorded in prism-clients' `tests/eia/cassettes/test_recorded/test_file[wpsr-table8.csv].yaml` interaction 1. Read by [`Transcoded`](../../src/flatread/archives.py), in eia `_Table`.

a Windows-1252 WPSR table: Guyana's row holds raw `0x96` en-dash bytes (no value), not UTF-8

```
"STUB_1","STUB_2","2025  Percentage ","10/30/26","10/23/26","Difference","10/31/25","Percent Change","11/1/24","Percent Change","10/30/26","10/31/25","Percent Change"
"Crude Imports By Country of Origin ","Canada","63.4","3,991","3,928","63","3,442","15.9","3,879","2.9","3,893","3,590","8.5"
"Crude Imports By Country of Origin ","Mexico","6.2","283","305","-23","322","-12.3","247","14.4","239","348","-31.5"
"Crude Imports By Country of Origin ","Saudi Arabia","4.4","293","227","66","403","-27.3","443","-33.9","241","300","-19.6"
"Crude Imports By Country of Origin ","Guyana","3.4","194","281","-86","–","–","–","–","217","–","–"
```

### `Transcoded._eof`

[`transcoded-eof.csv`](transcoded-eof.csv), recorded in prism-clients' `tests/eia/cassettes/test_recorded/test_file[wpsr-table1.csv].yaml` interaction 1. Read by [`Transcoded._eof`](../../src/flatread/archives.py), in eia `_Table`.

table1.csv's second section (header, two rows), its last rows, and the DOS end-of-file byte `0x1A` after the last CRLF

```
"STUB_1","STUB_2","10/30/26","10/23/26","Difference","10/31/25","Difference","10/30/26","10/31/25","Percent Change","10/30/26","10/31/25","Percent Change"
"Crude Oil Supply ","(1)     Domestic Production","14,003","13,992","11","13,651","352","13,736","13,640","0.7","13,746","13,463","2.1"
"Crude Oil Supply ","(2)        Alaska","487","482","5","436","51","487","430","13.1","434","419","3.4"
"Products Supplied ","(36)     Other Oils","5,266","4,834","432","4,883","383","5,242","4,967","5.5","5,036","4,799","4.9"
"Net Imports of Crude and Petroleum Products ","(33)   Total","-3,086","-4,839","1,753","-3,767","681","-4,003","-4,159","-3.7","-4,078","-2,824","44.4"
```

### `Transcoded._encodings`

[`transcoded-encodings.csv`](transcoded-encodings.csv), recorded in prism-clients' `tests/sec/cassettes/test_recorded/test_body[fails_deliver_202001a].yaml`, interaction 0. Read by [`Transcoded._encodings`](../../src/flatread/archives.py), in sec `_FailsFile` (`("utf-8", "latin-1")`).

January 2020's fails file is UTF-8 but for one latin-1 byte (`0xBB`, in `AM»RICAS`): the first encoding that decodes the body reads it. The zip's one member, cut to two rows; its trailers restated for them.

```
SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE
20200103|29273B104|NDRA|18060|ENDRA LIFE SCIENCES INC COM|1.90
20200103|29274F104|ENIA|692|ENEL AM»RICAS S.A.|11.15
Trailer record count 2
Trailer total quantity of shares 18752
```

### `FoundTable`

[`found_table.csv`](found_table.csv), recorded in prism-clients' `tests/sec/cassettes/test_recorded/test_body[file_foia_log_2020_05_b7a].yaml` interaction 0. Read by [`FoundTable`](../../src/flatread/found.py), in sec `_FoiaLog` (`_Export`).

a free-text title line (`B7A FOIA log ending 5-31-2020,,,`) above the header the reader finds by its key's words (`Request ID`), then rows

Its twins: [`TitledCsv`](#titledcsv) matches a known regex per line above the header, and [`PreambleCsv`](#preamblecsv) drops lines up to the first opening with the first label; this finds the header by the key field's words and places each column by its label's.

```
B7A FOIA log ending 5-31-2020,,,,,,,,
Request ID,Requester Name,Organization,Request Description,Requested Date,Received Date,Request Status,Closed Date,Final Disposition
19-02182-FOIA,"Morris, Meghan",Business Insider,BlackRock. Inc.,5/24/2019,5/24/2019,Closed,5/6/2020,Granted/Denied in Part
19-03014-FOIA,"Herrig, Kyle",Allied Progress,email correspondence between Commissioner Hester M. Peirce and representatives of the Bank Policy Institute (BPI; @bpi.com) or the Securities Industry and Financial Markets Associates (SIFMA; sifma.org),8/29/2019,8/29/2019,Closed,5/11/2020,Granted/Denied in Part
19-03273-FOIA,"Perry, Alyssa",Wondery Media,Jeffrey E. Epstein,9/26/2019,9/26/2019,Closed,5/8/2020,Granted/Denied in Part
```

### `FoundTable._open`

[`found_table-open.txt`](found_table-open.txt), recorded in prism-clients' `tests/sec/cassettes/test_recorded/test_body[file_advisers_2012_02_exempt].yaml` interaction 0. Read by [`FoundTable._open`](../../src/flatread/found.py), in sec `_Roster` (`_RosterSheet`).

an adviser roster (zipped xlsx): every header cell (SEC Region, Firm Type, ...) a column of its own, as written, none declared by the struct; columns M to BD dropped

```
# members: 2012-2-1 Exempt Reporting Adviser Public Report.xlsx
# sheet: IA_SEC_-_FIRM_ROSTER_FOIA_DOWNL
# columns: A | B | C | D | E | F | G | H | I | J | K | L
1: SEC Region | Organization CRD# | SEC# | Firm Type | Primary Business Name | Legal Name | Main Office Street Address 1 | Main Office Street Address 2 | Main Office City | Main Office State | Main Office Country | Main Office Postal Code
2: HQ | 155299 | 802-73114 | ERA | THE CATALYST CAPITAL GROUP INC. | THE CATALYST CAPITAL GROUP INC. | 77 KING STREET WEST, ROYAL TRUST TOWER, SUITE 4320 |  | TORONTO |  | ONTARIO, CANADA | M5K 1J3
3: HQ | 156645 | 802-73060 | ERA | ELSWORTHY CAPITAL LLP | ELSWORTHY CAPITAL LLP | 40 NEW BOND STREET |  | LONDON |  | UNITED KINGDOM | W1S 2RX
4: HQ | 159108 | 802-73118 | ERA | AZINI CAPITAL | AZINI CAPITAL PARTNERS LLP | 29 FARM STREET | LONDON | LONDON |  | UNITED KINGDOM | W1J 5RL
```

### `FoundTable(null_values=...)`

[`found_table-nulls.csv`](found_table-nulls.csv), recorded in prism-clients' `tests/sec/cassettes/test_recorded/test_body[file_bdc_report_2020].yaml` interaction 0. Read by [`FoundTable(null_values=...)`](../../src/flatread/found.py), in sec `_Export` (`_CsvExport`).

SEC's `[NULL]` in Address_2, read as no value only with `null_values`

```
File_No,CIK,Registrant_Name,Address_1,Address_2,City,State,Zip_Code,Filing Date,Filing Type
814-00011,0000701571,WESTERN CAPITAL CORP,1750 WEST FRONT STREET,[NULL],BOISE,ID,83702,09/15/14,D
814-00053,0000821472,WESTFORD TECHNOLOGY VENTURES LP,17 ACADEMY ST 5TH FLOOR,[NULL],NEWARK,NJ,07102-2905,01/28/04,NO ACT
814-00061,0000017313,CAPITAL SOUTHWEST CORP,5400 LYNDON B. JOHNSON FREEWAY,SUITE 1300,DALLAS,TX,75240,06/22/20,DEFA14A
```

### `FoundTable._row_count`

[`found_table-rowcount.csv`](found_table-rowcount.csv), recorded in prism-clients' `tests/sec/cassettes/test_recorded/test_body[file_bdc_report_2013].yaml` interaction 0. Read by [`FoundTable._row_count`](../../src/flatread/found.py), in sec `_Export` (`_CsvExport`).

2013's BDC export: an unlabelled first column of row numbers (`, File_No,...` then `1, 814-00637 ...`), padded cells and ` NULL ` too

```
, File_No,                     CIK,          Registrant_Name,Address_1,Address_2,City,State,Zip_Code,sub_type,last_filing_date, 
1, 814-00637                 , 0001090870 , 21ST CENTURY TECHNOLOGIES INC                                                                                                                          , 2700 W. SAHARA BOULEVARD                 , SUITE 440                                , LAS VEGAS                      , NV    ,89102, REVOKED    , 05/16/12 ,
2, 814-00873                 , 0001514534 , ALDA CAPITAL Corp                                                                                                                                      ," 233 S, WACKER DRIVE                      ", SUITE 9330                               , CHICAGO                        , IL    ,60606, CORRESP    , 06/30/11 ,
3, 814-00161                 , 0000941661 , ALLIED CAPITAL FINANCIAL CORP                                                                                                                          ," 1919 PENNSYLVANIA AVE., NW               ", NULL                                     , WASHINGTON                     , DC    ,20006, 40-17F2    , 03/28/03 ,
```

### `FoundTable._section_prefix`

[`found_table-section.csv`](found_table-section.csv), recorded in prism-clients' `tests/sec/cassettes/test_recorded/test_body[file_foia_log_2023_12].yaml` interaction 0. Read by [`FoundTable._section_prefix`](../../src/flatread/found.py), in sec `_FoiaLog`.

`Request Type: Consultation,,,` section lines stating `request_type` for the rows under them, and a section's `Total No of Requests ...` line

```
Request ID,Requester Name,Organization,Requester Category,Request Description,Requested Date,Received Date,Request Status,Closed Date,Final Disposition,
Request Type: Consultation,,,,,,,,,,
24-00006-C,"Royster-Mitchell, ReNay",Department of the Treasury,Federal Government,FOIA case number 2023-FOIA-00764 (PayPal),12/11/2023,12/11/2023,Closed,01/11/2024,Granted/Denied in Part,
Total No of Requests for 'Consultation' :1,,,,,,,,,,
Request Type: Exhibit,,,,,,,,,,
24-00224-E,"Stubblefield, Ayana",ktMINE,Commercial Organization,"Exhibit 10.22 to form 10-K filed on March 16, 2006 by Electronics for Imaging, Inc",12/05/2023,12/05/2023,Closed,12/06/2023,Other Reasons ,
```

### `FoundCsv`

[`found_csv.csv`](found_csv.csv), recorded in prism-clients' `tests/sec/cassettes/test_recorded/test_body[file_filings_by_form_type].yaml` interaction 0. Read by [`FoundCsv`](../../src/flatread/found.py), in sec `_CsvExport`.

a CSV export opening with a UTF-8 byte-order mark before its header (`Year,Type,Count`), dropped by FoundCsv

```
Year,Type,Count
2026,1,6
2026,1-A,125
2026,1-A POS,139
2026,1-A-W,24
```

### `FoundSheet`

[`found_sheet.txt`](found_sheet.txt), recorded in prism-clients' `tests/sec/cassettes/test_recorded/test_body[file_municipal_advisors_2026_11].yaml` interaction 0. Read by [`FoundSheet`](../../src/flatread/sheets.py), in sec `_SheetExport`.

a workbook's first sheet: two title rows, the header found in row 3, merged cells leaving blank columns between labels

```
# sheet: Current Municipal Advisor Infor
# columns: A | B | C | D | E | F | G | H | I | J
1: Current Municipal Advisor Information Report |  |  |  |  |  |  |  |  |
2: Report as 11/01/2026 |  |  |  |  |  |  |  |  |
3: Registrant Name |  |  |  |  | File Number |  |  |  | CIK
4: &PARTNERS |  |  |  |  | 867-01682 |  |  |  | 0000107136
5: 22Beacon, Inc. |  |  |  |  | 867-02770 |  |  |  | 0002069882
6: 30 Three Sixty Public Finance, Inc. |  |  |  |  | 867-02350 |  |  |  | 0001733578
413: Zomermaand Financial Advisory Services, L.L.C. |  |  |  |  | 867-00384 |  |  |  | 0001589157
```

### `HtmlFile`

[`html_file.html`](html_file.html), recorded in prism-clients' `tests/sec/cassettes/test_recorded/test_body[file_listing_form_type].yaml` interaction 0. Read by [`HtmlFile`](../../src/flatread/pages.py), in sec `_FilePage`.

a clean `File / Format / Size` table: header row, one row, the link's href a field (page lines 1079-1098)

```
  <table class="usa-table views-table views-view-table cols-3">
    <caption class="visually-hidden">
              
                </caption>
          <thead>
        <tr>
                                                            <th id="view-field-display-title-table-column" class="views-field views-field-field-display-title" scope="col">File</th>
                                                            <th id="view-extension-table-column" class="views-field views-field-extension" scope="col">Format</th>
                                                            <th id="view-filesize-table-column" class="views-field views-field-filesize" scope="col">Size</th>
                  </tr>
      </thead>
        <tbody>
              <tr>
                                                                                                        <td headers="view-field-display-title-table-column" class="views-field views-field-field-display-title">  <a href="/files/dera/data/number-edgar-filings-form-type/filings_type_year_0926.csv" download>EDGAR Filing Counts by Form Type</a>
            </td>
                                                                                                        <td headers="view-extension-table-column" class="views-field views-field-extension">CSV             </td>
                                                                                                        <td headers="view-filesize-table-column" class="views-field views-field-filesize">187.93 KB            </td>
                  </tr>
          </tbody>
  </table>
```

### `HtmlFile._table_selector`

[`html_file-table.html`](html_file-table.html), recorded in prism-clients' fixture `tests/farside/pages/zec.html`. Read by [`HtmlFile._table_selector`](../../src/flatread/pages.py), in farside `_FlowTable`.

the page's `table.etf` (three header rows, a day's row) among other tables (`table.tfooter`), which `_table_selector` leaves out

```
<table class="etf">
<thead>
<tr bgcolor="#eaeffa">
<th><div align="right"></div></th>
<th><div align="center"><a href="" class="grow"><img decoding="async" src="/images/etf/grayscale.jpg" alt="21 Shares" width="36" height="16"></a></div></th>
<th><div align="right"><span class="tabletext">Total</span></div></th>
</tr>
<tr bgcolor="#eaeffa">
<th><span class="tabletext"></span></th>
<th><div align="right"><span class="tabletext">ZCSH</span></div></th>
<th><div align="right"><span class="tabletext"></span></div></th>
</tr>
<tr bgcolor="#eaeffa">
<th><span class="tabletext">Fee</span></th>
<th><div align="right"><span class="tabletext">2.5%</span></div></th>
<th><div align="right"><span class="tabletext"></span></div></th>
</tr>
	</tr>
</thead>
<tbody>  
<tr>
            <tr> 
                <td><span class="tabletext">21 Oct 2026</span></td>
                <td><div align="right"><span class="tabletext">15.2</span></div></td><td><div align="right"><span class="tabletext">15.2</span></div></td>            </tr>
</tbody>
</table>
<table class="tfooter">
<tr><td style="background-color: #f2f2f2">
</td></tr></table>
```

### `HtmlRecords`

[`html_records.html`](html_records.html), recorded in prism-clients' `tests/congress/cassettes/test_recorded/test_foreign_travel_reports[2024-q1].yaml` interaction 0. Read by [`HtmlRecords`](../../src/flatread/pages.py), in congress `_ForeignTravelReports`.

records found by selector (`tbody tr`), each field by a selector inside its record (`a[href$='.txt']`); two records

```
<table class="table">
    <tbody>
        <tr role="row">
            <td class="table_description">
                January 09
            </td>
            <td class="table_link">
                    <a target="_blank" rel="noreferrer" href="foreign-reports/2024q1jan09.pdf"><i class="fa fa-file-pdf-o"></i></a>
            </td>
                <td class="table_link">
                    <a target="_blank" rel="noreferrer" href="foreign-reports/2024q1jan09.txt"><i class="fa fa-file-o"></i></a>
                </td>
            </tr>
        <tr role="row">
            <td class="table_description">
                January 25
            </td>
            <td class="table_link">
                    <a target="_blank" rel="noreferrer" href="foreign-reports/2024q1jan25.pdf"><i class="fa fa-file-pdf-o"></i></a>
            </td>
                <td class="table_link">
                    <a target="_blank" rel="noreferrer" href="foreign-reports/2024q1jan25.txt"><i class="fa fa-file-o"></i></a>
                </td>
            </tr>
    </tbody>
</table>
```

### `HtmlRecords._heading_selector` / `_heading_pattern`

[`html_records-heading.html`](html_records-heading.html), recorded in prism-clients' `tests/federal_reserve/cassettes/test_recorded/test_fomc_calendar.yaml` interaction 0. Read by [`HtmlRecords._heading_selector`](../../src/flatread/pages.py), in federal_reserve `_FomcCalendar` (also cftc `_SwapsReportsArchive`).

`div.panel-heading` `2026 FOMC Meetings` / `2025 FOMC Meetings` heading the `div.fomc-meeting` records below, `year` stated from each; one meeting per year, blank lines and one meeting's link columns dropped

```
<div class="panel panel-default"><div class="panel-heading"><h4><a id="42828">2026 FOMC Meetings</a></h4></div>
        <div class="fomc-meeting--shaded row fomc-meeting" ">
            <div class="fomc-meeting--shaded fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>December</strong></div>
            <div class="fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-1">8-9*</div>
            <div class="col-xs-12 col-md-4 col-lg-2">
            </div>
            <div class="col-xs-12 col-md-4 col-lg-3">
                <br>
            </div>
            <div class="col-xs-12 col-md-4 col-lg-4 fomc-meeting__minutes">
            </div>
        </div>
<div class="panel-footer">* Meeting associated with a Summary of Economic Projections.  </div>
</div>
<div class="panel panel-default"><div class="panel-heading"><h4><a id="42827">2025 FOMC Meetings</a></h4></div>
        <div class="row fomc-meeting" ">
            <div class="fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>January</strong></div>
            <div class="fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-1">28-29</div>
        </div>
<div class="panel-footer">* Meeting associated with a Summary of Economic Projections. </div>
</div>
```

### `PdfFile`

[`pdf_file.txt`](pdf_file.txt), recorded in prism-clients' `tests/congress/cassettes/test_recorded/test_periodic_transaction_report[20035492].yaml`, interaction 0. Read by [`PdfFile`](../../src/flatread/pdf.py), under [`LaidOutPdf`](../../src/flatread/pdf.py) in congress `_PeriodicTransactions`.

the text layer in drawn order: label and value as separate segments, the header's labels out of order (`Type` before `Date`), the `Filing ID` line drawn last

```
p1 top 601.6: 'Name:'
p1 top 601.6: 'Hon. Richard W. Allen'
p1 top 585.9: 'Status:'
p1 top 585.9: 'Member'
p1 top 570.1: 'State/District: GA12'
p1 top 535.9: 'T'
p1 top 510.6: 'ID'
p1 top 510.6: 'Owner Asset'
p1 top 510.6: 'Transaction'
p1 top 499.4: 'Type'
p1 top 510.6: 'Date'
p1 top 510.6: 'Notification'
p1 top 499.4: 'Date'
p1 top 510.6: 'Amount'
p1 top 510.6: 'Cap.'
p1 top 499.4: 'Gains >'
p1 top 488.1: '$200?'
p1 top 466.0: 'SP'
p1 top 466.0: 'Broadcom Inc. - Common Stock'
p1 top 455.5: '(AVGO) [ST]'
p1 top 466.0: 'P'
p1 top 466.0: '08/12/2026 09/15/2026'
p1 top 466.0: '$1,001 - $15,000'
p1 top 718.8: 'Filing ID #20035492'
```

### `LaidOutPdf`

[`laid_out_pdf.txt`](laid_out_pdf.txt), recorded in prism-clients' `tests/congress/cassettes/test_recorded/test_periodic_transaction_report[20035492].yaml` interaction 0. Read by [`LaidOutPdf`](../../src/flatread/pdf.py), in congress `_PeriodicTransactions` (also `_Disclosure`).

the same PTR laid out: top to bottom, one baseline one line, words at x; header edges, two rows, one wrapping its amount

```
p1 top 601.6: Name:@21.7 Hon.@99.0 Richard@122.9 W.@159.6 Allen@174.1
p1 top 585.9: Status:@21.7 Member@99.0
p1 top 570.1: State/District:@21.7 GA12@99.0
p1 top 535.9: T@21.7
p1 top 510.6: ID@25.3 Owner@65.1 Asset@104.1 Transaction@262.3 Date@326.8 Notification@381.6 Amount@446.1 Cap.@524.8
p1 top 499.4: Type@262.3 Date@381.6 Gains@524.8 >@555.2
p1 top 488.1: $200?@524.8
p1 top 466.0: SP@64.9 Broadcom@103.9 Inc.@147.1 -@164.6 Common@170.2 Stock@209.0 P@262.2 08/12/2026@326.7 09/15/2026@381.4 $1,001@445.9 -@474.8 $15,000@480.4
p1 top 455.5: (AVGO)@103.9 [ST]@138.1
p1 top 437.9: F@103.8 S:@131.0 New@162.4
p1 top 425.2: S@103.8 O:@153.9 R.W.@169.2 Allen@190.2 &@212.0 Associates,@220.2 Inc.@263.8 >@280.4 RWA&A@288.0 -@321.9 Securities@327.2
p1 top 399.3: SP@64.9 Rollins,@103.9 Inc.@136.7 Common@154.2 Stock@193.0 (ROL)@217.1 S@262.2 08/12/2026@326.7 09/15/2026@381.4 $15,001@445.9 -@479.6
p1 top 388.8: [ST]@103.9 $50,000@445.9
```

### `LaidOutPdf._slack`

[`laid_out_pdf-slack.txt`](laid_out_pdf-slack.txt), recorded in prism-clients' `tests/congress/cassettes/test_recorded/test_periodic_transaction_report[20035492].yaml` interaction 0. Read by [`LaidOutPdf._slack`](../../src/flatread/pdf.py), in congress `_PeriodicTransactions` (also `_Disclosure`).

header and first row at two decimals: each cell 0.1-0.2 pt left of its label (Asset 104.07, cell 103.95)

```
p1 top 510.6: ID@25.32 Owner@65.07 Asset@104.07 Transaction@262.32 Date@326.82 Notification@381.57 Amount@446.07 Cap.@524.82
p1 top 466.0: SP@64.95 Broadcom@103.95 Inc.@147.11 -@164.62 Common@170.15 Stock@208.98 P@262.20 08/12/2026@326.70 09/15/2026@381.45 $1,001@445.95 -@474.81 $15,000@480.35
```

### `SdmxCsv`

[`sdmx_csv.csv`](sdmx_csv.csv), recorded in prism-clients' `tests/ecb/cassettes/test_recorded/test_flow[AME].yaml` interaction 0. Read by [`SdmxCsv`](../../src/flatread/sdmx.py), in ecb `_SdmxCsv`.

a whole SDMX-CSV message: KEY, dimensions, TIME_PERIOD, OBS_VALUE, attributes upper-cased, two observations

```
KEY,FREQ,AME_REF_AREA,AME_TRANSFORMATION,AME_AGG_METHOD,AME_UNIT,AME_REFERENCE,AME_ITEM,TIME_PERIOD,OBS_VALUE,OBS_STATUS,OBS_CONF,OBS_PRE_BREAK,TIME_FORMAT,PUBL_ECB,PUBL_MU,PUBL_PUBLIC,COMPILATION,EXT_TITLE,EXT_UNIT,TITLE_COMPL
AME.A.AUT.1.0.0.0.OVGD,A,AUT,1,0,0,0,OVGD,2026,419.159,A,,,P1Y,,,,,Gross domestic product at 2020 reference levels,Mrd EURO-ATS,"Gross domestic product at 2020 reference levels, Mrd EURO-ATS"
AME.A.AUT.1.0.0.0.OVGD,A,AUT,1,0,0,0,OVGD,2027,422.839,A,,,P1Y,,,,,Gross domestic product at 2020 reference levels,Mrd EURO-ATS,"Gross domestic product at 2020 reference levels, Mrd EURO-ATS"
```

### `SdmxStructure`

[`sdmx_structure.txt`](sdmx_structure.txt), recorded in prism-clients' `tests/federal_reserve/cassettes/test_recorded/test_file[dsr].yaml` interaction 0. Read by [`SdmxStructure`](../../src/flatread/sdmx.py), in federal_reserve `_Structure`.

the zip's `DSR_struct.xml` cut to one code list (`CL_OBS_STATUS`, two codes), closing tags kept

```
# members: DSR_data.xml, DSR_DSR.xsd, DSR_struct.xml, frb_common.xsd
<?xml version="1.0"?>
<message:Structure xmlns:message="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message" xmlns:structure="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/structure" xmlns:common="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/common" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message SDMXMessage.xsd">
  <message:CodeLists>
    <structure:CodeList id="CL_OBS_STATUS" agency="FRB">
      <structure:Name>Observation Status</structure:Name>
      <structure:Code value="A">
        <structure:Description>Normal</structure:Description>
      </structure:Code>
      <structure:Code value="NA">
        <structure:Description>Not available</structure:Description>
      </structure:Code>
    </structure:CodeList>
  </message:CodeLists>
</message:Structure>
```

### `SdmxSeries`

[`sdmx_series.txt`](sdmx_series.txt), recorded in prism-clients' `tests/federal_reserve/cassettes/test_recorded/test_file[dsr].yaml` interaction 0. Read by [`SdmxSeries`](../../src/flatread/sdmx.py), in federal_reserve `_DdpSeries`.

the zip's `DSR_data.xml` cut to one series: attributes and Short/Long Description annotations, two observations, closing tags kept

```
# members: DSR_data.xml, DSR_DSR.xsd, DSR_struct.xml, frb_common.xsd
<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<message:MessageGroup xmlns:message="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message" xmlns:common="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/common" xmlns:frb="http://www.federalreserve.gov/structure/compact/common" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message SDMXMessage.xsd http://www.federalreserve.gov/structure/compact/common frb_common.xsd">
  <frb:DataSet id="DSR" xmlns:kf="http://www.federalreserve.gov/structure/compact/DSR_DSR" xsi:schemaLocation="http://www.federalreserve.gov/structure/compact/DSR_DSR DSR_DSR.xsd">
    <kf:Series CURRENCY="NA" DEBT="TOTAL" FREQ="162" SERIES_NAME="DSR_RATIO" TYPE="DSR" UNIT="Percent" UNIT_MULT="1">
      <frb:Annotations>
        <common:Annotation>
          <common:AnnotationType>Short Description</common:AnnotationType>
          <common:AnnotationText>Debt service ratio, seasonally adjusted</common:AnnotationText>
        </common:Annotation>
        <common:Annotation>
          <common:AnnotationType>Long Description</common:AnnotationType>
          <common:AnnotationText>Debt service ratio, seasonally adjusted</common:AnnotationText>
        </common:Annotation>
      </frb:Annotations>
      <frb:Obs OBS_STATUS="A" OBS_VALUE="14.799802" TIME_PERIOD="2005-03-31" />
      <frb:Obs OBS_STATUS="A" OBS_VALUE="14.828576" TIME_PERIOD="2005-06-30" />
    </kf:Series>
  </frb:DataSet>
</message:MessageGroup>
```

### `SdmxObservations`

[`sdmx_observations.txt`](sdmx_observations.txt), recorded in prism-clients' `tests/federal_reserve/cassettes/test_recorded/test_file[g20].yaml` interaction 0. Read by [`SdmxObservations`](../../src/flatread/sdmx.py), in federal_reserve `_DdpObservations`.

the zip's `G20_data.xml` cut to one TERMS series: five `frb:Obs`, three `ND` with the `-9999` sentinel, closing tags kept

```
# members: G20_HIST.xsd, G20_OWNED.xsd, G20_TERMS.xsd, G20_data.xml, G20_struct.xml, frb_common.xsd
<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<message:MessageGroup xmlns:message="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message" xmlns:common="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/common" xmlns:frb="http://www.federalreserve.gov/structure/compact/common" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message SDMXMessage.xsd http://www.federalreserve.gov/structure/compact/common frb_common.xsd">
  <frb:DataSet id="TERMS" xmlns:kf="http://www.federalreserve.gov/structure/compact/G20_TERMS" xsi:schemaLocation="http://www.federalreserve.gov/structure/compact/G20_TERMS G20_TERMS.xsd">
    <kf:Series CATEGORY="NEWCAR" CURRENCY="USD" FREQ="129" LOANTERM="AMTFIN" SERIES_NAME="DTCTLVENM_N.M" UNIT="Months" UNIT_MULT="1">
      <frb:Annotations>
        <common:Annotation>
          <common:AnnotationType>Short Description</common:AnnotationType>
          <common:AnnotationText>Average maturity of new car loans at finance companies, amount of finance weighted, not seasonally adjusted</common:AnnotationText>
        </common:Annotation>
        <common:Annotation>
          <common:AnnotationType>Long Description</common:AnnotationType>
          <common:AnnotationText>Average maturity of new car loans at finance companies, amount of finance weighted, not seasonally adjusted</common:AnnotationText>
        </common:Annotation>
      </frb:Annotations>
      <frb:Obs OBS_STATUS="A" OBS_VALUE="62.01" TIME_PERIOD="2008-03-31" />
      <frb:Obs OBS_STATUS="ND" OBS_VALUE="-9999" TIME_PERIOD="2008-04-30" />
      <frb:Obs OBS_STATUS="ND" OBS_VALUE="-9999" TIME_PERIOD="2008-05-31" />
      <frb:Obs OBS_STATUS="A" OBS_VALUE="61.63" TIME_PERIOD="2008-06-30" />
      <frb:Obs OBS_STATUS="ND" OBS_VALUE="-9999" TIME_PERIOD="2008-07-31" />
    </kf:Series>
  </frb:DataSet>
</message:MessageGroup>
```

## Archives and long tables

### `ZippedFile`

[`zipped_file.txt`](zipped_file.txt), recorded in prism-clients' `tests/congress/cassettes/test_recorded/test_gift_travel_reports[2018].yaml` interaction 0. Read by [`ZippedFile`](../../src/flatread/archives.py), in congress `_HouseZip`.

a zip of `2018Travel.txt` and `2018Travel.xml`; only the member matching `[^/]*\.xml` is read (its first record shown)

Its twins: [`ZippedFiles`](#zippedfiles) reads every member the regex matches, in name order, as one table; [`ZippedOrBare`](#zippedorbare) reads a body with no matching member whole.

```
# members: 2018Travel.txt, 2018Travel.xml
<?xml version="1.0" encoding="utf-8"?>
<GiftTravel>
  <Travel>
    <DocID>500019230</DocID>
    <FilerName>Lindley Kratovil Sherer</FilerName>
    <MemberName>Stefanik, Elise M.</MemberName>
    <State>NY</State>
    <District>21</District>
    <Year>2018</Year>
    <Destination>Cambridge, MD</Destination>
    <FilingType>Amendment</FilingType>
    <DepartureDate>3/8/2018</DepartureDate>
    <ReturnDate>3/9/2018</ReturnDate>
    <TravelSponsor>Congressional Institute</TravelSponsor>
  </Travel>
  ...
</GiftTravel>
```

### `ZippedFile._members`

[`zipped_file-members.txt`](zipped_file-members.txt), recorded in prism-clients' `tests/cftc/cassettes/test_recorded/test_year_file[get_supplemental_cit].yaml` interaction 0. Read by [`ZippedFile._members`](../../src/flatread/archives.py), in cftc `_YearFile`.

a zip of exactly one member whose name changes by year (`annualci2006.txt`, `Annual.TXT`); `_member` is `.*`, so `_members = 1` is the only thing pinning it

```
# members: annualci2006.txt
"Market_and_Exchange_Names","As_of_Date_In_Form_YYMMDD","As_of_Date_In_Form_MM/DD/YYYY","CFTC_Contract_Market_Code","CFTC_Market_Code","CFTC_Region_Code","CFTC_Commodity_Code","Open_Interest_All"
"WHEAT - CHICAGO BOARD OF TRADE" ,061226 ,2006-12-26,"001602" ,"CBT" ,00 ,"001" ,  509375
"WHEAT - CHICAGO BOARD OF TRADE" ,061219 ,2006-12-19,"001602" ,"CBT" ,00 ,"001" ,  488929
```

### `ZippedFiles`

[`zipped_files.txt`](zipped_files.txt), recorded in prism-clients' `tests/sec/cassettes/test_recorded/test_body[fails_deliver_2008q4].yaml` interaction 0. Read by [`ZippedFiles`](../../src/flatread/archives.py), in sec `_FailsQuarter`.

three monthly members, each with its own header, all matching `cnsp_sec_fails_\d{6}\.txt`, read in name order (stored 11, 10, 12) as one table

Its twins: [`ZippedFile`](#zippedfile) reads one member, picked by name; [`ZippedOrBare`](#zippedorbare) reads one member or, with none matching, the body whole.

```
# members: cnsp_sec_fails_200811.txt, cnsp_sec_fails_200810.txt, cnsp_sec_fails_200812.txt
# member: cnsp_sec_fails_200810.txt
SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE
20081001|D18190898|DB|10721|DEUTSCHE BANK AG NAMEN AKT|72.79
# member: cnsp_sec_fails_200811.txt
SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE
20081103|D18190898|DB|46596|DEUTSCHE BANK AG NAMEN AKT|37.98
# member: cnsp_sec_fails_200812.txt
SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE
20081201|G04074103|SIL|20928|APEX SILVER MINES LTD(ORD)|0.81
```

### `ZippedOrBare`

[`zipped_or_bare.txt`](zipped_or_bare.txt), recorded in prism-clients' `tests/sec/cassettes/test_recorded/test_body[file_municipal_advisors_2026_11].yaml` interaction 0 (and `..._2021_03].yaml` interaction 0). Read by [`ZippedOrBare`](../../src/flatread/archives.py), in sec `_Published` (`_WorkbookExport`).

the same file bare (an xlsx, a zip of parts none matching `.+\.(csv\ | xlsx?)`, read whole) and zipped (`ma 030121.xlsx` its one member)

Its twins: [`ZippedFile`](#zippedfile) refuses a body with no matching member; [`ZippedFiles`](#zippedfiles) reads every matching member as one table.

```
# members: [Content_Types].xml, _rels/.rels, xl/workbook.xml, xl/_rels/workbook.xml.rels, xl/worksheets/sheet1.xml, xl/worksheets/sheet2.xml, xl/theme/theme1.xml, xl/styles.xml, xl/sharedStrings.xml, docMetadata/LabelInfo.xml, docProps/core.xml, docProps/app.xml
# sheet: Current Municipal Advisor Infor
# columns: A | F | J
1: Current Municipal Advisor Information Report |  |
2: Report as 11/01/2026 |  |
3: Registrant Name | File Number | CIK
4: &PARTNERS | 867-01682 | 0000107136
5: 22Beacon, Inc. | 867-02770 | 0002069882

# members: ma 030121.xlsx
# sheet: Current Municipal Advisor Infor
# columns: A | B | C
1: Current Municipal Advisor Information Report |  |
2: Report as of: 3/1/2021 |  |
3: Registrant Name | File Number | CIK
4: 30 Three Sixty Public Finance, Inc. | 867-02350 | 0001733578
5: A. M. Peche & Associates LLC | 867-00111 | 0001613201
```

### `StreamedZip`

[`streamed_zip.txt`](streamed_zip.txt), recorded in prism-clients' `tests/finra/cassettes/test_recorded/test_month[FNQC-201904].yaml` interaction 0. Read by [`StreamedZip`](../../src/flatread/archives.py), in finra `_MonthlyZip`; sec's `_ByExchange` sets `_nested`.

one 2 MB member written with a data descriptor, read forward as the body arrives: its text yielded in steps, then its CRC-32 and size checked against the descriptor; `_alone` makes a second member raise

Its twin: [`ZippedFile`](#zippedfile) opens a whole body by its central directory.

```
FINRA's monthly short-sale zip for the TRF Chicago, April 2019 (FNQCsh201904.zip, 494,707 bytes), as StreamedZip
reads it forward: each record's bytes in hex, then what they hold. The deflated data is shown as the text it inflates to.

offset 0, the local file header (30 bytes, then the name):
  50 4b 03 04              signature PK\3\4
  14 00                    version needed: 2.0
  08 08                    flags 0x0808: bit 3, CRC-32 and sizes in a data descriptor; bit 11, a UTF-8 name
  08 00                    method 8, deflated
  57 18 a7 4e              time and date written
  00 00 00 00              CRC-32 0, stated in the descriptor
  00 00 00 00              deflated size 0, stated in the descriptor
  00 00 00 00              text size 0, stated in the descriptor
  10 00                    name length 16
  00 00                    extra field length 0
  FNQCsh201904.txt         the name

offset 46, the deflated data (494,561 bytes), inflating to 2,061,704 bytes of text:
  MarketCenter|Symbol|Date|Time|ShortType|Size|Price|LinkIndicator|
  B|BA|20190403|09:30:26|S|20|389.770000||
  B|TSLA|20190410|09:30:48|S|100|278.000000||
  ...
  48745                    the record count, then a line end

offset 494,607, the data descriptor (16 bytes):
  50 4b 07 08              signature PK\7\8 (optional)
  de f5 2b e7              CRC-32 of the text, checked once it ends
  e1 8b 07 00              deflated size 494,561
  88 75 1f 00              text size 2,061,704, checked once it ends

offset 494,623, the central directory (84 bytes: one header, then the end record), opening:
  50 4b 01 02              signature PK\1\2: the member's end, and (FINRA's: alone) the zip's last member
```

### `LongFile`

[`long_file.csv`](long_file.csv), recorded in prism-clients' `tests/cboe/cassettes/test_recorded/test_body[settlement_window].yaml` interaction 0. Read by [`LongFile`](../../src/flatread/layout.py), in cboe `_SettlementWindowCsv`.

`date`, then a column per contract (`VX1`, `VX2`): each cell becomes a row (date, contract, volume)

```
date,VX1,VX2
2024-10-14,1644,1976
2024-10-15,1859,4553
2024-10-16,3561,780
2024-10-17,455,512
2024-10-18,4164,1323
```

### `LongFile._empty_rows`

[`long_file-emptyrows.csv`](long_file-emptyrows.csv), recorded in prism-clients' `tests/cboe/cassettes/test_recorded/test_body[settlement_window].yaml` interaction 0. Read by [`LongFile._empty_rows`](../../src/flatread/layout.py), in cboe `_SettlementWindowCsv`.

`2025-01-09,,` (the national day of mourning): empty cells that are no rows, not rows with no volume

```
date,VX1,VX2
2025-01-07,1531,3414
2025-01-08,4757,2193
2025-01-09,,
2025-01-10,3415,1542
2025-01-13,1374,2002
```

### `LongFile._spacers`

[`long_file-spacers.txt`](long_file-spacers.txt), recorded in prism-clients' `tests/eia/cassettes/test_recorded/test_file[ngs-ngshistory.xls].yaml` interaction 1. Read by [`LongFile._spacers`](../../src/flatread/layout.py), in eia `_Dated` (`_NetChanges`, sheet `weekly_net_changes`).

header row 7 leaves column B unlabelled and B holds nothing: a spacer, not a key

```
# sheet: weekly_net_changes
# columns: A | B | C | D | E | F | G | H | I | J
6: Weekly Net Changes (Bcf) AS OF NOVEMBER 5, 2026 |  |  |  |  |  |  |  |  |
7:  |  | East Region | Midwest Region | Mountain Region | Pacific Region | South Central Region | Salt | NonSalt | Total Lower 48
8: 2010-01-01 00:00:00 |  |  |  |  |  |  |  |  |
9: 2010-01-08 00:00:00 |  | -66 | -80 | -10 | -11 | -99 | -36 | -63 | -267
10: 2010-01-15 00:00:00 |  | -61 | -70 | -9 | -11 | -93 | -32 | -61 | -243
11: 2010-01-22 00:00:00 |  | -26 | -40 | -5 | -11 | -4 | 11 | -15 | -86
12: 2010-01-29 00:00:00 |  | -34 | -49 | -7 | -14 | -10 | 6 | -16 | -115
```

### `LongFile._scattered`

[`long_file-scattered.txt`](long_file-scattered.txt), recorded in prism-clients' `tests/eia/cassettes/test_recorded/test_file[ngs-revisions.xls].yaml` interaction 1. Read by [`LongFile._scattered`](../../src/flatread/layout.py), in eia `_Revisions`.

the record's `Explanation` column stands after the region keys, found by its label

```
# sheet: original_data
# columns: A | B | C | D | E | F | G | H | I | J | K
2: Week ending | Source | East Region | Midwest Region | Mountain Region | Pacific Region | South Central Region | Salt | NonSalt | Total Lower 48 | Explanation
3: 2015-06-19 00:00:00 | EIA-912 | 524 | 517 | 150 | 333 | 982 | 295 | 686 | 2506 |
4: 2015-06-26 00:00:00 | EIA-912 | 552 | 546 | 155 | 333 | 993 | 292 | 700 | 2579 |
7: 2015-07-17 00:00:00 | EIA-912 | 627 | 636 | 167 | 337 | 1056 | 304 | 752 | 2823 | Reclassifications from working gas to base gas resulted in decreased working gas stocks of approximately 7 Bcf in the East region for the week ending July 17, 2015. The implied flow for the week is an increase of 68 Bcf to working gas stocks. (See Notes and Definitions for more information on "implied flow.")
8: 2015-07-24 00:00:00 | EIA-912 | 643 | 659 | 171 | 340 | 1059 | 300 | 759 | 2872 |
```

### `LongFile._beside`

[`long_file-beside.txt`](long_file-beside.txt), recorded in prism-clients' `tests/atlanta_fed/cassettes/test_recorded/test_file[get_gdpnow_gdp_tracking_model_data_and_forecasts].yaml` interaction 0. Read by [`LongFile._beside`](../../src/flatread/layout.py), in atlanta_fed `_ArchiveSheet` (`TrackingArchives`).

`Publication Date of Advance Estimate`, `Days until advance estimate`, `Data releases` are record fields standing among the component keys (`Forecast Error` between them)

```
# sheet: TrackingArchives
# columns: A | B | C | AB | AF | AG | AH | AI | AJ
1: Forecast Date | Quarter being forecasted | PCE | GDP Nowcast | Advance Estimate From BEA | Publication Date of Advance Estimate | Days until advance estimate | Forecast Error | Data releases
2: 2014-05-01 00:00:00 | 2014-06-30 00:00:00 | 4.5371 | 3.8686 | 3.950188 | 2014-07-30 00:00:00 | 90 | 0.081588 | Initial nowcast
3: 2014-05-02 00:00:00 | 2014-06-30 00:00:00 | 4.5536 | 4.1857 | 3.950188 | 2014-07-30 00:00:00 | 89 | -0.235512 | M3 Manufacturing, Employment situation
4: 2014-05-06 00:00:00 | 2014-06-30 00:00:00 | 4.4035 | 4.049973102 | 3.950188 | 2014-07-30 00:00:00 | 85 | -0.099785102 | International trade
```

### `LongFile._key_count`

[`long_file-keycount.txt`](long_file-keycount.txt), recorded in prism-clients' `tests/federal_reserve/cassettes/test_recorded/test_file[feds_note_portfolio_holdings].yaml` interaction 0. Read by [`LongFile._key_count`](../../src/flatread/layout.py), in federal_reserve `_PortfolioHoldings` (same layout: treasury `_FedsNote`).

one header cell names two keys, measure and year (`market_value_residence_2003`)

```
# members: common_stock_data_table.csv, corporate_bonds_data_table.csv, fund_shares_data_table.csv, gov_bonds_data_table.csv, Nationality readme(2025).txt, preferred_and_other_equity_data_table.csv, total_bonds_data_table.csv, total_equity_and_bonds_data_table.csv, total_equity_data_table.csv
# member: gov_bonds_data_table.csv
cntry_code,cntry_name,market_value_residence_2003,market_value_nationality_2003,mv_difference_nat_vs_res_2003,market_value_residence_2004,market_value_nationality_2004,mv_difference_nat_vs_res_2004,...,market_value_residence_2024,market_value_nationality_2024,mv_difference_nat_vs_res_2024
10189,AUSTRIA,2070,2070,0,2029,2029,0,...,5018,5018,0
10251,BELGIUM,2866,2866,0,2566,2566,0,...,6485,6485,0
```

### `Reshaped`

[`reshaped.csv`](reshaped.csv), recorded in prism-clients' `tests/chicago_fed/cassettes/test_recorded/test_file[get_carts_recent_monthly_values].yaml`, interaction 0. Read by [`Reshaped`](../../src/flatread/core.py), in chicago_fed `_CartsRecentValues`.

An inner reader reads it long (a row per series and month heading); `_shape` names each heading's month, the one before the one left of it. Unlike [`LongFile`](#longfile), which reads a wide table long itself, it reshapes a frame another reader made.

```
,Oct `26,Sep,Aug,Jul,Jun,May
Retail & food services sales ex. auto,+0.8*,+0.9,+1.4,-0.1,-0.3,+1.0
Inflation-adjusted,+0.6*,-0.3*,+0.9*,-0.1*,+0.7*,+0.2*
```

### `File._repeats`

[`file-repeats.csv`](file-repeats.csv), recorded in prism-clients' `tests/eia/cassettes/test_recorded/test_file[wpsr-table1.csv].yaml` interaction 1. Read by [`File._repeats`](../../src/flatread/core.py), in eia `_Table`.

`Difference` and `Percent Change` each head two columns (one per compared week), so a column is keyed by its place too

```
"STUB_1","10/30/26","10/23/26","Difference","Percent Change","10/31/25","Difference","Percent Change"
"Crude Oil","691.944","693.208","-1.264","-0.200","830.763","-138.819","-16.700"
"Commercial (Excluding SPR)","409.760","410.625","-0.865","-0.200","421.168","-11.408","-2.700"
"Strategic Petroleum Reserve (SPR)","282.184","282.583","-0.399","-0.100","409.595","-127.411","-31.100"
```

### `File._numbered`

[`file-numbered.txt`](file-numbered.txt), recorded in prism-clients' `tests/atlanta_fed/cassettes/test_recorded/test_file[get_gdpnow_gdp_tracking_model_data_and_forecasts].yaml` interaction 0. Read by [`File._numbered`](../../src/flatread/core.py), in atlanta_fed `_NumberedSheet` (`MonthlyPriceLevels`).

rows 33 and 34 both write ticker `SA_HN1PA_USECON_fr` with different values; only the sheet row tells them apart

```
# sheet: MonthlyPriceLevels
# columns: A | B | C | GL | GM | AEJ | AEK | AEL | AJA
1: Ticker | 1959-01-31 00:00:00 | 1959-02-28 00:00:00 | 1975-01-31 00:00:00 | 1975-02-28 00:00:00 | 2026-11-30 00:00:00 | 2026-12-31 00:00:00 | 2027-01-31 00:00:00 | 2036-12-31 00:00:00
2: MGDPN_USECONsplicefr | 490.98 | 494.84 | 1559.49 | 1580.75 | 33335.03757335 | 33460.278644492 | 33585.990251353 | 52476.162728054
3: PCU_USECONfr | 29.01 | 29 | 52.3 | 52.6 | 338.446747416 | 339.228378155 | 340.035316053 | 450.087066956
32: CPIMajappSplicefr | 61.896184938 | 61.988120167 | 67.979760902 | 68.476686661 | 87.432150994 | 87.559473798 | 87.660151323 | 84.974506188
33: SA_HN1PA_USECON_fr |  |  | 39922.342017933 | 40533.961609559 | 480810.597795178 | 480864.107691974 | 482629.762156586 | 702709.299413183
34: SA_HN1PA_USECON_fr |  |  | 39922.342017933 | 40533.961609559 | 631144.670119709 | 633518.784347226 |  |
```

## No recorded file needs these

Only the synthetic bodies in the tests reach these, so they have no excerpt:
- `SpilledCsv` (sec `_FailsMonth`): the 2004 Q3 to 2005 Q1 files whose descriptions hold a bar are not recorded.
- `SheetFile._trim` (eia `_Dated`, `_DateText`, `_Lines`): no recorded workbook has a cell of only spaces.
- `TextFile._as_written` (sec `_EdgarIndex`, `_FoiaLog`, `_Roster`): every recorded index, log and roster reads the
  same with it off.
- `ReportFile._skip_blank = False` (sec `_EdgarIndex`): no recorded index has a blank line below its rule.
- `ReportFile._text_lines` (sec `_EdgarIndex`): no recorded index is latin-1 or holds a form feed or vertical tab.
- `LongFile._bare_header_refused` (eia `_Dated`, `_Table`): it refuses a cut file, which no recording is.

`GridFile` has no reader of its own; `sheet_text.txt` and `csv_text.csv` show the grids its two sources cut.
