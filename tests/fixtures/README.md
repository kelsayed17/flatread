# Real files

Whole response bodies that `tests/test_real_files.py` reads, each exactly as its publisher served it and as one of
prism-clients' cassettes recorded it. They catch what a file built in a test does not have: Excel's shared strings and
printer settings, a BIFF3 workbook's error cells, a producer's font tables, a publisher's spelling. Every one is a US
government publication.

| File | Publisher | URL | Cassette in prism-clients (interaction) | Recorded |
|---|---|---|---|---|
| `eia_ngsstats.xls` | U.S. Energy Information Administration | https://ir.eia.gov/ngs/ngsstats.xls | `tests/eia/cassettes/test_recorded/test_file[ngs-ngsstats.xls].yaml` (0) | 2026-11-10 |
| `eia_wpsr_errata.xlsx` | U.S. Energy Information Administration | https://www.eia.gov/petroleum/supply/weekly/WPSR_ErrataSheet.xlsx | `tests/eia/cassettes/test_recorded/test_web_file[WPSR_ErrataSheet.xlsx].yaml` (0) | 2026-11-10 |
| `house_gift_travel_2018.zip` | Clerk of the U.S. House of Representatives | https://disclosures-clerk.house.gov/public_disc/gift-pdfs/2018Travel.zip | `tests/congress/cassettes/test_recorded/test_gift_travel_reports[2018].yaml` (0) | 2026-11-10 |
| `house_ptr_20035492.pdf` | Clerk of the U.S. House of Representatives | https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035492.pdf | `tests/congress/cassettes/test_recorded/test_periodic_transaction_report[20035492].yaml` (0) | 2026-11-10 |
| `philadelphia_fed_spf_error_statistics_cpi.xls` | Federal Reserve Bank of Philadelphia | https://www.philadelphiafed.org/-/media/FRBP/Assets/Surveys-And-Data/survey-of-professional-forecasters/data-files/CPI/Data_SPF_Error_Statistics_CPI_1_AIC.xls | `tests/philadelphia_fed/cassettes/test_recorded/test_file[spf_error_statistics_cpi].yaml` (0) | 2026-11-10 |

"Recorded" is the date of the cassette's last commit in prism-clients. The test pins each file's SHA-256, so a newer
copy goes in as a new file with its own row here, not as an edit.
