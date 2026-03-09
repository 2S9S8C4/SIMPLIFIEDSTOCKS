"""
Stock Simplify — EDGAR Downloader, Analyzer & Financial Viewer
==============================================================
All-in-one: SEC EDGAR filing downloader, financial analyzer, and interactive
desktop GUI combined into a single file.

Run as GUI (default — no arguments):
    python stock_simplify.py

Run as CLI downloader / analyzer:
    python stock_simplify.py --tickers AAPL MSFT
    python stock_simplify.py --tickers AAPL --analyze
    python stock_simplify.py --tickers TSLA NVDA --analyze-only
    python stock_simplify.py --forms 10-K --start-date 2020-01-01 --dry-run

SEC API docs  : https://www.sec.gov/developer
Rate limit    : 10 req/s  (this script stays safely under at ~8 req/s)
Dependencies  : pip install requests beautifulsoup4
                tkinter ships with Python on all platforms

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT STRUCTURE (CLI mode)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
edgar_filings/
  AAPL/
    10-K/
      2024-11-01_0000320193-24-000123/
        aapl-20240928.htm        ← downloaded filing
        metadata.json            ← filing metadata
    analysis.md                  ← generated report (--analyze)
    analysis.json                ← machine-readable report
  _summary.csv                   ← one row per company
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import queue
import re
import sys
import threading
import time
import webbrowser
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from datetime import datetime, datetime as _dt, date
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

import requests

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
    _TK_AVAILABLE = True
except ImportError:
    tk = None           # type: ignore[assignment]
    ttk = None          # type: ignore[assignment]
    messagebox = None   # type: ignore[assignment]
    _TK_AVAILABLE = False

try:
    from bs4 import BeautifulSoup  # type: ignore[import-untyped]
    _BS4_AVAILABLE = True
except ImportError:
    BeautifulSoup = None  # type: ignore[assignment,misc]
    _BS4_AVAILABLE = False

try:
    import matplotlib
    if _TK_AVAILABLE:
        matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    import matplotlib.ticker as mticker
    _MPL_AVAILABLE = True
except Exception:
    Figure = None           # type: ignore[assignment,misc]
    FigureCanvasTkAgg = None  # type: ignore[assignment,misc]
    mticker = None          # type: ignore[assignment]
    _MPL_AVAILABLE = False

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

COMPANY_TICKERS_URL  = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL      = "https://data.sec.gov/submissions/CIK{cik}.json"
FILING_DOC_URL       = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{filename}"
XBRL_COMPANY_FACTS   = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

USER_AGENT    = "StockSimplify script@example.com"
REQUEST_DELAY = 0.12   # seconds between requests  (~8/s, under the 10/s limit)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("stock_simplify.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── HTTP client ───────────────────────────────────────────────────────────────

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
_last_req_t = 0.0


def get(url: str, **kwargs) -> requests.Response:
    """Rate-limited GET with automatic retry and 429 back-off."""
    global _last_req_t
    _session.headers["Host"] = url.split("/")[2]
    elapsed = time.monotonic() - _last_req_t
    if elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)
    for attempt in range(3):
        try:
            resp = _session.get(url, timeout=45, **kwargs)
            _last_req_t = time.monotonic()
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 15))
                log.warning("Rate-limited — waiting %ds", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            if attempt == 2:
                raise
            log.warning("Request failed (%s), retrying in 5s…", exc)
            time.sleep(5)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — DOWNLOAD LAYER
# ══════════════════════════════════════════════════════════════════════════════

def fetch_company_list() -> dict[str, dict]:
    """
    Fetch the full EDGAR company list.
    Returns {ticker: {cik (zero-padded 10-digit str), name, ticker}}.
    """
    log.info("Fetching company list from SEC…")
    raw = get(COMPANY_TICKERS_URL).json()
    companies: dict[str, dict] = {}
    for entry in raw.values():
        ticker = entry["ticker"].upper()
        companies[ticker] = {
            "cik":    str(entry["cik_str"]).zfill(10),
            "name":   entry["title"],
            "ticker": ticker,
        }
    log.info("Found %d companies", len(companies))
    return companies


def fetch_submissions(cik: str) -> dict:
    """Fetch the submission history JSON for a CIK (zero-padded to 10 digits)."""
    return get(SUBMISSIONS_URL.format(cik=cik)).json()


def get_filings(
    submissions: dict,
    forms: set[str],
    start_date: date | None,
) -> list[dict]:
    """
    Extract matching filings from a submissions JSON.
    Returns list of filing dicts sorted newest-first.
    """
    recent = submissions.get("filings", {}).get("recent", {})
    if not recent:
        return []

    keys = ["accessionNumber", "filingDate", "form", "primaryDocument", "reportDate"]
    rows = list(zip(*[recent.get(k, []) for k in keys]))

    results = []

    def _add_rows(rows_iter):
        for accession, filing_date, form, primary_doc, report_date in rows_iter:
            if form not in forms:
                continue
            try:
                fd = datetime.strptime(filing_date, "%Y-%m-%d").date()
            except ValueError:
                continue
            if start_date and fd < start_date:
                continue
            results.append({
                "accession":        accession,
                "accession_nodash": accession.replace("-", ""),
                "filing_date":      filing_date,
                "form":             form,
                "primary_doc":      primary_doc,
                "report_date":      report_date,
            })

    _add_rows(rows)

    for old_file in submissions.get("filings", {}).get("files", []):
        old_name = old_file.get("name", "")
        if not old_name:
            continue
        try:
            old_data = get(f"https://data.sec.gov/submissions/{old_name}").json()
            old_rows = list(zip(*[old_data.get(k, []) for k in keys]))
            _add_rows(old_rows)
        except Exception as exc:
            log.debug("Skipping old filings file %s: %s", old_name, exc)

    results.sort(key=lambda x: x["filing_date"], reverse=True)
    return results


def fetch_filing_index(cik_raw: str, accession_nodash: str) -> list[dict]:
    """
    Fetch the filing index to get all documents within one accession.
    Returns list of {name, type, url}.
    """
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik_raw}/"
        f"{accession_nodash}/{accession_nodash}-index.json"
    )
    try:
        data  = get(index_url).json()
        items = data.get("directory", {}).get("item", [])
        return [
            {
                "name": item["name"],
                "type": item.get("type", ""),
                "url": (
                    f"https://www.sec.gov/Archives/edgar/data/"
                    f"{cik_raw}/{accession_nodash}/{item['name']}"
                ),
            }
            for item in items
            if item.get("name", "").endswith((".htm", ".html", ".txt", ".xml"))
        ]
    except Exception:
        return []


def download_filing(
    company: dict,
    filing: dict,
    output_dir: Path,
    download_all_docs: bool = False,
) -> bool:
    """
    Download a single filing. Returns True if at least one file was saved.

    Saves to:
      output_dir/{TICKER}/{FORM}/{filing_date}_{accession}/
    """
    cik_raw = str(int(company["cik"]))
    ticker  = company["ticker"]
    form    = filing["form"]
    acc_nd  = filing["accession_nodash"]
    acc     = filing["accession"]
    fd      = filing["filing_date"]
    primary = filing["primary_doc"]

    dest_dir = output_dir / ticker / form / f"{fd}_{acc}"
    dest_dir.mkdir(parents=True, exist_ok=True)

    meta_path = dest_dir / "metadata.json"
    if not meta_path.exists():
        meta_path.write_text(
            json.dumps({**company, **filing}, indent=2),
            encoding="utf-8",
        )

    docs = (
        fetch_filing_index(cik_raw, acc_nd) or
        [{"name": primary, "url": FILING_DOC_URL.format(
            cik=cik_raw, accession=acc_nd, filename=primary)}]
    ) if download_all_docs else [
        {"name": primary, "url": FILING_DOC_URL.format(
            cik=cik_raw, accession=acc_nd, filename=primary)}
    ]

    downloaded = 0
    for doc in docs:
        dest = dest_dir / doc["name"]
        if dest.exists() and dest.stat().st_size > 0:
            log.debug("  Skip (exists): %s", doc["name"])
            downloaded += 1
            continue
        try:
            resp = get(doc["url"])
            dest.write_bytes(resp.content)
            log.debug("  Saved: %s (%d bytes)", doc["name"], len(resp.content))
            downloaded += 1
        except Exception as exc:
            log.warning("  Failed to download %s: %s", doc["name"], exc)

    return downloaded > 0


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — ANALYSIS: XBRL / FINANCIAL METRICS
# ══════════════════════════════════════════════════════════════════════════════

CONCEPTS: dict[str, list[str]] = {
    # ── Income statement ──────────────────────────────────────────────────────
    "revenue": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "RevenuesNetOfInterestExpense",
    ],
    "cost_of_revenue": [
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
    ],
    "gross_profit":    ["GrossProfit"],
    "operating_income":["OperatingIncomeLoss"],
    "net_income":      ["NetIncomeLoss", "ProfitLoss"],
    "interest_expense":["InterestExpense", "InterestExpenseDebt"],
    "income_tax":      ["IncomeTaxExpenseBenefit"],
    "rd_expense":      ["ResearchAndDevelopmentExpense"],
    "da":              ["DepreciationDepletionAndAmortization", "Depreciation"],
    "eps_basic":       ["EarningsPerShareBasic"],
    "eps_diluted":     ["EarningsPerShareDiluted"],
    "shares_diluted":  [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingDiluted",
    ],
    # ── Balance sheet ─────────────────────────────────────────────────────────
    "total_assets":       ["Assets"],
    "total_liabilities":  ["Liabilities"],
    "current_assets":     ["AssetsCurrent"],
    "current_liabilities":["LiabilitiesCurrent"],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsAndShortTermInvestments",
        "Cash",
    ],
    "inventory":          ["InventoryNet", "Inventories"],
    "accounts_receivable":[
        "AccountsReceivableNetCurrent",
        "ReceivablesNetCurrent",
    ],
    "stockholders_equity":[
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "long_term_debt":     ["LongTermDebt", "LongTermDebtNoncurrent"],
    "short_term_debt":    ["ShortTermBorrowings", "LongTermDebtCurrent"],
    "retained_earnings":  ["RetainedEarningsAccumulatedDeficit"],
    "shares_outstanding": ["CommonStockSharesOutstanding"],
    # ── Cash flow statement ───────────────────────────────────────────────────
    "operating_cf":    ["NetCashProvidedByUsedInOperatingActivities"],
    "investing_cf":    ["NetCashProvidedByUsedInInvestingActivities"],
    "financing_cf":    ["NetCashProvidedByUsedInFinancingActivities"],
    "capex":           [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForCapitalImprovements",
    ],
    "dividends_paid":  ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"],
    "share_repurchases":["PaymentsForRepurchaseOfCommonStock"],
}


@dataclass
class Period:
    """One XBRL data point for a concept at a specific fiscal period."""
    end:   str            # ISO date "YYYY-MM-DD"
    value: float
    fy:    Optional[int]
    fp:    str            # "FY" | "Q1" | "Q2" | "Q3" | "Q4"
    form:  str            # "10-K" | "10-Q"
    filed: str            # ISO date when this was filed with SEC


@dataclass
class FinancialMetrics:
    company_name:           str = ""
    cik:                    str = ""
    latest_annual_fy:       Optional[int] = None
    latest_annual_date:     str = ""
    latest_quarterly_date:  str = ""
    annual:                 dict = field(default_factory=dict)
    quarterly:              dict = field(default_factory=dict)
    annual_trend:           dict = field(default_factory=dict)   # up to 10 annual periods
    quarterly_trend:        dict = field(default_factory=dict)   # 4 quarterly periods
    ratios:                 dict = field(default_factory=dict)


@dataclass
class QualitativeData:
    form_type:         str = ""
    filing_date:       str = ""
    accession:         str = ""
    business_overview: str = ""
    risk_factors:      str = ""
    legal_proceedings: str = ""
    mda_highlights:    str = ""
    auditor_notes:     str = ""
    controls_notes:    str = ""


@dataclass
class RedFlag:
    severity: str   # "CRITICAL" | "WARNING" | "INFO"
    category: str
    message:  str


@dataclass
class CompanyAnalysis:
    ticker:        str
    name:          str
    cik:           str
    analysis_date: str
    metrics:       FinancialMetrics
    qualitative:   QualitativeData
    red_flags:     list[RedFlag] = field(default_factory=list)


# ── XBRL helpers ──────────────────────────────────────────────────────────────

def _extract_periods(
    facts: dict,
    concept_names: list[str],
    form_filter: str,
    unit: str = "USD",
) -> list[Period]:
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    for name in concept_names:
        entries = (
            us_gaap.get(name, {}).get("units", {}).get(unit)
            or us_gaap.get(name, {}).get("units", {}).get("shares")
        )
        if not entries:
            continue
        seen: dict[tuple, Period] = {}
        for e in entries:
            if e.get("form") != form_filter:
                continue
            fp = e.get("fp", "")
            if not fp:
                continue
            p = Period(
                end=e.get("end", ""),
                value=float(e.get("val", 0)),
                fy=e.get("fy"),
                fp=fp,
                form=form_filter,
                filed=e.get("filed", ""),
            )
            key = (p.end, p.fp)
            if key not in seen or p.filed > seen[key].filed:
                seen[key] = p
        return sorted(seen.values(), key=lambda p: p.end)
    return []


def _latest(periods: list[Period]) -> Optional[float]:
    return periods[-1].value if periods else None


def _trend(periods: list[Period], n: int) -> list[dict]:
    return [
        {"end": p.end, "fy": p.fy, "fp": p.fp, "value": p.value, "filed": p.filed}
        for p in periods[-n:]
    ]


def _safe_div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return a / b


def _pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    v = _safe_div(a, b)
    return round(v * 100, 2) if v is not None else None


def _round2(v: Optional[float]) -> Optional[float]:
    return round(v, 2) if v is not None else None


def _round1(v: Optional[float]) -> Optional[float]:
    return round(v, 1) if v is not None else None


def build_financial_metrics(facts: dict) -> FinancialMetrics:
    """Derive all financial metrics and ratios from an XBRL companyfacts JSON."""
    m = FinancialMetrics(
        company_name=facts.get("entityName", ""),
        cik=str(facts.get("cik", "")),
    )
    av: dict[str, Optional[float]] = {}
    qv: dict[str, Optional[float]] = {}

    for key, concept_names in CONCEPTS.items():
        unit = "shares" if key in {"shares_outstanding", "shares_diluted"} else "USD"

        ann = _extract_periods(facts, concept_names, "10-K", unit)
        if ann:
            m.annual_trend[key] = _trend(ann, 10)
            av[key] = _latest(ann)
            if not m.latest_annual_date or ann[-1].end > m.latest_annual_date:
                m.latest_annual_date = ann[-1].end
                m.latest_annual_fy   = ann[-1].fy
        else:
            av[key] = None

        qtr = _extract_periods(facts, concept_names, "10-Q", unit)
        if qtr:
            m.quarterly_trend[key] = _trend(qtr, 4)
            qv[key] = _latest(qtr)
            if not m.latest_quarterly_date or qtr[-1].end > m.latest_quarterly_date:
                m.latest_quarterly_date = qtr[-1].end
        else:
            qv[key] = None

    m.annual    = {k: v for k, v in av.items() if v is not None}
    m.quarterly = {k: v for k, v in qv.items() if v is not None}

    def bsv(key: str) -> Optional[float]:
        return qv.get(key) or av.get(key)

    rev  = av.get("revenue")
    gp   = av.get("gross_profit") or (
        (rev - av["cost_of_revenue"]) if rev and av.get("cost_of_revenue") else None
    )
    ebit = av.get("operating_income")
    ni   = av.get("net_income")
    ocf  = av.get("operating_cf")
    capex= av.get("capex")
    fcf  = (ocf - capex) if (ocf is not None and capex is not None) else None

    ca   = bsv("current_assets")
    cl   = bsv("current_liabilities")
    cash = bsv("cash")
    inv  = bsv("inventory")
    ar   = bsv("accounts_receivable")
    ta   = bsv("total_assets")
    eq   = bsv("stockholders_equity")
    ltd  = bsv("long_term_debt")
    std  = bsv("short_term_debt")
    intx = av.get("interest_expense")

    total_debt = (
        (ltd or 0) + (std or 0) if (ltd is not None or std is not None) else None
    )

    m.ratios = {
        "gross_margin_pct":     _pct(gp, rev),
        "operating_margin_pct": _pct(ebit, rev),
        "net_margin_pct":       _pct(ni, rev),
        "fcf_margin_pct":       _pct(fcf, rev),
        "roe_pct":              _pct(ni, eq),
        "roa_pct":              _pct(ni, ta),
        "ebitda": (
            (ebit or 0) + (av.get("da") or 0) if ebit is not None else None
        ),
        "current_ratio":    _round2(_safe_div(ca, cl)),
        "quick_ratio":      _round2(_safe_div((ca or 0) - (inv or 0), cl))
                            if ca is not None and cl is not None else None,
        "cash_ratio":       _round2(_safe_div(cash, cl)),
        "debt_to_equity":   _round2(_safe_div(total_debt, eq)),
        "debt_to_assets":   _round2(_safe_div(total_debt, ta)),
        "interest_coverage":_round2(_safe_div(ebit, intx)),
        "net_debt":         ((total_debt or 0) - (cash or 0)) if total_debt is not None else None,
        "free_cash_flow":   fcf,
        "ocf_to_cl":        _round2(_safe_div(ocf, cl)),
        "asset_turnover":   _round2(_safe_div(rev, ta)),
        "inventory_turnover":_round1(_safe_div(av.get("cost_of_revenue"), inv)),
        "dso_days": (
            round(ar / (rev / 365), 1) if rev and ar else None
        ),
    }
    return m


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — ANALYSIS: QUALITATIVE / HTML EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

_SECTION_PATTERNS: dict[str, list[str]] = {
    "business_overview": [
        r"item\s*1[\.\s]+business",
        r"(?:our\s+)?business\s+overview",
    ],
    "risk_factors": [
        r"item\s*1a[\.\s]+risk\s+factors?",
        r"risk\s+factors?",
    ],
    "legal_proceedings": [
        r"item\s*3[\.\s]+legal\s+proceedings?",
    ],
    "mda_highlights": [
        r"item\s*7[\.\s]+management.{0,30}discussion",
        r"management.{0,30}discussion\s+and\s+analysis",
    ],
    "auditor_notes": [
        r"report\s+of\s+independent\s+registered",
        r"independent\s+auditors?\s+report",
    ],
    "controls_notes": [
        r"item\s*9a[\.\s]+controls",
        r"controls\s+and\s+procedures?",
    ],
}

_NEXT_SECTION_RE = re.compile(
    r"\n\s*(?:item\s+\d+[a-z]?|part\s+[IVX]+)\b", re.IGNORECASE
)
_MAX_SECTION_CHARS = 3000


def _html_to_text(html_bytes: bytes) -> str:
    soup = BeautifulSoup(html_bytes, "html.parser")
    for tag in soup(["script", "style", "footer", "header", "nav"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _find_section(text: str, patterns: list[str], max_chars: int) -> str:
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            continue
        start = m.end()
        nxt   = _NEXT_SECTION_RE.search(text, start)
        end   = nxt.start() if nxt else start + max_chars * 3
        snippet = text[start:end].strip()
        if len(snippet) > max_chars:
            trunc = snippet[:max_chars]
            last_dot = trunc.rfind(".")
            if last_dot > max_chars // 2:
                trunc = trunc[: last_dot + 1]
            snippet = trunc + " [truncated]"
        return snippet
    return ""


def extract_qualitative(filing_dir: Path, form: str) -> QualitativeData:
    if not _BS4_AVAILABLE:
        return QualitativeData(form_type=form)

    qd = QualitativeData(form_type=form)

    meta_path = filing_dir / "metadata.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            qd.filing_date = meta.get("filing_date", "")
            qd.accession   = meta.get("accession", "")
        except Exception:
            pass

    html_files = sorted(
        [f for f in filing_dir.iterdir()
         if f.suffix.lower() in {".htm", ".html"} and f.stem != "index"],
        key=lambda f: f.stat().st_size,
        reverse=True,
    )
    if not html_files:
        return qd

    try:
        text = _html_to_text(html_files[0].read_bytes())
    except Exception as exc:
        log.warning("  HTML parse error in %s: %s", html_files[0].name, exc)
        return qd

    for attr, patterns in _SECTION_PATTERNS.items():
        setattr(qd, attr, _find_section(text, patterns, _MAX_SECTION_CHARS))

    return qd


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — ANALYSIS: RED FLAGS & REPORTING
# ══════════════════════════════════════════════════════════════════════════════

_GOING_CONCERN_RE    = re.compile(r"going.concern",              re.IGNORECASE)
_MATERIAL_WEAKNESS_RE= re.compile(r"material\s+weakness",        re.IGNORECASE)
_RESTATEMENT_RE      = re.compile(r"restat(?:ed|ement|ing)",     re.IGNORECASE)
_QUALIFIED_RE        = re.compile(r"\bqualified\s+opinion\b|\bdisclaimer\s+of\s+opinion\b", re.IGNORECASE)
_COVENANT_RE         = re.compile(r"covenant\s+(?:violation|default|waiver|breach)", re.IGNORECASE)


def detect_red_flags(m: FinancialMetrics, q: QualitativeData) -> list[RedFlag]:
    flags: list[RedFlag] = []

    def flag(severity: str, category: str, msg: str):
        flags.append(RedFlag(severity, category, msg))

    r = m.ratios

    cr = r.get("current_ratio")
    if cr is not None:
        if cr < 1.0:
            flag("WARNING", "Liquidity",
                 f"Current ratio below 1.0 ({cr:.2f}x) — may struggle with short-term obligations")
        elif cr < 1.2:
            flag("INFO", "Liquidity", f"Current ratio slightly tight ({cr:.2f}x)")

    qr = r.get("quick_ratio")
    if qr is not None and qr < 0.8:
        flag("WARNING", "Liquidity",
             f"Quick ratio low ({qr:.2f}x) — limited liquid assets vs current liabilities")

    ic = r.get("interest_coverage")
    if ic is not None:
        if ic < 1.5:
            flag("CRITICAL", "Solvency",
                 f"Interest coverage critically low ({ic:.2f}x) — earnings barely cover interest")
        elif ic < 3.0:
            flag("WARNING", "Solvency", f"Interest coverage below 3.0x ({ic:.2f}x)")

    de = r.get("debt_to_equity")
    if de is not None and de > 3.0:
        flag("WARNING", "Leverage", f"High debt-to-equity ({de:.2f}x)")

    if m.annual.get("net_income", 0) < 0:
        flag("WARNING", "Profitability",
             f"Net loss of ${abs(m.annual['net_income'])/1e6:,.0f}M in latest annual period")

    if m.annual.get("operating_cf", 0) < 0:
        flag("WARNING", "Cash Flow",
             f"Negative operating cash flow (${m.annual['operating_cf']/1e6:,.0f}M)")

    if r.get("free_cash_flow") is not None and r["free_cash_flow"] < 0:
        flag("INFO", "Cash Flow", f"Negative free cash flow (${r['free_cash_flow']/1e6:,.0f}M)")

    rev_t = m.annual_trend.get("revenue", [])
    if len(rev_t) >= 2:
        curr, prev = rev_t[-1]["value"], rev_t[-2]["value"]
        if prev and curr < prev * 0.90:
            flag("WARNING", "Revenue",
                 f"Revenue declined {(curr/prev-1)*100:.1f}% YoY "
                 f"(${curr/1e9:.1f}B vs ${prev/1e9:.1f}B)")

    gp_t  = m.annual_trend.get("gross_profit", [])
    rv_t  = m.annual_trend.get("revenue", [])
    if len(gp_t) >= 2 and len(rv_t) >= 2 and rv_t[-1]["value"] and rv_t[-2]["value"]:
        gm_c = gp_t[-1]["value"] / rv_t[-1]["value"]
        gm_p = gp_t[-2]["value"] / rv_t[-2]["value"]
        if (gm_c - gm_p) < -0.05:
            flag("WARNING", "Margin",
                 f"Gross margin contracted {(gm_c-gm_p)*100:.1f}pp YoY")

    all_text = " ".join([
        q.auditor_notes, q.controls_notes, q.mda_highlights, q.risk_factors
    ])

    if _GOING_CONCERN_RE.search(all_text):
        flag("CRITICAL", "Audit",
             "Going concern language detected — auditor doubts the company's ability to continue")

    if _MATERIAL_WEAKNESS_RE.search(all_text):
        flag("CRITICAL", "Internal Controls",
             "Material weakness in internal controls disclosed")

    if _RESTATEMENT_RE.search(q.mda_highlights):
        flag("CRITICAL", "Accounting",
             "Possible financial restatement referenced in MD&A")

    if _QUALIFIED_RE.search(q.auditor_notes):
        flag("CRITICAL", "Audit",
             "Qualified or disclaimer opinion from auditor — significant accounting issue")

    if _COVENANT_RE.search(all_text):
        flag("WARNING", "Debt",
             "Possible debt covenant violation, waiver, or breach mentioned")

    return sorted(flags, key=lambda f: {"CRITICAL": 0, "WARNING": 1, "INFO": 2}[f.severity])


# ── Report formatting helpers (CLI / Markdown) ─────────────────────────────────

def _fmt_md(v: Optional[float], unit: str = "USD") -> str:
    """Format a value for Markdown reports (returns 'N/A' for None)."""
    if v is None:
        return "N/A"
    if unit == "USD":
        if abs(v) >= 1e12: return f"${v/1e12:,.2f}T"
        if abs(v) >= 1e9:  return f"${v/1e9:,.1f}B"
        if abs(v) >= 1e6:  return f"${v/1e6:,.0f}M"
        return f"${v:,.0f}"
    if unit == "pct": return f"{v:.1f}%"
    if unit == "x":   return f"{v:.2f}x"
    return str(v)


def _trend_table(trend: list[dict], unit: str = "USD") -> str:
    if not trend:
        return "_No data_\n"
    rows = ["| Period | Value | YoY |", "|--------|-------|-----|"]
    for i, p in enumerate(trend):
        label = f"{p.get('fy', '')} {p.get('fp', '')}".strip() or p["end"]
        val   = _fmt_md(p["value"], unit)
        yoy   = "—"
        if i > 0 and trend[i - 1]["value"]:
            chg = (p["value"] / trend[i - 1]["value"] - 1) * 100
            yoy = f"{'▲' if chg >= 0 else '▼'} {abs(chg):.1f}%"
        rows.append(f"| {label} | {val} | {yoy} |")
    return "\n".join(rows) + "\n"


def _sig_cr(v: Optional[float]) -> str:
    if v is None: return "N/A"
    return "✅ Healthy" if v >= 1.5 else ("⚠️ Tight" if v >= 1.0 else "🔴 Below 1.0")


def _sig_ic(v: Optional[float]) -> str:
    if v is None: return "N/A"
    return "✅ Strong" if v >= 5 else ("⚠️ Adequate" if v >= 2 else "🔴 Critical")


def _sig_de(v: Optional[float]) -> str:
    if v is None: return "N/A"
    return "✅ Conservative" if v < 1.0 else ("⚠️ Elevated" if v < 3.0 else "🔴 High")


def _sig_margin(v: Optional[float], good: float, warn: float) -> str:
    if v is None: return "N/A"
    return "✅ Strong" if v >= good else ("⚠️ Thin" if v >= warn else "🔴 Very Thin / Loss")


def generate_markdown(ca: CompanyAnalysis) -> str:
    m = ca.metrics
    q = ca.qualitative
    r = m.ratios

    def av(k): return m.annual.get(k)
    def qv(k): return m.quarterly.get(k)
    def bsv(k): return qv(k) or av(k)

    rev   = av("revenue")
    gp    = av("gross_profit")
    oi    = av("operating_income")
    ni    = av("net_income")
    ocf   = av("operating_cf")
    capex = av("capex")
    fcf   = r.get("free_cash_flow")
    ta    = bsv("total_assets")
    eq    = bsv("stockholders_equity")
    cash  = bsv("cash")
    ltd   = bsv("long_term_debt")
    std   = bsv("short_term_debt")
    total_debt = ((ltd or 0) + (std or 0)) if (ltd or std) else None

    L: list[str] = []
    A = L.append

    A(f"# {ca.ticker} — {ca.name}")
    A(f"**CIK:** {ca.cik} | **Analysis Date:** {ca.analysis_date} | "
      f"**Latest Annual:** FY{m.latest_annual_fy} (period end: {m.latest_annual_date})")
    A("")

    if ca.red_flags:
        A("---")
        A("## Red Flags / Warnings")
        A("")
        icons = {"CRITICAL": "🔴", "WARNING": "🟡", "INFO": "🔵"}
        for rf in ca.red_flags:
            A(f"- {icons.get(rf.severity, '')} **[{rf.severity}]** `{rf.category}` — {rf.message}")
        A("")

    if q.business_overview:
        A("---")
        A("## Business Overview")
        A("")
        A(q.business_overview[:1500])
        A("")

    A("---")
    A(f"## Financial Snapshot — Annual (FY{m.latest_annual_fy})")
    A("")
    A("| Metric | Value |")
    A("|--------|-------|")
    for label, val in [
        ("Revenue",          _fmt_md(rev)),
        ("Gross Profit",     _fmt_md(gp)),
        ("Gross Margin",     _fmt_md(r.get("gross_margin_pct"), "pct")),
        ("Operating Income", _fmt_md(oi)),
        ("Operating Margin", _fmt_md(r.get("operating_margin_pct"), "pct")),
        ("Net Income",       _fmt_md(ni)),
        ("Net Margin",       _fmt_md(r.get("net_margin_pct"), "pct")),
        ("EPS (Diluted)",    _fmt_md(av("eps_diluted"))),
        ("EBITDA",           _fmt_md(r.get("ebitda"))),
        ("R&D Expense",      _fmt_md(av("rd_expense"))),
    ]:
        A(f"| {label} | {val} |")
    A("")

    A("---")
    A("## Balance Sheet (Most Recent Quarter)")
    A("")
    A("| Metric | Value |")
    A("|--------|-------|")
    for label, val in [
        ("Total Assets",        _fmt_md(ta)),
        ("Cash & Equivalents",  _fmt_md(cash)),
        ("Accounts Receivable", _fmt_md(bsv("accounts_receivable"))),
        ("Inventory",           _fmt_md(bsv("inventory"))),
        ("Current Assets",      _fmt_md(bsv("current_assets"))),
        ("Current Liabilities", _fmt_md(bsv("current_liabilities"))),
        ("Total Liabilities",   _fmt_md(bsv("total_liabilities"))),
        ("Long-Term Debt",      _fmt_md(ltd)),
        ("Total Debt",          _fmt_md(total_debt)),
        ("Stockholders' Equity",_fmt_md(eq)),
        ("Retained Earnings",   _fmt_md(bsv("retained_earnings"))),
    ]:
        A(f"| {label} | {val} |")
    A("")

    A("---")
    A("## Key Financial Ratios")
    A("")
    A("| Ratio | Value | Signal |")
    A("|-------|-------|--------|")
    A("| **Liquidity** | | |")
    A(f"| Current Ratio | {_fmt_md(r.get('current_ratio'), 'x')} | {_sig_cr(r.get('current_ratio'))} |")
    A(f"| Quick Ratio | {_fmt_md(r.get('quick_ratio'), 'x')} | {_sig_cr(r.get('quick_ratio'))} |")
    A(f"| Cash Ratio | {_fmt_md(r.get('cash_ratio'), 'x')} | — |")
    A("| **Profitability** | | |")
    A(f"| Gross Margin | {_fmt_md(r.get('gross_margin_pct'), 'pct')} | {_sig_margin(r.get('gross_margin_pct'), 40, 20)} |")
    A(f"| Operating Margin | {_fmt_md(r.get('operating_margin_pct'), 'pct')} | {_sig_margin(r.get('operating_margin_pct'), 15, 5)} |")
    A(f"| Net Margin | {_fmt_md(r.get('net_margin_pct'), 'pct')} | {_sig_margin(r.get('net_margin_pct'), 10, 3)} |")
    A(f"| Return on Equity | {_fmt_md(r.get('roe_pct'), 'pct')} | — |")
    A(f"| Return on Assets | {_fmt_md(r.get('roa_pct'), 'pct')} | — |")
    A("| **Leverage** | | |")
    A(f"| Debt-to-Equity | {_fmt_md(r.get('debt_to_equity'), 'x')} | {_sig_de(r.get('debt_to_equity'))} |")
    A(f"| Debt-to-Assets | {_fmt_md(r.get('debt_to_assets'), 'x')} | — |")
    A(f"| Interest Coverage | {_fmt_md(r.get('interest_coverage'), 'x')} | {_sig_ic(r.get('interest_coverage'))} |")
    A(f"| Net Debt | {_fmt_md(r.get('net_debt'))} | — |")
    A("| **Cash Flow** | | |")
    A(f"| Operating Cash Flow | {_fmt_md(ocf)} | — |")
    A(f"| Capital Expenditures | {_fmt_md(capex)} | — |")
    A(f"| Free Cash Flow | {_fmt_md(fcf)} | — |")
    A(f"| FCF Margin | {_fmt_md(r.get('fcf_margin_pct'), 'pct')} | — |")
    A(f"| OCF / Current Liabilities | {_fmt_md(r.get('ocf_to_cl'), 'x')} | — |")
    A("| **Efficiency** | | |")
    A(f"| Asset Turnover | {_fmt_md(r.get('asset_turnover'), 'x')} | — |")
    A(f"| Inventory Turnover | {_fmt_md(r.get('inventory_turnover'), 'x')} | — |")
    A(f"| Days Sales Outstanding | {_fmt_md(r.get('dso_days'))} days | — |")
    A("")

    for title, key in [
        ("Revenue", "revenue"),
        ("Net Income", "net_income"),
    ]:
        if m.annual_trend.get(key):
            A("---")
            A(f"## {title} Trend — Annual (up to 10 Years)")
            A("")
            A(_trend_table(m.annual_trend[key]))

    gp_t = m.annual_trend.get("gross_profit", [])
    rv_t = m.annual_trend.get("revenue", [])
    if gp_t and rv_t and len(gp_t) == len(rv_t):
        A("---")
        A("## Gross Margin Trend — Annual")
        A("")
        A("| Period | Gross Margin |")
        A("|--------|-------------|")
        for gpt, rvt in zip(gp_t, rv_t):
            if rvt["value"]:
                gm = gpt["value"] / rvt["value"] * 100
                A(f"| {gpt.get('fy', '')} {gpt.get('fp', '')} | {gm:.1f}% |")
        A("")

    ocf_t = m.annual_trend.get("operating_cf", [])
    cap_t = m.annual_trend.get("capex", [])
    if ocf_t and cap_t:
        n = min(len(ocf_t), len(cap_t))
        A("---")
        A("## Free Cash Flow Trend — Annual")
        A("")
        A("| Period | OCF | CapEx | FCF |")
        A("|--------|-----|-------|-----|")
        for o, c in zip(ocf_t[-n:], cap_t[-n:]):
            label = f"{o.get('fy', '')} {o.get('fp', '')}".strip()
            A(f"| {label} | {_fmt_md(o['value'])} | {_fmt_md(c['value'])} | {_fmt_md(o['value'] - c['value'])} |")
        A("")

    if m.quarterly_trend.get("revenue"):
        A("---")
        A("## Revenue Trend — Quarterly (Last 4 Quarters)")
        A("")
        A(_trend_table(m.quarterly_trend["revenue"]))

    for title, text, maxc in [
        ("Management Discussion & Analysis (MD&A) — Highlights", q.mda_highlights,   2500),
        ("Risk Factors (Excerpt)",                               q.risk_factors,      2500),
        ("Auditor's Report — Key Notes",                        q.auditor_notes,     1500),
        ("Controls & Procedures",                               q.controls_notes,    1000),
        ("Legal Proceedings",                                   q.legal_proceedings, 1000),
    ]:
        if text:
            A("---")
            A(f"## {title}")
            A("")
            A(text[:maxc])
            A("")

    A("---")
    A(f"*Generated by Stock Simplify | {ca.analysis_date}*")

    return "\n".join(L)


def analysis_to_json(ca: CompanyAnalysis) -> dict:
    return {
        "ticker":                      ca.ticker,
        "name":                        ca.name,
        "cik":                         ca.cik,
        "analysis_date":               ca.analysis_date,
        "latest_annual_fy":            ca.metrics.latest_annual_fy,
        "latest_annual_period_end":    ca.metrics.latest_annual_date,
        "latest_quarterly_period_end": ca.metrics.latest_quarterly_date,
        "annual_metrics":              ca.metrics.annual,
        "quarterly_metrics":           ca.metrics.quarterly,
        "ratios":                      ca.metrics.ratios,
        "annual_trends":               ca.metrics.annual_trend,
        "quarterly_trends":            ca.metrics.quarterly_trend,
        "qualitative":                 asdict(ca.qualitative),
        "red_flags":                   [asdict(f) for f in ca.red_flags],
    }


_CSV_FIELDS = [
    "ticker", "name", "cik", "latest_annual_fy",
    "revenue", "gross_margin_pct", "operating_margin_pct", "net_margin_pct",
    "eps_diluted", "ebitda",
    "current_ratio", "quick_ratio", "cash_ratio",
    "debt_to_equity", "interest_coverage", "net_debt",
    "free_cash_flow", "fcf_margin_pct", "operating_cf",
    "roe_pct", "roa_pct",
    "total_assets", "cash",
    "red_flag_count", "critical_flag_count",
]


def analysis_to_csv_row(ca: CompanyAnalysis) -> dict:
    m = ca.metrics
    r = m.ratios
    row: dict = {
        "ticker":           ca.ticker,
        "name":             ca.name,
        "cik":              ca.cik,
        "latest_annual_fy": m.latest_annual_fy,
        "revenue":          m.annual.get("revenue"),
        "eps_diluted":      m.annual.get("eps_diluted"),
        "ebitda":           r.get("ebitda"),
        "operating_cf":     m.annual.get("operating_cf"),
        "total_assets":     m.quarterly.get("total_assets") or m.annual.get("total_assets"),
        "cash":             m.quarterly.get("cash") or m.annual.get("cash"),
        "red_flag_count":   len(ca.red_flags),
        "critical_flag_count": sum(1 for f in ca.red_flags if f.severity == "CRITICAL"),
    }
    for k in [
        "gross_margin_pct", "operating_margin_pct", "net_margin_pct",
        "fcf_margin_pct", "current_ratio", "quick_ratio", "cash_ratio",
        "debt_to_equity", "interest_coverage", "net_debt", "free_cash_flow",
        "roe_pct", "roa_pct",
    ]:
        row[k] = r.get(k)
    return row


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — ORCHESTRATION (used by both CLI and GUI)
# ══════════════════════════════════════════════════════════════════════════════

def _best_qualitative_from_dir(company_dir: Path) -> QualitativeData:
    for form in ("10-K", "10-Q"):
        form_dir = company_dir / form
        if not form_dir.exists():
            continue
        for folder in sorted(form_dir.iterdir(), reverse=True):
            if not folder.is_dir():
                continue
            q = extract_qualitative(folder, form)
            if q.business_overview or q.mda_highlights:
                return q
    return QualitativeData()


def analyze_company(
    ticker: str,
    cik_padded: str,
    name: str,
    output_dir: Path,
    skip_qualitative: bool = False,
) -> CompanyAnalysis:
    today = datetime.now().strftime("%Y-%m-%d")

    log.info("  Fetching XBRL company facts…")
    facts   = get(XBRL_COMPANY_FACTS.format(cik=cik_padded)).json()
    metrics = build_financial_metrics(facts)
    metrics.company_name = name or metrics.company_name

    qualitative = QualitativeData()
    if not skip_qualitative and _BS4_AVAILABLE:
        company_dir = output_dir / ticker
        if company_dir.exists():
            qualitative = _best_qualitative_from_dir(company_dir)

    red_flags = detect_red_flags(metrics, qualitative)

    return CompanyAnalysis(
        ticker=ticker,
        name=name or metrics.company_name,
        cik=cik_padded,
        analysis_date=today,
        metrics=metrics,
        qualitative=qualitative,
        red_flags=red_flags,
    )


def write_analysis(ca: CompanyAnalysis, output_dir: Path, formats: list[str]) -> None:
    company_dir = output_dir / ca.ticker
    company_dir.mkdir(parents=True, exist_ok=True)
    if "json" in formats:
        (company_dir / "analysis.json").write_text(
            json.dumps(analysis_to_json(ca), indent=2, default=str),
            encoding="utf-8",
        )
    if "markdown" in formats:
        (company_dir / "analysis.md").write_text(
            generate_markdown(ca),
            encoding="utf-8",
        )


# ══════════════════════════════════════════════════════════════════════════════
# GUI — FORMATTING HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _fmt(v: Optional[float], unit: str = "USD") -> str:
    """Format a financial value for GUI display (returns '—' for None)."""
    if v is None:
        return "—"
    if unit == "USD":
        if abs(v) >= 1e12: return f"${v/1e12:,.2f}T"
        if abs(v) >= 1e9:  return f"${v/1e9:,.2f}B"
        if abs(v) >= 1e6:  return f"${v/1e6:,.0f}M"
        return f"${v:,.0f}"
    if unit == "pct":    return f"{v:.1f}%"
    if unit == "x":      return f"{v:.2f}x"
    if unit == "eps":    return f"${v:.2f}"
    if unit == "shares":
        if abs(v) >= 1e9: return f"{v/1e9:.2f}B"
        if abs(v) >= 1e6: return f"{v/1e6:.0f}M"
        return f"{v:,.0f}"
    return f"{v:,.2f}"


# ══════════════════════════════════════════════════════════════════════════════
# GUI — DATA EXTRACTION  (reorganise FinancialMetrics into table rows)
# ══════════════════════════════════════════════════════════════════════════════

def get_fiscal_years(
    metrics: FinancialMetrics,
    min_year: Optional[int] = None,
    max_year: Optional[int] = None,
) -> list[int]:
    """Return fiscal years (newest first) across ALL concepts, optionally filtered."""
    all_years: set[int] = set()
    for trend in metrics.annual_trend.values():
        for p in trend:
            fy = p.get("fy")
            if fy:
                all_years.add(fy)
    if min_year is not None:
        all_years = {y for y in all_years if y >= min_year}
    if max_year is not None:
        all_years = {y for y in all_years if y <= max_year}
    return sorted(all_years, reverse=True)


def _av(metrics: FinancialMetrics, key: str, fy: int) -> Optional[float]:
    """Return the annual value for `key` in fiscal year `fy`."""
    for p in metrics.annual_trend.get(key, []):
        if p.get("fy") == fy:
            return p["value"]
    return None


# Row tuple: (label, [val_fy1, val_fy2, …], unit, is_section_header)
Row = tuple[str, Optional[list], Optional[str], bool]


def _section(label: str) -> Row:
    return (label, None, None, True)


def build_income_rows(metrics: FinancialMetrics, years: list[int]) -> list[Row]:
    def v(key):    return [_av(metrics, key, y) for y in years]
    def derived(fn): return [fn(y) for y in years]

    gm = derived(lambda y: (
        (_av(metrics, "gross_profit", y) / _av(metrics, "revenue", y) * 100)
        if _av(metrics, "revenue", y) and _av(metrics, "gross_profit", y) else None
    ))
    om = derived(lambda y: (
        (_av(metrics, "operating_income", y) / _av(metrics, "revenue", y) * 100)
        if _av(metrics, "revenue", y) and _av(metrics, "operating_income", y) else None
    ))
    nm = derived(lambda y: (
        (_av(metrics, "net_income", y) / _av(metrics, "revenue", y) * 100)
        if _av(metrics, "revenue", y) and _av(metrics, "net_income", y) else None
    ))
    ebitda = derived(lambda y: (
        ((_av(metrics, "operating_income", y) or 0) + (_av(metrics, "da", y) or 0))
        if _av(metrics, "operating_income", y) is not None else None
    ))

    return [
        _section("── Revenue ─────────────────────────────────"),
        ("Revenue",             v("revenue"),          "USD",  False),
        ("Cost of Revenue",     v("cost_of_revenue"),  "USD",  False),
        ("Gross Profit",        v("gross_profit"),     "USD",  False),
        ("Gross Margin",        gm,                    "pct",  False),
        _section("── Operating Expenses ──────────────────────"),
        ("R&D Expense",         v("rd_expense"),       "USD",  False),
        ("Operating Income",    v("operating_income"), "USD",  False),
        ("Operating Margin",    om,                    "pct",  False),
        _section("── Net Income ─────────────────────────────"),
        ("Net Income",          v("net_income"),       "USD",  False),
        ("Net Margin",          nm,                    "pct",  False),
        ("EPS (Basic)",         v("eps_basic"),        "eps",  False),
        ("EPS (Diluted)",       v("eps_diluted"),      "eps",  False),
        _section("── Other ──────────────────────────────────"),
        ("EBITDA",              ebitda,                "USD",  False),
        ("D&A",                 v("da"),               "USD",  False),
        ("Income Tax",          v("income_tax"),       "USD",  False),
    ]


def build_balance_rows(metrics: FinancialMetrics, years: list[int]) -> list[Row]:
    def v(key): return [_av(metrics, key, y) for y in years]

    total_debt = [
        (
            ((_av(metrics, "long_term_debt", y) or 0) +
             (_av(metrics, "short_term_debt", y) or 0))
            if (_av(metrics, "long_term_debt", y) is not None
                or _av(metrics, "short_term_debt", y) is not None)
            else None
        )
        for y in years
    ]

    return [
        _section("── Assets ──────────────────────────────────"),
        ("Cash & Equivalents",    v("cash"),                "USD",    False),
        ("Accounts Receivable",   v("accounts_receivable"), "USD",    False),
        ("Inventory",             v("inventory"),           "USD",    False),
        ("Current Assets",        v("current_assets"),      "USD",    False),
        ("Total Assets",          v("total_assets"),        "USD",    False),
        _section("── Liabilities ───────────────────────────"),
        ("Current Liabilities",   v("current_liabilities"), "USD",    False),
        ("Short-Term Debt",       v("short_term_debt"),     "USD",    False),
        ("Long-Term Debt",        v("long_term_debt"),      "USD",    False),
        ("Total Debt",            total_debt,               "USD",    False),
        ("Total Liabilities",     v("total_liabilities"),   "USD",    False),
        _section("── Equity ─────────────────────────────────"),
        ("Stockholders' Equity",  v("stockholders_equity"), "USD",    False),
        ("Retained Earnings",     v("retained_earnings"),   "USD",    False),
        ("Shares Outstanding",    v("shares_outstanding"),  "shares", False),
    ]


def build_cashflow_rows(metrics: FinancialMetrics, years: list[int]) -> list[Row]:
    def v(key): return [_av(metrics, key, y) for y in years]

    fcf = [
        ((_av(metrics, "operating_cf", y) - _av(metrics, "capex", y))
         if _av(metrics, "operating_cf", y) is not None
         and _av(metrics, "capex", y) is not None
         else None)
        for y in years
    ]
    fcf_margin = [
        ((f / _av(metrics, "revenue", y) * 100)
         if f is not None and _av(metrics, "revenue", y)
         else None)
        for f, y in zip(fcf, years)
    ]

    return [
        _section("── Operating Activities ─────────────────────"),
        ("Operating Cash Flow",   v("operating_cf"),       "USD", False),
        _section("── Investing Activities ─────────────────────"),
        ("Capital Expenditures",  v("capex"),              "USD", False),
        ("Investing Cash Flow",   v("investing_cf"),       "USD", False),
        _section("── Free Cash Flow ────────────────────────"),
        ("Free Cash Flow",        fcf,                     "USD", False),
        ("FCF Margin",            fcf_margin,              "pct", False),
        _section("── Financing Activities ─────────────────────"),
        ("Financing Cash Flow",   v("financing_cf"),       "USD", False),
        ("Dividends Paid",        v("dividends_paid"),     "USD", False),
        ("Share Repurchases",     v("share_repurchases"),  "USD", False),
    ]


def build_ratios_rows(metrics: FinancialMetrics, years: list[int]) -> list[Row]:
    def pct(nk, dk, y):
        n, d = _av(metrics, nk, y), _av(metrics, dk, y)
        return (n / d * 100) if (n is not None and d) else None

    def rat(nk, dk, y):
        n, d = _av(metrics, nk, y), _av(metrics, dk, y)
        return (n / d) if (n is not None and d) else None

    gm  = [pct("gross_profit",     "revenue",               y) for y in years]
    om  = [pct("operating_income", "revenue",               y) for y in years]
    nm  = [pct("net_income",       "revenue",               y) for y in years]
    roe = [pct("net_income",       "stockholders_equity",   y) for y in years]
    roa = [pct("net_income",       "total_assets",          y) for y in years]
    cr  = [rat("current_assets",   "current_liabilities",   y) for y in years]
    ic  = [rat("operating_income", "interest_expense",      y) for y in years]
    at  = [rat("revenue",          "total_assets",          y) for y in years]
    inv = [
        (
            _av(metrics, "cost_of_revenue", y) / _av(metrics, "inventory", y)
            if _av(metrics, "cost_of_revenue", y) and _av(metrics, "inventory", y)
            else None
        )
        for y in years
    ]
    fcf_m = [
        (
            ((_av(metrics, "operating_cf", y) - _av(metrics, "capex", y)) /
             _av(metrics, "revenue", y) * 100)
            if (_av(metrics, "operating_cf", y) is not None
                and _av(metrics, "capex", y) is not None
                and _av(metrics, "revenue", y))
            else None
        )
        for y in years
    ]
    de = [
        (
            ((_av(metrics, "long_term_debt", y) or 0) +
             (_av(metrics, "short_term_debt", y) or 0)) /
            _av(metrics, "stockholders_equity", y)
            if _av(metrics, "stockholders_equity", y) and
               (_av(metrics, "long_term_debt", y) is not None or
                _av(metrics, "short_term_debt", y) is not None)
            else None
        )
        for y in years
    ]
    qr = [
        (
            ((_av(metrics, "current_assets", y) or 0) -
             (_av(metrics, "inventory", y) or 0)) /
            _av(metrics, "current_liabilities", y)
            if _av(metrics, "current_assets", y) is not None
            and _av(metrics, "current_liabilities", y)
            else None
        )
        for y in years
    ]

    return [
        _section("── Profitability ───────────────────────────"),
        ("Gross Margin",      gm,    "pct", False),
        ("Operating Margin",  om,    "pct", False),
        ("Net Margin",        nm,    "pct", False),
        ("FCF Margin",        fcf_m, "pct", False),
        ("Return on Equity",  roe,   "pct", False),
        ("Return on Assets",  roa,   "pct", False),
        _section("── Liquidity ─────────────────────────────"),
        ("Current Ratio",     cr,    "x",   False),
        ("Quick Ratio",       qr,    "x",   False),
        _section("── Leverage ──────────────────────────────"),
        ("Debt-to-Equity",    de,    "x",   False),
        ("Interest Coverage", ic,    "x",   False),
        _section("── Efficiency ────────────────────────────"),
        ("Asset Turnover",    at,    "x",   False),
        ("Inventory Turnover",inv,   "x",   False),
    ]


# ══════════════════════════════════════════════════════════════════════════════
# NEWS / RSS HELPERS
# ══════════════════════════════════════════════════════════════════════════════

# ── Sentiment keyword sets ────────────────────────────────────────────────────
_SENT_POS = frozenset({
    "bull", "bullish", "buy", "bought", "long", "call", "calls",
    "moon", "rocket", "squeeze", "beat", "strong", "growth", "breakout",
    "rally", "surge", "profit", "gain", "undervalued", "opportunity",
    "upside", "outperform", "overweight", "upgrade", "cheap", "loading",
    "dip", "hold", "great", "excellent", "amazing", "underrated",
    "green", "positive", "boom", "run", "win", "winning", "confident",
    "accumulate", "oversold", "support", "bounce", "reversal",
})
_SENT_NEG = frozenset({
    "bear", "bearish", "sell", "sold", "short", "put", "puts", "crash",
    "dump", "miss", "weak", "decline", "downgrade", "drop", "overvalued",
    "fraud", "lawsuit", "loss", "collapse", "correction", "warning",
    "avoid", "scared", "worried", "concern", "bankrupt", "debt",
    "underperform", "negative", "terrible", "awful", "scam", "red",
    "danger", "down", "falling", "fell", "overbought", "resistance",
    "bloated", "dilution", "diluted", "layoffs", "recall",
})


def _score_text(text: str) -> tuple[int, int]:
    """Return (pos_count, neg_count) based on financial keyword matching."""
    words = re.findall(r'\b\w+\b', text.lower())
    pos = sum(1 for w in words if w in _SENT_POS)
    neg = sum(1 for w in words if w in _SENT_NEG)
    return pos, neg


def _parse_rss(content: bytes) -> list[dict]:
    """Parse an RSS 2.0 feed and return up to 10 article dicts."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []

    articles = []
    for item in root.findall(".//item")[:10]:
        title = (item.findtext("title") or "").strip()
        link  = (item.findtext("link")  or "").strip()
        desc  = (item.findtext("description") or "").strip()
        pub   = (item.findtext("pubDate") or "").strip()

        # Strip residual HTML tags from description
        desc = re.sub(r"<[^>]+>", "", desc).strip()
        # Collapse whitespace
        desc = re.sub(r"\s+", " ", desc)
        if len(desc) > 320:
            desc = desc[:317] + "…"

        # Parse and reformat the publication date
        pub_fmt = pub
        try:
            pub_fmt = parsedate_to_datetime(pub).strftime("%b %d, %Y  %H:%M")
        except Exception:
            pass

        articles.append({
            "title":       title,
            "link":        link,
            "description": desc,
            "pubDate":     pub_fmt,
        })
    return articles


# ══════════════════════════════════════════════════════════════════════════════
# GUI — MAIN APPLICATION
# ══════════════════════════════════════════════════════════════════════════════

class EdgarApp(tk.Frame):
    C = {
        "bg":          "#000000",
        "header_bg":   "#000000",
        "header_fg":   "#00ccff",
        "subhead_fg":  "#2a6a8a",
        "search_bg":   "#0d1728",
        "info_bg":     "#000000",
        "info_fg":     "#00aadd",
        "accent":      "#0077ee",
        "section_bg":  "#0a0a0a",
        "section_fg":  "#00bbdd",
        "row_odd":     "#080808",
        "row_even":    "#050505",
        "positive":    "#00e676",
        "negative":    "#ff3355",
        "border":      "#152035",
        "status_bg":   "#000000",
        "status_fg":   "#2a6a8a",
        "hud_bg":      "#000000",
    }

    def __init__(self, master: tk.Tk):
        super().__init__(master, bg=self.C["bg"])
        self.pack(fill=tk.BOTH, expand=True)

        self._company_list: dict[str, dict] = {}
        self._queue: queue.Queue = queue.Queue()
        self._charts: dict[str, tuple] = {}   # tab_name -> (Figure, FigureCanvasTkAgg)

        # Stored state for filter re-render without re-fetching
        self._current_metrics: Optional[FinancialMetrics] = None
        self._current_flags: list = []
        self._current_ticker: str = ""
        self._current_company: dict = {}

        self._setup_styles()
        self._build_ui()
        self._start_load_companies()
        self._start_ticker_poll()
        self._poll_queue()

        master.bind("<Control-f>", lambda _: self._combo.focus_set())
        master.bind("<Control-F>", lambda _: self._combo.focus_set())

    # ── Styles ────────────────────────────────────────────────────────────────

    def _setup_styles(self) -> None:
        s = ttk.Style()
        s.theme_use("clam")

        s.configure("Financial.Treeview",
            background=self.C["row_odd"],
            foreground="#a8c8e8",
            fieldbackground=self.C["row_odd"],
            rowheight=26,
            font=("Consolas", 11),
            borderwidth=0,
        )
        s.configure("Financial.Treeview.Heading",
            background=self.C["header_bg"],
            foreground=self.C["header_fg"],
            font=("Segoe UI", 11, "bold"),
            relief="flat",
            padding=(6, 5),
        )
        s.map("Financial.Treeview",
            background=[("selected", self.C["accent"])],
            foreground=[("selected", "#ffffff")],
        )
        s.configure("Accent.TButton",
            background=self.C["accent"],
            foreground="#ffffff",
            font=("Segoe UI", 12, "bold"),
            padding=(14, 6),
            relief="flat",
        )
        s.map("Accent.TButton",
            background=[("active", "#005acc"), ("pressed", "#0044aa")],
        )
        s.configure("TButton",
            background=self.C["border"],
            foreground=self.C["header_fg"],
            font=("Segoe UI", 11),
            padding=(8, 5),
        )
        s.map("TButton",
            background=[("active", self.C["section_bg"])],
            foreground=[("active", self.C["header_fg"])],
        )
        s.configure("TNotebook",
            background=self.C["bg"],
            borderwidth=0,
        )
        s.configure("TNotebook.Tab",
            font=("Segoe UI", 11, "bold"),
            padding=(16, 8),
            background=self.C["border"],
            foreground=self.C["subhead_fg"],
        )
        s.map("TNotebook.Tab",
            background=[("selected", self.C["section_bg"])],
            foreground=[("selected", self.C["header_fg"])],
            padding=[("selected", (16, 10))],
        )
        s.configure("TCombobox",
            fieldbackground=self.C["row_odd"],
            background=self.C["border"],
            foreground=self.C["header_fg"],
            selectbackground=self.C["accent"],
            selectforeground="#ffffff",
        )
        s.map("TCombobox",
            fieldbackground=[("readonly", self.C["row_odd"])],
            foreground=[("readonly", self.C["header_fg"])],
        )
        s.configure("TPanedwindow", background=self.C["border"])

    # ── UI Construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._build_header()
        self._build_ticker_bar()
        self._build_search_bar()
        self._build_filter_bar()
        self._build_info_bar()
        self._build_price_bar()
        self._build_notebook()
        self._build_status_bar()

    def _build_header(self) -> None:
        hdr = tk.Frame(self, bg=self.C["header_bg"], pady=8)
        hdr.pack(fill=tk.X)

        # Left: title block
        title_block = tk.Frame(hdr, bg=self.C["header_bg"])
        title_block.pack(side=tk.LEFT, padx=12)

        tk.Label(
            title_block,
            text="◈  STOCK SIMPLIFY",
            bg=self.C["header_bg"], fg=self.C["header_fg"],
            font=("Consolas", 17, "bold"),
        ).pack(side=tk.LEFT)

        tk.Label(
            title_block,
            text="  //  EDGAR FINANCIAL ANALYZER",
            bg=self.C["header_bg"], fg=self.C["subhead_fg"],
            font=("Consolas", 12),
        ).pack(side=tk.LEFT)

        # Right: version tag
        tk.Label(
            hdr,
            text="SEC EDGAR XBRL  ▸  v2.0  ",
            bg=self.C["header_bg"], fg=self.C["subhead_fg"],
            font=("Consolas", 10),
        ).pack(side=tk.RIGHT)

    def _build_ticker_bar(self) -> None:
        """Live market-index strip shown directly below the header."""
        bar = tk.Frame(self, bg=self.C["header_bg"], padx=12, pady=6)
        bar.pack(fill=tk.X)

        tk.Label(
            bar, text="LIVE MARKETS ▸",
            bg=self.C["header_bg"], fg=self.C["subhead_fg"],
            font=("Consolas", 10, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 14))

        self._ticker_widgets: dict[str, tuple] = {}

        for i, (symbol, short, _) in enumerate(self._MARKET_INDICES):
            if i > 0:
                tk.Label(
                    bar, text="│",
                    bg=self.C["header_bg"], fg=self.C["border"],
                    font=("Consolas", 20),
                ).pack(side=tk.LEFT, padx=8)

            blk = tk.Frame(bar, bg=self.C["header_bg"])
            blk.pack(side=tk.LEFT)

            tk.Label(
                blk, text=short,
                bg=self.C["header_bg"], fg=self.C["subhead_fg"],
                font=("Consolas", 9, "bold"),
            ).grid(row=0, column=0, sticky="w")

            price_lbl = tk.Label(
                blk, text="  ——————  ",
                bg=self.C["header_bg"], fg=self.C["header_fg"],
                font=("Consolas", 14, "bold"),
            )
            price_lbl.grid(row=1, column=0, sticky="w")

            chg_lbl = tk.Label(
                blk, text="  loading…",
                bg=self.C["header_bg"], fg=self.C["subhead_fg"],
                font=("Consolas", 10),
            )
            chg_lbl.grid(row=2, column=0, sticky="w")

            self._ticker_widgets[symbol] = (price_lbl, chg_lbl)

        # Right-aligned timestamp
        self._ticker_ts_var = tk.StringVar(value="")
        tk.Label(
            bar, textvariable=self._ticker_ts_var,
            bg=self.C["header_bg"], fg=self.C["border"],
            font=("Consolas", 9),
        ).pack(side=tk.RIGHT)

    def _build_search_bar(self) -> None:
        sf = tk.Frame(self, bg=self.C["search_bg"], pady=9, padx=14)
        sf.pack(fill=tk.X)

        tk.Label(
            sf, text="▶  TICKER / COMPANY:", bg=self.C["search_bg"],
            fg=self.C["subhead_fg"], font=("Consolas", 11, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 7))

        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", self._on_type)

        self._combo = ttk.Combobox(
            sf, textvariable=self._search_var,
            width=42, font=("Consolas", 13),
        )
        self._combo.pack(side=tk.LEFT, padx=(0, 8))
        self._combo.bind("<Return>", self._on_search)
        self._combo.bind("<<ComboboxSelected>>", self._on_combo_select)

        self._search_btn = ttk.Button(
            sf, text="▶  SCAN", style="Accent.TButton",
            command=self._on_search,
        )
        self._search_btn.pack(side=tk.LEFT, padx=(0, 6))

        ttk.Button(sf, text="✕  CLEAR", command=self._clear).pack(side=tk.LEFT)

        self._load_lbl = tk.Label(
            sf, text="◌  Indexing SEC database…",
            bg=self.C["search_bg"], fg=self.C["subhead_fg"],
            font=("Consolas", 10, "italic"),
        )
        self._load_lbl.pack(side=tk.LEFT, padx=14)

        tk.Label(
            sf, text="Ctrl+F",
            bg=self.C["search_bg"], fg=self.C["subhead_fg"],
            font=("Consolas", 10),
        ).pack(side=tk.RIGHT, padx=8)

    def _build_filter_bar(self) -> None:
        current_yr = _dt.now().year
        bar = tk.Frame(self, bg=self.C["search_bg"], pady=5, padx=14)
        bar.pack(fill=tk.X)

        tk.Label(
            bar, text="YEAR RANGE:", bg=self.C["search_bg"],
            fg=self.C["subhead_fg"], font=("Consolas", 10, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 6))

        tk.Label(bar, text="FROM", bg=self.C["search_bg"],
                 fg=self.C["subhead_fg"], font=("Consolas", 10)).pack(side=tk.LEFT, padx=(0, 3))
        self._min_year_var = tk.IntVar(value=current_yr - 9)
        self._min_spin = tk.Spinbox(
            bar, from_=1993, to=current_yr,
            textvariable=self._min_year_var,
            width=6, font=("Consolas", 11),
            state="readonly",
            bg=self.C["row_even"], fg=self.C["header_fg"],
            readonlybackground=self.C["row_even"],
            buttonbackground=self.C["border"],
            relief="flat",
        )
        self._min_spin.pack(side=tk.LEFT, padx=(0, 10))

        tk.Label(bar, text="TO", bg=self.C["search_bg"],
                 fg=self.C["subhead_fg"], font=("Consolas", 10)).pack(side=tk.LEFT, padx=(0, 3))
        self._max_year_var = tk.IntVar(value=current_yr)
        self._max_spin = tk.Spinbox(
            bar, from_=1993, to=current_yr,
            textvariable=self._max_year_var,
            width=6, font=("Consolas", 11),
            state="readonly",
            bg=self.C["row_even"], fg=self.C["header_fg"],
            readonlybackground=self.C["row_even"],
            buttonbackground=self.C["border"],
            relief="flat",
        )
        self._max_spin.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Button(bar, text="APPLY", command=self._apply_filter).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(bar, text="RESET", command=self._reset_filter).pack(side=tk.LEFT, padx=(0, 14))

        self._avail_years_lbl = tk.Label(
            bar, text="",
            bg=self.C["search_bg"], fg=self.C["subhead_fg"],
            font=("Consolas", 10),
        )
        self._avail_years_lbl.pack(side=tk.LEFT)

    def _build_info_bar(self) -> None:
        self._info_var = tk.StringVar(
            value="Enter a ticker symbol (e.g. AAPL) or company name to begin."
        )
        bar = tk.Frame(self, bg=self.C["info_bg"], padx=14, pady=5)
        bar.pack(fill=tk.X)
        tk.Label(
            bar, textvariable=self._info_var,
            bg=self.C["info_bg"], fg=self.C["info_fg"],
            font=("Consolas", 11, "bold"),
        ).pack(side=tk.LEFT)

    # ── Market indices ────────────────────────────────────────────────────────

    # (Yahoo Finance symbol, short display label, full name)
    _MARKET_INDICES = [
        ("^DJI",  "DOW",     "Dow Jones Industrial Avg"),
        ("^GSPC", "S&P 500", "S&P 500"),
        ("^IXIC", "NASDAQ",  "Nasdaq Composite"),
        ("^NYA",  "NYSE",    "NYSE Composite"),
        ("^RUT",  "RUSSELL", "Russell 2000"),
    ]

    # ── HUD / Score bar ───────────────────────────────────────────────────────

    _TIER_COLORS = {
        "S": "#ffd700", "A": "#00e676", "B": "#00aaff",
        "C": "#ff8800", "D": "#ff6644", "F": "#ff3355",
    }

    def _build_price_bar(self) -> None:
        """Live stock price bar shown between filters and notebook."""
        bar = tk.Frame(self, bg=self.C["hud_bg"], pady=7, padx=14)
        bar.pack(fill=tk.X)

        # Ticker symbol
        self._price_ticker_lbl = tk.Label(
            bar, text="  ——", bg=self.C["hud_bg"],
            fg=self.C["subhead_fg"], font=("Consolas", 13, "bold"),
            width=10, anchor="w",
        )
        self._price_ticker_lbl.pack(side=tk.LEFT, padx=(0, 4))

        tk.Label(bar, text="│", bg=self.C["hud_bg"],
                 fg=self.C["border"], font=("Consolas", 14),
                 ).pack(side=tk.LEFT, padx=6)

        # Current price
        self._price_lbl = tk.Label(
            bar, text="—", bg=self.C["hud_bg"],
            fg=self.C["header_fg"], font=("Consolas", 16, "bold"),
            width=13, anchor="e",
        )
        self._price_lbl.pack(side=tk.LEFT, padx=(0, 6))

        # Day change ($ and %)
        self._price_change_lbl = tk.Label(
            bar, text="—", bg=self.C["hud_bg"],
            fg=self.C["subhead_fg"], font=("Consolas", 12),
            width=26, anchor="w",
        )
        self._price_change_lbl.pack(side=tk.LEFT, padx=(0, 4))

        tk.Label(bar, text="│", bg=self.C["hud_bg"],
                 fg=self.C["border"], font=("Consolas", 14),
                 ).pack(side=tk.LEFT, padx=6)

        # Company name (fills remaining space)
        self._price_company_lbl = tk.Label(
            bar, text="Search for a stock to see live price",
            bg=self.C["hud_bg"], fg=self.C["subhead_fg"],
            font=("Consolas", 11), anchor="w",
        )
        self._price_company_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

    _SCORE_BAR_W = 260
    _SCORE_BAR_H = 16

    def _build_summary_tab(self, parent: ttk.Frame) -> None:
        """Summary tab: health score bar, tier badge, and category star cards."""
        wrap = tk.Frame(parent, bg=self.C["bg"])
        wrap.pack(fill=tk.BOTH, expand=True, padx=22, pady=18)

        tk.Label(wrap, text="FINANCIAL HEALTH SUMMARY", bg=self.C["bg"],
                 fg=self.C["subhead_fg"], font=("Consolas", 10, "bold"),
                 anchor="w").pack(fill=tk.X, pady=(0, 14))

        # ── Score row ──
        score_row = tk.Frame(wrap, bg=self.C["bg"])
        score_row.pack(fill=tk.X, pady=(0, 4))

        tk.Label(score_row, text="HEALTH SCORE", bg=self.C["bg"],
                 fg=self.C["subhead_fg"], font=("Consolas", 10, "bold"),
                 ).pack(side=tk.LEFT, padx=(0, 10))

        self._score_canvas = tk.Canvas(
            score_row, width=self._SCORE_BAR_W, height=self._SCORE_BAR_H,
            bg=self.C["bg"], highlightthickness=0,
        )
        self._score_canvas.pack(side=tk.LEFT, pady=2)
        self._score_canvas.create_rectangle(
            0, 0, self._SCORE_BAR_W, self._SCORE_BAR_H,
            fill=self.C["border"], outline="", tags="track",
        )
        self._score_canvas.create_rectangle(
            0, 0, 0, self._SCORE_BAR_H,
            fill=self.C["subhead_fg"], outline="", tags="fill",
        )

        self._score_lbl = tk.Label(
            score_row, text=" -- ", bg=self.C["bg"],
            fg=self.C["header_fg"], font=("Consolas", 16, "bold"),
        )
        self._score_lbl.pack(side=tk.LEFT, padx=(8, 4))

        self._tier_lbl = tk.Label(
            score_row, text=" ? ", bg=self.C["border"],
            fg=self.C["subhead_fg"], font=("Consolas", 14, "bold"),
            padx=8, pady=2, relief="flat",
        )
        self._tier_lbl.pack(side=tk.LEFT)

        # ── Separator ──
        tk.Frame(wrap, bg=self.C["border"], height=1).pack(fill=tk.X, pady=(16, 18))

        # ── Category star cards ──
        self._star_labels: dict[str, tk.Label] = {}
        cards_frame = tk.Frame(wrap, bg=self.C["bg"])
        cards_frame.pack(fill=tk.X)

        categories = [
            ("PROFIT",   "Profitability",      "Gross & net margins · Graham/Buffett"),
            ("RETURNS",  "Return Quality",     "ROE & ROA · Buffett"),
            ("STRENGTH", "Financial Strength", "Current ratio & coverage · Graham"),
            ("LEVERAGE", "Leverage",           "Debt-to-equity · Graham"),
            ("CASHFLOW", "Owner Earnings",     "Free cash flow · Buffett"),
        ]
        for key, full_name, desc in categories:
            card = tk.Frame(cards_frame, bg=self.C["section_bg"], padx=18, pady=14)
            card.pack(side=tk.LEFT, padx=(0, 14), pady=(0, 14))

            tk.Label(card, text=full_name, bg=self.C["section_bg"],
                     fg=self.C["section_fg"], font=("Consolas", 11, "bold"),
                     ).pack(anchor="w")

            lbl = tk.Label(card, text="○○○○○", bg=self.C["section_bg"],
                           fg=self.C["border"], font=("Segoe UI Symbol", 18))
            lbl.pack(anchor="w", pady=(6, 4))
            self._star_labels[key] = lbl

            tk.Label(card, text=desc, bg=self.C["section_bg"],
                     fg=self.C["subhead_fg"], font=("Consolas", 9),
                     ).pack(anchor="w")

        # ── Business Overview ──────────────────────────────────────────────────
        tk.Frame(wrap, bg=self.C["border"], height=1).pack(fill=tk.X, pady=(18, 14))

        biz_hdr = tk.Frame(wrap, bg=self.C["bg"])
        biz_hdr.pack(fill=tk.X, pady=(0, 6))
        tk.Label(
            biz_hdr, text="BUSINESS OVERVIEW",
            bg=self.C["bg"], fg=self.C["subhead_fg"],
            font=("Consolas", 10, "bold"), anchor="w",
        ).pack(side=tk.LEFT)
        self._biz_source_lbl = tk.Label(
            biz_hdr, text="",
            bg=self.C["bg"], fg=self.C["subhead_fg"],
            font=("Consolas", 9, "italic"),
        )
        self._biz_source_lbl.pack(side=tk.LEFT, padx=(10, 0))

        biz_frame = tk.Frame(wrap, bg=self.C["section_bg"], padx=12, pady=10)
        biz_frame.pack(fill=tk.BOTH, expand=True)

        biz_scroll = ttk.Scrollbar(biz_frame, orient=tk.VERTICAL)
        biz_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self._biz_text = tk.Text(
            biz_frame,
            bg=self.C["section_bg"],
            fg="#a8c8e8",
            insertbackground=self.C["header_fg"],
            selectbackground=self.C["accent"],
            font=("Segoe UI", 11),
            wrap=tk.WORD,
            relief="flat",
            borderwidth=0,
            state="disabled",
            yscrollcommand=biz_scroll.set,
            height=7,
            padx=4,
            pady=4,
        )
        self._biz_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        biz_scroll.configure(command=self._biz_text.yview)

    def _build_notebook(self) -> None:
        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill=tk.BOTH, expand=True)

        self._trees: dict[str, ttk.Treeview] = {}
        tabs = [
            ("Summary",               "Summary",                "summary"),
            ("Income\nStatement",     "Income Statement",       "chart"),
            ("Balance\nSheet",        "Balance Sheet",          "chart"),
            ("Cash\nFlow",            "Cash Flow",              "chart"),
            ("Key\nRatios",           "Key Ratios",             "chart"),
            ("Red\nFlags",            "Red Flags",              "flags"),
            ("Top\nStories",          "Top Stories",            "news"),
            ("Social\nMedia\nSentiment", "Social Media Sentiment", "sentiment"),
            ("Glossary",              "Glossary",               "glossary"),
            ("Disclaimer",            "Disclaimer",             "disclaimer"),
        ]
        for label, key, tab_type in tabs:
            frame = ttk.Frame(self._notebook)
            self._notebook.add(frame, text=label)
            if tab_type == "summary":
                self._build_summary_tab(frame)
            elif tab_type == "flags":
                self._build_flags_tab(frame)
            elif tab_type == "news":
                self._build_news_tab(frame)
            elif tab_type == "sentiment":
                self._build_sentiment_tab(frame)
            elif tab_type == "glossary":
                self._build_glossary_tab(frame)
            elif tab_type == "disclaimer":
                self._build_disclaimer_tab(frame)
            else:
                self._trees[key] = self._build_chart_tab(frame, key)

    def _build_table(self, parent: ttk.Frame) -> ttk.Treeview:
        wrap = tk.Frame(parent, bg=self.C["bg"])
        wrap.pack(fill=tk.BOTH, expand=True)

        tree = ttk.Treeview(
            wrap, style="Financial.Treeview",
            selectmode="browse", show="tree headings",
        )
        ysb = ttk.Scrollbar(wrap, orient=tk.VERTICAL,   command=tree.yview)
        xsb = ttk.Scrollbar(wrap, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        ysb.pack(side=tk.RIGHT,  fill=tk.Y)
        xsb.pack(side=tk.BOTTOM, fill=tk.X)
        tree.pack(fill=tk.BOTH, expand=True)

        tree.tag_configure("section",
            background=self.C["section_bg"],
            foreground=self.C["section_fg"],
            font=("Segoe UI", 11, "bold"),
        )
        tree.tag_configure("odd",  background=self.C["row_odd"])
        tree.tag_configure("even", background=self.C["row_even"])
        tree.tag_configure("odd_neg",
            background=self.C["row_odd"],  foreground=self.C["negative"])
        tree.tag_configure("even_neg",
            background=self.C["row_even"], foreground=self.C["negative"])
        tree.tag_configure("odd_pos",
            background=self.C["row_odd"],  foreground=self.C["positive"])
        tree.tag_configure("even_pos",
            background=self.C["row_even"], foreground=self.C["positive"])
        return tree

    def _build_chart_tab(self, parent: ttk.Frame, tab_name: str) -> ttk.Treeview:
        """Split pane: Treeview table on top, matplotlib chart on the bottom."""
        if _MPL_AVAILABLE:
            paned = tk.PanedWindow(
                parent, orient=tk.VERTICAL,
                sashwidth=6, sashrelief="flat",
                bg=self.C["border"],
            )
            paned.pack(fill=tk.BOTH, expand=True)

            table_frame = tk.Frame(paned, bg=self.C["bg"])
            paned.add(table_frame, minsize=120)

            chart_frame = tk.Frame(paned, bg=self.C["bg"])
            paned.add(chart_frame, minsize=160)

            # Set initial sash so table gets ~60% and chart ~40% of space
            paned.update_idletasks()
            total = paned.winfo_height() or 600
            paned.sash_place(0, 0, int(total * 0.58))

            fig = Figure(facecolor=self.C["bg"])
            canvas = FigureCanvasTkAgg(fig, master=chart_frame)
            canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
            self._charts[tab_name] = (fig, canvas)
        else:
            table_frame = ttk.Frame(parent)
            table_frame.pack(fill=tk.BOTH, expand=True)

        return self._build_table(table_frame)

    def _build_flags_tab(self, parent: ttk.Frame) -> None:
        if _MPL_AVAILABLE:
            paned = tk.PanedWindow(
                parent, orient=tk.HORIZONTAL,
                sashwidth=6, sashrelief="flat",
                bg=self.C["border"],
            )
            paned.pack(fill=tk.BOTH, expand=True)
            text_frame = tk.Frame(paned, bg=self.C["bg"])
            paned.add(text_frame, minsize=300)
            chart_frame = tk.Frame(paned, bg=self.C["bg"])
            paned.add(chart_frame, minsize=200)

            fig = Figure(facecolor=self.C["bg"])
            canvas = FigureCanvasTkAgg(fig, master=chart_frame)
            canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
            self._charts["Red Flags"] = (fig, canvas)
        else:
            text_frame = parent

        wrap = tk.Frame(text_frame, bg=self.C["bg"])
        wrap.pack(fill=tk.BOTH, expand=True, padx=14, pady=10)

        self._flags_text = tk.Text(
            wrap, wrap=tk.WORD,
            font=("Consolas", 12),
            bg=self.C["row_odd"], fg="#a8c8e8",
            relief="flat",
            padx=12, pady=10,
            insertbackground=self.C["header_fg"],
            state=tk.DISABLED,
        )
        sb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self._flags_text.yview)
        self._flags_text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._flags_text.pack(fill=tk.BOTH, expand=True)

        self._flags_text.tag_configure(
            "h1",       foreground=self.C["header_fg"],
            font=("Consolas", 15, "bold"))
        self._flags_text.tag_configure(
            "h2",       foreground=self.C["section_fg"],
            font=("Consolas", 12, "bold"))
        self._flags_text.tag_configure(
            "CRITICAL", foreground="#ff3355",
            font=("Consolas", 12, "bold"))
        self._flags_text.tag_configure(
            "WARNING",  foreground="#ff8800",
            font=("Consolas", 12, "bold"))
        self._flags_text.tag_configure(
            "INFO",     foreground="#44aaff",
            font=("Consolas", 12, "bold"))
        self._flags_text.tag_configure(
            "ok",       foreground="#00e676",
            font=("Consolas", 12, "bold"))
        self._flags_text.tag_configure(
            "achievement", foreground="#ffd700",
            font=("Consolas", 12, "bold"))
        self._flags_text.tag_configure(
            "locked",   foreground=self.C["border"],
            font=("Consolas", 12))
        self._flags_text.tag_configure(
            "mono",     font=("Consolas", 11))

    def _build_news_tab(self, parent: ttk.Frame) -> None:
        wrap = tk.Frame(parent, bg=self.C["bg"])
        wrap.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        self._news_text = tk.Text(
            wrap, wrap=tk.WORD,
            font=("Consolas", 12),
            bg=self.C["row_odd"], fg="#a8c8e8",
            relief="flat", padx=14, pady=10,
            cursor="arrow",
            insertbackground=self.C["header_fg"],
            spacing1=2, spacing3=2,
            state=tk.DISABLED,
        )
        sb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self._news_text.yview)
        self._news_text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._news_text.pack(fill=tk.BOTH, expand=True)

        self._news_text.tag_configure(
            "header",   foreground=self.C["header_fg"],
            font=("Consolas", 15, "bold"))
        self._news_text.tag_configure(
            "prompt",   foreground=self.C["subhead_fg"],
            font=("Consolas", 12, "italic"))
        self._news_text.tag_configure(
            "number",   foreground=self.C["subhead_fg"],
            font=("Consolas", 11, "bold"))
        self._news_text.tag_configure(
            "headline", foreground=self.C["header_fg"],
            font=("Consolas", 12, "bold"))
        self._news_text.tag_configure(
            "meta",     foreground=self.C["subhead_fg"],
            font=("Consolas", 10))
        self._news_text.tag_configure(
            "desc",     foreground="#7a9ab8",
            font=("Consolas", 11))
        self._news_text.tag_configure(
            "sep",      foreground=self.C["border"],
            font=("Consolas", 10))

        self._news_link_tags: list[str] = []

        # Initial placeholder
        self._news_text.configure(state=tk.NORMAL)
        self._news_text.insert(tk.END, "TOP STORIES\n\n", "header")
        self._news_text.insert(
            tk.END, "Search a stock to load the latest news.\n", "prompt"
        )
        self._news_text.configure(state=tk.DISABLED)

    def _build_sentiment_tab(self, parent: ttk.Frame) -> None:
        """Social Media Sentiment tab — scrollable Text widget with rich formatting."""
        wrap = tk.Frame(parent, bg=self.C["bg"])
        wrap.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        self._sent_text = tk.Text(
            wrap, wrap=tk.WORD, font=("Consolas", 12),
            bg=self.C["row_odd"], fg="#a8c8e8",
            relief="flat", padx=14, pady=10,
            cursor="arrow", insertbackground=self.C["header_fg"],
            spacing1=2, spacing3=2, state=tk.DISABLED,
        )
        sb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self._sent_text.yview)
        self._sent_text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._sent_text.pack(fill=tk.BOTH, expand=True)

        C = self.C
        t = self._sent_text
        t.tag_configure("header",          foreground=C["header_fg"],  font=("Consolas", 15, "bold"))
        t.tag_configure("section_h",       foreground=C["subhead_fg"], font=("Consolas", 10,  "bold"))
        t.tag_configure("platform_h",      foreground=C["section_fg"], font=("Consolas", 12, "bold"))
        t.tag_configure("mono",            foreground="#a8c8e8",        font=("Consolas", 12))
        t.tag_configure("meta",            foreground=C["subhead_fg"],  font=("Consolas", 10))
        t.tag_configure("desc",            foreground="#7a9ab8",        font=("Consolas", 11))
        t.tag_configure("headline",        foreground=C["header_fg"],   font=("Consolas", 11, "bold"))
        t.tag_configure("sep",             foreground=C["border"],      font=("Consolas", 10))
        t.tag_configure("unavail",         foreground=C["subhead_fg"],  font=("Consolas", 11, "italic"))
        t.tag_configure("error_text",      foreground=C["negative"],    font=("Consolas", 11))
        t.tag_configure("prompt",          foreground=C["subhead_fg"],  font=("Consolas", 12, "italic"))
        t.tag_configure("sent_strong_pos", foreground="#00e676",        font=("Consolas", 12, "bold"))
        t.tag_configure("sent_pos",        foreground="#44dd88",        font=("Consolas", 12, "bold"))
        t.tag_configure("sent_neutral",    foreground="#ffcc00",        font=("Consolas", 12, "bold"))
        t.tag_configure("sent_neg",        foreground="#ff8844",        font=("Consolas", 12, "bold"))
        t.tag_configure("sent_strong_neg", foreground="#ff3355",        font=("Consolas", 12, "bold"))

        self._sent_link_tags: list[str] = []

        t.configure(state=tk.NORMAL)
        t.insert(tk.END, "SOCIAL MEDIA SENTIMENT\n\n", "header")
        t.insert(tk.END, "  Search a stock to load sentiment analysis.\n", "prompt")
        t.configure(state=tk.DISABLED)

    def _build_disclaimer_tab(self, parent: ttk.Frame) -> None:
        wrap = tk.Frame(parent, bg=self.C["bg"])
        wrap.pack(fill=tk.BOTH, expand=True, padx=14, pady=10)

        text = tk.Text(
            wrap, wrap=tk.WORD,
            font=("Consolas", 12),
            bg=self.C["row_odd"], fg="#a8c8e8",
            relief="flat", padx=16, pady=14,
            cursor="arrow", spacing1=2, spacing3=4,
            state=tk.DISABLED,
        )
        sb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        text.pack(fill=tk.BOTH, expand=True)

        text.tag_configure("title",   foreground=self.C["header_fg"],  font=("Consolas", 15, "bold"))
        text.tag_configure("warning", foreground="#ff3355",             font=("Consolas", 12, "bold"))
        text.tag_configure("section", foreground=self.C["section_fg"], font=("Consolas", 12, "bold"))
        text.tag_configure("body",    foreground="#a8c8e8",             font=("Consolas", 11))
        text.tag_configure("sep",     foreground=self.C["border"],      font=("Consolas", 10))
        text.tag_configure("label",   foreground=self.C["subhead_fg"], font=("Consolas", 10, "bold"))

        _SEP = "  " + "─" * 68 + "\n\n"

        DISCLAIMERS = [
            (
                "NOT FINANCIAL ADVICE",
                "⚠  IMPORTANT — PLEASE READ",
                "Stock Simplify is an informational and educational tool only. Nothing "
                "displayed in this application — including Health Scores, tier ratings "
                "(S/A/B/C/D/F), Red Flags, star ratings, achievement badges, sentiment "
                "scores, news articles, or any other metric, indicator, or ranking — "
                "constitutes financial, investment, legal, or tax advice of any kind.\n\n"
                "Do not make any investment decision based solely or in part on information "
                "shown in this application. Always consult a licensed financial advisor "
                "before buying, selling, or holding any security.",
            ),
            (
                "INVESTMENT RISK",
                "Risk Warning",
                "Investing in securities involves substantial risk of loss, including the "
                "possible loss of the entire amount invested. Past financial performance "
                "of a company does not guarantee or predict future results. Stock prices "
                "are volatile and can decline significantly for reasons unrelated to a "
                "company's financial statements.",
            ),
            (
                "DATA ACCURACY & COMPLETENESS",
                "Data Sources & Limitations",
                "Financial data is sourced from SEC EDGAR XBRL structured filings. This "
                "data may be incomplete, delayed, incorrectly tagged by the filer, or "
                "contain parsing errors introduced by this application. Calculated metrics "
                "— including revenue, earnings, margins, and ratios — may differ from "
                "audited financial statements or values reported by professional data "
                "providers.\n\n"
                "Not all public companies file structured XBRL data, and some figures may "
                "be absent, estimated, or derived from non-standard tags.",
            ),
            (
                "HEALTH SCORE & SCORING SYSTEM",
                "Simplified Scoring Model",
                "The Financial Health Score (0–100), tier ratings, and category star "
                "ratings are computed using a simplified proprietary formula based on a "
                "small set of financial ratios. This model does not account for industry "
                "norms, business model differences, macroeconomic context, qualitative "
                "factors, or the full complexity of a company's financial position.\n\n"
                "Scores are not comparable across industries without appropriate context. "
                "A score in one sector may carry an entirely different meaning in another. "
                "Achievements and gamification elements are illustrative only.",
            ),
            (
                "LIVE PRICE & MARKET DATA",
                "Delayed Quotes",
                "Live stock prices and market index data are sourced from Yahoo Finance "
                "and may be delayed by 15 minutes or more. This data is not suitable for "
                "use in real-time trading decisions and should not be relied upon for "
                "execution pricing.",
            ),
            (
                "SOCIAL MEDIA SENTIMENT",
                "Automated Keyword Analysis — Not Professional NLP",
                "Sentiment scores are generated by automated keyword matching against "
                "posts from Reddit. This method is a crude approximation "
                "that cannot reliably detect sarcasm, irony, context, or nuanced language. "
                "Sentiment scores may be significantly inaccurate and are highly volatile.\n\n"
                "Social media sentiment can be artificially inflated or suppressed by "
                "coordinated activity and is not a reliable indicator of future stock "
                "performance. No professional-grade natural language processing is used.",
            ),
            (
                "THIRD-PARTY DATA SOURCES",
                "External Services",
                "This application retrieves data from third-party services including the "
                "U.S. Securities and Exchange Commission (SEC EDGAR), Yahoo Finance, "
                "and Reddit. The availability, accuracy, timeliness, and "
                "terms of use of these services are entirely outside the control of this "
                "application. Data may become unavailable, rate-limited, or discontinued "
                "at any time without notice.",
            ),
            (
                "NO AFFILIATION",
                "Independent Open-Source Tool",
                "Stock Simplify is an independent open-source application and is not "
                "affiliated with, endorsed by, sponsored by, or associated with the U.S. "
                "Securities and Exchange Commission, Yahoo Finance, Reddit, "
                "or any other data provider, financial institution, or regulatory body.",
            ),
            (
                "NO WARRANTY",
                "Provided 'As Is'",
                "This software is provided 'as is', without warranty of any kind, express "
                "or implied. The authors accept no liability for any direct, indirect, "
                "incidental, or consequential damages arising from the use of or reliance "
                "on this application or the data it displays.",
            ),
        ]

        text.configure(state=tk.NORMAL)
        text.insert(tk.END, "DISCLAIMER\n\n", "title")
        text.insert(
            tk.END,
            "  ⚠  This application is for informational purposes only.\n"
            "  ⚠  Nothing here constitutes financial advice.\n"
            "  ⚠  Do not make investment decisions solely based on this tool.\n\n",
            "warning",
        )
        text.insert(tk.END, _SEP, "sep")

        for key, heading, body in DISCLAIMERS:
            text.insert(tk.END, f"  {key}\n", "section")
            text.insert(tk.END, f"  {heading}\n\n", "label")
            for line in body.split("\n"):
                text.insert(tk.END, f"  {line}\n" if line else "\n", "body")
            text.insert(tk.END, "\n")
            text.insert(tk.END, _SEP, "sep")

        text.configure(state=tk.DISABLED)

    def _build_glossary_tab(self, parent: ttk.Frame) -> None:
        wrap = tk.Frame(parent, bg=self.C["bg"])
        wrap.pack(fill=tk.BOTH, expand=True, padx=14, pady=10)

        text = tk.Text(
            wrap, wrap=tk.WORD,
            font=("Consolas", 12),
            bg=self.C["row_odd"], fg="#a8c8e8",
            relief="flat",
            padx=12, pady=10,
            insertbackground=self.C["header_fg"],
            state=tk.DISABLED,
        )
        sb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        text.pack(fill=tk.BOTH, expand=True)

        text.tag_configure("title",
            foreground=self.C["header_fg"],
            font=("Consolas", 16, "bold"))
        text.tag_configure("section",
            foreground=self.C["section_fg"],
            font=("Consolas", 13, "bold"))
        text.tag_configure("metric",
            foreground="#00ccff",
            font=("Consolas", 12, "bold"))
        text.tag_configure("desc",
            foreground="#7a9ab8",
            font=("Consolas", 12))

        GLOSSARY = [
            ("Income Statement", [
                ("Revenue",
                 "Total income a company generates from selling its products or services before any "
                 "expenses are deducted. Also called the 'top line'."),
                ("Cost of Revenue",
                 "Direct costs attributable to producing the goods or services sold (also known as "
                 "Cost of Goods Sold / COGS). Includes materials, labour, and manufacturing overhead."),
                ("Gross Profit",
                 "Revenue minus Cost of Revenue. Represents how much a company earns before "
                 "operating expenses are subtracted."),
                ("Gross Margin",
                 "Gross Profit as a percentage of Revenue. Higher is generally better — it shows "
                 "how efficiently a company produces its goods or services."),
                ("R&D Expense",
                 "Money spent on research and development. High R&D can signal future growth "
                 "investment, especially for technology or pharmaceutical companies."),
                ("Operating Income",
                 "Profit from core business operations, after deducting operating expenses "
                 "(COGS, R&D, SG&A) but before interest and taxes. Also called EBIT."),
                ("Operating Margin",
                 "Operating Income as a percentage of Revenue. Measures operational efficiency — "
                 "how much profit is generated from every dollar of sales."),
                ("Net Income",
                 "The company's total profit (or loss) after all expenses, interest, and taxes. "
                 "Also called the 'bottom line'."),
                ("Net Margin",
                 "Net Income as a percentage of Revenue. Shows what fraction of each revenue "
                 "dollar ultimately becomes profit."),
                ("EPS (Basic)",
                 "Earnings Per Share — Net Income divided by basic shares outstanding. "
                 "Measures profitability on a per-share basis."),
                ("EPS (Diluted)",
                 "EPS calculated using the fully diluted share count, which includes potential "
                 "shares from options, warrants, and convertible securities. More conservative "
                 "than Basic EPS."),
                ("EBITDA",
                 "Earnings Before Interest, Taxes, Depreciation & Amortisation. A proxy for "
                 "operating cash earnings; widely used for company comparisons and valuation."),
                ("D&A",
                 "Depreciation & Amortisation — the non-cash expense for the wear-and-tear of "
                 "tangible assets (depreciation) and the expensing of intangible assets "
                 "(amortisation)."),
                ("Income Tax",
                 "Taxes owed to governments on the company's taxable income for the period."),
            ]),
            ("Balance Sheet", [
                ("Cash & Equivalents",
                 "Highly liquid assets: physical cash, bank deposits, and short-term instruments "
                 "(e.g. Treasury bills). Indicates the company's immediate financial flexibility."),
                ("Accounts Receivable",
                 "Money owed to the company by customers for goods or services already delivered "
                 "but not yet paid for."),
                ("Inventory",
                 "Raw materials, work-in-progress, and finished goods a company holds. High "
                 "inventory relative to sales may signal slow demand or supply-chain issues."),
                ("Current Assets",
                 "Assets expected to be converted to cash within one year: cash, receivables, "
                 "inventory, and similar items."),
                ("Total Assets",
                 "Everything the company owns — current assets plus long-term assets such as "
                 "property, plant, equipment, and intangibles."),
                ("Current Liabilities",
                 "Financial obligations due within one year: accounts payable, short-term debt, "
                 "accrued expenses, and similar items."),
                ("Short-Term Debt",
                 "Borrowings due within 12 months, including the current portion of long-term "
                 "debt obligations."),
                ("Long-Term Debt",
                 "Borrowings with maturities beyond one year, such as bonds, term loans, or "
                 "lease obligations."),
                ("Total Debt",
                 "Sum of Short-Term Debt and Long-Term Debt. Reflects the total amount of "
                 "borrowed capital on the balance sheet."),
                ("Total Liabilities",
                 "All financial obligations the company owes — current and non-current "
                 "liabilities combined."),
                ("Stockholders' Equity",
                 "Net worth from the shareholders' perspective: Total Assets minus Total "
                 "Liabilities. Also called book value or shareholders' equity."),
                ("Retained Earnings",
                 "Cumulative net income kept by the company rather than paid out as dividends. "
                 "Represents reinvested profits accumulated over the company's history."),
                ("Shares Outstanding",
                 "Total number of common shares currently held by all shareholders, including "
                 "insiders and the general public."),
            ]),
            ("Cash Flow", [
                ("Operating Cash Flow",
                 "Cash generated (or consumed) by the company's core business operations. "
                 "Consistently positive OCF indicates a healthy, self-sustaining business."),
                ("Capital Expenditures",
                 "Cash spent on purchasing or upgrading physical assets (property, plant, "
                 "equipment). Necessary for maintaining and growing the business; shown as a "
                 "negative number."),
                ("Investing Cash Flow",
                 "Net cash from investing activities — buying or selling assets, investments, "
                 "or subsidiaries. Often negative for growing companies making acquisitions."),
                ("Free Cash Flow",
                 "Operating Cash Flow minus Capital Expenditures. Represents cash available to "
                 "repay debt, pay dividends, buy back shares, or pursue new investments. "
                 "A key indicator of long-term financial health."),
                ("FCF Margin",
                 "Free Cash Flow as a percentage of Revenue. Measures how efficiently a company "
                 "converts revenue into free cash. Above 10% is healthy; above 20% is excellent."),
                ("Financing Cash Flow",
                 "Net cash from financing activities: issuing or repaying debt, issuing or "
                 "repurchasing shares, and paying dividends."),
                ("Dividends Paid",
                 "Cash distributed to shareholders as dividends during the period."),
                ("Share Repurchases",
                 "Cash spent buying back the company's own shares, which reduces the share count "
                 "and typically increases Earnings Per Share."),
            ]),
            ("Key Ratios", [
                ("Gross Margin",
                 "Gross Profit ÷ Revenue. Benchmarks vary widely by industry — software is "
                 "typically >60%, while retail can be <30%."),
                ("Operating Margin",
                 "Operating Income ÷ Revenue. Above 15% is generally strong, but varies "
                 "significantly by sector."),
                ("Net Margin",
                 "Net Income ÷ Revenue. Shows overall profitability after all costs and taxes "
                 "have been deducted."),
                ("FCF Margin",
                 "Free Cash Flow ÷ Revenue. Above 10% is generally healthy; above 20% is "
                 "considered excellent."),
                ("Return on Equity (ROE)",
                 "Net Income ÷ Stockholders' Equity. Measures how efficiently a company uses "
                 "shareholders' money to generate profit. Above 15% is generally considered "
                 "strong."),
                ("Return on Assets (ROA)",
                 "Net Income ÷ Total Assets. Measures how efficiently assets are used to "
                 "generate profit. Above 5% is generally considered good."),
                ("Current Ratio",
                 "Current Assets ÷ Current Liabilities. Measures short-term liquidity. "
                 "A ratio >1.0 means assets can cover near-term obligations; <1.0 may signal "
                 "liquidity stress."),
                ("Quick Ratio",
                 "(Current Assets − Inventory) ÷ Current Liabilities. A stricter liquidity "
                 "test that excludes inventory, which may be difficult to convert to cash "
                 "quickly."),
                ("Debt-to-Equity",
                 "Total Debt ÷ Stockholders' Equity. Measures financial leverage. A high ratio "
                 "means the company is heavily financed by debt, which increases financial risk "
                 "in downturns."),
                ("Interest Coverage",
                 "Operating Income ÷ Interest Expense. Measures how comfortably a company can "
                 "pay interest on its debt. Below 2× is a warning sign; above 3× is "
                 "comfortable."),
                ("Asset Turnover",
                 "Revenue ÷ Total Assets. Measures how efficiently the company uses its asset "
                 "base to generate revenue. Higher ratios indicate more efficient use of assets."),
                ("Inventory Turnover",
                 "Cost of Revenue ÷ Inventory. Measures how many times inventory is sold and "
                 "replaced per year. Higher generally means efficient inventory management and "
                 "strong demand."),
            ]),
        ]

        text.configure(state=tk.NORMAL)
        text.insert(tk.END, "Metric Glossary\n\n", "title")
        for section_name, items in GLOSSARY:
            text.insert(tk.END, f"{section_name}\n\n", "section")
            for metric, description in items:
                text.insert(tk.END, f"  {metric}\n", "metric")
                text.insert(tk.END, f"    {description}\n\n", "desc")
        text.configure(state=tk.DISABLED)

    # ── Score & Achievement logic ─────────────────────────────────────────────

    _ACHIEVEMENTS = [
        ("Revenue Champion",   "Revenue grew 3+ consecutive years",   "_ach_rev_growth"),
        ("Margin Master",      "Gross margin ≥ 40%",                  "_ach_gross_margin"),
        ("Profit Machine",     "Net margin ≥ 15%",                    "_ach_net_margin"),
        ("Cash Fortress",      "Free cash flow positive",             "_ach_fcf"),
        ("Liquidity Pro",      "Current ratio ≥ 2.0",                 "_ach_liquidity"),
        ("Debt-Free Legend",   "Debt-to-Equity ≤ 0.5",               "_ach_low_debt"),
        ("No-Debt Champion",   "Near-zero debt (D/E ≤ 0.1)",         "_ach_no_debt"),
        ("Growth Accelerator", "Revenue grew > 15% YoY",             "_ach_rev_15"),
    ]

    def _check_achievements(self, metrics: FinancialMetrics) -> list[bool]:
        r = metrics.ratios
        rev_t = metrics.annual_trend.get("revenue", [])

        def _rev_consecutive(n):
            vals = [p["value"] for p in rev_t if p.get("value") is not None]
            if len(vals) < n + 1:
                return False
            return all(vals[i] > vals[i - 1] for i in range(1, n + 1))

        def _rev_yoy_pct():
            vals = [p["value"] for p in rev_t if p.get("value") is not None]
            if len(vals) < 2 or not vals[-2]:
                return 0.0
            return (vals[-1] / vals[-2] - 1) * 100

        checks = [
            _rev_consecutive(3),
            (r.get("gross_margin_pct") or 0) >= 40,
            (r.get("net_margin_pct") or 0) >= 15,
            (r.get("free_cash_flow") or 0) > 0,
            (r.get("current_ratio") or 0) >= 2.0,
            (r.get("debt_to_equity") is not None and r["debt_to_equity"] <= 0.5),
            (r.get("debt_to_equity") is not None and r["debt_to_equity"] <= 0.1),
            _rev_yoy_pct() > 15,
        ]
        return checks

    def _compute_score(self, metrics: FinancialMetrics) -> tuple[int, str]:
        """
        Score based on principles from classic investing books:
          - Benjamin Graham  (The Intelligent Investor): margins, current ratio, leverage
          - Warren Buffett   (The Warren Buffett Way):   ROE, ROA, owner earnings (FCF)
          - Peter Lynch      (One Up on Wall Street):    consistent multi-year growth
          - Joel Greenblatt  (Little Book That Beats the Market): operating efficiency
        """
        r   = metrics.ratios
        pts = 0.0

        # ── Profitability · Graham + Buffett (20 pts) ─────────────────────────
        # Gross margin: Buffett looks for durable moat (>40% = wide moat)
        gm = r.get("gross_margin_pct") or 0
        pts += min(gm / 50 * 12, 12)   # 12 pts max
        # Net margin: Graham floor, Buffett ceiling
        nm = r.get("net_margin_pct") or 0
        pts += max(min(nm / 20 * 8, 8), 0)   # 8 pts max

        # ── Return Quality · Buffett (20 pts) ─────────────────────────────────
        # ROE > 15% = Buffett's minimum for a quality business
        roe = r.get("roe_pct") or 0
        if roe >= 20:   pts += 12
        elif roe >= 15: pts += 10
        elif roe >= 10: pts += 7
        elif roe >= 5:  pts += 4
        elif roe > 0:   pts += 1
        # ROA > 7% = Greenblatt high-return business
        roa = r.get("roa_pct") or 0
        if roa >= 10:   pts += 8
        elif roa >= 7:  pts += 6
        elif roa >= 4:  pts += 4
        elif roa > 0:   pts += 2

        # ── Financial Strength · Graham (20 pts) ──────────────────────────────
        # Current ratio: Graham required > 2.0 for safety
        cr = r.get("current_ratio") or 0
        if cr >= 2.0:   pts += 12
        elif cr >= 1.5: pts += 9
        elif cr >= 1.0: pts += 5
        # Interest coverage: Graham wanted > 5× for industrials
        ic = r.get("interest_coverage")
        if ic is None:      pts += 8   # no debt = safe
        elif ic >= 8:       pts += 8
        elif ic >= 5:       pts += 6
        elif ic >= 3:       pts += 3
        elif ic >= 1:       pts += 1

        # ── Leverage · Graham (15 pts) ────────────────────────────────────────
        # Graham: D/E < 1.0 for safety; Buffett tolerates more if earnings are stable
        de = r.get("debt_to_equity")
        if de is None:    pts += 10
        elif de <= 0.3:   pts += 15
        elif de <= 0.7:   pts += 12
        elif de <= 1.5:   pts += 8
        elif de <= 3.0:   pts += 4

        # ── Owner Earnings · Buffett (15 pts) ─────────────────────────────────
        # Buffett: net income + D&A - capex; proxied by FCF
        fcf   = r.get("free_cash_flow")
        fcf_m = r.get("fcf_margin_pct") or 0
        if fcf is None:
            pts += 7
        elif fcf > 0:
            pts += min(fcf_m / 15 * 15, 15)

        # ── Growth Consistency · Lynch (10 pts) ───────────────────────────────
        # Lynch valued consistent multi-year earners over one-year spikes
        def _trend_vals(key: str) -> list[float]:
            return [p["value"] for p in metrics.annual_trend.get(key, [])
                    if p.get("value") is not None]

        rev_vals = _trend_vals("revenue")
        ni_vals  = _trend_vals("net_income")

        def _consistent_growth(vals: list[float], bonus: float) -> float:
            if len(vals) < 3:
                return bonus * 0.4
            years_up = sum(1 for a, b in zip(vals, vals[1:]) if b > a)
            consistency = years_up / (len(vals) - 1)
            latest_growth = (vals[-1] / vals[-2] - 1) * 100 if vals[-2] else 0
            return min(bonus, consistency * bonus * 0.6 +
                       max(min(latest_growth / 15 * bonus * 0.4, bonus * 0.4), 0))

        pts += _consistent_growth(rev_vals, 5)
        pts += _consistent_growth(ni_vals,  5)

        score = max(0, min(100, int(pts)))
        if score >= 85:   tier = "S"
        elif score >= 70: tier = "A"
        elif score >= 55: tier = "B"
        elif score >= 40: tier = "C"
        elif score >= 25: tier = "D"
        else:             tier = "F"
        return score, tier

    def _category_stars(self, metrics: FinancialMetrics) -> dict[str, int]:
        """Star ratings per category, aligned with book-based scoring principles."""
        r     = metrics.ratios
        gm    = r.get("gross_margin_pct") or 0
        nm    = r.get("net_margin_pct") or 0
        roe   = r.get("roe_pct") or 0
        roa   = r.get("roa_pct") or 0
        cr    = r.get("current_ratio") or 0
        de    = r.get("debt_to_equity")
        fcf_m = r.get("fcf_margin_pct") or 0
        fcf   = r.get("free_cash_flow")

        # Profitability: Graham + Buffett moat proxy
        profit = int(min(5, max(0, gm / 12 + nm / 5)))

        # Return quality: Buffett ROE/ROA
        returns = (5 if roe >= 20 and roa >= 10 else
                   4 if roe >= 15 and roa >= 7  else
                   3 if roe >= 10 and roa >= 4  else
                   2 if roe >= 5  or  roa >= 2  else 1)

        # Financial strength: Graham current ratio + coverage
        ic = r.get("interest_coverage")
        ic_stars = (2 if ic is None or ic >= 8 else
                    2 if ic >= 5 else 1 if ic >= 3 else 0)
        cr_stars = (3 if cr >= 2.0 else 2 if cr >= 1.5 else 1 if cr >= 1.0 else 0)
        strength = min(5, cr_stars + ic_stars)

        # Leverage: Graham safety margin
        leverage = (5 if de is None or de <= 0.3 else
                    4 if de <= 0.7 else
                    3 if de <= 1.5 else
                    2 if de <= 3.0 else 1)

        # Owner earnings: Buffett FCF
        cashflow = (0 if fcf is None or fcf <= 0 else
                    int(min(5, max(0, fcf_m / 5))))

        return {
            "PROFIT":   profit,
            "RETURNS":  returns,
            "STRENGTH": strength,
            "LEVERAGE": leverage,
            "CASHFLOW": cashflow,
        }

    def _update_hud(self, metrics: FinancialMetrics) -> None:
        score, tier = self._compute_score(metrics)
        color = self._TIER_COLORS.get(tier, self.C["header_fg"])
        fill_w = int(self._SCORE_BAR_W * score / 100)

        self._score_canvas.itemconfigure("fill", fill=color)
        self._score_canvas.coords("fill", 0, 0, fill_w, self._SCORE_BAR_H)
        self._score_lbl.configure(text=f" {score:3d} ", fg=color)
        self._tier_lbl.configure(text=f"  {tier}  ", bg=color,
                                  fg="#000000" if tier in ("S", "A") else "#ffffff")

        stars = self._category_stars(metrics)
        for key, n in stars.items():
            filled = "●" * n + "○" * (5 - n)
            self._star_labels[key].configure(
                text=filled,
                fg=self._TIER_COLORS.get(
                    "S" if n >= 5 else "A" if n >= 4 else "B" if n >= 3
                    else "C" if n >= 2 else "D", self.C["border"]
                ),
            )

    def _update_biz_overview(self, sec_text: str, source: str = "") -> None:
        """
        Populate the business overview Text widget with SEC filing text (read-only).
        Called immediately after a search; Wikipedia text is layered in later via
        _update_profile once the background fetch completes.
        """
        self._biz_text.configure(state="normal")
        self._biz_text.delete("1.0", tk.END)

        if sec_text:
            self._biz_text.tag_configure("src",
                font=("Segoe UI", 10, "italic"), foreground=self.C["subhead_fg"])
            form_label = f"SEC {source}" if source else "SEC 10-K filing"
            self._biz_text.insert(tk.END, f"[{form_label}]\n", "src")
            self._biz_text.insert(tk.END, sec_text[:1500])
            self._biz_source_lbl.configure(text=f"— from {form_label}")
        else:
            self._biz_text.tag_configure("hint",
                font=("Segoe UI", 11, "italic"), foreground=self.C["subhead_fg"])
            self._biz_text.insert(
                tk.END,
                "Fetching company profile…\n\n"
                "Full qualitative text requires local filings:\n"
                "    python stock_simplify.py --tickers <TICKER>",
                "hint",
            )
            self._biz_source_lbl.configure(text="")

        self._biz_text.configure(state="disabled")

    def _update_profile(self, payload: dict) -> None:
        """
        Called on the main thread when the Wikipedia profile fetch completes.
        Prepends the Wikipedia extract to whatever is already in the widget.
        """
        extract = payload.get("extract", "")
        title   = payload.get("title", "")
        if not extract:
            # Nothing useful came back — leave existing content as-is
            if self.C["subhead_fg"] in (self._biz_source_lbl.cget("foreground") or ""):
                pass
            return

        self._biz_text.configure(state="normal")
        self._biz_text.delete("1.0", tk.END)

        # ── Wikipedia block ──
        self._biz_text.tag_configure("wiki_src",
            font=("Segoe UI", 10, "italic"), foreground=self.C["subhead_fg"])
        self._biz_text.tag_configure("wiki_body",
            font=("Segoe UI", 11), foreground="#a8c8e8")

        wiki_label = f"Wikipedia — {title}" if title else "Wikipedia"
        self._biz_text.insert(tk.END, f"[{wiki_label}]\n", "wiki_src")
        self._biz_text.insert(tk.END, extract + "\n", "wiki_body")

        self._biz_source_lbl.configure(text=f"— {wiki_label}")
        self._biz_text.configure(state="disabled")

    def _reset_hud(self) -> None:
        self._score_canvas.coords("fill", 0, 0, 0, self._SCORE_BAR_H)
        self._score_canvas.itemconfigure("fill", fill=self.C["subhead_fg"])
        self._score_lbl.configure(text=" -- ", fg=self.C["header_fg"])
        self._tier_lbl.configure(text=" ? ", bg=self.C["border"],
                                  fg=self.C["subhead_fg"])
        for lbl in self._star_labels.values():
            lbl.configure(text="○○○○○", fg=self.C["border"])
        self._update_biz_overview("")

    def _build_status_bar(self) -> None:
        self._status_var = tk.StringVar(value="Ready")
        tk.Label(
            self, textvariable=self._status_var,
            bg=self.C["status_bg"], fg=self.C["status_fg"],
            font=("Segoe UI", 10), anchor="w",
            padx=10, pady=3,
        ).pack(fill=tk.X, side=tk.BOTTOM)

    # ── Table population ──────────────────────────────────────────────────────

    def _setup_columns(self, tree: ttk.Treeview, years: list[int]) -> None:
        cols = [f"y{i}" for i in range(len(years))]
        tree["columns"] = cols
        tree.column("#0", width=250, minwidth=200, stretch=False, anchor="w")
        tree.heading("#0", text="  Metric", anchor="w")
        for col, yr in zip(cols, years):
            tree.column(col, width=135, minwidth=100, anchor="e", stretch=True)
            tree.heading(col, text=f"FY {yr}", anchor="e")

    def _populate_table(
        self,
        tree: ttk.Treeview,
        rows: list[Row],
        years: list[int],
    ) -> None:
        tree.delete(*tree.get_children())
        self._setup_columns(tree, years)
        n_cols = len(years)

        _naturally_neg = frozenset([
            "cost of revenue", "r&d expense", "income tax",
            "capital expenditures", "investing cash flow",
            "financing cash flow", "dividends paid", "share repurchases",
        ])

        data_idx = 0
        for label, vals, unit, is_section in rows:
            if is_section:
                tree.insert(
                    "", tk.END, text=f"   {label}",
                    values=[""] * n_cols, tags=("section",),
                )
                continue

            formatted = []
            for v in (vals or []):
                formatted.append(_fmt(v, unit) if v is not None else "—")
            while len(formatted) < n_cols:
                formatted.append("—")

            parity   = "even" if data_idx % 2 == 0 else "odd"
            nat_neg  = label.lower() in _naturally_neg
            latest   = next((v for v in (vals or []) if v is not None), None)

            if latest is not None and not nat_neg:
                if latest < 0:
                    tag = f"{parity}_neg"
                elif latest > 0:
                    tag = f"{parity}_pos"
                else:
                    tag = parity
            else:
                tag = parity

            tree.insert(
                "", tk.END,
                text=f"   {label}",
                values=formatted,
                tags=(tag,),
            )
            data_idx += 1

    # ── Red Flags tab ─────────────────────────────────────────────────────────

    # ── Chart helpers ─────────────────────────────────────────────────────────

    _CHART_COLORS = {
        "blue":       "#00aaff",
        "blue_lt":    "#44ccff",
        "green":      "#00e676",
        "green_lt":   "#66ffaa",
        "red":        "#ff2244",
        "red_lt":     "#ff6677",
        "orange":     "#ff8800",
        "orange_lt":  "#ffaa44",
        "purple":     "#cc44ff",
        "grey":       "#446688",
        "text":       "#a8c8e8",
    }

    def _chart_style(self, fig) -> None:
        """Apply consistent look to every figure."""
        fig.patch.set_facecolor(self.C["bg"])

    def _ax_style(self, ax) -> None:
        """Apply consistent look to every axes."""
        ax.set_facecolor(self.C["row_even"])
        ax.tick_params(colors=self._CHART_COLORS["text"], labelsize=7.5)
        ax.title.set_color(self.C["header_fg"])
        ax.title.set_fontsize(9)
        ax.title.set_fontweight("bold")
        for spine in ax.spines.values():
            spine.set_edgecolor(self.C["border"])
            spine.set_linewidth(0.6)
        ax.xaxis.label.set_color(self._CHART_COLORS["text"])
        ax.yaxis.label.set_color(self._CHART_COLORS["text"])
        ax.tick_params(axis="both", which="both", length=0)

    @staticmethod
    def _scale_B(vals: list) -> tuple[list, str]:
        """Return values scaled to billions/millions and a unit label."""
        defined = [v for v in vals if v is not None]
        if not defined:
            return vals, ""
        mx = max(abs(v) for v in defined)
        if mx >= 1e9:
            return [v / 1e9 if v is not None else None for v in vals], "B"
        if mx >= 1e6:
            return [v / 1e6 if v is not None else None for v in vals], "M"
        return vals, ""

    def _get_annual(self, metrics: "FinancialMetrics", key: str, years_asc: list[int]):
        return [_av(metrics, key, y) for y in years_asc]

    # ── Income Statement chart ────────────────────────────────────────────────

    def _update_income_chart(self, metrics: "FinancialMetrics", years: list[int]) -> None:
        if not _MPL_AVAILABLE or "Income Statement" not in self._charts:
            return
        fig, canvas = self._charts["Income Statement"]
        fig.clear()
        self._chart_style(fig)

        years_asc = list(reversed(years))
        xlabels   = [str(y) for y in years_asc]
        x         = list(range(len(years_asc)))

        rev  = self._get_annual(metrics, "revenue",          years_asc)
        gp   = self._get_annual(metrics, "gross_profit",     years_asc)
        oi   = self._get_annual(metrics, "operating_income",  years_asc)
        ni   = self._get_annual(metrics, "net_income",        years_asc)
        rev_s, unit = self._scale_B(rev)
        gp_s,  _    = self._scale_B(gp)
        oi_s,  _    = self._scale_B(oi)
        ni_s,  _    = self._scale_B(ni)

        gm = [
            (g / r * 100) if g is not None and r else None
            for g, r in zip(gp, rev)
        ]
        om = [
            (o / r * 100) if o is not None and r else None
            for o, r in zip(oi, rev)
        ]
        nm = [
            (n / r * 100) if n is not None and r else None
            for n, r in zip(ni, rev)
        ]

        # ── left: bar chart of $ amounts ──
        ax1 = fig.add_subplot(1, 2, 1)
        self._ax_style(ax1)
        w = 0.2
        cc = self._CHART_COLORS
        for i, (ser, lbl, col) in enumerate([
            (rev_s, "Revenue",         cc["blue"]),
            (gp_s,  "Gross Profit",    cc["green_lt"]),
            (oi_s,  "Operating Inc.",  cc["orange"]),
            (ni_s,  "Net Income",      cc["green"]),
        ]):
            xpos  = [xi + i * w for xi in x]
            vals  = [v if v is not None else 0 for v in ser]
            colors = [cc["red_lt"] if v < 0 else col for v in vals]
            ax1.bar(xpos, vals, width=w, label=lbl, color=colors, zorder=3)

        ax1.set_xticks([xi + 1.5 * w for xi in x])
        ax1.set_xticklabels(xlabels, fontsize=7.5)
        ax1.set_title("Revenue & Profit" + (f"  (${unit})" if unit else ""))
        ax1.yaxis.set_major_formatter(
            mticker.FuncFormatter(lambda v, _: f"{v:,.1f}")
        )
        ax1.axhline(0, color=self.C["border"], linewidth=0.8)
        ax1.legend(fontsize=6.5, framealpha=0.7)
        ax1.grid(axis="y", linewidth=0.5, color=self.C["border"], zorder=0)

        # ── right: margin % lines ──
        ax2 = fig.add_subplot(1, 2, 2)
        self._ax_style(ax2)
        for ser, lbl, col in [
            (gm, "Gross Margin",    cc["blue"]),
            (om, "Operating Margin", cc["orange"]),
            (nm, "Net Margin",      cc["green"]),
        ]:
            clean = [(xi, v) for xi, v in zip(x, ser) if v is not None]
            if clean:
                xs, ys = zip(*clean)
                ax2.plot(xs, ys, marker="o", markersize=4, linewidth=1.8,
                         label=lbl, color=col)
        ax2.set_xticks(x)
        ax2.set_xticklabels(xlabels, fontsize=7.5)
        ax2.set_title("Margins (%)")
        ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.1f}%"))
        ax2.axhline(0, color=self.C["border"], linewidth=0.8)
        ax2.legend(fontsize=6.5, framealpha=0.7)
        ax2.grid(axis="y", linewidth=0.5, color=self.C["border"], zorder=0)

        fig.tight_layout(pad=1.2)
        canvas.draw()

    # ── Balance Sheet chart ───────────────────────────────────────────────────

    def _update_balance_chart(self, metrics: "FinancialMetrics", years: list[int]) -> None:
        if not _MPL_AVAILABLE or "Balance Sheet" not in self._charts:
            return
        fig, canvas = self._charts["Balance Sheet"]
        fig.clear()
        self._chart_style(fig)

        years_asc = list(reversed(years))
        xlabels   = [str(y) for y in years_asc]
        x         = list(range(len(years_asc)))
        cc        = self._CHART_COLORS

        ta  = self._get_annual(metrics, "total_assets",         years_asc)
        tl  = self._get_annual(metrics, "total_liabilities",    years_asc)
        eq  = self._get_annual(metrics, "stockholders_equity",  years_asc)
        ca  = self._get_annual(metrics, "current_assets",       years_asc)
        cash = self._get_annual(metrics, "cash",                years_asc)

        ta_s,  unit = self._scale_B(ta)
        tl_s,  _    = self._scale_B(tl)
        eq_s,  _    = self._scale_B(eq)
        ca_s,  _    = self._scale_B(ca)
        cash_s,_    = self._scale_B(cash)

        # ── left: Assets vs Liabilities vs Equity grouped bars ──
        ax1 = fig.add_subplot(1, 2, 1)
        self._ax_style(ax1)
        w = 0.25
        for i, (ser, lbl, col) in enumerate([
            (ta_s, "Total Assets",  cc["blue"]),
            (tl_s, "Total Liab.",   cc["red_lt"]),
            (eq_s, "Equity",        cc["green"]),
        ]):
            xpos  = [xi + i * w for xi in x]
            vals  = [v if v is not None else 0 for v in ser]
            colors = [cc["red"] if v < 0 else col for v in vals]
            ax1.bar(xpos, vals, width=w, label=lbl, color=colors, zorder=3)
        ax1.set_xticks([xi + w for xi in x])
        ax1.set_xticklabels(xlabels, fontsize=7.5)
        ax1.set_title("Assets / Liabilities / Equity" + (f"  (${unit})" if unit else ""))
        ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.1f}"))
        ax1.axhline(0, color=self.C["border"], linewidth=0.8)
        ax1.legend(fontsize=6.5, framealpha=0.7)
        ax1.grid(axis="y", linewidth=0.5, color=self.C["border"], zorder=0)

        # ── right: stacked bar — Cash / Other Current / Long-term ──
        ax2 = fig.add_subplot(1, 2, 2)
        self._ax_style(ax2)
        cash_v  = [v if v is not None else 0 for v in cash_s]
        cur_v   = [(ca if ca is not None else 0) - (c if c is not None else 0)
                   for ca, c in zip(ca_s, cash_s)]
        lt_v    = [(ta if ta is not None else 0) - (ca if ca is not None else 0)
                   for ta, ca in zip(ta_s, ca_s)]
        ax2.bar(x, cash_v, label="Cash",             color=cc["blue"],     zorder=3)
        ax2.bar(x, cur_v,  bottom=cash_v, label="Other Current",  color=cc["blue_lt"],  zorder=3)
        ax2.bar(x, lt_v,   bottom=[a + b for a, b in zip(cash_v, cur_v)],
                label="Long-term Assets", color=cc["grey"], zorder=3)
        ax2.set_xticks(x)
        ax2.set_xticklabels(xlabels, fontsize=7.5)
        ax2.set_title("Asset Composition" + (f"  (${unit})" if unit else ""))
        ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.1f}"))
        ax2.legend(fontsize=6.5, framealpha=0.7)
        ax2.grid(axis="y", linewidth=0.5, color=self.C["border"], zorder=0)

        fig.tight_layout(pad=1.2)
        canvas.draw()

    # ── Cash Flow chart ───────────────────────────────────────────────────────

    def _update_cashflow_chart(self, metrics: "FinancialMetrics", years: list[int]) -> None:
        if not _MPL_AVAILABLE or "Cash Flow" not in self._charts:
            return
        fig, canvas = self._charts["Cash Flow"]
        fig.clear()
        self._chart_style(fig)

        years_asc = list(reversed(years))
        xlabels   = [str(y) for y in years_asc]
        x         = list(range(len(years_asc)))
        cc        = self._CHART_COLORS

        ocf   = self._get_annual(metrics, "operating_cf", years_asc)
        capex = self._get_annual(metrics, "capex",         years_asc)
        fcf   = [
            (o - c) if o is not None and c is not None else None
            for o, c in zip(ocf, capex)
        ]
        divs  = self._get_annual(metrics, "dividends_paid",   years_asc)
        buyb  = self._get_annual(metrics, "share_repurchases", years_asc)

        ocf_s,  unit = self._scale_B(ocf)
        capex_s, _   = self._scale_B(capex)
        fcf_s,   _   = self._scale_B(fcf)
        divs_s,  _   = self._scale_B(divs)
        buyb_s,  _   = self._scale_B(buyb)

        # ── left: OCF / CapEx / FCF bars ──
        ax1 = fig.add_subplot(1, 2, 1)
        self._ax_style(ax1)
        w = 0.25
        for i, (ser, lbl, col) in enumerate([
            (ocf_s,   "Operating CF",  cc["blue"]),
            (capex_s, "CapEx",         cc["red_lt"]),
            (fcf_s,   "Free CF",       cc["green"]),
        ]):
            xpos  = [xi + i * w for xi in x]
            vals  = [v if v is not None else 0 for v in ser]
            colors = [cc["red"] if v < 0 and lbl != "CapEx" else col for v in vals]
            ax1.bar(xpos, vals, width=w, label=lbl, color=colors, zorder=3)
        ax1.set_xticks([xi + w for xi in x])
        ax1.set_xticklabels(xlabels, fontsize=7.5)
        ax1.set_title("Cash Flow" + (f"  (${unit})" if unit else ""))
        ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.1f}"))
        ax1.axhline(0, color=self.C["border"], linewidth=0.8)
        ax1.legend(fontsize=6.5, framealpha=0.7)
        ax1.grid(axis="y", linewidth=0.5, color=self.C["border"], zorder=0)

        # ── right: shareholder returns — dividends + buybacks ──
        ax2 = fig.add_subplot(1, 2, 2)
        self._ax_style(ax2)
        divs_v = [abs(v) if v is not None else 0 for v in divs_s]
        buyb_v = [abs(v) if v is not None else 0 for v in buyb_s]
        ax2.bar(x, divs_v, label="Dividends",       color=cc["purple"],  zorder=3)
        ax2.bar(x, buyb_v, bottom=divs_v, label="Buybacks", color=cc["blue_lt"], zorder=3)
        ax2.set_xticks(x)
        ax2.set_xticklabels(xlabels, fontsize=7.5)
        ax2.set_title("Shareholder Returns" + (f"  (${unit})" if unit else ""))
        ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.1f}"))
        ax2.legend(fontsize=6.5, framealpha=0.7)
        ax2.grid(axis="y", linewidth=0.5, color=self.C["border"], zorder=0)

        fig.tight_layout(pad=1.2)
        canvas.draw()

    # ── Key Ratios chart ──────────────────────────────────────────────────────

    def _update_ratios_chart(self, metrics: "FinancialMetrics", years: list[int]) -> None:
        if not _MPL_AVAILABLE or "Key Ratios" not in self._charts:
            return
        fig, canvas = self._charts["Key Ratios"]
        fig.clear()
        self._chart_style(fig)

        years_asc = list(reversed(years))
        xlabels   = [str(y) for y in years_asc]
        x         = list(range(len(years_asc)))
        cc        = self._CHART_COLORS

        def pct(nk, dk, y):
            n, d = _av(metrics, nk, y), _av(metrics, dk, y)
            return (n / d * 100) if n is not None and d else None

        def rat(nk, dk, y):
            n, d = _av(metrics, nk, y), _av(metrics, dk, y)
            return (n / d) if n is not None and d else None

        gm  = [pct("gross_profit",    "revenue",            y) for y in years_asc]
        om  = [pct("operating_income","revenue",            y) for y in years_asc]
        nm  = [pct("net_income",      "revenue",            y) for y in years_asc]
        roe = [pct("net_income",      "stockholders_equity",y) for y in years_asc]
        cr  = [rat("current_assets",  "current_liabilities",y) for y in years_asc]
        qr  = [
            (((_av(metrics,"current_assets",y) or 0) - (_av(metrics,"inventory",y) or 0)) /
             _av(metrics,"current_liabilities",y))
            if _av(metrics,"current_assets",y) is not None
            and _av(metrics,"current_liabilities",y)
            else None
            for y in years_asc
        ]
        de = [
            (((_av(metrics,"long_term_debt",y) or 0) +
              (_av(metrics,"short_term_debt",y) or 0)) /
             _av(metrics,"stockholders_equity",y))
            if _av(metrics,"stockholders_equity",y)
            and (_av(metrics,"long_term_debt",y) is not None
                 or _av(metrics,"short_term_debt",y) is not None)
            else None
            for y in years_asc
        ]

        # ── left: margin % lines ──
        ax1 = fig.add_subplot(1, 2, 1)
        self._ax_style(ax1)
        for ser, lbl, col in [
            (gm,  "Gross Margin",     cc["blue"]),
            (om,  "Operating Margin", cc["orange"]),
            (nm,  "Net Margin",       cc["green"]),
            (roe, "Return on Equity", cc["purple"]),
        ]:
            clean = [(xi, v) for xi, v in zip(x, ser) if v is not None]
            if clean:
                xs, ys = zip(*clean)
                ax1.plot(xs, ys, marker="o", markersize=4, linewidth=1.8,
                         label=lbl, color=col)
        ax1.set_xticks(x)
        ax1.set_xticklabels(xlabels, fontsize=7.5)
        ax1.set_title("Profitability Margins (%)")
        ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.1f}%"))
        ax1.axhline(0, color=self.C["border"], linewidth=0.8)
        ax1.legend(fontsize=6.5, framealpha=0.7)
        ax1.grid(axis="y", linewidth=0.5, color=self.C["border"], zorder=0)

        # ── right: liquidity & leverage ──
        ax2 = fig.add_subplot(1, 2, 2)
        self._ax_style(ax2)
        for ser, lbl, col in [
            (cr, "Current Ratio",  cc["blue"]),
            (qr, "Quick Ratio",    cc["blue_lt"]),
            (de, "Debt/Equity",    cc["red"]),
        ]:
            clean = [(xi, v) for xi, v in zip(x, ser) if v is not None]
            if clean:
                xs, ys = zip(*clean)
                ax2.plot(xs, ys, marker="o", markersize=4, linewidth=1.8,
                         label=lbl, color=col)
        ax2.axhline(1.0, color=cc["grey"], linewidth=1, linestyle="--", alpha=0.7)
        ax2.set_xticks(x)
        ax2.set_xticklabels(xlabels, fontsize=7.5)
        ax2.set_title("Liquidity & Leverage  (×)")
        ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.2f}×"))
        ax2.legend(fontsize=6.5, framealpha=0.7)
        ax2.grid(axis="y", linewidth=0.5, color=self.C["border"], zorder=0)

        fig.tight_layout(pad=1.2)
        canvas.draw()

    # ── Red Flags chart ───────────────────────────────────────────────────────

    def _update_flags_chart(self, flags, metrics: "FinancialMetrics") -> None:
        if not _MPL_AVAILABLE or "Red Flags" not in self._charts:
            return
        fig, canvas = self._charts["Red Flags"]
        fig.clear()
        self._chart_style(fig)
        cc = self._CHART_COLORS

        counts = {s: sum(1 for f in flags if f.severity == s)
                  for s in ("CRITICAL", "WARNING", "INFO")}

        # ── top: flag severity horizontal bars ──
        ax1 = fig.add_subplot(2, 1, 1)
        self._ax_style(ax1)
        labels  = ["CRITICAL", "WARNING", "INFO"]
        vals    = [counts[s] for s in labels]
        colors  = [cc["red"], cc["orange"], cc["blue"]]
        bars    = ax1.barh(labels, vals, color=colors, zorder=3)
        for bar, v in zip(bars, vals):
            if v:
                ax1.text(bar.get_width() + 0.05, bar.get_y() + bar.get_height() / 2,
                         str(v), va="center", fontsize=8, color=cc["text"])
        ax1.set_xlim(0, max(vals or [1]) + 1.5)
        ax1.set_title("Flag Severity Counts")
        ax1.xaxis.set_major_locator(mticker.MaxNLocator(integer=True))
        ax1.grid(axis="x", linewidth=0.5, color=self.C["border"], zorder=0)

        # ── bottom: margin snapshot (latest year) ──
        ax2 = fig.add_subplot(2, 1, 2)
        self._ax_style(ax2)
        r = metrics.ratios
        ratio_items = [
            ("Gross\nMargin",    r.get("gross_margin_pct"),     cc["blue"]),
            ("Op.\nMargin",      r.get("operating_margin_pct"), cc["orange"]),
            ("Net\nMargin",      r.get("net_margin_pct"),       cc["green"]),
            ("FCF\nMargin",      r.get("fcf_margin_pct"),       cc["blue_lt"]),
            ("ROE",              r.get("roe_pct"),              cc["purple"]),
            ("ROA",              r.get("roa_pct"),              cc["grey"]),
        ]
        xlabels_r = [item[0] for item in ratio_items]
        xvals     = list(range(len(ratio_items)))
        yvals     = [item[1] if item[1] is not None else 0 for item in ratio_items]
        bar_colors = [cc["red_lt"] if v < 0 else item[2]
                      for v, item in zip(yvals, ratio_items)]
        ax2.bar(xvals, yvals, color=bar_colors, zorder=3)
        ax2.set_xticks(xvals)
        ax2.set_xticklabels(xlabels_r, fontsize=7)
        ax2.set_title("Key Margins — Latest Period (%)")
        ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.1f}%"))
        ax2.axhline(0, color=self.C["border"], linewidth=0.8)
        ax2.grid(axis="y", linewidth=0.5, color=self.C["border"], zorder=0)

        fig.tight_layout(pad=1.0)
        canvas.draw()

    # ── Red Flags tab ─────────────────────────────────────────────────────────

    def _update_flags(
        self, ticker: str, flags, metrics: FinancialMetrics, years: list[int]
    ) -> None:
        t = self._flags_text
        t.configure(state=tk.NORMAL)
        t.delete("1.0", tk.END)

        t.insert(tk.END, f"Red Flag Analysis — {ticker}\n", "h1")
        t.insert(tk.END, f"Based on annual financial data from FY{years[-1]} to FY{years[0]}\n\n")

        if not flags:
            t.insert(tk.END, "✅  No red flags detected.\n", "ok")
        else:
            counts = {s: sum(1 for f in flags if f.severity == s)
                      for s in ("CRITICAL", "WARNING", "INFO")}
            t.insert(
                tk.END,
                f"Summary: {counts['CRITICAL']} CRITICAL   "
                f"{counts['WARNING']} WARNING   {counts['INFO']} INFO\n\n",
                "h2",
            )
            icons = {"CRITICAL": "🔴", "WARNING": "🟡", "INFO": "🔵"}
            for sev in ("CRITICAL", "WARNING", "INFO"):
                for f in [x for x in flags if x.severity == sev]:
                    t.insert(tk.END,
                             f"{icons[sev]} [{f.severity}] {f.category}\n", sev)
                    t.insert(tk.END, f"    {f.message}\n\n")

        r = metrics.ratios
        t.insert(tk.END, "\nKey Ratios Snapshot (Latest Annual Period)\n\n", "h2")

        ratio_rows = [
            ("─── Liquidity ───────────────────────────────", None,  None),
            ("Current Ratio",         r.get("current_ratio"),      "x"),
            ("Quick Ratio",           r.get("quick_ratio"),        "x"),
            ("Cash Ratio",            r.get("cash_ratio"),         "x"),
            ("─── Profitability ──────────────────────────", None,  None),
            ("Gross Margin",          r.get("gross_margin_pct"),   "pct"),
            ("Operating Margin",      r.get("operating_margin_pct"), "pct"),
            ("Net Margin",            r.get("net_margin_pct"),     "pct"),
            ("FCF Margin",            r.get("fcf_margin_pct"),     "pct"),
            ("Return on Equity",      r.get("roe_pct"),            "pct"),
            ("Return on Assets",      r.get("roa_pct"),            "pct"),
            ("─── Leverage ───────────────────────────────", None,  None),
            ("Debt-to-Equity",        r.get("debt_to_equity"),     "x"),
            ("Interest Coverage",     r.get("interest_coverage"),  "x"),
            ("Net Debt",              r.get("net_debt"),           "USD"),
            ("─── Cash Flow ──────────────────────────────", None,  None),
            ("Free Cash Flow",        r.get("free_cash_flow"),     "USD"),
            ("OCF / Current Liab.",   r.get("ocf_to_cl"),          "x"),
        ]
        for name, val, unit in ratio_rows:
            if val is None and unit is None:
                t.insert(tk.END, f"\n  {name}\n", "h2")
            else:
                fv = _fmt(val, unit) if val is not None else "N/A"
                t.insert(tk.END, f"  {name:<28}  {fv}\n", "mono")

        # ── Achievements ──
        t.insert(tk.END, "\n\nACHIEVEMENTS\n\n", "h1")
        unlocked = self._check_achievements(metrics)
        score, tier = self._compute_score(metrics)
        n_unlocked = sum(unlocked)
        t.insert(tk.END,
                 f"  Financial Tier: {tier}   Score: {score}/100   "
                 f"Unlocked: {n_unlocked}/{len(self._ACHIEVEMENTS)}\n\n",
                 "h2")
        for (name, desc, _), earned in zip(self._ACHIEVEMENTS, unlocked):
            if earned:
                t.insert(tk.END, f"  ★  {name}\n", "achievement")
                t.insert(tk.END, f"     {desc}\n\n", "mono")
            else:
                t.insert(tk.END, f"  ✗  {name}\n", "locked")
                t.insert(tk.END, f"     {desc}\n\n", "locked")

        t.configure(state=tk.DISABLED)

    # ── Year filter ───────────────────────────────────────────────────────────

    def _apply_filter(self) -> None:
        if self._current_metrics is None:
            return
        min_y = self._min_year_var.get()
        max_y = self._max_year_var.get()
        if min_y > max_y:
            messagebox.showwarning("Invalid Range", "Min year must be ≤ Max year.")
            return
        years = get_fiscal_years(self._current_metrics, min_year=min_y, max_year=max_y)
        if not years:
            messagebox.showwarning(
                "No Data", f"No annual data available between FY{min_y} and FY{max_y}."
            )
            return
        self._render_tables(
            self._current_ticker,
            self._current_company,
            self._current_metrics,
            self._current_flags,
            years,
        )

    def _reset_filter(self) -> None:
        if self._current_metrics is None:
            return
        all_years = get_fiscal_years(self._current_metrics)
        if all_years:
            self._min_year_var.set(all_years[-1])
            self._max_year_var.set(all_years[0])
        self._apply_filter()

    def _render_tables(
        self,
        ticker: str,
        company: dict,
        metrics: FinancialMetrics,
        flags: list,
        years: list[int],
    ) -> None:
        """Populate all tabs for the given years without re-fetching data."""
        yr_range = f"FY{years[-1]}–FY{years[0]}" if len(years) > 1 else f"FY{years[0]}"
        self._info_var.set(
            f"  {company['name']}    |    {ticker}    |    "
            f"CIK: {company['cik']}    |    {yr_range}    |    "
            f"{len(years)} annual periods"
        )

        self._populate_table(
            self._trees["Income Statement"],
            build_income_rows(metrics, years), years,
        )
        self._populate_table(
            self._trees["Balance Sheet"],
            build_balance_rows(metrics, years), years,
        )
        self._populate_table(
            self._trees["Cash Flow"],
            build_cashflow_rows(metrics, years), years,
        )
        self._populate_table(
            self._trees["Key Ratios"],
            build_ratios_rows(metrics, years), years,
        )
        self._update_flags(ticker, flags, metrics, years)

        self._update_income_chart(metrics, years)
        self._update_balance_chart(metrics, years)
        self._update_cashflow_chart(metrics, years)
        self._update_ratios_chart(metrics, years)
        self._update_flags_chart(flags, metrics)
        self._update_hud(metrics)

        n_crit = sum(1 for f in flags if f.severity == "CRITICAL")
        n_warn = sum(1 for f in flags if f.severity == "WARNING")
        score, tier = self._compute_score(metrics)
        self._set_status(
            f"DATA ACQUIRED: {ticker}  ▸  {len(years)} years: {yr_range}  "
            f"▸  Score: {score}/100 [{tier}]  "
            f"▸  {n_crit} CRITICAL  {n_warn} WARNING"
        )

    # ── Search / Autocomplete ─────────────────────────────────────────────────

    def _on_type(self, *_) -> None:
        text = self._search_var.get()
        if "  —  " in text:
            return
        text = text.upper().strip()
        if len(text) < 1 or not self._company_list:
            return
        ticker_hits = sorted(t for t in self._company_list if t.startswith(text))[:12]
        name_hits   = [
            t for t, v in self._company_list.items()
            if text.lower() in v["name"].lower() and t not in ticker_hits
        ][:8]
        suggestions = ticker_hits + name_hits
        self._combo["values"] = [
            f"{t}  —  {self._company_list[t]['name']}" for t in suggestions
        ]

    def _on_combo_select(self, _event=None) -> None:
        val = self._search_var.get()
        if "  —  " in val:
            self._search_var.set(val.split("  —  ")[0].strip())
        self._on_search()

    def _on_search(self, _event=None) -> None:
        raw = self._search_var.get()
        if "  —  " in raw:
            raw = raw.split("  —  ")[0]
            self._search_var.set(raw.strip())

        ticker = raw.strip().upper()
        if not ticker:
            return

        if not self._company_list:
            self._set_status("Company list still loading — please wait…")
            return

        if ticker not in self._company_list:
            matches = [
                t for t, v in self._company_list.items()
                if ticker.lower() in v["name"].lower()
            ]
            if len(matches) == 1:
                ticker = matches[0]
                self._search_var.set(ticker)
            elif matches:
                self._combo["values"] = [
                    f"{t}  —  {self._company_list[t]['name']}"
                    for t in matches[:20]
                ]
                self._combo.event_generate("<Down>")
                return
            else:
                messagebox.showerror(
                    "Not Found",
                    f"'{ticker}' not found in EDGAR.\n\n"
                    "Try the company's full name or check the ticker spelling.",
                )
                return

        company = self._company_list[ticker]
        self._set_status(f"Fetching {ticker} — {company['name']}…")
        self._info_var.set(f"⏳  Loading {ticker} — {company['name']}…")
        self._search_btn.configure(state="disabled")
        self.master.title(f"Stock Simplify — {ticker}")

        threading.Thread(
            target=self._fetch_data, args=(ticker, company), daemon=True
        ).start()

    def _clear(self) -> None:
        self._search_var.set("")
        self._info_var.set("Enter a ticker symbol (e.g. AAPL) or company name to begin.")
        self._set_status("Ready")
        self._search_btn.configure(state="normal")
        self.master.title("Stock Simplify — EDGAR Financial Viewer")
        self._reset_price_bar()
        for tree in self._trees.values():
            tree.delete(*tree.get_children())
        self._flags_text.configure(state=tk.NORMAL)
        self._flags_text.delete("1.0", tk.END)
        self._flags_text.configure(state=tk.DISABLED)
        for tag in self._news_link_tags:
            try:
                self._news_text.tag_delete(tag)
            except tk.TclError:
                pass
        self._news_link_tags.clear()
        self._news_text.configure(state=tk.NORMAL)
        self._news_text.delete("1.0", tk.END)
        self._news_text.insert(tk.END, "\n\n  Search for a stock to load top stories.", "prompt")
        self._news_text.configure(state=tk.DISABLED)
        self._reset_sentiment_tab()
        if _MPL_AVAILABLE:
            for fig, canvas in self._charts.values():
                fig.clear()
                canvas.draw()
        self._reset_hud()
        self._current_metrics = None
        self._current_flags   = []
        self._current_ticker  = ""
        self._current_company = {}
        current_yr = _dt.now().year
        self._min_spin.configure(from_=1993, to=current_yr)
        self._max_spin.configure(from_=1993, to=current_yr)
        self._min_year_var.set(current_yr - 9)
        self._max_year_var.set(current_yr)
        self._avail_years_lbl.configure(text="")

    # ── Live stock price fetch ────────────────────────────────────────────────

    def _fetch_price_task(self, ticker: str, company_name: str) -> None:
        """Fetch the current quote for the searched stock; puts result on queue."""
        _yf = requests.Session()
        _yf.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "application/json",
        })
        try:
            url  = (
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                f"?interval=1d&range=1d"
            )
            resp = _yf.get(url, timeout=10)
            resp.raise_for_status()
            meta  = resp.json()["chart"]["result"][0]["meta"]
            price = float(meta.get("regularMarketPrice") or 0)
            prev  = float(
                meta.get("chartPreviousClose")
                or meta.get("previousClose")
                or price
            )
            self._queue.put(("price", {
                "ticker":  ticker,
                "company": company_name,
                "price":   price,
                "change":  price - prev,
                "pct":     (price / prev - 1) * 100 if prev else 0.0,
            }))
        except Exception:
            self._queue.put(("price", {
                "ticker":  ticker,
                "company": company_name,
                "price":   None,
            }))

    def _update_price_bar(self, data: dict) -> None:
        """Populate the price bar with live quote data (called on main thread)."""
        ticker  = data["ticker"]
        company = data.get("company") or ticker
        price   = data.get("price")

        self._price_ticker_lbl.configure(
            text=f"  {ticker}", fg=self.C["header_fg"],
        )
        self._price_company_lbl.configure(text=company, fg=self.C["subhead_fg"])

        if price is None:
            self._price_lbl.configure(text="  N/A", fg=self.C["subhead_fg"])
            self._price_change_lbl.configure(text="  unavailable", fg=self.C["subhead_fg"])
            return

        change = data["change"]
        pct    = data["pct"]
        arrow  = "▲" if change >= 0 else "▼"
        color  = self.C["positive"] if change >= 0 else self.C["negative"]

        self._price_lbl.configure(
            text=f"  ${price:,.2f}", fg=self.C["header_fg"],
        )
        self._price_change_lbl.configure(
            text=f"  {arrow} {abs(change):,.2f}  ({pct:+.2f}%)",
            fg=color,
        )

    def _reset_price_bar(self) -> None:
        self._price_ticker_lbl.configure(text="  ——", fg=self.C["subhead_fg"])
        self._price_lbl.configure(text="—", fg=self.C["header_fg"])
        self._price_change_lbl.configure(text="—", fg=self.C["subhead_fg"])
        self._price_company_lbl.configure(
            text="Search for a stock to see live price",
            fg=self.C["subhead_fg"],
        )

    # ── Live market index polling ─────────────────────────────────────────────

    _TICKER_REFRESH_MS = 60_000   # 1 minute between refreshes

    def _start_ticker_poll(self) -> None:
        """Spawn a one-shot fetch thread then reschedule for the next interval."""
        threading.Thread(target=self._fetch_market_indices, daemon=True).start()
        self.after(self._TICKER_REFRESH_MS, self._start_ticker_poll)

    def _fetch_market_indices(self) -> None:
        """Fetch current quote data for every index; runs in a background thread."""
        _yf_session = requests.Session()
        _yf_session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept": "application/json",
        })

        results: dict = {}
        for symbol, _, _ in self._MARKET_INDICES:
            try:
                sym_enc = symbol.replace("^", "%5E")
                url  = (
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{sym_enc}"
                    f"?interval=1d&range=1d"
                )
                resp = _yf_session.get(url, timeout=10)
                resp.raise_for_status()
                meta  = resp.json()["chart"]["result"][0]["meta"]
                price = float(meta.get("regularMarketPrice") or 0)
                prev  = float(
                    meta.get("chartPreviousClose")
                    or meta.get("previousClose")
                    or price
                )
                results[symbol] = {
                    "price":  price,
                    "change": price - prev,
                    "pct":    (price / prev - 1) * 100 if prev else 0.0,
                }
            except Exception:
                results[symbol] = None

        self._queue.put(("tickers", results))

    def _update_ticker_widgets(self, data: dict) -> None:
        """Push fresh quote data into the ticker bar labels (called on main thread)."""
        for symbol, _, _ in self._MARKET_INDICES:
            widgets = self._ticker_widgets.get(symbol)
            if not widgets:
                continue
            price_lbl, chg_lbl = widgets
            d = data.get(symbol)

            if d is None:
                price_lbl.configure(text="  N/A     ", fg=self.C["subhead_fg"])
                chg_lbl.configure(  text="  unavailable", fg=self.C["subhead_fg"])
                continue

            price  = d["price"]
            change = d["change"]
            pct    = d["pct"]
            arrow  = "▲" if change >= 0 else "▼"
            color  = self.C["positive"] if change >= 0 else self.C["negative"]

            price_lbl.configure(
                text=f"  {price:>12,.2f}  ",
                fg=self.C["header_fg"],
            )
            chg_lbl.configure(
                text=f"  {arrow} {abs(change):,.2f}  ({pct:+.2f}%)",
                fg=color,
            )

        self._ticker_ts_var.set(
            _dt.now().strftime("  updated %H:%M:%S")
        )

    # ── Company profile (Wikipedia REST API) ─────────────────────────────────

    # ── Profile helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _wiki_summary(sess: requests.Session, title: str) -> tuple[str, str]:
        """
        Fetch a single Wikipedia summary by exact title.
        Returns (extract, canonical_title) or ("", "") on failure / too short.
        """
        try:
            encoded = requests.utils.quote(title, safe="")
            resp = sess.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded}",
                timeout=10,
            )
            if resp.status_code != 200:
                return "", ""
            data = resp.json()
            if data.get("type") == "disambiguation":
                return "", ""
            text = data.get("extract", "").strip()
            if len(text) > 80:
                return text, data.get("title", title)
        except Exception:
            pass
        return "", ""

    @staticmethod
    def _wiki_search_titles(sess: requests.Session, query: str) -> list[str]:
        """
        Use the Wikipedia opensearch API to turn a free-text query into a
        ranked list of article titles (up to 5).
        """
        try:
            resp = sess.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action":   "query",
                    "list":     "search",
                    "srsearch": query,
                    "srlimit":  "5",
                    "format":   "json",
                    "srprop":   "",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                hits = resp.json().get("query", {}).get("search", [])
                return [h["title"] for h in hits]
        except Exception:
            pass
        return []

    @staticmethod
    def _duckduckgo_abstract(sess: requests.Session, query: str) -> tuple[str, str]:
        """
        Query the DuckDuckGo Instant Answer API (free, no key).
        Returns (abstract_text, source_label) or ("", "").
        """
        try:
            resp = sess.get(
                "https://api.duckduckgo.com/",
                params={
                    "q":            query,
                    "format":       "json",
                    "no_html":      "1",
                    "skip_disambig":"1",
                },
                timeout=12,
            )
            if resp.status_code != 200:
                return "", ""
            data     = resp.json()
            abstract = (data.get("AbstractText") or "").strip()
            source   = (data.get("AbstractSource") or "DuckDuckGo").strip()
            if len(abstract) > 80:
                return abstract, source
            # AbstractText empty — try the first related topic that has a Text
            for topic in data.get("RelatedTopics", []):
                text = (topic.get("Text") or "").strip()
                if len(text) > 80:
                    return text, source or "DuckDuckGo"
        except Exception:
            pass
        return "", ""

    def _fetch_profile_task(self, ticker: str, company_name: str) -> None:
        """
        Fetch a company business description from web sources.
        Three-stage fallback (all free, no API key required):

          Stage 1 — Wikipedia direct:  try several exact title candidates.
          Stage 2 — Wikipedia search:  use the opensearch API to find the
                                        right article when titles don't match.
          Stage 3 — DuckDuckGo:        Instant Answer API as final fallback.
        """
        _sess = requests.Session()
        _sess.headers.update({
            "User-Agent": "StockSimplify/2.0 (educational; contact@example.com)",
            "Accept":     "application/json",
        })

        # Strip common legal suffixes to build cleaner search tokens
        clean_name = re.sub(
            r"\s+(Inc\.?|Corp\.?|Ltd\.?|LLC\.?|Co\.?|PLC\.?|N\.?V\.?|S\.?A\.?)$",
            "", company_name, flags=re.IGNORECASE,
        ).strip()
        # Also normalise ampersands which trip up URL encoding
        clean_name_and = clean_name.replace("&", "and")

        extract    = ""
        wiki_title = ""
        source_lbl = "Wikipedia"

        # ── Stage 1: direct Wikipedia title candidates ────────────────────────
        direct_candidates = [
            company_name,
            clean_name,
            clean_name_and,
            f"{clean_name} (company)",
            f"{clean_name_and} (company)",
            f"{clean_name} Inc.",
            f"{clean_name} Corporation",
        ]
        for title in direct_candidates:
            extract, wiki_title = self._wiki_summary(_sess, title)
            if extract:
                break

        # ── Stage 2: Wikipedia search API ────────────────────────────────────
        if not extract:
            search_queries = [
                f"{company_name} company",
                f"{clean_name} company",
                f"{clean_name_and} company {ticker}",
            ]
            for sq in search_queries:
                for title in self._wiki_search_titles(_sess, sq):
                    extract, wiki_title = self._wiki_summary(_sess, title)
                    if extract:
                        break
                if extract:
                    break

        # ── Stage 3: DuckDuckGo Instant Answer ───────────────────────────────
        if not extract:
            ddg_queries = [
                f"{company_name} company",
                f"{clean_name} {ticker} company",
            ]
            for dq in ddg_queries:
                extract, source_lbl = self._duckduckgo_abstract(_sess, dq)
                if extract:
                    wiki_title = source_lbl   # displayed as "[DuckDuckGo]" etc.
                    break

        self._queue.put(("profile", {
            "ticker":  ticker,
            "extract": extract,
            "title":   wiki_title,
        }))

    # ── News fetching ─────────────────────────────────────────────────────────

    _NEWS_SOURCES = [
        # Yahoo Finance RSS — most relevant for individual stocks
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US",
        # Google News RSS — broader coverage fallback
        "https://news.google.com/rss/search?q={ticker}+stock&hl=en-US&gl=US&ceid=US:en",
    ]

    def _fetch_news_task(self, ticker: str) -> None:
        """Try each RSS source in order; put the first non-empty result on the queue."""
        _session = requests.Session()
        _session.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )

        articles: list[dict] = []
        for url_tpl in self._NEWS_SOURCES:
            if articles:
                break
            try:
                resp = _session.get(
                    url_tpl.format(ticker=ticker), timeout=12
                )
                resp.raise_for_status()
                articles = _parse_rss(resp.content)
            except Exception:
                pass

        self._queue.put(("news", {"ticker": ticker, "articles": articles}))

    def _update_news_tab(self, ticker: str, articles: list[dict]) -> None:
        """Populate the Top Stories tab (must be called on the main thread)."""
        t = self._news_text

        # Remove previous clickable link tags
        for tag in self._news_link_tags:
            try:
                t.tag_delete(tag)
            except tk.TclError:
                pass
        self._news_link_tags.clear()

        t.configure(state=tk.NORMAL)
        t.delete("1.0", tk.END)

        t.insert(tk.END, f"TOP STORIES  —  {ticker}\n\n", "header")

        if not articles:
            t.insert(tk.END, "No news articles found for this ticker.\n", "prompt")
            t.configure(state=tk.DISABLED)
            return

        t.insert(
            tk.END,
            f"  {len(articles)} article(s)  ·  click a headline to open in browser\n\n",
            "meta",
        )

        for i, art in enumerate(articles, 1):
            url       = art["link"]
            link_tag  = f"news_link_{i}"

            # Article number
            t.insert(tk.END, f"  {i:>2}.  ", "number")

            # Headline — clickable if we have a URL
            if url:
                t.insert(tk.END, f"{art['title']}\n", (link_tag, "headline"))
                # Click → open browser
                t.tag_bind(
                    link_tag, "<Button-1>",
                    lambda _, u=url: webbrowser.open(u),
                )
                # Hover on
                t.tag_bind(
                    link_tag, "<Enter>",
                    lambda _, lt=link_tag: (
                        t.configure(cursor="hand2"),
                        t.tag_configure(lt, foreground=self.C["positive"], underline=True),
                    ),
                )
                # Hover off
                t.tag_bind(
                    link_tag, "<Leave>",
                    lambda _, lt=link_tag: (
                        t.configure(cursor="arrow"),
                        t.tag_configure(lt, foreground=self.C["header_fg"], underline=False),
                    ),
                )
                self._news_link_tags.append(link_tag)
            else:
                t.insert(tk.END, f"{art['title']}\n", "headline")

            # Date / meta
            if art["pubDate"]:
                t.insert(tk.END, f"        {art['pubDate']}\n", "meta")

            # Description summary
            if art["description"]:
                t.insert(tk.END, f"        {art['description']}\n", "desc")

            t.insert(tk.END, "\n")

            # Separator between articles (not after last)
            if i < len(articles):
                t.insert(tk.END, "       " + "─" * 62 + "\n\n", "sep")

        t.configure(state=tk.DISABLED)

    # ── Social Media Sentiment ────────────────────────────────────────────────

    _SENTIMENT_PLATFORMS = [
        ("reddit", "Reddit"),
    ]

    def _fetch_sentiment_task(self, ticker: str) -> None:
        """Fetch Reddit sentiment; put result on queue."""
        result: dict = {}

        # ── Reddit ────────────────────────────────────────────────────────────
        try:
            _rd = requests.Session()
            _rd.headers["User-Agent"] = "StockSimplify/1.0 financial-research"
            resp = _rd.get(
                "https://www.reddit.com/search.json",
                params={
                    "q":     f"{ticker} stock",
                    "sort":  "hot",
                    "t":     "week",
                    "limit": "25",
                    "type":  "link",
                },
                timeout=12,
            )
            if resp.status_code == 200:
                posts = resp.json().get("data", {}).get("children", [])
                total_pos = total_neg = 0
                enriched: list[dict] = []
                for p in posts:
                    d    = p.get("data", {})
                    text = (d.get("title") or "") + " " + (d.get("selftext") or "")
                    pos, neg = _score_text(text)
                    total_pos += pos
                    total_neg += neg
                    enriched.append({
                        "title":     (d.get("title") or "")[:100],
                        "score":     d.get("score", 0),
                        "subreddit": d.get("subreddit", ""),
                        "url":       "https://reddit.com" + (d.get("permalink") or ""),
                        "pos":       pos,
                        "neg":       neg,
                    })
                enriched.sort(key=lambda x: x["score"], reverse=True)
                rd_score = (
                    total_pos / (total_pos + total_neg) * 100
                    if total_pos + total_neg > 0 else 50.0
                )
                result["reddit"] = {
                    "score":    round(rd_score),
                    "total":    len(posts),
                    "pos":      total_pos,
                    "neg":      total_neg,
                    "snippets": enriched[:5],
                }
            else:
                result["reddit"] = {"error": f"HTTP {resp.status_code}"}
        except Exception as exc:
            result["reddit"] = {"error": str(exc)[:80]}

        # ── Overall score (weighted average of available sources) ─────────────
        weighted: list[tuple[float, float]] = []
        for key, wt in [("reddit", 1.0)]:
            if "score" in result.get(key, {}):
                weighted.append((result[key]["score"], wt))
        if weighted:
            total_wt = sum(w for _, w in weighted)
            overall  = sum(s * w for s, w in weighted) / total_wt
        else:
            overall = 50.0

        self._queue.put(("sentiment", {
            "ticker":    ticker,
            "overall":   round(overall),
            "platforms": result,
        }))

    def _update_sentiment_tab(self, ticker: str, overall: int, platforms: dict) -> None:
        """Populate the Social Media Sentiment tab (must be called on main thread)."""
        t = self._sent_text

        for tag in self._sent_link_tags:
            try:
                t.tag_delete(tag)
            except tk.TclError:
                pass
        self._sent_link_tags.clear()

        t.configure(state=tk.NORMAL)
        t.delete("1.0", tk.END)

        # ── Header ────────────────────────────────────────────────────────────
        t.insert(tk.END, f"SOCIAL MEDIA SENTIMENT  —  {ticker}\n\n", "header")

        # ── Overall bar ───────────────────────────────────────────────────────
        if overall >= 75:
            sent_label, color_tag = "VERY BULLISH",  "sent_strong_pos"
        elif overall >= 60:
            sent_label, color_tag = "BULLISH",        "sent_pos"
        elif overall >= 40:
            sent_label, color_tag = "NEUTRAL",        "sent_neutral"
        elif overall >= 25:
            sent_label, color_tag = "BEARISH",        "sent_neg"
        else:
            sent_label, color_tag = "VERY BEARISH",   "sent_strong_neg"

        filled  = int(overall / 5)          # 0-20 blocks
        bar_str = "  " + "█" * filled + "░" * (20 - filled) + f"  {overall}/100\n"
        t.insert(tk.END, "  OVERALL SENTIMENT\n", "section_h")
        t.insert(tk.END, bar_str,          color_tag)
        t.insert(tk.END, f"  {sent_label}\n\n", color_tag)
        t.insert(tk.END, "  " + "─" * 64 + "\n\n", "sep")

        # ── Per-platform ──────────────────────────────────────────────────────
        link_idx = 0
        for key, display_name in self._SENTIMENT_PLATFORMS:
            d = platforms.get(key, {})
            t.insert(tk.END, f"  [{display_name}]\n", "platform_h")

            if d.get("unavailable"):
                t.insert(tk.END, f"    —  {d['unavailable']}\n\n", "unavail")
                continue
            if d.get("error"):
                t.insert(tk.END, f"    Error: {d['error']}\n\n", "error_text")
                continue
            if d.get("not_found"):
                t.insert(tk.END, f"    Ticker not listed on {display_name}\n\n", "unavail")
                continue

            score = d.get("score", 50)
            if score >= 60:
                plabel, ptag = "BULLISH",  "sent_pos"
            elif score < 40:
                plabel, ptag = "BEARISH",  "sent_neg"
            else:
                plabel, ptag = "NEUTRAL",  "sent_neutral"

            pbar = "█" * int(score / 10) + "░" * (10 - int(score / 10))
            t.insert(tk.END, f"    {pbar}  {score}/100  ", "mono")
            t.insert(tk.END, plabel + "\n", ptag)

            if key == "reddit":
                total = d.get("total", 0)
                pos   = d.get("pos",   0)
                neg   = d.get("neg",   0)
                t.insert(
                    tk.END,
                    f"    {total} posts analyzed  ·  "
                    f"{pos} positive signals  ·  {neg} negative signals\n\n",
                    "meta",
                )
                for post in d.get("snippets", []):
                    url      = post["url"]
                    link_tag = f"sent_link_{link_idx}"
                    link_idx += 1
                    sub      = post["subreddit"]
                    sc       = post["score"]
                    t.insert(tk.END, f"    ↑{sc:<6} r/{sub:<22} ", "meta")
                    t.insert(tk.END, post["title"] + "\n", (link_tag, "headline"))
                    if url:
                        t.tag_bind(link_tag, "<Button-1>",
                                   lambda _, u=url: webbrowser.open(u))
                        t.tag_bind(link_tag, "<Enter>",
                                   lambda _, lt=link_tag: (
                                       t.configure(cursor="hand2"),
                                       t.tag_configure(lt, foreground=self.C["positive"],
                                                       underline=True),
                                   ))
                        t.tag_bind(link_tag, "<Leave>",
                                   lambda _, lt=link_tag: (
                                       t.configure(cursor="arrow"),
                                       t.tag_configure(lt, foreground=self.C["header_fg"],
                                                       underline=False),
                                   ))
                        self._sent_link_tags.append(link_tag)
                t.insert(tk.END, "\n")

        t.configure(state=tk.DISABLED)

    def _reset_sentiment_tab(self) -> None:
        for tag in self._sent_link_tags:
            try:
                self._sent_text.tag_delete(tag)
            except tk.TclError:
                pass
        self._sent_link_tags.clear()
        self._sent_text.configure(state=tk.NORMAL)
        self._sent_text.delete("1.0", tk.END)
        self._sent_text.insert(tk.END, "SOCIAL MEDIA SENTIMENT\n\n", "header")
        self._sent_text.insert(
            tk.END, "  Search a stock to load sentiment analysis.\n", "prompt"
        )
        self._sent_text.configure(state=tk.DISABLED)

    # ── Background workers ────────────────────────────────────────────────────

    def _start_load_companies(self) -> None:
        threading.Thread(target=self._load_companies, daemon=True).start()

    def _load_companies(self) -> None:
        try:
            self._queue.put(("companies", fetch_company_list()))
        except Exception as exc:
            self._queue.put(("error", f"Failed to load company list:\n{exc}"))

    def _fetch_data(self, ticker: str, company: dict) -> None:
        try:
            facts   = get(XBRL_COMPANY_FACTS.format(cik=company["cik"])).json()
            metrics = build_financial_metrics(facts)
            qualitative = QualitativeData()
            filing_form = ""
            if _BS4_AVAILABLE:
                company_dir = Path("edgar_filings") / ticker
                if company_dir.exists():
                    qualitative = _best_qualitative_from_dir(company_dir)
                    filing_form = qualitative.form_type
            flags   = detect_red_flags(metrics, qualitative)
            self._queue.put(("data", {
                "ticker":   ticker,
                "company":  company,
                "metrics":  metrics,
                "flags":    flags,
                "overview": qualitative.business_overview,
                "overview_form": filing_form,
            }))
        except Exception as exc:
            self._queue.put(("error", f"Failed to fetch data for {ticker}:\n{exc}"))

    # ── Queue polling / UI updates ────────────────────────────────────────────

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "companies":
                    self._company_list = payload
                    n = len(payload)
                    self._load_lbl.configure(text=f"◉  {n:,} COMPANIES INDEXED")
                    self._set_status(f"SYSTEM READY  ▸  {n:,} companies available")
                elif kind == "data":
                    self._update_display(payload)
                elif kind == "tickers":
                    self._update_ticker_widgets(payload)
                elif kind == "news":
                    self._update_news_tab(payload["ticker"], payload["articles"])
                elif kind == "price":
                    self._update_price_bar(payload)
                elif kind == "sentiment":
                    self._update_sentiment_tab(
                        payload["ticker"], payload["overall"], payload["platforms"]
                    )
                elif kind == "profile":
                    self._update_profile(payload)
                elif kind == "error":
                    messagebox.showerror("Error", payload)
                    self._search_btn.configure(state="normal")
                    self._set_status("Error")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _update_display(self, payload: dict) -> None:
        ticker  = payload["ticker"]
        company = payload["company"]
        metrics = payload["metrics"]
        flags   = payload["flags"]

        all_years = get_fiscal_years(metrics)
        if not all_years:
            messagebox.showwarning(
                "No Data",
                f"No annual XBRL financial data found for {ticker}.\n"
                "The company may not file structured data with the SEC.",
            )
            self._search_btn.configure(state="normal")
            self._set_status("No data found")
            return

        self._current_metrics = metrics
        self._current_flags   = flags
        self._current_ticker  = ticker
        self._current_company = company

        self._min_spin.configure(from_=all_years[-1], to=all_years[0])
        self._max_spin.configure(from_=all_years[-1], to=all_years[0])
        self._min_year_var.set(all_years[-1])
        self._max_year_var.set(all_years[0])
        self._avail_years_lbl.configure(
            text=f"Available: FY{all_years[-1]}–FY{all_years[0]}  ({len(all_years)} years)"
        )

        self._render_tables(ticker, company, metrics, flags, all_years)
        self._update_biz_overview("")  # web sources loaded async via _fetch_profile_task
        self._search_btn.configure(state="normal")
        self._notebook.select(0)

        # Kick off live price + news + profile fetches in the background
        company_name = company.get("name", ticker)
        threading.Thread(
            target=self._fetch_price_task, args=(ticker, company_name), daemon=True
        ).start()
        threading.Thread(
            target=self._fetch_news_task, args=(ticker,), daemon=True
        ).start()
        threading.Thread(
            target=self._fetch_profile_task, args=(ticker, company_name), daemon=True
        ).start()
        threading.Thread(
            target=self._fetch_sentiment_task, args=(ticker,), daemon=True
        ).start()

    def _set_status(self, msg: str) -> None:
        self._status_var.set(f"  {msg}")


# ══════════════════════════════════════════════════════════════════════════════
# CLI MODE (invoked when arguments are passed)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download and/or analyze SEC EDGAR 10-K / 10-Q filings. "
                    "Run without arguments to launch the GUI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    p.add_argument("--tickers", nargs="+", metavar="TICKER",
                   help="Ticker symbols to process (e.g. AAPL MSFT).")
    p.add_argument("--forms", nargs="+", default=["10-K", "10-Q"],
                   choices=["10-K", "10-Q"],
                   help="Form types to download/analyze (default: both).")
    p.add_argument("--start-date", metavar="YYYY-MM-DD",
                   help="Only include filings on or after this date.")
    p.add_argument("--max-companies", type=int, default=None,
                   help="Stop after this many companies.")
    p.add_argument("--output-dir", default="edgar_filings",
                   help="Root directory for all output (default: edgar_filings/).")

    dl = p.add_argument_group("Download options")
    dl.add_argument("--max-filings-per-company", type=int, default=None,
                    help="Maximum filings to download per company.")
    dl.add_argument("--all-docs", action="store_true",
                    help="Download every document in each filing, not just the primary.")
    dl.add_argument("--dry-run", action="store_true",
                    help="List filings without downloading.")

    an = p.add_argument_group("Analysis options")
    an.add_argument("--analyze", action="store_true",
                    help="Run analysis after downloading.")
    an.add_argument("--analyze-only", action="store_true",
                    help="Skip downloading; only run analysis.")
    an.add_argument("--no-qualitative", action="store_true",
                    help="Skip HTML qualitative extraction (financial data only).")
    an.add_argument("--analysis-formats", nargs="+", default=["markdown", "json"],
                    choices=["markdown", "json"],
                    help="Report formats to generate (default: markdown json).")

    return p.parse_args()


def _run_cli(args: argparse.Namespace) -> None:
    if not _BS4_AVAILABLE and not args.no_qualitative and (args.analyze or args.analyze_only):
        log.warning(
            "beautifulsoup4 is not installed — qualitative extraction will be skipped.\n"
            "Install it with:  pip install beautifulsoup4\n"
            "Or use --no-qualitative to silence this warning."
        )

    run_download = not args.analyze_only
    run_analysis = args.analyze or args.analyze_only

    forms      = set(args.forms)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    start_date: date | None = None
    if args.start_date:
        start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()

    all_companies = fetch_company_list()

    if args.tickers:
        tickers   = [t.upper() for t in args.tickers]
        companies = {t: all_companies[t] for t in tickers if t in all_companies}
        missing   = [t for t in tickers if t not in all_companies]
        if missing:
            log.warning("Tickers not found in EDGAR: %s", missing)
    elif args.analyze_only:
        companies = {}
        for d in sorted(output_dir.iterdir()):
            if d.is_dir() and not d.name.startswith("_"):
                ticker = d.name.upper()
                if ticker in all_companies:
                    companies[ticker] = all_companies[ticker]
        if not companies:
            log.info("No existing company directories found; using full EDGAR list.")
            companies = all_companies
    else:
        companies = all_companies

    if args.max_companies:
        companies = dict(list(companies.items())[: args.max_companies])

    log.info(
        "Mode: %s | Companies: %d | Forms: %s | Start date: %s",
        "download+analyze" if (run_download and run_analysis)
        else ("analyze-only" if run_analysis else "download-only"),
        len(companies),
        sorted(forms),
        start_date or "all time",
    )

    csv_path   = output_dir / "_summary.csv"
    csv_file   = open(csv_path, "w", newline="", encoding="utf-8") if run_analysis else None
    csv_writer = None
    if csv_file:
        csv_writer = csv.DictWriter(csv_file, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        csv_writer.writeheader()

    total_downloaded = 0
    total_analyzed   = 0
    total_companies  = len(companies)

    for idx, (ticker, company) in enumerate(companies.items(), 1):
        log.info("[%d/%d] %s — %s (CIK %s)",
                 idx, total_companies, ticker, company["name"], company["cik"])

        if run_download:
            try:
                submissions = fetch_submissions(company["cik"])
            except Exception as exc:
                log.error("  Failed to fetch submissions: %s", exc)
                if not run_analysis:
                    continue
                submissions = {}

            filings = get_filings(submissions, forms, start_date)
            if not filings:
                log.info("  No matching filings found.")
            else:
                if args.max_filings_per_company:
                    filings = filings[: args.max_filings_per_company]
                log.info("  Found %d filing(s)", len(filings))
                for filing in filings:
                    log.info(
                        "  [%s] filed %s (accession: %s)",
                        filing["form"], filing["filing_date"], filing["accession"],
                    )
                    if args.dry_run:
                        continue
                    try:
                        if download_filing(company, filing, output_dir, args.all_docs):
                            total_downloaded += 1
                    except Exception as exc:
                        log.error("  Download error: %s", exc)

        if run_analysis:
            try:
                ca = analyze_company(
                    ticker=ticker,
                    cik_padded=company["cik"],
                    name=company["name"],
                    output_dir=output_dir,
                    skip_qualitative=args.no_qualitative,
                )
                write_analysis(ca, output_dir, args.analysis_formats)
                if csv_writer:
                    csv_writer.writerow(analysis_to_csv_row(ca))
                    csv_file.flush()
                total_analyzed += 1

                r = ca.metrics.ratios
                crit = sum(1 for f in ca.red_flags if f.severity == "CRITICAL")
                flag_str = (
                    f" | {len(ca.red_flags)} flag(s)"
                    + (f" [{crit} CRITICAL]" if crit else "")
                ) if ca.red_flags else ""
                log.info(
                    "  ✓ Rev %s | GM %s | NM %s | FCF %s%s",
                    _fmt_md(ca.metrics.annual.get("revenue")),
                    _fmt_md(r.get("gross_margin_pct"), "pct"),
                    _fmt_md(r.get("net_margin_pct"),   "pct"),
                    _fmt_md(r.get("free_cash_flow")),
                    flag_str,
                )
            except Exception as exc:
                log.error("  Analysis failed: %s", exc)

    if csv_file:
        csv_file.close()

    if run_download and not args.dry_run:
        log.info("Downloaded %d filing(s) → %s/", total_downloaded, output_dir)
    if run_analysis:
        log.info("Analyzed %d company/companies → %s/", total_analyzed, output_dir)
        log.info("Summary CSV → %s", csv_path)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    # If any CLI arguments are provided, run in headless CLI mode.
    # Otherwise launch the GUI.
    if len(sys.argv) > 1:
        _run_cli(_parse_args())
    else:
        root = tk.Tk()
        root.title("Stocks-Simplified — Financial disclosures of publicly listed companies on the NYSE and NASDAQ")
        root.geometry("1120x740")
        root.minsize(920, 600)
        root.configure(bg="#1e3a5f")
        EdgarApp(root)
        root.mainloop()


if __name__ == "__main__":
    main()
