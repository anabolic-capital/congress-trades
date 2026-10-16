#!/usr/bin/env python3
"""
congress_trades.py: scrape the stock trades members of Congress disclose under the STOCK Act.

Sources
  Senate  efdsearch.senate.gov             (electronic Periodic Transaction Reports, HTML)
  House   disclosures-clerk.house.gov      (Periodic Transaction Reports, PDF)

Every transaction becomes one flat record with the same fields for both chambers,
written to JSON or CSV.

    python congress_trades.py                                  # both chambers, last 30 days
    python congress_trades.py --chamber senate --start-date 2026-01-01 -o senate.csv
    python congress_trades.py --chamber house --name pelosi --ticker NVDA

Dates filter on the FILING date (when the report was submitted), not the trade date.

Requires: requests (both chambers) and pdfplumber (House only).
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta
from typing import Iterable

import requests

__version__ = "1.0.0"

log = logging.getLogger("congress_trades")

USER_AGENT = f"congress-trades/{__version__} (STOCK Act disclosure scraper; python-requests)"

# Output columns, in order. Fields that one chamber doesn't publish are null.
FIELDS = [
    "chamber",               # "senate" | "house"
    "filer_name",            # "First Last" as filed
    "first_name",
    "last_name",
    "state_district",        # House only, e.g. "CA12"
    "filing_id",             # Senate: report UUID; House: DocID
    "filing_url",
    "filing_date",           # YYYY-MM-DD
    "report_title",          # Senate only, e.g. "Periodic Transaction Report for 09/14/2026 (Amendment 1)"
    "transaction_date",      # YYYY-MM-DD
    "notification_date",     # House only: when the member was notified of the trade
    "owner",                 # self | spouse | joint | dependent | <raw lower-cased value>
    "ticker",
    "asset_name",
    "asset_type",            # e.g. "Stock", "Corporate Bond", "Stock Option"
    "asset_type_code",       # House only: the bracketed code, e.g. "ST"
    "transaction_type",      # buy | sell | exchange | <raw lower-cased value>
    "transaction_type_raw",  # as disclosed, e.g. "Sale (Partial)", "S (partial)"
    "amount_range",          # as disclosed, e.g. "$1,001 - $15,000"
    "amount_min",
    "amount_max",            # null for open-ended bands ("Over $50,000,000")
    "account",               # House only: the "Subholding Of" account
    "comment",               # Senate comment column / House description line
]

DEFAULT_LOOKBACK_DAYS = 30


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def _request(session: requests.Session, method: str, url: str, *, retries: int = 3, **kw) -> requests.Response:
    """HTTP request with a timeout and exponential backoff on network errors, 429 and 5xx."""
    kw.setdefault("timeout", 60)
    for attempt in range(1, retries + 1):
        try:
            resp = session.request(method, url, **kw)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            return resp
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            if attempt == retries:
                raise
            wait = 5 * 2 ** (attempt - 1)
            log.warning("%s %s failed (%s); retrying in %ds", method, url, e, wait)
            time.sleep(wait)
    raise AssertionError("unreachable")


def _clean(s: str | None) -> str:
    """Strip HTML tags, unescape entities and collapse whitespace."""
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _null(s: str | None) -> str | None:
    s = (s or "").strip()
    return None if s in ("", "--", "N/A", "n/a") else s


def _iso(s: str | None) -> str | None:
    """'9/5/2026' or '09/05/2026' -> '2026-09-05'; anything else -> None."""
    m = re.search(r"\d{1,2}/\d{1,2}/\d{4}", s or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(0), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


_NUM = r"([\d,]+(?:\.\d+)?)"
_AMOUNT_RE = re.compile(rf"(Over\s+)?\$\s*{_NUM}(?:\s*(-)\s*(?:\$\s*{_NUM})?|\s*(\+))?", re.I)


def parse_amount(text: str | None) -> tuple[str | None, float | None, float | None]:
    """'$15,001 - $50,000' -> ('$15,001 - $50,000', 15001.0, 50000.0).

    Open-ended bands ('Over $50,000,000', '$50,000,001 +') return max=None.
    A few filers report an exact figure ('$224.00'); that returns min == max.
    """
    text = re.sub(r"\s+", " ", text or "").strip()
    m = _AMOUNT_RE.search(text)
    if not m:
        return _null(text), None, None
    over, lo, dash, hi, plus = m.groups()
    lo_v = float(lo.replace(",", ""))
    if over:
        return m.group(0), lo_v + 1, None
    if dash or plus:
        return m.group(0), lo_v, float(hi.replace(",", "")) if hi else None
    return m.group(0), lo_v, lo_v


def normalize_type(raw: str | None) -> str | None:
    r = (raw or "").strip().lower()
    if not r:
        return None
    if r == "p" or "purchase" in r or "buy" in r:
        return "buy"
    if r.startswith("s (") or r == "s" or "sale" in r or "sell" in r:
        return "sell"
    if r == "e" or "exchange" in r:
        return "exchange"
    return r


_OWNER_MAP = {
    "self": "self", "": "self",
    "spouse": "spouse", "sp": "spouse",
    "joint": "joint", "jt": "joint",
    "child": "dependent", "dependent": "dependent", "dependent child": "dependent", "dc": "dependent",
}


def normalize_owner(raw: str | None) -> str | None:
    r = (raw or "").strip().lower()
    if r == "--":
        return None
    return _OWNER_MAP.get(r, r)


def _matches(name: str, needle: str | None) -> bool:
    return not needle or needle.lower() in name.lower()


def _record(**kw) -> dict:
    return {f: kw.get(f) for f in FIELDS}


# --------------------------------------------------------------------------- #
# Senate: efdsearch.senate.gov
# --------------------------------------------------------------------------- #

SENATE_BASE = "https://efdsearch.senate.gov"
SENATE_HOME = SENATE_BASE + "/search/home/"
SENATE_SEARCH = SENATE_BASE + "/search/"
SENATE_DATA = SENATE_BASE + "/search/report/data/"
SENATE_PAGE_SIZE = 100          # the server caps page length at 100
SENATE_REPORT_TYPE_PTR = 11
SENATE_FILER_TYPES = (1, 5)     # 1 = Senator, 5 = Former Senator


class SenateClient:
    """Plain-HTTP client for the Senate eFD search (no browser needed).

    The site gates search behind a terms-of-use agreement stored in the session.
    By using this client you are accepting that agreement yourself; see the README.
    """

    def __init__(self, session: requests.Session | None = None):
        self.s = session or _session()
        self._agreed = False

    def _csrf(self) -> str:
        return self.s.cookies.get("csrftoken", domain="efdsearch.senate.gov") or self.s.cookies.get("csrftoken") or ""

    def accept_agreement(self) -> None:
        resp = _request(self.s, "GET", SENATE_HOME)
        resp.raise_for_status()
        m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', resp.text)
        if not m:
            raise RuntimeError("Senate agreement form not found; the site layout may have changed")
        resp = _request(
            self.s, "POST", SENATE_HOME,
            data={"prohibition_agreement": "1", "csrfmiddlewaretoken": m.group(1)},
            headers={"Referer": SENATE_HOME},
        )
        resp.raise_for_status()
        if "agreement_form" in resp.text:
            raise RuntimeError("Senate agreement was not accepted; the site layout may have changed")
        self._agreed = True

    def _ensure_agreed(self) -> None:
        if not self._agreed:
            self.accept_agreement()

    def search_ptrs(self, start: date, end: date) -> list[dict]:
        """Return every PTR filing (electronic and paper) filed between start and end inclusive."""
        self._ensure_agreed()
        filings: dict[str, dict] = {}
        offset, total = 0, None
        while total is None or offset < total:
            payload = {
                "start": str(offset),
                "length": str(SENATE_PAGE_SIZE),
                "report_types": f"[{SENATE_REPORT_TYPE_PTR}]",
                "filer_types": "[" + ",".join(map(str, SENATE_FILER_TYPES)) + "]",
                "submitted_start_date": start.strftime("%m/%d/%Y 00:00:00"),
                "submitted_end_date": end.strftime("%m/%d/%Y 23:59:59"),
                "candidate_state": "", "senator_state": "", "office_id": "",
                "first_name": "", "last_name": "",
                # Sort by filing date so offsets stay stable while we page.
                "order[0][column]": "4", "order[0][dir]": "asc",
                "csrfmiddlewaretoken": self._csrf(),
            }
            resp = _request(self.s, "POST", SENATE_DATA, data=payload,
                            headers={"Referer": SENATE_SEARCH, "X-CSRFToken": self._csrf()})
            resp.raise_for_status()
            try:
                body = resp.json()
            except ValueError:
                raise RuntimeError("Senate search returned non-JSON (session expired or site changed)")
            total = int(body.get("recordsTotal") or 0)
            rows = body.get("data") or []
            log.debug("Senate search offset=%d got %d of %d", offset, len(rows), total)
            if not rows:
                break
            for row in rows:
                f = self._parse_search_row(row)
                if f:
                    filings.setdefault(f["filing_url"], f)
            offset += len(rows)
        if total and len(filings) < total:
            log.warning("Senate search reported %d filings but %d were collected", total, len(filings))
        return sorted(filings.values(), key=lambda f: (f["filing_date"] or "", f["filer_name"]))

    @staticmethod
    def _parse_search_row(row: list) -> dict | None:
        if len(row) < 5:
            return None
        m = re.search(r'href="([^"]+)"', row[3])
        if not m:
            return None
        path = m.group(1)
        kind = re.search(r"/search/view/([^/]+)/", path)
        first, last = _clean(row[0]), _clean(row[1])
        return {
            "first_name": first,
            "last_name": last,
            "filer_name": f"{first} {last}".strip(),
            "filing_url": SENATE_BASE + path if path.startswith("/") else path,
            "filing_id": path.rstrip("/").rsplit("/", 1)[-1],
            "filing_date": _iso(row[4]),
            "report_title": _clean(row[3]),
            "is_paper": bool(kind and kind.group(1) == "paper"),
        }

    def fetch_ptr_html(self, url: str) -> str:
        self._ensure_agreed()
        resp = _request(self.s, "GET", url)
        if resp.status_code == 404:
            raise LookupError(f"404 for {url}")
        resp.raise_for_status()
        if "agreement_form" in resp.text:          # session lapsed: agree again and retry once
            self.accept_agreement()
            resp = _request(self.s, "GET", url)
            resp.raise_for_status()
        return resp.text


def parse_senate_ptr(page_html: str) -> list[dict]:
    """Parse the transaction table of an electronic Senate PTR page.

    Returns partial records (transaction fields only); the caller adds the filing fields.
    """
    m = re.search(r"<tbody[^>]*>(.*?)</tbody>", page_html, re.S | re.I)
    if not m:
        return []
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S | re.I):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)
        if len(cells) < 8:
            continue
        # The asset cell may carry a muted <div> with bond coupon/maturity or option details.
        asset_cell = cells[4]
        detail = re.search(r'<div[^>]*class="[^"]*text-muted[^"]*"[^>]*>(.*?)</div>', asset_cell, re.S | re.I)
        asset_name = _clean(re.sub(r"<div.*?</div>", " ", asset_cell, flags=re.S | re.I))
        if detail and _clean(detail.group(1)):
            asset_name = f"{asset_name} ({_clean(detail.group(1))})".strip()
        ticker = _null(_clean(cells[3]))
        raw_type = _clean(cells[6])
        amount_range, lo, hi = parse_amount(_clean(cells[7]))
        out.append({
            "transaction_date": _iso(_clean(cells[1])),
            "owner": normalize_owner(_clean(cells[2])),
            "ticker": ticker.upper() if ticker else None,
            "asset_name": asset_name or None,
            "asset_type": _null(_clean(cells[5])),
            "transaction_type": normalize_type(raw_type),
            "transaction_type_raw": raw_type or None,
            "amount_range": amount_range,
            "amount_min": lo,
            "amount_max": hi,
            "comment": _null(_clean(cells[8])) if len(cells) > 8 else None,
        })
    return out


def scrape_senate(start: date, end: date, name: str | None = None, delay: float = 1.0,
                  stats: dict | None = None, sink: list | None = None) -> list[dict]:
    """Scrape Senate PTR trades filed between start and end (inclusive).

    Trades are appended to `sink` (a new list by default) as each filing is parsed, so a
    caller that passes its own list keeps the partial results if the run is interrupted.
    """
    stats = stats if stats is not None else {}
    trades = sink if sink is not None else []
    client = SenateClient()
    log.info("Senate: searching PTRs filed %s to %s", start, end)
    filings = [f for f in client.search_ptrs(start, end) if _matches(f["filer_name"], name)]
    paper = [f for f in filings if f["is_paper"]]
    electronic = [f for f in filings if not f["is_paper"]]
    stats.update(filings=len(filings), paper=len(paper), parsed=0, failed=0, trades=0)
    log.info("Senate: %d filings (%d electronic, %d paper; paper filings are scanned images and are skipped)",
             len(filings), len(electronic), len(paper))
    for f in paper:
        log.debug("Senate paper filing skipped: %s %s %s", f["filing_date"], f["filer_name"], f["filing_url"])

    for i, f in enumerate(electronic, 1):
        if i > 1 and delay:
            time.sleep(delay)
        try:
            rows = parse_senate_ptr(client.fetch_ptr_html(f["filing_url"]))
        except Exception as e:  # keep going; one bad filing shouldn't end the run
            stats["failed"] += 1
            log.warning("Senate [%d/%d] %s %s failed: %s", i, len(electronic), f["filer_name"], f["filing_url"], e)
            continue
        stats["parsed"] += 1
        log.info("Senate [%d/%d] %s %s: %d transactions", i, len(electronic), f["filing_date"], f["filer_name"], len(rows))
        for r in rows:
            trades.append(_record(chamber="senate", **{k: f[k] for k in (
                "filer_name", "first_name", "last_name", "filing_id", "filing_url", "filing_date", "report_title")}, **r))
        stats["trades"] += len(rows)
    return trades


# --------------------------------------------------------------------------- #
# House: disclosures-clerk.house.gov
# --------------------------------------------------------------------------- #

HOUSE_BASE = "https://disclosures-clerk.house.gov/public_disc"
HOUSE_MANIFEST = HOUSE_BASE + "/financial-pdfs/{year}FD.txt"
HOUSE_PDF = HOUSE_BASE + "/ptr-pdfs/{year}/{doc_id}.pdf"

# Asset type codes used on House PTRs (https://fd.house.gov/reference/asset-type-codes.aspx).
HOUSE_ASSET_TYPES = {
    "4K": "401K and Other Non-Federal Retirement Accounts", "5C": "529 College Savings Plan",
    "5F": "529 Portfolio", "5P": "529 Prepaid Tuition Plan", "AB": "Asset-Backed Securities",
    "BA": "Bank Accounts, Money Market Accounts and CDs", "BK": "Brokerage Accounts",
    "CO": "Collectibles", "CS": "Corporate Securities (Bonds and Notes)", "CT": "Cryptocurrency",
    "DB": "Defined Benefit Pension", "DO": "Debts Owed to the Filer", "DS": "Delaware Statutory Trust",
    "EF": "Exchange Traded Funds (ETF)", "EQ": "Excepted/Qualified Blind Trust",
    "ET": "Exchange Traded Notes", "FA": "Farms", "FE": "Foreign Exchange Position (Currency)",
    "FN": "Fixed Annuity", "FU": "Futures", "GS": "Government Securities and Agency Debt",
    "HE": "Hedge Funds & Private Equity Funds (EIF)", "HN": "Hedge Funds & Private Equity Funds (non-EIF)",
    "IC": "Investment Club", "IH": "IRA (Held in Cash)", "IP": "Intellectual Property & Royalties",
    "IR": "IRA", "MA": "Managed Accounts (e.g., SMA and UMA)", "MF": "Mutual Funds",
    "MO": "Mineral/Oil/Solar Energy Rights", "OI": "Ownership Interest (Holding Investments)",
    "OL": "Ownership Interest (Engaged in a Trade or Business)", "OP": "Options",
    "OT": "Other", "PE": "Pensions", "PM": "Precious Metals", "PS": "Stock (Not Publicly Traded)",
    "RE": "Real Estate Invest. Trust (REIT)", "RF": "REIT (EIF)", "RN": "REIT (non-EIF)",
    "RP": "Real Property", "RS": "Restricted Stock Units (RSUs)",
    "SA": "Stock Appreciation Right", "ST": "Stocks (including ADRs)", "TR": "Trust",
    "VA": "Variable Annuity", "VI": "Variable Insurance", "WU": "Whole/Universal Insurance",
}


def _house_years(start: date, end: date) -> list[int]:
    # Each year's manifest also lists filings from early the following January,
    # so a range that touches January needs the previous year's manifest too.
    years = set(range(start.year, end.year + 1))
    if start.month == 1:
        years.add(start.year - 1)
    return sorted(years)


def fetch_house_manifest(year: int, session: requests.Session | None = None) -> list[dict]:
    """Return the PTR rows (FilingType 'P') from the House Clerk's yearly manifest."""
    s = session or _session()
    url = HOUSE_MANIFEST.format(year=year)
    resp = _request(s, "GET", url)
    if resp.status_code == 404:
        log.info("House: no manifest for %d yet", year)
        return []
    resp.raise_for_status()
    text = resp.content.decode("utf-8-sig", errors="replace")
    out = []
    for row in csv.DictReader(io.StringIO(text), delimiter="\t"):
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        if row.get("FilingType") != "P" or not row.get("DocID"):
            continue
        doc_id, ryear = row["DocID"], int(row.get("Year") or year)
        first, last, suffix = row.get("First", ""), row.get("Last", ""), row.get("Suffix", "")
        out.append({
            "doc_id": doc_id,
            "first_name": first,
            "last_name": last,
            "filer_name": " ".join(p for p in (first, last, suffix) if p),
            "state_district": row.get("StateDst") or None,
            "filing_date": _iso(row.get("FilingDate")),
            "year": ryear,
            "pdf_url": HOUSE_PDF.format(year=ryear, doc_id=doc_id),
            # Electronic DocIDs are 8 digits starting with 2; paper (scanned) ones start with 8 or 9.
            "is_paper": doc_id[:1] in ("8", "9"),
        })
    return out


def extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        import pdfplumber
    except ImportError:
        raise SystemExit("House scraping needs pdfplumber:  pip install pdfplumber")
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


# One transaction starts on a line like:
#   "SP Apple Inc. - Common Stock (AAPL) [ST] S (partial) 03/19/2026 04/07/2026 $1,001 - $15,000"
# i.e. [owner] asset-text  TYPE  transaction-date  notification-date  [amount]
# The asset name and amount often wrap onto the following lines.
_H_TXN = re.compile(
    r"^(?:(?P<owner>SP|JT|DC)\s+)?(?P<asset>.*?)\s*\b(?P<type>S \(partial\)|S \(full\)|P|S|E)\s+"
    r"(?P<tdate>\d{1,2}/\d{1,2}/\d{4})\s+(?P<ndate>\d{1,2}/\d{1,2}/\d{4})\s*(?P<rest>.*)$"
)
# pdfplumber can't map the lower-case glyphs of the PDF's label font, so "Filing Status:"
# comes out as "F      S     :" and "Subholding Of:" as "S          O :".
_H_META = re.compile(r"^(?P<key>[A-Z](?:\s+[A-Z])?)\s*:\s*(?P<val>.*)$")
_H_NOISE = re.compile(r"^(ID Owner Asset|Type Date Gains|\$200\?|Filing ID #)")
_H_END = re.compile(r"^(\* For the complete list|I CERTIFY|Digitally Signed)")
# Tickers are 1-6 capitals in parentheses, with an optional class suffix: (AAPL) (BRK.B) (T$A)
_H_TICKER = re.compile(r"\(([A-Z][A-Z0-9]{0,5}(?:[.\-/$][A-Z0-9]{1,3})?)\)")
_H_ASSET_CODE = re.compile(r"\[([A-Z0-9]{2})\]")
_H_AMOUNT_BITS = re.compile(r"(?:Over\s+)?\$[\d,]+(?:\.\d+)?(?:\s*-)?|^-$")


def parse_house_ptr(text: str) -> list[dict]:
    """Parse transactions from the extracted text of an electronic House PTR PDF.

    Returns partial records (transaction fields only); the caller adds the filing fields.
    """
    # Dropped glyphs come through as NUL bytes; treat them as spaces.
    lines = [ln.replace("\x00", " ").strip() for ln in text.splitlines()]
    try:
        start = next(i for i, ln in enumerate(lines) if ln.startswith("ID Owner Asset"))
    except StopIteration:
        return []

    txns: list[dict] = []
    cur: dict | None = None
    field: str | None = None   # which metadata field a wrapped line continues

    def finish():
        if cur is None:
            return
        asset_text = " ".join(cur["asset_parts"])
        tickers = _H_TICKER.findall(asset_text)
        code = _H_ASSET_CODE.search(asset_text)
        name = _H_ASSET_CODE.sub(" ", asset_text)
        if tickers:
            name = name.replace(f"({tickers[-1]})", " ")
        amount_range, lo, hi = parse_amount(" ".join(cur["amount_parts"]))
        txns.append({
            "transaction_date": _iso(cur["tdate"]),
            "notification_date": _iso(cur["ndate"]),
            "owner": normalize_owner(cur["owner"]),
            "ticker": tickers[-1] if tickers else None,
            "asset_name": re.sub(r"\s+", " ", name).strip(" -") or None,
            "asset_type": HOUSE_ASSET_TYPES.get(code.group(1), code.group(1)) if code else None,
            "asset_type_code": code.group(1) if code else None,
            "transaction_type": normalize_type(cur["type"]),
            "transaction_type_raw": cur["type"],
            "amount_range": amount_range,
            "amount_min": lo,
            "amount_max": hi,
            "account": cur["account"] or None,
            "comment": cur["comment"] or None,
        })

    for ln in lines[start:]:
        if not ln or _H_NOISE.match(ln):
            continue
        if _H_END.match(ln):
            break
        m = _H_TXN.match(ln)
        if m:
            finish()
            cur = {"owner": m["owner"] or "", "type": m["type"], "tdate": m["tdate"], "ndate": m["ndate"],
                   "asset_parts": [m["asset"]], "amount_parts": [m["rest"]], "account": "", "comment": ""}
            field = None
            continue
        if cur is None:
            continue
        meta = _H_META.match(ln)
        if meta:
            key, val = re.sub(r"\s+", "", meta["key"]), meta["val"].strip()
            field = {"SO": "account", "D": "comment"}.get(key, "_ignored")
            if field != "_ignored":
                cur[field] = val
            continue
        if field:  # wrapped line of the last metadata field
            if field != "_ignored":
                cur[field] = f"{cur[field]} {ln}".strip()
            continue
        # Wrapped asset-name line, possibly with the tail of the amount on it ("(PEG) [ST] $50,000").
        bits = _H_AMOUNT_BITS.findall(ln)
        if bits:
            cur["amount_parts"].extend(bits)
            ln = _H_AMOUNT_BITS.sub(" ", ln).strip()
        if ln:
            cur["asset_parts"].append(ln)
    finish()
    return txns


def scrape_house(start: date, end: date, name: str | None = None, delay: float = 1.5,
                 stats: dict | None = None, sink: list | None = None) -> list[dict]:
    """Scrape House PTR trades filed between start and end (inclusive). See scrape_senate for `sink`."""
    stats = stats if stats is not None else {}
    trades = sink if sink is not None else []
    s = _session()
    seen: dict[str, dict] = {}
    for year in _house_years(start, end):
        log.info("House: fetching %d manifest", year)
        for f in fetch_house_manifest(year, s):
            if f["filing_date"] and start.isoformat() <= f["filing_date"] <= end.isoformat() \
                    and _matches(f["filer_name"], name):
                seen.setdefault(f["doc_id"], f)
    filings = sorted(seen.values(), key=lambda f: (f["filing_date"], f["filer_name"]))
    electronic = [f for f in filings if not f["is_paper"]]
    stats.update(filings=len(filings), paper=len(filings) - len(electronic), parsed=0, failed=0, trades=0)
    log.info("House: %d filings (%d electronic, %d paper; paper filings are scanned images and are skipped)",
             len(filings), len(electronic), stats["paper"])

    for i, f in enumerate(electronic, 1):
        if i > 1 and delay:
            time.sleep(delay)
        tag = f"House [{i}/{len(electronic)}] {f['filing_date']} {f['filer_name']}"
        try:
            resp = _request(s, "GET", f["pdf_url"])
            if resp.status_code == 404:
                raise LookupError("PDF not published yet (404)")
            resp.raise_for_status()
            text = extract_pdf_text(resp.content)
        except SystemExit:
            raise
        except Exception as e:
            stats["failed"] += 1
            log.warning("%s %s failed: %s", tag, f["pdf_url"], e)
            continue
        if len(text.strip()) < 100:
            stats["paper"] += 1
            log.info("%s: no text layer (scanned), skipped", tag)
            continue
        rows = parse_house_ptr(text)
        stats["parsed"] += 1
        log.info("%s: %d transactions", tag, len(rows))
        if not rows:
            log.warning("%s: no transactions found; check %s", tag, f["pdf_url"])
        for r in rows:
            trades.append(_record(
                chamber="house", filer_name=f["filer_name"], first_name=f["first_name"],
                last_name=f["last_name"], state_district=f["state_district"], filing_id=f["doc_id"],
                filing_url=f["pdf_url"], filing_date=f["filing_date"], **r))
        stats["trades"] += len(rows)
    return trades


# --------------------------------------------------------------------------- #
# Top-level API + CLI
# --------------------------------------------------------------------------- #

def scrape(start_date: str | date | None = None, end_date: str | date | None = None,
           chamber: str = "both", name: str | None = None, ticker: str | None = None,
           stats: dict | None = None, sink: list | None = None) -> list[dict]:
    """Scrape trades from one or both chambers. Dates are filing dates (YYYY-MM-DD), inclusive.

    >>> trades = scrape("2026-09-01", "2026-10-15", chamber="senate")
    """
    end = _to_date(end_date) or date.today()
    start = _to_date(start_date) or end - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    if start > end:
        raise ValueError(f"start date {start} is after end date {end}")
    stats = stats if stats is not None else {}
    trades = sink if sink is not None else []
    if chamber in ("senate", "both"):
        scrape_senate(start, end, name, stats=stats.setdefault("senate", {}), sink=trades)
    if chamber in ("house", "both"):
        scrape_house(start, end, name, stats=stats.setdefault("house", {}), sink=trades)
    return filter_tickers(trades, ticker)


def filter_tickers(trades: list[dict], ticker: str | None) -> list[dict]:
    """Keep only trades whose ticker is in the comma-separated `ticker` list (all if empty)."""
    wanted = {t.strip().upper() for t in (ticker or "").split(",") if t.strip()}
    return [t for t in trades if t["ticker"] in wanted] if wanted else trades


def _to_date(d: str | date | None) -> date | None:
    if d is None or isinstance(d, date):
        return d
    return date.fromisoformat(d)


def write_output(trades: Iterable[dict], path: str, fmt: str) -> None:
    trades = list(trades)
    out = sys.stdout if path == "-" else open(path, "w", newline="", encoding="utf-8")
    try:
        if fmt == "csv":
            w = csv.DictWriter(out, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(trades)
        else:
            json.dump(trades, out, indent=2, ensure_ascii=False)
            out.write("\n")
    finally:
        if out is not sys.stdout:
            out.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="congress_trades.py",
        description="Scrape STOCK Act trade disclosures (Periodic Transaction Reports) "
                    "from the U.S. Senate and House into JSON or CSV.",
        epilog="Dates filter on the filing date, not the trade date. By running this you accept "
               "the Senate eFD terms of use (5 U.S.C. app. 105(c)); see the README.",
    )
    p.add_argument("--chamber", choices=("both", "senate", "house"), default="both",
                   help="which chamber to scrape (default: both)")
    p.add_argument("--start-date", metavar="YYYY-MM-DD", type=date.fromisoformat,
                   help=f"first filing date, inclusive (default: {DEFAULT_LOOKBACK_DAYS} days before end date)")
    p.add_argument("--end-date", metavar="YYYY-MM-DD", type=date.fromisoformat,
                   help="last filing date, inclusive (default: today)")
    p.add_argument("--name", help="only filers whose name contains this text (case-insensitive)")
    p.add_argument("--ticker", help="only trades in these tickers, comma-separated (e.g. NVDA,AAPL)")
    p.add_argument("-o", "--output", default="trades.json",
                   help="output file; .csv writes CSV, '-' writes to stdout (default: trades.json)")
    p.add_argument("--format", choices=("json", "csv"),
                   help="output format (default: from the --output extension, else json)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("-q", "--quiet", action="store_true", help="only log warnings and errors")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S", stream=sys.stderr,
    )
    if not args.verbose:
        logging.getLogger("pdfminer").setLevel(logging.ERROR)
    fmt = args.format or ("csv" if args.output.lower().endswith(".csv") else "json")

    stats: dict = {}
    collected: list[dict] = []
    status = 0
    try:
        trades = scrape(args.start_date, args.end_date, args.chamber, args.name, args.ticker,
                        stats=stats, sink=collected)
    except KeyboardInterrupt:
        log.warning("Interrupted; writing the %d trades scraped so far", len(collected))
        trades, status = filter_tickers(collected, args.ticker), 130
    except ValueError as e:
        p.error(str(e))
    except (requests.RequestException, RuntimeError) as e:
        log.error("%s", e)
        if not collected:
            return 1
        log.warning("Writing the %d trades scraped before the error", len(collected))
        trades, status = filter_tickers(collected, args.ticker), 1

    write_output(trades, args.output, fmt)
    for ch, st in stats.items():
        if st:
            log.info("%s: %d filings, %d parsed, %d paper (skipped), %d failed, %d trades",
                     ch.capitalize(), st.get("filings", 0), st.get("parsed", 0), st.get("paper", 0),
                     st.get("failed", 0), st.get("trades", 0))
    log.info("Wrote %d trades to %s", len(trades), "stdout" if args.output == "-" else args.output)
    return status


if __name__ == "__main__":
    sys.exit(main())
