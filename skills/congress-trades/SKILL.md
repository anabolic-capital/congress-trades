---
name: congress-trades
description: Look up stock trades disclosed by U.S. senators and representatives under the STOCK Act (Periodic Transaction Reports). Use when the user asks what members of Congress bought or sold, who traded a ticker, a specific member's recent trades, or wants congressional trading data as JSON/CSV.
---

# congress-trades

Fetches live data from the official Senate (efdsearch.senate.gov) and House (disclosures-clerk.house.gov) disclosure sites, using the `congress-trades` CLI by Anabolic Capital: https://github.com/anabolic-capital/congress-trades

## Run

Use `uvx`, which needs no install. Use `-o -` for JSON on stdout and `-q` for quiet stderr:

```bash
uvx --from git+https://github.com/anabolic-capital/congress-trades congress-trades [flags] -o - -q
```

If `uvx` isn't available: `pip install git+https://github.com/anabolic-capital/congress-trades`, then run `congress-trades` the same way.

| Flag | Meaning |
|---|---|
| `--chamber senate\|house\|both` | Default `both`. |
| `--start-date YYYY-MM-DD` / `--end-date YYYY-MM-DD` | **Filing** date window. Default: last 30 days. |
| `--name TEXT` | Member name substring, e.g. `pelosi`. Filters before downloading, so it's faster. |
| `--ticker A,B` | Only these tickers. |
| `-o file.csv` | Write CSV instead. |

It's polite and slow: about 1 s per Senate report and 1.5 s per House PDF. Keep windows narrow (weeks, not years) and give the command a long timeout (10 min). Members have up to 45 days to file, so to catch trades made in a given month, extend `--end-date` about 45 days past it.

## Read the results

JSON array; one object per transaction. The important keys are `chamber`, `filer_name`, `state_district` (House), `transaction_date`, `filing_date`, `ticker`, `asset_name`, `transaction_type` (`buy`/`sell`/`exchange`), `owner` (`self`/`spouse`/`joint`/`dependent`), `amount_range`, `amount_min`, `amount_max` and `filing_url`. Run `--schema` for the full schema.

When you answer the user:

- **Amounts are ranges, not trade sizes.** Quote `amount_range`. If you total, say "at least $X" (sum of `amount_min`) or say you used the midpoint.
- Note who owned the asset: many trades are a spouse's or a dependent's.
- Link `filing_url` for specific trades.
- The data isn't complete coverage. Scanned paper filings are skipped, and an amended report can repeat a trade (Senate `report_title` contains `(Amendment N)`).
- Disclosures lag trades by up to 45 days.
- Federal law (5 U.S.C. app. § 105(c)) bars commercial use of these reports (except news media), using them for credit ratings, or using them to solicit money. Mention this if the user's purpose looks like one of those.
