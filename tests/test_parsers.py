"""Offline tests for the parsers. No network needed:  python -m pytest -q"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import congress_trades as ct  # noqa: E402


# --- shared helpers -------------------------------------------------------- #

def test_parse_amount_bands():
    assert ct.parse_amount("$1,001 - $15,000") == ("$1,001 - $15,000", 1001.0, 15000.0)
    assert ct.parse_amount("Over $50,000,000") == ("Over $50,000,000", 50000001.0, None)
    assert ct.parse_amount("$50,000,001 +") == ("$50,000,001 +", 50000001.0, None)


def test_parse_amount_exact_and_missing():
    assert ct.parse_amount("$224.00") == ("$224.00", 224.0, 224.0)
    assert ct.parse_amount("--") == (None, None, None)
    assert ct.parse_amount("") == (None, None, None)


def test_normalize_type():
    for raw, want in [("Purchase", "buy"), ("P", "buy"), ("Sale (Partial)", "sell"), ("Sale (Full)", "sell"),
                      ("S", "sell"), ("S (partial)", "sell"), ("Exchange", "exchange"), ("E", "exchange")]:
        assert ct.normalize_type(raw) == want, raw


def test_normalize_owner():
    assert [ct.normalize_owner(o) for o in ("Self", "Spouse", "Joint", "Child", "SP", "JT", "DC", "")] == \
        ["self", "spouse", "joint", "dependent", "spouse", "joint", "dependent", "self"]


# --- Senate ---------------------------------------------------------------- #

SENATE_PTR_HTML = """
<table class="table table-striped"><thead><tr class="header"><th>#</th><th>Transaction Date</th>
<th>Owner</th><th>Ticker</th><th>Asset Name</th><th>Asset Type</th><th>Type</th><th>Amount</th><th>Comment</th>
</tr></thead><tbody>
<tr><td>1</td><td> 09/04/2026 </td><td>Spouse</td>
    <td><a href="https://finance.yahoo.com/quote/JPM" target="_blank">JPM</a></td>
    <td>JP Morgan Chase &amp; Co. Common Stock</td><td>Stock</td><td>Sale (Partial)</td>
    <td>$15,001 - $50,000</td><td>--</td></tr>
<tr><td>2</td><td>09/15/2026</td><td>Child</td><td> -- </td>
    <td> DELL INTL LLC/EMC CORP
       <div class="text-muted"><em>Rate/Coupon:</em> 5.6%<br> <em>Matures:</em> 2033-09-15</div></td>
    <td>Corporate Bond</td><td>Purchase</td><td>$1,001 - $15,000</td><td>Rebalance</td></tr>
</tbody></table>
"""


def test_parse_senate_ptr():
    rows = ct.parse_senate_ptr(SENATE_PTR_HTML)
    assert len(rows) == 2
    a, b = rows
    assert a["transaction_date"] == "2026-09-04"
    assert a["owner"] == "spouse"
    assert a["ticker"] == "JPM"
    assert a["asset_name"] == "JP Morgan Chase & Co. Common Stock"
    assert (a["transaction_type"], a["transaction_type_raw"]) == ("sell", "Sale (Partial)")
    assert (a["amount_min"], a["amount_max"]) == (15001.0, 50000.0)
    assert a["comment"] is None
    assert b["ticker"] is None
    assert b["owner"] == "dependent"
    assert b["asset_name"] == "DELL INTL LLC/EMC CORP (Rate/Coupon: 5.6% Matures: 2033-09-15)"
    assert b["transaction_type"] == "buy"
    assert b["comment"] == "Rebalance"


def test_parse_senate_search_row():
    row = ["A. Mitchell", "McConnell, Jr.", "McConnell, A. Mitchell Jr. (Senator)",
           '<a href="/search/view/ptr/028aef33-dc0d-44a1-992f-aa35ee42685b/" target="_blank">'
           "Periodic Transaction Report for 09/21/2026 (Amendment 1)</a>", "09/21/2026"]
    f = ct.SenateClient._parse_search_row(row)
    assert f["filer_name"] == "A. Mitchell McConnell, Jr."
    assert f["filing_id"] == "028aef33-dc0d-44a1-992f-aa35ee42685b"
    assert f["filing_url"] == "https://efdsearch.senate.gov/search/view/ptr/028aef33-dc0d-44a1-992f-aa35ee42685b/"
    assert f["filing_date"] == "2026-09-21"
    assert f["report_title"].endswith("(Amendment 1)")
    assert f["is_paper"] is False
    row[3] = row[3].replace("/view/ptr/", "/view/paper/")
    assert ct.SenateClient._parse_search_row(row)["is_paper"] is True


# --- House ----------------------------------------------------------------- #
# Text below is what pdfplumber extracts from real House PTRs, including the
# NUL bytes it emits for the label font's lower-case letters.

N = "\x00"
HOUSE_TEXT = f"""Filing ID #20034348
Name: Hon. Jane Smith
State/District: TX01
ID Owner Asset Transaction Date Notification Amount Cap.
Type Date Gains >
$200?
SP GE Aerospace Common Stock (GE) S 02/17/2026 02/17/2026 $1,001 - $15,000
[ST]
F{N*5} S{N*5}: New
S{N*9} O{N}: Vivien IRA
D{N*10}: sale of 33 shares of GE @ $328.035
Berkshire Hathaway Inc. New P 03/13/2026 03/31/2026 $15,001 -
Common Stock (BRK.B) [ST] $50,000
F{N*5} S{N*5}: New
S{N*9} O{N}: Investment
ID Owner Asset Transaction Date Notification Amount Cap.
Type Date Gains >
$200?
JT UBS AG [CS] S (partial) 12/12/2025 01/12/2026 $15,001 -
$50,000
F{N*5} S{N*5}: New
L{N*7}: US
D{N*10}: Professionally managed account. Structured
product (EEM & IWM).
Kyndryl Holdings, Inc. Common S 10/10/2025 10/14/2025 $224.00
Stock (KD) [ST]
DC AT&T Inc. Depositary Shares, Series A (T$A) [ST] E 03/01/2026 03/02/2026 Over $50,000,000
* For the complete list of asset type abbreviations, please visit https://fd.house.gov/reference/asset-type-codes.aspx.
I CERTIFY that the statements I have made are true.
Digitally Signed: Hon. Jane Smith, 03/31/2026
"""


def test_parse_house_ptr():
    rows = ct.parse_house_ptr(HOUSE_TEXT)
    assert len(rows) == 5
    ge, brk, ubs, kd, t = rows

    assert ge["owner"] == "spouse"
    assert (ge["ticker"], ge["asset_name"], ge["asset_type_code"]) == ("GE", "GE Aerospace Common Stock", "ST")
    assert ge["asset_type"] == "Stocks (including ADRs)"
    assert (ge["transaction_type"], ge["transaction_type_raw"]) == ("sell", "S")
    assert (ge["transaction_date"], ge["notification_date"]) == ("2026-02-17", "2026-02-17")
    assert ge["account"] == "Vivien IRA"
    assert ge["comment"] == "sale of 33 shares of GE @ $328.035"

    # Asset name and amount both wrap onto the next line.
    assert brk["owner"] == "self"
    assert (brk["ticker"], brk["asset_name"]) == ("BRK.B", "Berkshire Hathaway Inc. New Common Stock")
    assert (brk["amount_range"], brk["amount_min"], brk["amount_max"]) == ("$15,001 - $50,000", 15001.0, 50000.0)
    assert brk["account"] == "Investment"

    # No ticker; repeated page header is skipped; description wraps; L: line ignored.
    assert (ubs["owner"], ubs["ticker"], ubs["asset_name"], ubs["asset_type_code"]) == ("joint", None, "UBS AG", "CS")
    assert ubs["transaction_type_raw"] == "S (partial)"
    assert ubs["amount_max"] == 50000.0
    assert ubs["comment"] == "Professionally managed account. Structured product (EEM & IWM)."

    # Exact dollar figure instead of a band.
    assert (kd["ticker"], kd["amount_min"], kd["amount_max"]) == ("KD", 224.0, 224.0)

    # Preferred-share ticker, open-ended band.
    assert (t["owner"], t["ticker"], t["transaction_type"]) == ("dependent", "T$A", "exchange")
    assert (t["amount_min"], t["amount_max"]) == (50000001.0, None)


def test_parse_house_ptr_without_table():
    assert ct.parse_house_ptr("scanned page with no text layer") == []


def test_house_years_include_previous_year_in_january():
    from datetime import date
    assert ct._house_years(date(2026, 1, 5), date(2026, 2, 1)) == [2025, 2026]
    assert ct._house_years(date(2026, 3, 1), date(2026, 10, 1)) == [2026]
    assert ct._house_years(date(2024, 6, 1), date(2026, 10, 1)) == [2024, 2025, 2026]
