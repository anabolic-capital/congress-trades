<div align="center">

# 🏛️ congress-trades

**Every stock trade Congress discloses, as clean JSON or CSV.**

Scrapes STOCK Act filings from the U.S. Senate and House straight from the official sites. One file, two dependencies, no API keys.

[![tests](https://github.com/anabolic-capital/congress-trades/actions/workflows/tests.yml/badge.svg)](https://github.com/anabolic-capital/congress-trades/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![agent ready](https://img.shields.io/badge/AI%20agents-ready-8A2BE2)

Built by **[Anabolic Capital](https://github.com/anabolic-capital)**

</div>

---

## Quick start

```bash
uvx --from git+https://github.com/anabolic-capital/congress-trades congress-trades
# → trades.json: every Senate + House trade filed in the last 30 days
```

No `uv`? Use pip instead:

```bash
pip install git+https://github.com/anabolic-capital/congress-trades
congress-trades
```

Or clone it and run the script directly. It's one file:

```bash
git clone https://github.com/anabolic-capital/congress-trades.git
cd congress-trades
pip install -r requirements.txt     # requests + pdfplumber
python congress_trades.py
```

Python 3.9 or newer. `requests` covers the Senate; `pdfplumber` reads the House PDFs.

### Where the data comes from

| Chamber | Source | Format |
|---|---|---|
| Senate | [efdsearch.senate.gov](https://efdsearch.senate.gov/search/) | HTML reports behind a terms-of-use page |
| House | [disclosures-clerk.house.gov](https://disclosures-clerk.house.gov/FinancialDisclosure) | One PDF per filing, listed in a yearly index |

---

## Usage

The examples use `congress-trades`. From a clone, `python congress_trades.py` works the same way.

```bash
# Both chambers, last 30 days → trades.json
congress-trades

# A specific window, as CSV
congress-trades --start-date 2026-01-01 --end-date 2026-03-31 -o q1.csv

# One chamber
congress-trades --chamber senate
congress-trades --chamber house --start-date 2026-01-01

# One member (case-insensitive substring of their name)
congress-trades --name pelosi --start-date 2025-01-01 -o pelosi.csv

# Only certain tickers
congress-trades --ticker NVDA,AAPL,MSFT

# Pipe JSON to another tool; progress logs go to stderr
congress-trades -o - -q | jq '.[] | select(.transaction_type == "buy")'
```

| Flag | Default | |
|---|---|---|
| `--chamber {both,senate,house}` | `both` | Which chamber to scrape. |
| `--start-date YYYY-MM-DD` | 30 days before end date | First **filing** date, inclusive. |
| `--end-date YYYY-MM-DD` | today | Last **filing** date, inclusive. |
| `--name TEXT` | all | Only filers whose name contains `TEXT`. |
| `--ticker LIST` | all | Only these tickers, comma-separated. |
| `-o, --output FILE` | `trades.json` | `.csv` writes CSV; `-` writes to stdout. |
| `--format {json,csv}` | from extension | Force the output format. |
| `-q` / `-v` | | Quieter / debug logging. |
| `--schema` | | Print the output's JSON Schema and exit. |

> **Dates are filing dates.** The window filters on when the report was filed, not when the trade happened. Members have up to 45 days to report, so a trade from June can show up in an August search.

**How long it takes:** it waits 1 second between Senate reports and 1.5 seconds between House PDFs to be polite to government servers. A month of both chambers is typically 1–3 minutes; a full year of the House is roughly an hour. Press **Ctrl-C** any time and it writes what it has so far.

### From Python

```python
from congress_trades import scrape

trades = scrape("2026-09-01", "2026-10-15", chamber="both", name=None, ticker=None)

import pandas as pd
df = pd.DataFrame(trades)
df[df.transaction_type == "buy"].groupby("ticker").amount_min.sum().nlargest(20)
```

You can also call `scrape_senate(start, end)` and `scrape_house(start, end)` with `datetime.date` arguments, or use the parsers on their own: `parse_senate_ptr(html)` and `parse_house_ptr(pdf_text)`.

---

## Use it with AI agents

congress-trades is built to be easy for AI agents to call:

- **One command, JSON on stdout:** `congress-trades -o - -q` writes data to stdout and logs to stderr, with fixed keys on every record.
- **Machine-readable schema:** `congress-trades --schema`, or [`schema.json`](schema.json).
- **Predictable exit codes:** `0` ok, `1` error, `2` bad arguments, `130` interrupted. Partial results are still written on error or Ctrl-C.
- **[`AGENTS.md`](AGENTS.md)**: instructions for coding agents (Codex, Cursor, Copilot, Gemini CLI…), covering how to call it, how to read the results, and how the scrapers work. [`CLAUDE.md`](CLAUDE.md) points Claude Code at the same file.
- **[`llms.txt`](llms.txt)**: a short index of the project for LLMs.
- **Claude skill:** copy [`skills/congress-trades/`](skills/congress-trades/SKILL.md) into `~/.claude/skills/`. Claude can then answer questions like *"what did senators buy last month?"* by running the tool itself.

```bash
mkdir -p ~/.claude/skills && cp -r skills/congress-trades ~/.claude/skills/
```

---

## Output

One record per transaction, with the same fields for both chambers. A field one chamber doesn't publish is `null`.

```json
{
  "chamber": "house",
  "filer_name": "Nancy Pelosi",
  "first_name": "Nancy",
  "last_name": "Pelosi",
  "state_district": "CA11",
  "filing_id": "20033725",
  "filing_url": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20033725.pdf",
  "filing_date": "2026-01-23",
  "report_title": null,
  "transaction_date": "2026-01-16",
  "notification_date": "2026-01-16",
  "owner": "spouse",
  "ticker": "GOOGL",
  "asset_name": "Alphabet Inc. - Class A Common Stock",
  "asset_type": "Stocks (including ADRs)",
  "asset_type_code": "ST",
  "transaction_type": "buy",
  "transaction_type_raw": "P",
  "amount_range": "$500,001 - $1,000,000",
  "amount_min": 500001.0,
  "amount_max": 1000000.0,
  "account": null,
  "comment": "Exercised 50 call options purchased 1/14/25 (5,000 shares) at a strike price of $150 with an expiration date of 1/16/26."
}
```

| Field | Notes |
|---|---|
| `chamber` | `senate` or `house`. |
| `filer_name`, `first_name`, `last_name` | As they appear in the official index. |
| `state_district` | House only, e.g. `CA11`. |
| `filing_id` | Senate report UUID or House DocID. |
| `filing_url` | Link to the original report, for checking a row against the source. |
| `filing_date` | `YYYY-MM-DD`. |
| `report_title` | Senate only. Amended reports say so here, e.g. `… (Amendment 1)`. |
| `transaction_date` | `YYYY-MM-DD`. |
| `notification_date` | House only: when the member was told about the trade. |
| `owner` | `self`, `spouse`, `joint` or `dependent`. |
| `ticker` | `null` for bonds, Treasuries, private funds and other assets without one. |
| `asset_name` | Asset description. Senate bond and option details are appended in parentheses. |
| `asset_type` | E.g. `Stock`, `Corporate Bond` (Senate); the Clerk's code name, e.g. `Stocks (including ADRs)` (House). |
| `asset_type_code` | House only: the [Clerk's code](https://fd.house.gov/reference/asset-type-codes.aspx), e.g. `ST`, `OP`, `GS`. |
| `transaction_type` | `buy`, `sell` or `exchange`. |
| `transaction_type_raw` | As filed: `Sale (Partial)`, `Purchase` (Senate); `P`, `S`, `S (partial)`, `E` (House). |
| `amount_range` | The disclosed band, e.g. `$1,001 - $15,000`. |
| `amount_min`, `amount_max` | Parsed bounds. `amount_max` is `null` for open-ended bands like `Over $50,000,000`. A few filers report an exact figure; then min = max. |
| `account` | House only: the account the asset is held in ("Subholding Of"). |
| `comment` | Senate comment column or House description line. |

**Amounts are ranges, not trade sizes.** Members only disclose a band ($1,001–$15,000, $15,001–$50,000, … up to "Over $50,000,000"). If you add them up, use the lower bound or the midpoint, and say which.

---

## What it doesn't cover

- **Paper filings.** Some reports are scanned images with no text, and those trades aren't in the output. The end-of-run summary says how many were skipped; run with `-v` to list their URLs so you can read them by hand.
- **Amendments aren't merged.** An amended report is a separate filing, so a corrected trade can appear twice: once from the original and once from the amendment. Senate amendments are labelled in `report_title`.
- **House parsing is heuristic.** House PDFs have no structured data, so the script reads their extracted text. It was checked line by line against 62 real 2025–2026 filings (718 transactions) with no misses, but an unusual layout can still trip it. Every row has a `filing_url`, so spot-check before you publish anything.
- **Site changes break scrapers.** If either chamber redesigns its site, this will need updating. Please open an issue.

## Terms of use and legal notice

These disclosures are public records, but **the law restricts how you can use them.** Under the Ethics in Government Act of 1978 (5 U.S.C. app. § 105(c)) it is unlawful to obtain or use a report:

- for any unlawful purpose;
- for any commercial purpose, other than by news and communications media for dissemination to the general public;
- for determining or establishing the credit rating of any individual; or
- for use, directly or indirectly, in the solicitation of money for any political, charitable or other purpose.

The Senate site makes you agree to these terms before you can search, and **this script clicks "I agree" for you.** By running it you accept those terms yourself. This isn't legal advice.

Please be considerate. Keep the built-in delays, don't run several copies at once, and don't re-scrape years of history on a schedule. These are public servers, not an API.

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q        # offline tests, no network
```

If you change the output fields in `SCHEMA`, regenerate the schema file with `python congress_trades.py --schema > schema.json`. A test checks that it's in sync.

The tests use text from real Senate HTML and House PDF filings. If you find a filing that parses wrong, add its text as a test case.

## License

[MIT](LICENSE) © Anabolic Capital. Not affiliated with the U.S. Congress. The disclosure data is a U.S. government public record, subject to the use restrictions above.
