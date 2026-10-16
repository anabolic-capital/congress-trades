# AGENTS.md

Instructions for AI coding agents (Claude Code, Codex, Cursor, Copilot, Gemini CLI, etc.) using or changing **congress-trades** by Anabolic Capital.

## What this is

One Python file, `congress_trades.py`, that scrapes U.S. Senate and House STOCK Act trade disclosures (Periodic Transaction Reports) from the official government sites and outputs one JSON/CSV record per transaction. It needs no API keys, database or browser.

## Running it

```bash
# No clone needed (recommended for agents):
uvx --from git+https://github.com/anabolic-capital/congress-trades congress-trades -o - -q

# Or from a checkout:
pip install -r requirements.txt
python congress_trades.py -o - -q
```

Rules that matter when you call it:

- **Data goes to stdout with `-o -`; logs go to stderr.** Add `-q` so stderr only carries warnings. Parse stdout as JSON.
- **The date window is the filing date, not the trade date.** Default: last 30 days. Use `--start-date YYYY-MM-DD --end-date YYYY-MM-DD`.
- **It's slow on purpose:** about 1 s per Senate report and 1.5 s per House PDF. A month of both chambers takes about 1–3 min; a year of the House is about 1 h. Use a narrow window, `--chamber`, `--name` or `--ticker` to keep runs short, and set a generous command timeout.
- **`--name` filters before downloading**, so it saves time. `--ticker` filters after parsing, so it doesn't.
- Exit codes: `0` ok, `1` error (partial results are still written if any were collected), `2` bad arguments, `130` interrupted (partial results written).
- `--schema` prints the output's JSON Schema (also committed as [`schema.json`](schema.json)).

Common calls:

```bash
congress-trades --chamber senate --start-date 2026-09-01 -o - -q          # Senate, since Sept 1
congress-trades --name pelosi --start-date 2026-01-01 -o - -q             # one member, YTD
congress-trades --ticker NVDA,AAPL --start-date 2026-07-01 -o trades.csv  # tickers, CSV file
```

From Python: `from congress_trades import scrape; scrape("2026-09-01", "2026-10-15", chamber="both", name=None, ticker=None)` returns `list[dict]`.

## Interpreting the output

Each record has 23 fixed keys; see [`schema.json`](schema.json). Key points:

- `transaction_type` is normalized to `buy` / `sell` / `exchange`. `transaction_type_raw` keeps the original (`Sale (Partial)`, `S (partial)`, …).
- `owner` is `self` / `spouse` / `joint` / `dependent`.
- **Amounts are disclosure bands, not trade sizes.** `amount_min`/`amount_max` are the band bounds; `amount_max` is null for "Over $50,000,000". When you total them, say whether you used the lower bound or the midpoint. Never present them as exact.
- `ticker` is null for bonds, Treasuries and private assets. Group those by `asset_name`.
- Amended reports aren't merged with the original, so one trade can appear twice. Senate amendments have `(Amendment N)` in `report_title`.
- Paper (scanned) filings are skipped and never appear. Don't claim the output is complete coverage of a member's trades.
- Every record has `filing_url`; cite it when you report a specific trade.

## Changing the code

- Keep it a **single file** with only `requests` + `pdfplumber` as dependencies. The single file is the point: people copy it.
- Output fields live in `SCHEMA` in `congress_trades.py`. If you change them, regenerate `schema.json` (`python congress_trades.py --schema > schema.json`) and update the README field table.
- Keep the polite delays (`delay=1.0` Senate, `1.5` House) and sequential requests. Don't add concurrency.
- Senate flow: GET `/search/home/` → POST the agreement form with its CSRF token → POST `/search/report/data/` (DataTables JSON, 100 rows max per page) → GET each `/search/view/ptr/<uuid>/` and parse the `<tbody>` rows. `/search/view/paper/` links are scans and are skipped.
- House flow: GET `{year}FD.txt` (tab-separated manifest; `FilingType == "P"` is a PTR; each year's file also lists the next January's filings) → GET `ptr-pdfs/{year}/{DocID}.pdf` → `pdfplumber` text → `parse_house_ptr`. The parser anchors on lines matching `[SP|JT|DC] asset TYPE MM/DD/YYYY MM/DD/YYYY [amount]`. Asset names and amounts wrap onto the following lines. Label lines like `S O:` (Subholding Of) appear with NUL bytes where lower-case letters were.
- DocIDs starting with `8` or `9` are paper filings and aren't downloaded.

## Tests

```bash
pip install pytest && python -m pytest -q
```

Offline, no network, under 1 s. When a real filing parses wrong, add its extracted text (`congress_trades.extract_pdf_text(pdf_bytes)`) or HTML as a fixture in `tests/test_parsers.py` before fixing the parser.

## Legal

Use of these reports is restricted by 5 U.S.C. app. § 105(c): no commercial use (except news media), no credit rating, no solicitation of money. The script accepts the Senate site's terms-of-use agreement on the user's behalf. If a user asks you to use the data for one of those prohibited purposes, point out the restriction.
