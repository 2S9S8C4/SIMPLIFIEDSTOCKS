"""
Simplified Stocks — Web Application
=====================================
Streamlit web interface for https://www.simplified-stocks.com/
Built on top of the stock_simplify.py business-logic layer.

Run locally:
    streamlit run app.py

Deploy:
    Push to GitHub → connect repo on streamlit.io/cloud → set custom domain.
"""

from __future__ import annotations

import os
import re
import sys
import json
import requests
from datetime import datetime
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import streamlit as st

# ── Import all business logic from the existing module ────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from stock_simplify import (
    fetch_company_list,
    build_financial_metrics,
    detect_red_flags,
    _fmt,
    get_fiscal_years,
    build_income_rows,
    build_balance_rows,
    build_cashflow_rows,
    build_ratios_rows,
    _av,
    _score_text,
    _parse_rss,
    XBRL_COMPANY_FACTS,
    get as sec_get,
    FinancialMetrics,
    QualitativeData,
)

# ══════════════════════════════════════════════════════════════════════════════
# PAGE CONFIG
# ══════════════════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="Simplified Stocks",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
    menu_items={
        "Get Help": "https://www.simplified-stocks.com/",
        "About": "Simplified Stocks — SEC EDGAR Financial Analyzer",
    },
)

# ══════════════════════════════════════════════════════════════════════════════
# CUSTOM CSS — dark theme matching desktop app
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("""
<style>
/* ─── Global ─────────────────────────────────────────────────────────────── */
html, body, [data-testid="stAppViewContainer"], [data-testid="stApp"] {
    background-color: #000000 !important;
    color: #a8c8e8 !important;
    font-family: 'Consolas', 'Courier New', monospace;
}
[data-testid="stHeader"] { background-color: #000000 !important; }
[data-testid="stSidebar"] { background-color: #050505 !important; }
section[data-testid="stMain"] { background-color: #000000 !important; }

/* ─── Typography ─────────────────────────────────────────────────────────── */
h1, h2, h3 { color: #00ccff !important; }
p, li, label { color: #a8c8e8 !important; }
a { color: #0077ee !important; }

/* ─── Inputs ─────────────────────────────────────────────────────────────── */
input, textarea, select,
[data-testid="stTextInput"] input,
[data-baseweb="input"] input {
    background-color: #0d1728 !important;
    color: #00ccff !important;
    border: 1px solid #152035 !important;
    border-radius: 4px !important;
    font-family: 'Consolas', monospace !important;
}
[data-baseweb="select"] div,
[data-baseweb="select"] span {
    background-color: #0d1728 !important;
    color: #00ccff !important;
}

/* ─── Buttons ────────────────────────────────────────────────────────────── */
.stButton > button {
    background-color: #0077ee !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 4px !important;
    font-family: 'Consolas', monospace !important;
    font-weight: bold !important;
    padding: 0.4rem 1.2rem !important;
    transition: background-color 0.15s ease !important;
}
.stButton > button:hover { background-color: #005acc !important; }

/* ─── Tabs ───────────────────────────────────────────────────────────────── */
[data-testid="stTabs"] [role="tablist"] {
    background-color: #0a0a0a;
    border-bottom: 2px solid #152035;
}
[data-testid="stTabs"] [role="tab"] {
    color: #2a6a8a !important;
    font-family: 'Consolas', monospace !important;
    font-weight: bold !important;
    border-radius: 0 !important;
}
[data-testid="stTabs"] [role="tab"][aria-selected="true"] {
    color: #00ccff !important;
    border-bottom: 2px solid #00ccff !important;
    background-color: #0a0a0a !important;
}
[data-testid="stTabContent"] { background-color: #000000; padding-top: 1rem; }

/* ─── Metrics ────────────────────────────────────────────────────────────── */
[data-testid="stMetric"] {
    background-color: #0a0a0a !important;
    border: 1px solid #152035 !important;
    border-radius: 6px !important;
    padding: 0.6rem 1rem !important;
}
[data-testid="stMetricLabel"]  { color: #2a6a8a !important; font-size: 0.75rem !important; }
[data-testid="stMetricValue"]  { color: #00ccff !important; }
[data-testid="stMetricDelta"]  { font-size: 0.8rem !important; }

/* ─── Alerts / info ──────────────────────────────────────────────────────── */
[data-testid="stAlert"] {
    background-color: #0a0a0a !important;
    border-left: 4px solid #0077ee !important;
}

/* ─── Tables (HTML) ──────────────────────────────────────────────────────── */
.fin-table { width: 100%; border-collapse: collapse; font-family: Consolas, monospace; font-size: 0.85rem; }
.fin-table th {
    background: #000000; color: #00ccff; text-align: right;
    padding: 6px 12px; border-bottom: 2px solid #152035; font-size: 0.8rem;
}
.fin-table th:first-child { text-align: left; }
.fin-table td { padding: 5px 12px; border-bottom: 1px solid #0a0a0a; }
.fin-table td:first-child { text-align: left; }
.fin-table td:not(:first-child) { text-align: right; }
.fin-table tr.section td {
    background: #0a0a0a; color: #00bbdd; font-weight: bold; padding: 8px 12px;
}
.fin-table tr.row-even td { background: #050505; color: #a8c8e8; }
.fin-table tr.row-odd  td { background: #080808; color: #a8c8e8; }
.fin-table tr.row-neg  td { color: #ff3355 !important; }
.fin-table tr.row-pos  td { color: #00e676 !important; }

/* ─── Market ticker strip ────────────────────────────────────────────────── */
.ticker-strip {
    background: #000000; border-bottom: 1px solid #152035;
    padding: 6px 16px; display: flex; align-items: center; gap: 28px;
    font-family: Consolas, monospace; flex-wrap: wrap;
}
.ticker-item { display: flex; flex-direction: column; align-items: flex-start; }
.ticker-label { color: #2a6a8a; font-size: 0.7rem; font-weight: bold; }
.ticker-price { color: #00ccff; font-size: 1.05rem; font-weight: bold; }
.ticker-chg-pos { color: #00e676; font-size: 0.78rem; }
.ticker-chg-neg { color: #ff3355; font-size: 0.78rem; }
.ticker-chg-neu { color: #2a6a8a; font-size: 0.78rem; }

/* ─── Price bar ──────────────────────────────────────────────────────────── */
.price-bar {
    background: #000000; border: 1px solid #152035; border-radius: 6px;
    padding: 8px 16px; display: flex; align-items: center; gap: 18px;
    font-family: Consolas, monospace; margin-bottom: 0.5rem;
}
.price-ticker { color: #00ccff; font-size: 1.1rem; font-weight: bold; }
.price-value  { color: #00ccff; font-size: 1.4rem; font-weight: bold; }
.price-pos    { color: #00e676; font-size: 0.95rem; }
.price-neg    { color: #ff3355; font-size: 0.95rem; }
.price-name   { color: #2a6a8a; font-size: 0.9rem; }

/* ─── Score / tier ───────────────────────────────────────────────────────── */
.score-card {
    background: #0a0a0a; border: 1px solid #152035; border-radius: 8px;
    padding: 1.2rem 1.6rem; margin-bottom: 1rem;
}
.tier-badge {
    display: inline-block; padding: 2px 14px 3px;
    border-radius: 4px; font-family: Consolas, monospace;
    font-size: 1.3rem; font-weight: bold; margin-left: 10px;
}
.star-card {
    background: #0a0a0a; border: 1px solid #152035; border-radius: 6px;
    padding: 12px 16px; flex: 1; min-width: 120px;
}
.star-title  { color: #00bbdd; font-size: 0.78rem; font-weight: bold; margin-bottom: 4px; }
.stars       { font-size: 1.1rem; letter-spacing: 2px; }
.star-desc   { color: #2a6a8a; font-size: 0.65rem; margin-top: 4px; }

/* ─── Flag badges ────────────────────────────────────────────────────────── */
.flag-critical { color: #ff3355; font-weight: bold; }
.flag-warning  { color: #ff8800; font-weight: bold; }
.flag-info     { color: #44aaff; font-weight: bold; }
.flag-card {
    border-left: 4px solid #152035; padding: 8px 14px; margin-bottom: 8px;
    background: #080808; border-radius: 0 4px 4px 0;
}
.flag-card.flag-critical-card { border-left-color: #ff3355; }
.flag-card.flag-warning-card  { border-left-color: #ff8800; }
.flag-card.flag-info-card     { border-left-color: #44aaff; }

/* ─── News cards ─────────────────────────────────────────────────────────── */
.news-card {
    background: #080808; border: 1px solid #152035; border-radius: 6px;
    padding: 12px 16px; margin-bottom: 10px;
}
.news-headline { color: #00ccff; font-size: 0.95rem; font-weight: bold; text-decoration: none; }
.news-headline:hover { color: #44ccff; text-decoration: underline; }
.news-meta   { color: #2a6a8a; font-size: 0.72rem; margin: 3px 0; }
.news-desc   { color: #7a9ab8; font-size: 0.82rem; margin-top: 5px; }

/* ─── Sentiment ──────────────────────────────────────────────────────────── */
.sent-strong-pos { color: #00e676; font-weight: bold; }
.sent-pos        { color: #44dd88; font-weight: bold; }
.sent-neutral    { color: #ffcc00; font-weight: bold; }
.sent-neg        { color: #ff8844; font-weight: bold; }
.sent-strong-neg { color: #ff3355; font-weight: bold; }

/* ─── Achievement badge ──────────────────────────────────────────────────── */
.ach-earned { color: #ffd700; font-weight: bold; }
.ach-locked { color: #2a6a8a; }

/* ─── Divider ────────────────────────────────────────────────────────────── */
hr { border-color: #152035 !important; }

/* ─── Scrollbar ──────────────────────────────────────────────────────────── */
::-webkit-scrollbar       { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: #0a0a0a; }
::-webkit-scrollbar-thumb { background: #152035; border-radius: 3px; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

_MARKET_INDICES = [
    ("^DJI",  "DOW",     "Dow Jones"),
    ("^GSPC", "S&P 500", "S&P 500"),
    ("^IXIC", "NASDAQ",  "Nasdaq"),
    ("^NYA",  "NYSE",    "NYSE Composite"),
    ("^RUT",  "RUSSELL", "Russell 2000"),
]

_TIER_COLORS = {
    "S": "#ffd700", "A": "#00e676", "B": "#00aaff",
    "C": "#ff8800", "D": "#ff6644", "F": "#ff3355",
}

_CHART_BG     = "#000000"
_CHART_ROW    = "#050505"
_CHART_BORDER = "#152035"
_CHART_TEXT   = "#a8c8e8"
_CHART_ACCENT = "#00ccff"

_NATURALLY_NEG = frozenset([
    "cost of revenue", "r&d expense", "income tax",
    "capital expenditures", "investing cash flow",
    "financing cash flow", "dividends paid", "share repurchases",
])

_ACHIEVEMENTS = [
    ("Revenue Champion",   "Revenue grew 3+ consecutive years"),
    ("Margin Master",      "Gross margin ≥ 40%"),
    ("Profit Machine",     "Net margin ≥ 15%"),
    ("Cash Fortress",      "Free cash flow positive"),
    ("Liquidity Pro",      "Current ratio ≥ 2.0"),
    ("Debt-Free Legend",   "Debt-to-Equity ≤ 0.5"),
    ("No-Debt Champion",   "Near-zero debt (D/E ≤ 0.1)"),
    ("Growth Accelerator", "Revenue grew > 15% YoY"),
]

_NEWS_SOURCES = [
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US",
    "https://news.google.com/rss/search?q={ticker}+stock&hl=en-US&gl=US&ceid=US:en",
]

_SENTIMENT_SOURCES = [
    ("Reddit (r/stocks)",       "https://www.reddit.com/r/stocks/search.rss?q={ticker}&sort=new"),
    ("Reddit (r/investing)",    "https://www.reddit.com/r/investing/search.rss?q={ticker}&sort=new"),
    ("Reddit (r/wallstreetbets)","https://www.reddit.com/r/wallstreetbets/search.rss?q={ticker}&sort=new"),
]

_GLOSSARY = [
    ("Income Statement", [
        ("Revenue",          "Total income from selling products/services before any expenses. The 'top line'."),
        ("Cost of Revenue",  "Direct costs of producing goods/services sold (COGS). Materials, labour, overhead."),
        ("Gross Profit",     "Revenue minus Cost of Revenue."),
        ("Gross Margin",     "Gross Profit as a % of Revenue. Higher = more efficient production."),
        ("R&D Expense",      "Money spent on research & development. High R&D can signal future growth investment."),
        ("Operating Income", "Profit from core operations after COGS/R&D/SG&A but before interest and taxes (EBIT)."),
        ("Operating Margin", "Operating Income as a % of Revenue. Measures operational efficiency."),
        ("Net Income",       "Total profit (or loss) after all expenses, interest, and taxes. The 'bottom line'."),
        ("Net Margin",       "Net Income as a % of Revenue."),
        ("EPS (Basic)",      "Earnings Per Share — Net Income ÷ basic shares outstanding."),
        ("EPS (Diluted)",    "EPS using fully diluted share count (includes options, warrants). More conservative."),
        ("EBITDA",           "Earnings Before Interest, Taxes, Depreciation & Amortisation. Proxy for operating cash."),
        ("D&A",              "Depreciation & Amortisation — non-cash expense for asset wear-and-tear."),
        ("Income Tax",       "Taxes owed on the company's taxable income."),
    ]),
    ("Balance Sheet", [
        ("Cash & Equivalents",   "Liquid assets: cash + short-term instruments. Indicates financial flexibility."),
        ("Accounts Receivable",  "Money owed by customers for goods/services already delivered."),
        ("Inventory",            "Raw materials, WIP, finished goods. High vs sales may signal slow demand."),
        ("Current Assets",       "Assets convertible to cash within one year."),
        ("Total Assets",         "Everything the company owns — current + long-term assets."),
        ("Current Liabilities",  "Financial obligations due within one year."),
        ("Short-Term Debt",      "Borrowings due within 12 months."),
        ("Long-Term Debt",       "Borrowings maturing beyond one year."),
        ("Total Debt",           "Short-Term Debt + Long-Term Debt."),
        ("Total Liabilities",    "All financial obligations the company owes."),
        ("Stockholders' Equity", "Net worth: Total Assets − Total Liabilities. Also called book value."),
        ("Retained Earnings",    "Cumulative net income kept rather than paid as dividends."),
        ("Shares Outstanding",   "Total common shares held by all shareholders."),
    ]),
    ("Cash Flow", [
        ("Operating Cash Flow",  "Cash generated by core operations. Consistent positive OCF = healthy business."),
        ("Capital Expenditures", "Cash spent on physical assets (PP&E). Shown negative."),
        ("Investing Cash Flow",  "Net cash from buying/selling assets. Often negative for growing companies."),
        ("Free Cash Flow",       "OCF − CapEx. Cash available to repay debt, pay dividends, buy back shares."),
        ("FCF Margin",           "FCF as a % of Revenue. >10% healthy; >20% excellent."),
        ("Financing Cash Flow",  "Net cash from debt issuance, repurchases, dividends."),
        ("Dividends Paid",       "Cash distributed to shareholders as dividends."),
        ("Share Repurchases",    "Cash spent buying back the company's own shares."),
    ]),
    ("Key Ratios", [
        ("Gross Margin",         "Gross Profit ÷ Revenue. Benchmarks vary by industry (software >60%, retail <30%)."),
        ("Operating Margin",     "Operating Income ÷ Revenue. Above 15% is generally strong."),
        ("Net Margin",           "Net Income ÷ Revenue. Overall profitability after all costs."),
        ("FCF Margin",           "Free Cash Flow ÷ Revenue."),
        ("Return on Equity",     "Net Income ÷ Stockholders' Equity. Above 15% is considered strong."),
        ("Return on Assets",     "Net Income ÷ Total Assets. Above 5% is generally good."),
        ("Current Ratio",        "Current Assets ÷ Current Liabilities. >1.0 = can cover near-term obligations."),
        ("Quick Ratio",          "(Current Assets − Inventory) ÷ Current Liabilities. Stricter liquidity test."),
        ("Debt-to-Equity",       "Total Debt ÷ Stockholders' Equity. High = more financially leveraged."),
        ("Interest Coverage",    "Operating Income ÷ Interest Expense. Below 2× is a warning sign."),
        ("Asset Turnover",       "Revenue ÷ Total Assets. Higher = more efficient asset use."),
        ("Inventory Turnover",   "Cost of Revenue ÷ Inventory. Higher = efficient inventory management."),
    ]),
]

_DISCLAIMER_TEXT = """
IMPORTANT DISCLAIMER

This website and its content are provided for EDUCATIONAL AND INFORMATIONAL
PURPOSES ONLY. Nothing on this site constitutes financial, investment, legal,
or tax advice.

NOT INVESTMENT ADVICE
All data, analysis, metrics, scores, and commentary displayed on Simplified
Stocks are derived from publicly available SEC EDGAR filings. They do not
represent a recommendation to buy, sell, or hold any security.

ACCURACY OF DATA
Financial data is sourced directly from SEC EDGAR XBRL submissions. While
we strive for accuracy, data may contain errors, omissions, or delays.
Always verify figures directly from official company filings.

NO LIABILITY
Simplified Stocks, its owners, developers, and contributors accept no
liability for investment decisions made based on information presented here.
Past financial performance does not guarantee future results.

ALWAYS DO YOUR OWN RESEARCH
Before making any investment decision, consult a qualified financial advisor
and conduct your own independent research.

Data Source: U.S. Securities and Exchange Commission (SEC) EDGAR
             https://www.sec.gov/developer
"""


# ══════════════════════════════════════════════════════════════════════════════
# SCORING FUNCTIONS (extracted from EdgarApp class)
# ══════════════════════════════════════════════════════════════════════════════

def compute_score(metrics: FinancialMetrics) -> tuple[int, str]:
    r   = metrics.ratios
    pts = 0.0

    gm = r.get("gross_margin_pct") or 0
    pts += min(gm / 50 * 12, 12)
    nm = r.get("net_margin_pct") or 0
    pts += max(min(nm / 20 * 8, 8), 0)

    roe = r.get("roe_pct") or 0
    if   roe >= 20: pts += 12
    elif roe >= 15: pts += 10
    elif roe >= 10: pts += 7
    elif roe >= 5:  pts += 4
    elif roe > 0:   pts += 1
    roa = r.get("roa_pct") or 0
    if   roa >= 10: pts += 8
    elif roa >= 7:  pts += 6
    elif roa >= 4:  pts += 4
    elif roa > 0:   pts += 2

    cr = r.get("current_ratio") or 0
    if   cr >= 2.0: pts += 12
    elif cr >= 1.5: pts += 9
    elif cr >= 1.0: pts += 5
    ic = r.get("interest_coverage")
    if   ic is None: pts += 8
    elif ic >= 8:    pts += 8
    elif ic >= 5:    pts += 6
    elif ic >= 3:    pts += 3
    elif ic >= 1:    pts += 1

    de = r.get("debt_to_equity")
    if   de is None: pts += 10
    elif de <= 0.3:  pts += 15
    elif de <= 0.7:  pts += 12
    elif de <= 1.5:  pts += 8
    elif de <= 3.0:  pts += 4

    fcf   = r.get("free_cash_flow")
    fcf_m = r.get("fcf_margin_pct") or 0
    if fcf is None:  pts += 7
    elif fcf > 0:    pts += min(fcf_m / 15 * 15, 15)

    def _trend_vals(key):
        return [p["value"] for p in metrics.annual_trend.get(key, [])
                if p.get("value") is not None]

    def _consistent_growth(vals, bonus):
        if len(vals) < 3:
            return bonus * 0.4
        years_up = sum(1 for a, b in zip(vals, vals[1:]) if b > a)
        consistency = years_up / (len(vals) - 1)
        latest_growth = (vals[-1] / vals[-2] - 1) * 100 if vals[-2] else 0
        return min(bonus, consistency * bonus * 0.6 +
                   max(min(latest_growth / 15 * bonus * 0.4, bonus * 0.4), 0))

    pts += _consistent_growth(_trend_vals("revenue"),    5)
    pts += _consistent_growth(_trend_vals("net_income"), 5)

    score = max(0, min(100, int(pts)))
    if   score >= 85: tier = "S"
    elif score >= 70: tier = "A"
    elif score >= 55: tier = "B"
    elif score >= 40: tier = "C"
    elif score >= 25: tier = "D"
    else:             tier = "F"
    return score, tier


def category_stars(metrics: FinancialMetrics) -> dict[str, int]:
    r     = metrics.ratios
    gm    = r.get("gross_margin_pct") or 0
    nm    = r.get("net_margin_pct") or 0
    roe   = r.get("roe_pct") or 0
    roa   = r.get("roa_pct") or 0
    cr    = r.get("current_ratio") or 0
    de    = r.get("debt_to_equity")
    fcf_m = r.get("fcf_margin_pct") or 0
    fcf   = r.get("free_cash_flow")
    ic    = r.get("interest_coverage")

    profit = int(min(5, max(0, gm / 12 + nm / 5)))

    returns = (5 if roe >= 20 and roa >= 10 else
               4 if roe >= 15 and roa >= 7  else
               3 if roe >= 10 and roa >= 4  else
               2 if roe >= 5  or  roa >= 2  else 1)

    ic_stars = (2 if ic is None or ic >= 8 else
                2 if ic >= 5 else 1 if ic >= 3 else 0)
    cr_stars = (3 if cr >= 2.0 else 2 if cr >= 1.5 else 1 if cr >= 1.0 else 0)
    strength = min(5, cr_stars + ic_stars)

    leverage = (5 if de is None or de <= 0.3 else
                4 if de <= 0.7 else
                3 if de <= 1.5 else
                2 if de <= 3.0 else 1)

    cashflow = (0 if fcf is None or fcf <= 0 else
                int(min(5, max(0, fcf_m / 5))))

    return {
        "PROFIT":   profit,
        "RETURNS":  returns,
        "STRENGTH": strength,
        "LEVERAGE": leverage,
        "CASHFLOW": cashflow,
    }


def check_achievements(metrics: FinancialMetrics) -> list[bool]:
    r     = metrics.ratios
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

    return [
        _rev_consecutive(3),
        (r.get("gross_margin_pct") or 0) >= 40,
        (r.get("net_margin_pct")   or 0) >= 15,
        (r.get("free_cash_flow")   or 0)  > 0,
        (r.get("current_ratio")    or 0) >= 2.0,
        (r.get("debt_to_equity") is not None and r["debt_to_equity"] <= 0.5),
        (r.get("debt_to_equity") is not None and r["debt_to_equity"] <= 0.1),
        _rev_yoy_pct() > 15,
    ]


# ══════════════════════════════════════════════════════════════════════════════
# DATA FETCHING (cached)
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(show_spinner="Loading SEC company database…")
def get_company_list() -> dict[str, dict]:
    return fetch_company_list()


@st.cache_data(show_spinner="Fetching financial data from SEC EDGAR…", ttl=300)
def get_financial_data(ticker: str, cik: str, name: str):
    facts   = sec_get(XBRL_COMPANY_FACTS.format(cik=cik)).json()
    metrics = build_financial_metrics(facts)
    metrics.company_name = name or metrics.company_name
    flags   = detect_red_flags(metrics, QualitativeData())
    return metrics, flags


@st.cache_data(ttl=60)
def get_market_indices() -> dict:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Accept": "application/json",
    })
    results = {}
    for symbol, _, _ in _MARKET_INDICES:
        try:
            sym_enc = symbol.replace("^", "%5E")
            resp = session.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym_enc}"
                f"?interval=1d&range=1d",
                timeout=8,
            )
            resp.raise_for_status()
            meta  = resp.json()["chart"]["result"][0]["meta"]
            price = float(meta.get("regularMarketPrice") or 0)
            prev  = float(meta.get("chartPreviousClose") or meta.get("previousClose") or price)
            results[symbol] = {
                "price":  price,
                "change": price - prev,
                "pct":    (price / prev - 1) * 100 if prev else 0.0,
            }
        except Exception:
            results[symbol] = None
    return results


@st.cache_data(ttl=60)
def get_stock_price(ticker: str) -> dict | None:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Accept": "application/json",
    })
    try:
        resp = session.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            f"?interval=1d&range=1d",
            timeout=8,
        )
        resp.raise_for_status()
        meta  = resp.json()["chart"]["result"][0]["meta"]
        price = float(meta.get("regularMarketPrice") or 0)
        prev  = float(meta.get("chartPreviousClose") or meta.get("previousClose") or price)
        return {
            "price":  price,
            "change": price - prev,
            "pct":    (price / prev - 1) * 100 if prev else 0.0,
        }
    except Exception:
        return None


@st.cache_data(ttl=300)
def get_news(ticker: str) -> list[dict]:
    session = requests.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
    )
    for url_tpl in _NEWS_SOURCES:
        try:
            resp = session.get(url_tpl.format(ticker=ticker), timeout=10)
            resp.raise_for_status()
            articles = _parse_rss(resp.content)
            if articles:
                return articles
        except Exception:
            pass
    return []


@st.cache_data(ttl=300)
def get_sentiment(ticker: str) -> list[dict]:
    """Fetch Reddit RSS feeds for social sentiment."""
    session = requests.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
    )
    results = []
    for platform, url_tpl in _SENTIMENT_SOURCES:
        try:
            resp = session.get(url_tpl.format(ticker=ticker), timeout=10)
            resp.raise_for_status()
            articles = _parse_rss(resp.content)
            if articles:
                results.append((platform, articles[:5]))
        except Exception:
            pass
    return results


@st.cache_data(ttl=600)
def get_company_profile(ticker: str, company_name: str) -> tuple[str, str]:
    """Fetch company profile from Wikipedia → DuckDuckGo fallback."""
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "SimplifiedStocks/1.0 (educational)",
        "Accept": "application/json",
    })
    clean = re.sub(
        r"\s+(Inc\.?|Corp\.?|Ltd\.?|LLC\.?|Co\.?|PLC\.?|N\.?V\.?|S\.?A\.?)$",
        "", company_name, flags=re.IGNORECASE,
    ).strip()
    clean_and = clean.replace("&", "and")

    candidates = [
        company_name, clean, clean_and,
        f"{clean} (company)", f"{clean_and} (company)",
        f"{clean} Inc.", f"{clean} Corporation",
    ]
    for title in candidates:
        try:
            encoded = requests.utils.quote(title, safe="")
            r = sess.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded}",
                timeout=8,
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("type") != "disambiguation":
                    text = data.get("extract", "").strip()
                    if len(text) > 80:
                        return text, f"Wikipedia — {data.get('title', title)}"
        except Exception:
            pass

    try:
        r = sess.get(
            "https://api.duckduckgo.com/",
            params={"q": f"{company_name} company", "format": "json",
                    "no_html": "1", "skip_disambig": "1"},
            timeout=10,
        )
        if r.status_code == 200:
            abstract = r.json().get("AbstractText", "").strip()
            if len(abstract) > 80:
                return abstract, "DuckDuckGo"
    except Exception:
        pass
    return "", ""


# ══════════════════════════════════════════════════════════════════════════════
# UI HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _scale(vals: list) -> tuple[list, str]:
    defined = [v for v in vals if v is not None]
    if not defined:
        return vals, ""
    mx = max(abs(v) for v in defined)
    if mx >= 1e9: return [v / 1e9 if v is not None else None for v in vals], "B"
    if mx >= 1e6: return [v / 1e6 if v is not None else None for v in vals], "M"
    return vals, ""


def render_financial_table(rows, years: list[int]) -> str:
    """Build an HTML financial table from Row list and year list."""
    hdrs = "".join(f"<th>FY {y}</th>" for y in years)
    html = f'<table class="fin-table"><thead><tr><th>Metric</th>{hdrs}</tr></thead><tbody>'

    data_idx = 0
    for label, vals, unit, is_section in rows:
        if is_section:
            n_cols = len(years) + 1
            html += f'<tr class="section"><td colspan="{n_cols}">{label}</td></tr>'
            continue

        cells = []
        for v in (vals or []):
            cells.append(_fmt(v, unit) if v is not None else "—")
        while len(cells) < len(years):
            cells.append("—")

        nat_neg = label.lower() in _NATURALLY_NEG
        latest  = next((v for v in (vals or []) if v is not None), None)

        if latest is not None and not nat_neg:
            row_cls = "row-neg" if latest < 0 else "row-pos"
        else:
            row_cls = "row-even" if data_idx % 2 == 0 else "row-odd"

        tds = "".join(f"<td>{c}</td>" for c in cells)
        html += f'<tr class="{row_cls}"><td>{label}</td>{tds}</tr>'
        data_idx += 1

    html += "</tbody></table>"
    return html


def _ax_style(ax) -> None:
    ax.set_facecolor(_CHART_ROW)
    ax.tick_params(colors=_CHART_TEXT, labelsize=8)
    ax.title.set_color(_CHART_ACCENT)
    ax.title.set_fontsize(9)
    ax.title.set_fontweight("bold")
    for spine in ax.spines.values():
        spine.set_edgecolor(_CHART_BORDER)
        spine.set_linewidth(0.6)
    ax.xaxis.label.set_color(_CHART_TEXT)
    ax.yaxis.label.set_color(_CHART_TEXT)
    ax.tick_params(axis="both", which="both", length=0)


def make_income_chart(metrics: FinancialMetrics, years: list[int]):
    years_asc = list(reversed(years))
    x = list(range(len(years_asc)))
    lx = [str(y) for y in years_asc]

    rev = [_av(metrics, "revenue",          y) for y in years_asc]
    gp  = [_av(metrics, "gross_profit",     y) for y in years_asc]
    oi  = [_av(metrics, "operating_income", y) for y in years_asc]
    ni  = [_av(metrics, "net_income",       y) for y in years_asc]
    rev_s, unit = _scale(rev)
    gp_s,  _    = _scale(gp)
    oi_s,  _    = _scale(oi)
    ni_s,  _    = _scale(ni)
    gm = [(g / r * 100) if g is not None and r else None for g, r in zip(gp, rev)]
    om = [(o / r * 100) if o is not None and r else None for o, r in zip(oi, rev)]

    fig, axes = plt.subplots(1, 2, figsize=(11, 3.6), facecolor=_CHART_BG)
    fig.subplots_adjust(wspace=0.35)

    ax1, ax2 = axes
    for ax in axes:
        _ax_style(ax)

    def _plot_bar(ax, data, color, label, title, ylabel):
        vals = [v if v is not None else 0 for v in data]
        colors = ["#ff2244" if v < 0 else color for v in vals]
        ax.bar(x, vals, color=colors, width=0.6)
        ax.set_xticks(x); ax.set_xticklabels(lx, fontsize=7.5, color=_CHART_TEXT)
        ax.set_title(title); ax.set_ylabel(ylabel, fontsize=8, color=_CHART_TEXT)
        ax.axhline(0, color=_CHART_BORDER, linewidth=0.8, linestyle="--")

    _plot_bar(ax1, rev_s or [0]*len(x), "#00aaff", "Revenue",
              f"Revenue vs Net Income ({unit}$)", f"${unit}")
    ni_vals = [v if v is not None else 0 for v in (ni_s or [0]*len(x))]
    ni_colors = ["#ff2244" if v < 0 else "#00e676" for v in ni_vals]
    ax1.bar(x, ni_vals, color=ni_colors, width=0.4, alpha=0.85, label="Net Income")

    def _plot_line(ax, data, color, label, title, ylabel):
        xs = [i for i, v in enumerate(data) if v is not None]
        ys = [v for v in data if v is not None]
        ax.plot(xs, ys, color=color, linewidth=2, marker="o", markersize=4, label=label)
        ax.set_xticks(x); ax.set_xticklabels(lx, fontsize=7.5, color=_CHART_TEXT)
        ax.set_title(title); ax.set_ylabel(ylabel, fontsize=8, color=_CHART_TEXT)
        ax.axhline(0, color=_CHART_BORDER, linewidth=0.6, linestyle="--")

    _plot_line(ax2, gm, "#00aaff", "Gross Margin",    "Margins (%)", "%")
    if any(v is not None for v in om):
        xs2 = [i for i, v in enumerate(om) if v is not None]
        ys2 = [v for v in om if v is not None]
        ax2.plot(xs2, ys2, color="#00e676", linewidth=2, marker="s",
                 markersize=4, label="Operating Margin")
        ax2.legend(fontsize=7, labelcolor=_CHART_TEXT,
                   facecolor=_CHART_ROW, edgecolor=_CHART_BORDER)

    fig.tight_layout()
    return fig


def make_balance_chart(metrics: FinancialMetrics, years: list[int]):
    years_asc = list(reversed(years))
    x  = list(range(len(years_asc)))
    lx = [str(y) for y in years_asc]

    ta   = [_av(metrics, "total_assets",       y) for y in years_asc]
    tl   = [_av(metrics, "total_liabilities",  y) for y in years_asc]
    eq   = [_av(metrics, "stockholders_equity",y) for y in years_asc]
    cash = [_av(metrics, "cash",               y) for y in years_asc]
    ta_s, unit = _scale(ta)
    tl_s, _    = _scale(tl)
    eq_s, _    = _scale(eq)
    ca_s, _    = _scale(cash)

    fig, axes = plt.subplots(1, 2, figsize=(11, 3.6), facecolor=_CHART_BG)
    fig.subplots_adjust(wspace=0.35)

    for ax in axes:
        _ax_style(ax)

    def bar(ax, data, color, label):
        vals = [v if v is not None else 0 for v in data]
        ax.bar(x, vals, color=color, width=0.55, label=label, alpha=0.85)
        ax.set_xticks(x); ax.set_xticklabels(lx, fontsize=7.5, color=_CHART_TEXT)

    bar(axes[0], ta_s or [0]*len(x), "#00aaff",  "Total Assets")
    bar(axes[0], tl_s or [0]*len(x), "#ff3355",  "Total Liabilities")
    axes[0].set_title(f"Assets vs Liabilities ({unit}$)")
    axes[0].set_ylabel(f"${unit}", fontsize=8, color=_CHART_TEXT)
    axes[0].legend(fontsize=7, labelcolor=_CHART_TEXT,
                   facecolor=_CHART_ROW, edgecolor=_CHART_BORDER)

    xs_eq = [i for i, v in enumerate(eq_s or [None]*len(x)) if v is not None]
    ys_eq = [v for v in (eq_s or []) if v is not None]
    xs_ca = [i for i, v in enumerate(ca_s or [None]*len(x)) if v is not None]
    ys_ca = [v for v in (ca_s or []) if v is not None]
    if xs_eq:
        axes[1].plot(xs_eq, ys_eq, color="#00e676", linewidth=2,
                     marker="o", markersize=4, label="Stockholders' Equity")
    if xs_ca:
        axes[1].plot(xs_ca, ys_ca, color="#ffcc00", linewidth=2,
                     marker="s", markersize=4, label="Cash")
    axes[1].set_title(f"Equity & Cash ({unit}$)")
    axes[1].set_ylabel(f"${unit}", fontsize=8, color=_CHART_TEXT)
    axes[1].set_xticks(x); axes[1].set_xticklabels(lx, fontsize=7.5, color=_CHART_TEXT)
    axes[1].axhline(0, color=_CHART_BORDER, linewidth=0.6, linestyle="--")
    axes[1].legend(fontsize=7, labelcolor=_CHART_TEXT,
                   facecolor=_CHART_ROW, edgecolor=_CHART_BORDER)

    fig.tight_layout()
    return fig


def make_cashflow_chart(metrics: FinancialMetrics, years: list[int]):
    years_asc = list(reversed(years))
    x  = list(range(len(years_asc)))
    lx = [str(y) for y in years_asc]

    ocf  = [_av(metrics, "operating_cf",  y) for y in years_asc]
    capx = [_av(metrics, "capex",         y) for y in years_asc]
    fcf  = [(o - c) if o is not None and c is not None else None
            for o, c in zip(ocf, capx)]

    ocf_s, unit = _scale(ocf)
    cap_s, _    = _scale(capx)
    fcf_s, _    = _scale(fcf)

    fig, ax = plt.subplots(figsize=(11, 3.6), facecolor=_CHART_BG)
    _ax_style(ax)

    w = 0.28
    x_arr = list(range(len(years_asc)))
    x1 = [xi - w for xi in x_arr]
    x2 = x_arr
    x3 = [xi + w for xi in x_arr]

    def bvals(data):
        return [v if v is not None else 0 for v in (data or [0]*len(x_arr))]

    ax.bar(x1, bvals(ocf_s),  color="#00aaff", width=w, label=f"OCF (${unit})")
    ax.bar(x2, bvals(cap_s),  color="#ff8800", width=w, label=f"CapEx (${unit})")
    ax.bar(x3, bvals(fcf_s),  color="#00e676", width=w, label=f"FCF (${unit})")
    ax.axhline(0, color=_CHART_BORDER, linewidth=0.8, linestyle="--")
    ax.set_xticks(x_arr); ax.set_xticklabels(lx, fontsize=7.5, color=_CHART_TEXT)
    ax.set_title(f"Cash Flow Breakdown ({unit}$)")
    ax.set_ylabel(f"${unit}", fontsize=8, color=_CHART_TEXT)
    ax.legend(fontsize=7, labelcolor=_CHART_TEXT,
              facecolor=_CHART_ROW, edgecolor=_CHART_BORDER)
    fig.tight_layout()
    return fig


def make_ratios_chart(metrics: FinancialMetrics, years: list[int]):
    years_asc = list(reversed(years))
    x  = list(range(len(years_asc)))
    lx = [str(y) for y in years_asc]

    gm  = [(_av(metrics, "gross_profit",     y) / _av(metrics, "revenue", y) * 100)
           if _av(metrics, "revenue", y) and _av(metrics, "gross_profit", y) else None
           for y in years_asc]
    om  = [(_av(metrics, "operating_income", y) / _av(metrics, "revenue", y) * 100)
           if _av(metrics, "revenue", y) and _av(metrics, "operating_income", y) else None
           for y in years_asc]
    nm  = [(_av(metrics, "net_income",       y) / _av(metrics, "revenue", y) * 100)
           if _av(metrics, "revenue", y) and _av(metrics, "net_income", y) else None
           for y in years_asc]

    cr  = [(_av(metrics, "current_assets",  y) / _av(metrics, "current_liabilities", y))
           if _av(metrics, "current_assets", y) and _av(metrics, "current_liabilities", y) else None
           for y in years_asc]
    roe = [(_av(metrics, "net_income",       y) / _av(metrics, "stockholders_equity", y) * 100)
           if _av(metrics, "net_income", y) is not None and _av(metrics, "stockholders_equity", y) else None
           for y in years_asc]

    fig, axes = plt.subplots(1, 2, figsize=(11, 3.6), facecolor=_CHART_BG)
    fig.subplots_adjust(wspace=0.35)
    for ax in axes:
        _ax_style(ax)

    def _plot(ax, data, color, label):
        xs = [i for i, v in enumerate(data) if v is not None]
        ys = [v for v in data if v is not None]
        if xs:
            ax.plot(xs, ys, color=color, linewidth=2, marker="o",
                    markersize=4, label=label)

    _plot(axes[0], gm,  "#00aaff", "Gross Margin")
    _plot(axes[0], om,  "#00e676", "Operating Margin")
    _plot(axes[0], nm,  "#ffcc00", "Net Margin")
    axes[0].axhline(0, color=_CHART_BORDER, linewidth=0.6, linestyle="--")
    axes[0].set_title("Margins (%)")
    axes[0].set_ylabel("%", fontsize=8, color=_CHART_TEXT)
    axes[0].set_xticks(x); axes[0].set_xticklabels(lx, fontsize=7.5, color=_CHART_TEXT)
    axes[0].legend(fontsize=7, labelcolor=_CHART_TEXT,
                   facecolor=_CHART_ROW, edgecolor=_CHART_BORDER)

    _plot(axes[1], cr,  "#00aaff", "Current Ratio")
    _plot(axes[1], roe, "#ffd700", "ROE (%)")
    axes[1].axhline(1, color="#ff8800", linewidth=0.8, linestyle=":", alpha=0.7)
    axes[1].set_title("Liquidity & Return")
    axes[1].set_xticks(x); axes[1].set_xticklabels(lx, fontsize=7.5, color=_CHART_TEXT)
    axes[1].legend(fontsize=7, labelcolor=_CHART_TEXT,
                   facecolor=_CHART_ROW, edgecolor=_CHART_BORDER)

    fig.tight_layout()
    return fig


def make_flags_chart(flags: list, metrics: FinancialMetrics):
    from collections import Counter
    severity_counts = Counter(f.severity for f in flags)
    categories = {}
    for f in flags:
        categories[f.category] = categories.get(f.category, 0) + 1

    fig, axes = plt.subplots(1, 2, figsize=(10, 3.2), facecolor=_CHART_BG)
    fig.subplots_adjust(wspace=0.4)
    for ax in axes:
        _ax_style(ax)

    # Severity pie
    if severity_counts:
        colors_map = {"CRITICAL": "#ff3355", "WARNING": "#ff8800", "INFO": "#44aaff"}
        labels = list(severity_counts.keys())
        sizes  = [severity_counts[l] for l in labels]
        colors = [colors_map.get(l, "#446688") for l in labels]
        axes[0].pie(sizes, labels=labels, colors=colors, autopct="%1.0f%%",
                    textprops={"color": _CHART_TEXT, "fontsize": 8},
                    pctdistance=0.75)
        axes[0].set_title("Flags by Severity")
    else:
        axes[0].text(0.5, 0.5, "No flags", ha="center", va="center",
                     color="#00e676", fontsize=10, transform=axes[0].transAxes)
        axes[0].set_title("Flags by Severity")

    # Category bar
    if categories:
        cats   = list(categories.keys())
        counts = [categories[c] for c in cats]
        axes[1].barh(cats, counts, color="#0077ee")
        axes[1].set_title("Flags by Category")
        axes[1].set_xlabel("Count", fontsize=8, color=_CHART_TEXT)
    else:
        axes[1].text(0.5, 0.5, "No flags", ha="center", va="center",
                     color="#00e676", fontsize=10, transform=axes[1].transAxes)
        axes[1].set_title("Flags by Category")

    fig.tight_layout()
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# RENDER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def render_market_strip(indices: dict) -> None:
    parts = ["<div class='ticker-strip'><span style='color:#2a6a8a;font-size:0.75rem;font-weight:bold;'>LIVE MARKETS ▸</span>"]
    for symbol, label, _ in _MARKET_INDICES:
        d = indices.get(symbol)
        if d:
            arrow = "▲" if d["change"] >= 0 else "▼"
            chg_cls = "ticker-chg-pos" if d["change"] >= 0 else "ticker-chg-neg"
            parts.append(
                f"<div class='ticker-item'>"
                f"<span class='ticker-label'>{label}</span>"
                f"<span class='ticker-price'>{d['price']:,.2f}</span>"
                f"<span class='{chg_cls}'>{arrow} {abs(d['change']):,.2f} ({d['pct']:+.2f}%)</span>"
                f"</div>"
            )
        else:
            parts.append(
                f"<div class='ticker-item'>"
                f"<span class='ticker-label'>{label}</span>"
                f"<span class='ticker-price' style='color:#2a6a8a'>N/A</span>"
                f"<span class='ticker-chg-neu'>—</span>"
                f"</div>"
            )
    ts = datetime.now().strftime("updated %H:%M")
    parts.append(f"<span style='color:#152035;font-size:0.7rem;margin-left:auto;'>{ts}</span>")
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def render_price_bar(ticker: str, company: str, quote: dict | None) -> None:
    if quote:
        arrow  = "▲" if quote["change"] >= 0 else "▼"
        chg_cls = "price-pos" if quote["change"] >= 0 else "price-neg"
        chg_txt = f"{arrow} {abs(quote['change']):,.2f}  ({quote['pct']:+.2f}%)"
        html = (
            f"<div class='price-bar'>"
            f"<span class='price-ticker'>{ticker}</span>"
            f"<span style='color:#152035;font-size:1.2rem;'>│</span>"
            f"<span class='price-value'>${quote['price']:,.2f}</span>"
            f"<span class='{chg_cls}'>{chg_txt}</span>"
            f"<span style='color:#152035;font-size:1.2rem;'>│</span>"
            f"<span class='price-name'>{company}</span>"
            f"</div>"
        )
    else:
        html = (
            f"<div class='price-bar'>"
            f"<span class='price-ticker'>{ticker}</span>"
            f"<span style='color:#2a6a8a;'>  Price unavailable</span>"
            f"<span style='color:#152035;font-size:1.2rem;'>│</span>"
            f"<span class='price-name'>{company}</span>"
            f"</div>"
        )
    st.markdown(html, unsafe_allow_html=True)


def render_summary_tab(metrics: FinancialMetrics, flags: list, ticker: str, company: dict) -> None:
    score, tier = compute_score(metrics)
    stars       = category_stars(metrics)
    achieved    = check_achievements(metrics)
    tier_color  = _TIER_COLORS.get(tier, "#00ccff")

    # Score bar
    bar_pct  = score / 100 * 100
    tier_txt_color = "#000000" if tier in ("S", "A") else "#ffffff"
    st.markdown(
        f"""<div class="score-card">
        <div style="color:#2a6a8a;font-size:0.75rem;font-weight:bold;margin-bottom:8px;">FINANCIAL HEALTH SUMMARY</div>
        <div style="display:flex;align-items:center;gap:14px;margin-bottom:16px;">
          <span style="color:#2a6a8a;font-size:0.8rem;font-weight:bold;">HEALTH SCORE</span>
          <div style="flex:0 0 260px;height:14px;background:#152035;border-radius:3px;overflow:hidden;">
            <div style="width:{bar_pct}%;height:100%;background:{tier_color};border-radius:3px;"></div>
          </div>
          <span style="color:{tier_color};font-size:1.4rem;font-weight:bold;">{score}</span>
          <span class="tier-badge" style="background:{tier_color};color:{tier_txt_color};">&nbsp;{tier}&nbsp;</span>
        </div>
        <hr style="border-color:#152035;margin:10px 0;">
        <div style="display:flex;gap:12px;flex-wrap:wrap;">
        """,
        unsafe_allow_html=True,
    )

    cat_meta = [
        ("PROFIT",   "Profitability",      "Gross & net margins · Graham/Buffett"),
        ("RETURNS",  "Return Quality",     "ROE & ROA · Buffett"),
        ("STRENGTH", "Financial Strength", "Current ratio & coverage · Graham"),
        ("LEVERAGE", "Leverage",           "Debt-to-equity · Graham"),
        ("CASHFLOW", "Owner Earnings",     "Free cash flow · Buffett"),
    ]
    cards_html = ""
    for key, full_name, desc in cat_meta:
        n = stars.get(key, 0)
        star_color = (_TIER_COLORS.get("S") if n >= 5 else
                      _TIER_COLORS.get("A") if n >= 4 else
                      _TIER_COLORS.get("B") if n >= 3 else
                      _TIER_COLORS.get("C") if n >= 2 else
                      _TIER_COLORS.get("D"))
        filled = "●" * n + "○" * (5 - n)
        cards_html += (
            f"<div class='star-card'>"
            f"<div class='star-title'>{full_name}</div>"
            f"<div class='stars' style='color:{star_color};'>{filled}</div>"
            f"<div class='star-desc'>{desc}</div>"
            f"</div>"
        )
    st.markdown(cards_html + "</div></div>", unsafe_allow_html=True)

    # Business overview
    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown("**BUSINESS OVERVIEW**", unsafe_allow_html=False)
    profile, source = get_company_profile(ticker, company.get("name", ticker))
    if profile:
        st.info(f"*— {source}*\n\n{profile}")
    else:
        st.caption("Profile not available. Data sourced from SEC EDGAR XBRL only.")

    # Achievements
    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown("**ACHIEVEMENTS**", unsafe_allow_html=False)
    ach_html = ""
    n_earned = sum(achieved)
    ach_html += f"<p style='color:#2a6a8a;font-size:0.8rem;'>Unlocked {n_earned}/{len(_ACHIEVEMENTS)}</p>"
    for (name, desc), earned in zip(_ACHIEVEMENTS, achieved):
        if earned:
            ach_html += f"<p class='ach-earned'>★ {name} <span style='font-weight:normal;color:#a8c8e8;font-size:0.85rem;'>— {desc}</span></p>"
        else:
            ach_html += f"<p class='ach-locked'>✗ {name} <span style='font-size:0.85rem;'>— {desc}</span></p>"
    st.markdown(ach_html, unsafe_allow_html=True)


def render_flags_tab(flags: list, metrics: FinancialMetrics, ticker: str) -> None:
    score, tier = compute_score(metrics)
    n_crit = sum(1 for f in flags if f.severity == "CRITICAL")
    n_warn = sum(1 for f in flags if f.severity == "WARNING")
    n_info = sum(1 for f in flags if f.severity == "INFO")

    # Summary header
    col1, col2, col3, col4 = st.columns(4)
    tier_color = _TIER_COLORS.get(tier, "#00ccff")
    col1.metric("Health Score", f"{score}/100", f"Tier {tier}")
    col2.metric("CRITICAL", n_crit)
    col3.metric("WARNING",  n_warn)
    col4.metric("INFO",     n_info)

    st.markdown("<hr>", unsafe_allow_html=True)

    # Chart
    fig = make_flags_chart(flags, metrics)
    st.pyplot(fig, use_container_width=True)
    plt.close(fig)

    st.markdown("<hr>", unsafe_allow_html=True)

    if not flags:
        st.markdown(
            "<p style='color:#00e676;font-size:1rem;'>✓ No red flags detected for this company.</p>",
            unsafe_allow_html=True,
        )
        return

    # Group by severity
    for severity, cls, label in [
        ("CRITICAL", "flag-critical-card", "🔴 CRITICAL"),
        ("WARNING",  "flag-warning-card",  "🟠 WARNING"),
        ("INFO",     "flag-info-card",     "🔵 INFO"),
    ]:
        group = [f for f in flags if f.severity == severity]
        if not group:
            continue
        st.markdown(f"**{label}**", unsafe_allow_html=False)
        for f in group:
            sev_cls = f"flag-{severity.lower()}"
            st.markdown(
                f'<div class="flag-card {cls}">'
                f'<span class="{sev_cls}">[{f.severity}]</span> '
                f'<strong style="color:#a8c8e8;">{f.category}</strong>'
                f'<p style="color:#7a9ab8;margin:4px 0 0;font-size:0.88rem;">{f.message}</p>'
                f'</div>',
                unsafe_allow_html=True,
            )
        st.markdown("", unsafe_allow_html=False)


def render_news_tab(ticker: str) -> None:
    with st.spinner("Loading latest news…"):
        articles = get_news(ticker)

    st.markdown(
        f"<h3 style='color:#00ccff;margin-bottom:4px;'>TOP STORIES — {ticker}</h3>",
        unsafe_allow_html=True,
    )
    if not articles:
        st.info("No news articles found for this ticker.")
        return

    st.caption(f"{len(articles)} article(s) · click headlines to read")
    for art in articles:
        url   = art.get("link", "")
        title = art.get("title", "Untitled")
        desc  = art.get("description", "")
        pub   = art.get("pubDate", "")
        link_html = (f"<a class='news-headline' href='{url}' target='_blank'>{title}</a>"
                     if url else f"<span class='news-headline'>{title}</span>")
        st.markdown(
            f'<div class="news-card">'
            f'{link_html}'
            f'<div class="news-meta">{pub}</div>'
            f'<div class="news-desc">{desc}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )


def render_sentiment_tab(ticker: str) -> None:
    with st.spinner("Fetching social sentiment…"):
        feeds = get_sentiment(ticker)

    st.markdown(
        f"<h3 style='color:#00ccff;margin-bottom:4px;'>SOCIAL MEDIA SENTIMENT — {ticker}</h3>",
        unsafe_allow_html=True,
    )

    if not feeds:
        st.info("No social media data could be retrieved for this ticker.")
        st.caption("Note: Reddit RSS feeds may be rate-limited at times.")
        return

    for platform, articles in feeds:
        total_pos = total_neg = 0
        st.markdown(
            f"<p style='color:#00bbdd;font-weight:bold;margin-top:14px;'>{platform}</p>",
            unsafe_allow_html=True,
        )
        for art in articles:
            text = f"{art.get('title','')} {art.get('description','')}"
            pos, neg = _score_text(text)
            total_pos += pos
            total_neg += neg
            total = pos + neg or 1
            pct_pos = pos / total * 100
            if pct_pos >= 65:
                sent_cls, sent_label = "sent-strong-pos", "BULLISH"
            elif pct_pos >= 55:
                sent_cls, sent_label = "sent-pos",        "Leaning Bullish"
            elif pct_pos >= 45:
                sent_cls, sent_label = "sent-neutral",    "Neutral"
            elif pct_pos >= 35:
                sent_cls, sent_label = "sent-neg",        "Leaning Bearish"
            else:
                sent_cls, sent_label = "sent-strong-neg", "BEARISH"

            url   = art.get("link", "")
            title = art.get("title", "Untitled")
            pub   = art.get("pubDate", "")
            link_html = (f"<a class='news-headline' href='{url}' target='_blank'>{title}</a>"
                         if url else f"<span class='news-headline'>{title}</span>")
            st.markdown(
                f'<div class="news-card">'
                f'{link_html}'
                f'<div class="news-meta">{pub}'
                f'  &nbsp;·&nbsp;  <span class="{sent_cls}">{sent_label}</span>'
                f'  (+{pos} / -{neg})'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # Platform-level summary
        if total_pos + total_neg > 0:
            overall_pct = total_pos / (total_pos + total_neg) * 100
            if overall_pct >= 60:
                ov_cls, ov_lbl = "sent-strong-pos", "OVERALL BULLISH"
            elif overall_pct >= 50:
                ov_cls, ov_lbl = "sent-pos", "OVERALL LEANING BULLISH"
            elif overall_pct >= 40:
                ov_cls, ov_lbl = "sent-neg", "OVERALL LEANING BEARISH"
            else:
                ov_cls, ov_lbl = "sent-strong-neg", "OVERALL BEARISH"
            st.markdown(
                f"<p style='font-size:0.8rem;color:#2a6a8a;'>Aggregate signal: "
                f"<span class='{ov_cls}'>{ov_lbl}</span> "
                f"({overall_pct:.0f}% positive keywords)</p>",
                unsafe_allow_html=True,
            )


def render_glossary_tab() -> None:
    for section_name, items in _GLOSSARY:
        st.markdown(
            f"<h4 style='color:#00bbdd;margin-top:18px;'>{section_name}</h4>",
            unsafe_allow_html=True,
        )
        for metric, description in items:
            st.markdown(
                f"<p style='margin:6px 0;'>"
                f"<strong style='color:#00ccff;'>{metric}</strong>"
                f"<span style='color:#a8c8e8;'> — {description}</span></p>",
                unsafe_allow_html=True,
            )


def render_disclaimer_tab() -> None:
    st.markdown(
        f"<pre style='color:#a8c8e8;background:#080808;padding:20px;border-radius:6px;"
        f"font-family:Consolas,monospace;font-size:0.82rem;white-space:pre-wrap;'>"
        f"{_DISCLAIMER_TEXT}</pre>",
        unsafe_allow_html=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN APP
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    # ── Header ────────────────────────────────────────────────────────────────
    st.markdown(
        """<div style="background:#000000;padding:10px 16px 6px;border-bottom:1px solid #152035;">
        <span style="color:#00ccff;font-family:Consolas,monospace;font-size:1.35rem;font-weight:bold;">
          ◈  SIMPLIFIED STOCKS
        </span>
        <span style="color:#2a6a8a;font-family:Consolas,monospace;font-size:0.9rem;">
          &nbsp;&nbsp;//&nbsp;&nbsp;SEC EDGAR FINANCIAL ANALYZER
        </span>
        <span style="float:right;color:#2a6a8a;font-family:Consolas,monospace;font-size:0.75rem;padding-top:4px;">
          SEC EDGAR XBRL &nbsp;▸&nbsp; v2.0
        </span>
        </div>""",
        unsafe_allow_html=True,
    )

    # ── Live market strip ──────────────────────────────────────────────────────
    with st.spinner(""):
        indices = get_market_indices()
    render_market_strip(indices)

    # ── Company list (cached) ─────────────────────────────────────────────────
    companies = get_company_list()

    # ── Search bar ─────────────────────────────────────────────────────────────
    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
    col_search, col_btn, col_info = st.columns([4, 1, 4])

    with col_search:
        raw_input = st.text_input(
            label="▶  TICKER / COMPANY",
            placeholder="e.g. AAPL, MSFT, Tesla…",
            key="ticker_input",
            label_visibility="collapsed",
        )

    with col_btn:
        search_clicked = st.button("▶  SCAN", use_container_width=True)

    # ── Year range filter ──────────────────────────────────────────────────────
    current_yr = datetime.now().year
    col_from, col_to, col_apply, col_reset, col_gap = st.columns([2, 2, 1, 1, 4])
    with col_from:
        min_year = st.number_input("FROM", min_value=1993, max_value=current_yr,
                                   value=current_yr - 9, step=1, key="min_year")
    with col_to:
        max_year = st.number_input("TO",   min_value=1993, max_value=current_yr,
                                   value=current_yr, step=1, key="max_year")
    with col_apply:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        apply_filter = st.button("APPLY", key="apply_filter")
    with col_reset:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        reset_filter = st.button("RESET", key="reset_filter")

    # ── Resolve ticker ────────────────────────────────────────────────────────
    ticker = ""
    if raw_input:
        t = raw_input.strip().upper()
        if t in companies:
            ticker = t
        else:
            name_matches = [k for k, v in companies.items()
                            if t.lower() in v["name"].lower()]
            if len(name_matches) == 1:
                ticker = name_matches[0]
            elif name_matches:
                st.warning(f"Multiple matches for '{t}'. Showing top results:")
                options = [f"{k} — {companies[k]['name']}" for k in name_matches[:20]]
                chosen  = st.selectbox("Select company:", options, key="picker")
                if chosen:
                    ticker = chosen.split(" — ")[0].strip()
            elif raw_input:
                st.error(f"'{t}' not found in SEC EDGAR. Check the ticker spelling.")

    if not ticker:
        st.markdown(
            "<p style='color:#2a6a8a;font-style:italic;font-size:0.9rem;'>"
            "Enter a ticker symbol (e.g. AAPL) or company name to begin.</p>",
            unsafe_allow_html=True,
        )
        # Still show tabs (glossary, disclaimer work without data)
        _render_empty_tabs()
        return

    # ── Fetch data ────────────────────────────────────────────────────────────
    company = companies[ticker]
    try:
        metrics, flags = get_financial_data(ticker, company["cik"], company["name"])
    except Exception as exc:
        st.error(f"Failed to load data for {ticker}: {exc}")
        return

    # ── Year range ────────────────────────────────────────────────────────────
    all_years = get_fiscal_years(metrics)
    if reset_filter or not all_years:
        min_year = all_years[-1] if all_years else current_yr - 9
        max_year = all_years[0]  if all_years else current_yr
    years = get_fiscal_years(metrics, min_year=int(min_year), max_year=int(max_year))
    if not years:
        st.warning(f"No annual data between FY{min_year} and FY{max_year}. Showing all available years.")
        years = all_years

    # ── Info bar ──────────────────────────────────────────────────────────────
    yr_range = f"FY{years[-1]}–FY{years[0]}" if len(years) > 1 else f"FY{years[0]}"
    st.markdown(
        f"<p style='color:#00aadd;font-family:Consolas,monospace;font-size:0.88rem;"
        f"background:#000;padding:4px 0;'>"
        f"&nbsp; {company['name']} &nbsp;│&nbsp; {ticker} &nbsp;│&nbsp; "
        f"CIK: {company['cik']} &nbsp;│&nbsp; {yr_range} &nbsp;│&nbsp; "
        f"{len(years)} annual periods</p>",
        unsafe_allow_html=True,
    )

    # ── Live price bar ────────────────────────────────────────────────────────
    with st.spinner(""):
        quote = get_stock_price(ticker)
    render_price_bar(ticker, company["name"], quote)

    # ── Score summary in sidebar ──────────────────────────────────────────────
    score, tier = compute_score(metrics)
    tier_color  = _TIER_COLORS.get(tier, "#00ccff")
    with st.sidebar:
        st.markdown(
            f"<h3 style='color:#00ccff;font-family:Consolas;'>📊 {ticker}</h3>",
            unsafe_allow_html=True,
        )
        st.markdown(
            f"<div style='text-align:center;padding:12px;background:#0a0a0a;"
            f"border:1px solid #152035;border-radius:8px;'>"
            f"<div style='color:#2a6a8a;font-size:0.7rem;'>HEALTH SCORE</div>"
            f"<div style='color:{tier_color};font-size:2.5rem;font-weight:bold;'>{score}</div>"
            f"<div style='background:{tier_color};color:#000;display:inline-block;"
            f"padding:2px 14px;border-radius:4px;font-weight:bold;font-size:1.1rem;'>{tier}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            f"<p style='color:#2a6a8a;font-size:0.75rem;margin-top:8px;'>{company['name']}</p>",
            unsafe_allow_html=True,
        )
        n_crit = sum(1 for f in flags if f.severity == "CRITICAL")
        n_warn = sum(1 for f in flags if f.severity == "WARNING")
        if n_crit:
            st.error(f"🔴 {n_crit} CRITICAL flag(s)")
        if n_warn:
            st.warning(f"🟠 {n_warn} WARNING flag(s)")
        if not flags:
            st.success("✓ No red flags")
        st.markdown("<hr>", unsafe_allow_html=True)
        st.caption("Data: SEC EDGAR XBRL")
        st.caption("simplified-stocks.com")

    # ── Tabs ──────────────────────────────────────────────────────────────────
    tab_labels = [
        "📊 Summary",
        "💰 Income Statement",
        "🏦 Balance Sheet",
        "💵 Cash Flow",
        "📐 Key Ratios",
        "🚩 Red Flags",
        "📰 Top Stories",
        "💬 Sentiment",
        "📖 Glossary",
        "⚠️ Disclaimer",
    ]
    tabs = st.tabs(tab_labels)

    # Summary
    with tabs[0]:
        render_summary_tab(metrics, flags, ticker, company)

    # Income Statement
    with tabs[1]:
        rows = build_income_rows(metrics, years)
        st.markdown(render_financial_table(rows, years), unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)
        fig = make_income_chart(metrics, years)
        st.pyplot(fig, use_container_width=True)
        plt.close(fig)

    # Balance Sheet
    with tabs[2]:
        rows = build_balance_rows(metrics, years)
        st.markdown(render_financial_table(rows, years), unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)
        fig = make_balance_chart(metrics, years)
        st.pyplot(fig, use_container_width=True)
        plt.close(fig)

    # Cash Flow
    with tabs[3]:
        rows = build_cashflow_rows(metrics, years)
        st.markdown(render_financial_table(rows, years), unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)
        fig = make_cashflow_chart(metrics, years)
        st.pyplot(fig, use_container_width=True)
        plt.close(fig)

    # Key Ratios
    with tabs[4]:
        rows = build_ratios_rows(metrics, years)
        st.markdown(render_financial_table(rows, years), unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)
        fig = make_ratios_chart(metrics, years)
        st.pyplot(fig, use_container_width=True)
        plt.close(fig)

    # Red Flags
    with tabs[5]:
        render_flags_tab(flags, metrics, ticker)

    # News
    with tabs[6]:
        render_news_tab(ticker)

    # Sentiment
    with tabs[7]:
        render_sentiment_tab(ticker)

    # Glossary
    with tabs[8]:
        render_glossary_tab()

    # Disclaimer
    with tabs[9]:
        render_disclaimer_tab()


def _render_empty_tabs() -> None:
    """Show glossary and disclaimer even before a ticker is searched."""
    tabs = st.tabs(["📖 Glossary", "⚠️ Disclaimer"])
    with tabs[0]:
        render_glossary_tab()
    with tabs[1]:
        render_disclaimer_tab()


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__" or True:
    main()
