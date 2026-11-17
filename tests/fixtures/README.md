# Real files

Whole response bodies that `tests/test_real_files.py` reads, each exactly as its publisher served it. They catch what
a file built in a test does not have: Excel's shared strings and printer settings, a BIFF3 workbook's error cells, a
producer's font tables, a publisher's spelling. Every one is a US government publication.

| File | Publisher | URL | Recorded |
|---|---|---|---|
| `eia_ngsstats.xls` | U.S. Energy Information Administration | https://ir.eia.gov/ngs/ngsstats.xls | 2026-11-10 |
| `eia_wpsr_errata.xlsx` | U.S. Energy Information Administration | https://www.eia.gov/petroleum/supply/weekly/WPSR_ErrataSheet.xlsx | 2026-11-10 |
| `house_gift_travel_2018.zip` | Clerk of the U.S. House of Representatives | https://disclosures-clerk.house.gov/public_disc/gift-pdfs/2018Travel.zip | 2026-11-10 |
| `house_ptr_20035492.pdf` | Clerk of the U.S. House of Representatives | https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035492.pdf | 2026-11-10 |
| `philadelphia_fed_spf_error_statistics_cpi.xls` | Federal Reserve Bank of Philadelphia | https://www.philadelphiafed.org/-/media/FRBP/Assets/Surveys-And-Data/survey-of-professional-forecasters/data-files/CPI/Data_SPF_Error_Statistics_CPI_1_AIC.xls | 2026-11-10 |

"Recorded" is the date the file was fetched. The test pins each file's SHA-256, so a newer copy goes in as a new file
with its own row here, not as an edit.
