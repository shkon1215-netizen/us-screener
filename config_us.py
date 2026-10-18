"""Configuration for the United States (NYSE + Nasdaq) valuation screener.

Eighth market after Korea, the UK, Japan, Germany, Poland, France and Hong
Kong, built on Hong Kong's copy because it had already combined what the
others learned. The quality floor, the discount test and the history screen
are the user's screening preferences and are carried over unchanged. What is
local:

  * the roster - Nasdaq's stock screener feed, one request for every common
    line on NYSE, Nasdaq and NYSE American with a market cap, joined to
    Nasdaq Trader's symbol directory (the exchange, the ETF flag, test
    issues, Nasdaq's financial-status code) and to the SEC's ticker file
    (the CIK - the issuer key that makes one-line-per-issuer exact)
  * investment vehicles - the SEC's own registers of closed-end funds and
    business development companies, by CIK: the US counterpart of the UK's
    AIC register, and for the same reason - Yahoo files PIMCO's income funds
    and Ares Capital under "Asset Management", beside BlackRock
  * the pre-gate - Nasdaq's market cap, as Hong Kong used HKEX's: Yahoo's
    slow .info runs on ~3,500 lines, not 7,000
  * classification - Yahoo's industry, falling back to Yahoo's sector, as in
    the UK and Hong Kong. Nasdaq's own sector field files Agilent under
    Industrials and is not used
  * the currency trap, small - the home currency is the dollar, so it touches
    only the companies that list here and report in another currency
    (Canada's interlisted banks and miners, Spotify in euros)
"""
from __future__ import annotations

import re
from dataclasses import dataclass

VALUATION_METRICS = ("trailing_pe", "price_to_book", "ev_to_ebitda")

METRIC_LABELS = {
    "trailing_pe": "P/E",
    "price_to_book": "P/B",
    "ev_to_ebitda": "EV/EBITDA",
}

# Same reasoning as Korea: a zero or negative multiple means "no earnings" or
# "negative equity", never "cheap". The lower P/B bound also removes
# Berkshire's B line, whose Yahoo bookValue is per A share (P/B 0.001).
METRIC_BOUNDS = {
    "trailing_pe": (1.0, 200.0),
    "price_to_book": (0.05, 30.0),
    "ev_to_ebitda": (0.5, 100.0),
}

HOME_CURRENCY = "USD"

# ---------------------------------------------------------------------------
# Exchanges - Nasdaq Trader's codes
# ---------------------------------------------------------------------------
# nasdaqlisted.txt carries a Market Category (Q Global Select, G Global
# Market, S Capital Market); otherlisted.txt an Exchange code. Arca and Cboe
# list almost only ETPs; the handful of operating companies there are kept.
OTHER_EXCHANGES = {"N": "NYSE", "A": "NYSE American", "P": "NYSE Arca",
                   "Z": "Cboe", "V": "IEX", "M": "NYSE Texas"}
NASDAQ_BOARD = "Nasdaq"

# Nasdaq's Financial Status codes. D (deficient: a listing standard such as
# the $1 bid) says nothing at USD 600m; the rest mean filings are late or the
# company is in bankruptcy - Korea's 관리종목, decided by the exchange's own
# label, not a heuristic. NYSE publishes no counterpart in the directory.
DISTRESS_STATUS = {"E", "Q", "G", "H", "J", "K"}

# ---------------------------------------------------------------------------
# Security types, from the exchange's own security name
# ---------------------------------------------------------------------------
# Nasdaq's screener lists every exchange line, not every company: preferreds
# (ACGLN "Depositary Shares, each representing 1/1000th... Preferred"),
# baby bonds (AFGB "5.875% Subordinated Debentures"), warrants, rights, SPAC
# units. The names are the exchange's and say what the line is. ORDER
# matters: "American Depositary Shares" is checked before "Depositary
# Shares", which on its own means a slice of a preferred.
# "New York Registry Shares" (ASML, Philips, Aegon) are the Dutch form of
# the same thing: a foreign share registered for trading here.
ADR_MARK = re.compile(r"\bAMERICAN DEPOS[IO]T[AO]RY\b|\bGLOBAL DEPOSITARY\b"
                      r"|\b(NY|NEW YORK) REGISTRY\b")
# "ADS" / "ADR" as a word anywhere but the first: "Uxin Limited ADS",
# "Anheuser-Busch Inbev SA Sponsored ADR" - not ADS-TEC Energy's ordinary
# shares.
ADR_WORDS = {"ADS", "ADSS", "ADR", "ADRS"}
PREFERRED_MARK = re.compile(r"\bPREFERRED\b|\bPREF\b|\bPFD\b|\bDEPOSIT[AO]RY SHARES?\b"
                            r"|\bPERPETUAL\b|\bCUMULATIVE\b|\bFIXED[- ]TO[- ]FLOATING\b")
DEBT_MARK = re.compile(r"\bNOTES?\b|\bDEBENTURES?\b|\bBONDS?\b|\bSENIOR\b.*\bDUE\b"
                       r"|\bDUE \d{4}\b|\bSUBORDINATED\b")
WARRANT_MARK = re.compile(r"\bWARRANTS?\b")
RIGHT_MARK = re.compile(r"\bRIGHTS?\b")
# SPAC units ("Units, each consisting of one Class A ordinary share and one-
# half of one redeemable warrant"). An MLP's "Common Units representing
# limited partner interests" is the partnership's equity and stays.
UNIT_MARK = re.compile(r"\bUNITS?\b")
LP_UNITS = re.compile(r"\bCOMMON UNITS?\b|\bLIMITED PARTNER(SHIP)? (INTERESTS?|UNITS?)\b"
                      r"|\bUNITS REPRESENTING\b|\bL\.?P\.? UNITS?\b|\bCLASS [A-Z] UNITS?\b")
# NYSE / Cboe ACT symbols mark the class in the symbol itself: "$" preferred,
# ".W" warrant, ".U" unit, ".R" right ("JPM$C", "XYZ.U").
ACT_NONCOMMON = re.compile(r"\$|\.(W|WS|U|R|RT|WI)$|\.WS\.")
# Nasdaq's screener writes an NYSE preferred as "ABR^D" and a warrant as
# "XYZ/WS" - the same marks in its own symbology.
SCREENER_NONCOMMON = re.compile(r"\^|/WS|/U$|/R$")


# A line the exchange calls common is common, whatever the company is
# called: "Preferred Bank Common Stock", "Bank Nova Scotia Halifax Pfd 3
# Ordinary Shares" (Nasdaq's name for the Bank of Nova Scotia), "Cohen &
# Steers Select Preferred and Income Fund Inc. Common Stock" (a closed-end
# fund - the SEC register removes it, not this rule).
COMMON_MARK = re.compile(r"\bCOMMON (STOCK|SHARES?)\b|\bORDINARY SHARES?\b|\bCAPITAL STOCK\b"
                         r"|\bSHARES OF BENEFICIAL INTEREST\b")


def security_kind(name: str) -> str:
    """'adr', 'preferred', 'debt', 'warrant', 'right', 'unit' or '' (common).

    Pure; test_us.py plants the exchange's own phrasings. Order matters:
    an ADR, a warrant, a right or a SPAC unit names the common share it is
    written on ("Warrants to purchase Common Stock", "Units, each consisting
    of one Class A Ordinary Share and..."), so those are decided first; then
    a line the exchange calls common is common; then preferreds and debt.
    """
    n = " ".join(str(name or "").upper().replace(",", " ").split())
    if ADR_MARK.search(n) or ADR_WORDS & set(plain(n).split()[1:]):
        return "adr"
    if WARRANT_MARK.search(n):
        return "warrant"
    if RIGHT_MARK.search(n):
        return "right"
    if UNIT_MARK.search(n) and not LP_UNITS.search(n):
        return "unit"
    if COMMON_MARK.search(n):
        return ""
    if PREFERRED_MARK.search(n):
        return "preferred"
    if DEBT_MARK.search(n):
        return "debt"
    return ""


# ---------------------------------------------------------------------------
# Yahoo classification
# ---------------------------------------------------------------------------
# Financials, by Yahoo's sector: banks, insurers, brokers, asset managers.
# Yahoo gives real estate its own sector, so no carve-out is needed.
FINANCIAL_SECTORS = ("Financial Services",)

# SPACs: a trust account of Treasury bills at ~10 dollars a share. Yahoo
# files every one under "Shell Companies"; its multiples are a trust's.
SHELL_INDUSTRIES = ("Shell Companies",)

# Property is NOT excluded here, unlike Hong Kong and Germany. Their reason
# was IAS 40 / HKAS 40: investment property at fair value, revaluations
# through earnings, an appraised book. US GAAP carries real estate at
# depreciated cost, so a US developer's P/B and P/E mean what they mean
# anywhere else. REITs still go (every market's rule: a pass-through vehicle
# priced on its distribution).

# ---------------------------------------------------------------------------
# Holding companies - flags by default, never silent
# ---------------------------------------------------------------------------
# Hong Kong's answer, by ticker: a named list with what each holds, plus
# France's data test for a parent that consolidates a listed subsidiary
# (us_filters.flag_pyramids). Add to it rather than invent a heuristic.
KNOWN_HOLDCOS = {
    "L": "Loews (CNA Financial ~92%, Boardwalk Pipelines, Loews Hotels)",
    "IEP": "Icahn Enterprises (CVR Energy ~66%, investment funds)",
    "BN": "Brookfield Corporation (Brookfield Asset Management and affiliates)",
    "IAC": "IAC (MGM Resorts stake, People Inc., Care.com)",
    "CNNE": "Cannae Holdings (minority stakes in private and listed companies)",
}

# A parent consolidating a listed subsidiary files the subsidiary's income
# statement plus its own small activities. France's thresholds, unchanged.
PYRAMID_REVENUE_TOL = 0.01
PYRAMID_OP_TOL = 0.05
PYRAMID_MIN_MI_SHARE = 0.25
PYRAMID_MI_GAP = 0.15

# Closely held: Yahoo's heldPercentInsiders at a majority - a founder
# (Meta's class B, counted by Yahoo as insiders) or a parent. France's flag.
CONTROL_MIN_INSIDERS = 0.50


@dataclass
class ScreenConfig:
    # --- Size / liquidity, specified in USD ---
    min_market_cap_usd: float = 600_000_000
    # Measured, not inherited - see CLAUDE.md "The liquidity gate".
    min_adv_usd: float = 4_000_000
    adv_lookback_days: int = 60                 # trading days
    # The Nasdaq pre-gate: lines whose Nasdaq market cap is under this share
    # of the floor are not sent to Yahoo at all. Not the floor itself: for a
    # multi-class company Nasdaq's figure can be one class only.
    pregate_fraction: float = 0.25

    # --- Valuation test ---
    discount_threshold: float = 0.20
    metrics: tuple[str, ...] = VALUATION_METRICS
    min_metrics_passing: int = 2
    min_valid_metrics: int = 2

    # --- Quality floor ---
    min_roe_pct: float = 5.0
    roe_good_pct: float = 10.0

    # --- Absolute value screen ---
    # Re-measured on the US universe - see CLAUDE.md "Calibration". Both at
    # ~p25 (P/B p25 1.53, EV/EBITDA p25 9.03 on 2026-10-16); Korea's 1.0 and
    # 8 sit at ~p7 and ~p17 here.
    abs_max_pbr: float = 1.5
    abs_max_ev_ebitda: float = 9.0
    abs_require_roe: bool = True
    abs_financials_pbr_only: bool = True
    abs_require_pbr_vs_roe: bool = True
    abs_cost_of_equity_pct: float = 10.0
    abs_min_div_yield: float = 2.0

    # --- Own-history screen ---
    hist_min_discount: float = 0.30
    hist_min_metrics: int = 2
    hist_min_years: int = 3
    hist_require_roe: bool = True

    # --- Peer groups ---
    # industry = Yahoo industry, sector = Yahoo sector (the UK's shape).
    peer_keys: tuple[str, ...] = ("industry",)
    fallback_peer_keys: tuple[str, ...] = ("sector",)
    min_peers: int = 5
    winsor_pct: float = 0.05

    # --- US universe hygiene ---
    exclude_investment_companies: bool = True   # closed-end funds and BDCs (SEC registers)
    exclude_reits: bool = True
    exclude_adrs: bool = True                   # foreign secondaries, by name
    # Accounts in another currency: measured 2026-10-18, 121 names past USD
    # 600m, nearly all with a home market elsewhere - ADRs Nasdaq names
    # "Common Stock" (TSMC, Toyota, HDFC Bank, Novo Nordisk, KB Financial)
    # and Canada's interlisted banks, pipelines and miners. Germany's,
    # France's and Hong Kong's foreign-secondary rule. The cost: a handful of
    # foreign companies listed ONLY here (Spotify, On, Birkenstock, StoneCo)
    # go with them - --include-foreign keeps all of them, with the currency
    # repair.
    exclude_foreign_reporters: bool = True
    exclude_distressed: bool = True             # Nasdaq status: delinquent / bankrupt
    exclude_pyramid_parents: bool = True        # one set of earnings, twice
    flag_holdcos: bool = True
    exclude_holdcos: bool = False               # flagged by default, not dropped
    exclude_controlled: bool = False

    exclude_sectors: tuple[str, ...] = ()

    # --- Fetching ---
    # Yahoo's .info is the call that throttles, globally and silently. The
    # UK build's 2 workers, with a longer pause than its 0.25s: at that pace
    # the first US run was cut off 1,500 calls in (~4 a second).
    max_workers: int = 2
    request_delay: float = 0.5
    cache_dir: str = ".us_cache"
    cache_ttl_hours: int = 20


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------
REIT_TOKENS = ("REIT",)

# Words dropped from names before lines are grouped by issuer when the SEC
# file has no CIK for one of them: "Alphabet Inc. Class A Common Stock" and
# "Alphabet Inc. Class C Capital Stock".
CLASS_WORDS = {"CLASS", "SERIES", "A", "B", "C", "K", "COMMON", "CAPITAL", "STOCK",
               "SHARES", "ORDINARY", "SUBORDINATE", "VOTING", "NON", "NEW", "LIMITED"}
LEGAL_WORDS = {"INC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "PLC", "LLC",
               "HOLDINGS", "GROUP", "SA", "NV", "AG", "THE"}


def plain(s: str) -> str:
    """Upper-case, punctuation to spaces."""
    return " ".join(re.sub(r"[^A-Z0-9&]+", " ", str(s or "").upper()).split())


def yahoo_symbol(symbol: str) -> str:
    """Exchange symbol -> Yahoo's: BRK/B, BRK.B and BRK B all become BRK-B."""
    s = str(symbol or "").strip().upper()
    return re.sub(r"[/. ]", "-", s)


def issuer_key(name: str) -> str:
    """Name-based issuer key, the fallback when a line has no CIK. Class and
    stock words go, then legal forms - Germany's lesson about order."""
    toks = [t for t in plain(name).split() if t not in CLASS_WORDS]
    toks = [t for t in toks if t not in LEGAL_WORDS]
    return " ".join(toks)


def board_of(exchange_code: str, in_nasdaq_file: bool) -> str:
    if in_nasdaq_file:
        return NASDAQ_BOARD
    return OTHER_EXCHANGES.get(str(exchange_code or "").strip().upper(), "")


def is_financial(sector: str = "", industry: str = "") -> bool:
    """Yahoo's Financial Services sector. Called (sector, industry) by the
    dashboard too, so its carve-out agrees with Python's."""
    return str(sector or "").strip() in FINANCIAL_SECTORS


def is_shell(industry: str) -> bool:
    return str(industry or "").strip() in SHELL_INDUSTRIES


def is_reit(name: str, industry: str = "") -> bool:
    return ("REIT" in str(industry or "").upper()
            or any(t in plain(name).split() for t in REIT_TOKENS))


def is_holdco(symbol: str) -> bool:
    return yahoo_symbol(symbol) in KNOWN_HOLDCOS
