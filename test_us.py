"""Offline validation of the US screener. No network required.

Run: python test_us.py

Every planted case is something a naive screener gets wrong, and most were
measured on the first live run (2026-10-18): Nasdaq's screener listing
preferreds and baby bonds beside the common, "Preferred Bank Common Stock",
ADS-TEC Energy's ordinary shares beside real ADSs, Alphabet's two classes,
a closed-end fund and a BDC that Yahoo calls "Asset Management", a SPAC,
Berkshire's B line with a book value per A share, and a Canadian bank that
quotes in dollars and reports in Canadian dollars.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

import config_us as K
import us_filters as UF
from config_us import ScreenConfig
from providers_us import (parse_screener, parse_sec_register, parse_sec_tickers,
                          parse_submissions, parse_symbol_directory)
from screener import run_screen

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

rng = np.random.default_rng(7)

USD = 1.0
MCAP = 1.5e9           # clears the gate
ADV = 2.0e7            # clears the gate

FAILURES: list[str] = []


def ok(cond, label):
    print(f"  {'OK  ' if cond else 'FAIL'} {label}")
    if not cond:
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# Parsers and helpers
# ---------------------------------------------------------------------------
def check_parsers():
    print("=== parsers and helpers ===")
    ok(K.yahoo_symbol("BRK/B") == "BRK-B" and K.yahoo_symbol("BRK.B") == "BRK-B"
       and K.yahoo_symbol("AAPL") == "AAPL",
       "exchange symbols -> Yahoo's spelling (BRK/B, BRK.B -> BRK-B)")

    scr = parse_screener({"data": {"rows": [
        {"symbol": "A", "name": "Agilent Technologies Inc. Common Stock", "lastsale": "$175.55",
         "marketCap": "49492610320.00", "country": "United States"},
        {"symbol": "BRK/B", "name": "Berkshire Hathaway Inc.", "lastsale": "$480.10",
         "marketCap": "", "country": "United States"},
        {"symbol": "", "name": "blank", "marketCap": "1"},
    ]}})
    m = scr.set_index("symbol")
    ok(len(scr) == 2 and abs(m.loc["A", "nasdaq_mcap_usd"] - 49492610320.0) < 1
       and np.isnan(m.loc["BRK/B", "nasdaq_mcap_usd"]) and m.loc["A", "last_usd"] == 175.55,
       "Nasdaq screener: dollars parsed, a blank cap is missing not zero, blank symbols dropped")

    nl = ("Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
          "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N\n"
          "QQQ|Invesco QQQ Trust|G|N|N|100|Y|N\n"
          "ZZZT|Test Issue Corp|S|Y|N|100|N|N\n"
          "HUBG|Hub Group - Class A Common Stock|Q|N|E|100|N|N\n"
          "File Creation Time: 1018202608:00|||||||\n")
    ol = ("ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
          "BRK.B|Berkshire Hathaway Inc. Class B|N|BRK.B|N|100|N|BRK=B\n"
          "JPM$C|JPMorgan Chase Depositary Shares Series C|N|JPMpC|N|100|N|JPM-C\n"
          "SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY\n"
          "File Creation Time: 1018202608:00|||||||\n")
    sd = parse_symbol_directory(nl, ol).set_index("ysym")
    ok(len(sd) == 7 and sd.loc["AAPL", "board"] == "Nasdaq" and sd.loc["BRK-B", "board"] == "NYSE"
       and sd.loc["SPY", "board"] == "NYSE Arca",
       "symbol directory: both files read, footer dropped, exchange named")
    ok(bool(sd.loc["QQQ", "is_etf"]) and bool(sd.loc["ZZZT", "is_test"])
       and sd.loc["HUBG", "fin_status"] == "E" and bool(sd.loc["JPM$C", "act_noncommon"])
       and not bool(sd.loc["BRK-B", "act_noncommon"]),
       "ETF and test flags, Nasdaq's status code, and the ACT '$' preferred mark read")

    sec = parse_sec_tickers({"fields": ["cik", "name", "ticker", "exchange"],
                             "data": [[1652044, "Alphabet Inc.", "GOOGL", "Nasdaq"],
                                      [1652044, "Alphabet Inc.", "GOOG", "Nasdaq"],
                                      [1067983, "BERKSHIRE HATHAWAY INC", "BRK-B", "NYSE"]]})
    ok(len(sec) == 3 and set(sec.loc[sec["cik"] == 1652044, "ysym"]) == {"GOOGL", "GOOG"},
       "SEC tickers: one CIK, two classes")
    reg = parse_sec_register("﻿File_No,CIK,Registrant_Name\n811-22673,0001510599,PIMCO Dynamic Income Fund\n"
                             "814-00746,0001396440,Main Street Capital CORP\n")
    ok(reg == {1510599, 1396440}, "SEC register: byte-order mark and zero-padded CIKs handled")

    sub = lambda forms, files: {"filings": {"recent": {"form": forms,  # noqa: E731
                                                       "fileNumber": files}}}
    ok(parse_submissions(sub(["N-CSRS", "NPORT-P", "N-PX"], ["811-21432"])) == "cef"
       and parse_submissions(sub(["10-K", "N-2"], ["814-00663", "333-1"])) == "bdc"
       and parse_submissions(sub(["10-K", "N-PX", "8-K"], ["001-33099", "028-1"])) == ""
       and parse_submissions(sub(["10-K", "SC 13G"], ["001-1", "814-00663"])) == ""
       and parse_submissions(sub(["10-K"], ["001-1", "811-9"])) == ""
       and parse_submissions({}) == "",
       "EDGAR: a fund's own reports -> CEF, an 814- number on its own 10-K -> BDC; "
       "a manager's N-PX, an 814- on a 13G about a BDC, or an 811- alone is not a fund")

    kinds = {
        "Agilent Technologies Inc. Common Stock": "",
        "Sea Limited American Depositary Shares each representing one Class A Ordinary Share": "adr",
        "Anheuser-Busch Inbev SA Sponsored ADR (Belgium)": "adr",
        "Uxin Limited ADS": "adr",
        "IRSA Inversiones Y Representaciones S.A. Global Depositary Shares": "adr",
        "ASML Holding N.V. New York Registry Shares": "adr",
        "ADS-TEC ENERGY PLC Ordinary Shares": "",
        "Alphabet Inc. Depositary Shares representing a 1/20th Interest in a Share of Series B": "preferred",
        "First Merchants Corporation Depository Shares": "preferred",
        "Regency Centers Corporation 6.25% Series A Cumulative Redeemable Preferred Stock": "preferred",
        "Preferred Bank Common Stock": "",
        "Bank Nova Scotia Halifax Pfd 3 Ordinary Shares": "",
        "Trinity Capital Inc. 7.875% Notes Due 2029": "debt",
        "Entergy Mississippi LLC First Mortgage Bonds 4.90% Series Due October 1 2066": "debt",
        "SoundHound AI Inc. Warrant": "warrant",
        "Gen Digital Inc. Contingent Value Rights": "right",
        "PureCycle Technologies Inc. Unit": "unit",
        "Novanta Inc. Tangible Equity Units": "unit",
        "MPLX LP Common Units Representing Limited Partner Interests": "",
        "Brookfield Infrastructure Partners LP Limited Partnership Units": "",
        "AllianceBernstein Holding L.P.  Units": "",
        "Plains GP Holdings L.P. Class A Units representing Limited Partner Interests": "",
        "Unity Software Inc. Common Stock": "",
        "United Parcel Service Inc. Common Stock": "",
    }
    bad = {n: (K.security_kind(n), k) for n, k in kinds.items() if K.security_kind(n) != k}
    ok(not bad, f"security type from the exchange's own name ({len(kinds)} phrasings) {bad or ''}")

    ok(K.issuer_key("Alphabet Inc. Class A Common Stock")
       == K.issuer_key("Alphabet Inc. Class C Capital Stock") == "ALPHABET",
       "name fallback groups Alphabet's classes")
    ok(K.is_financial("Financial Services") and not K.is_financial("Real Estate"),
       "financials by Yahoo's sector; real estate is not one")
    ok(K.is_holdco("L") and not K.is_holdco("LOW"), "named holding company by exact symbol")


# ---------------------------------------------------------------------------
# Roster hygiene and the pre-gate
# ---------------------------------------------------------------------------
def check_roster():
    print("\n=== roster hygiene and pre-gate ===")
    cfg = ScreenConfig()

    def line(sym, name, cik=None, cap=MCAP, raw=None, **kw):
        r = dict(ticker=K.yahoo_symbol(sym), symbol=K.yahoo_symbol(sym), raw_symbol=raw or sym,
                 name=name, board="NYSE", cik=cik, nasdaq_mcap_usd=cap, is_etf=False,
                 is_test=False, fin_status="", act_noncommon=False, country="United States")
        r.update(kw)
        return r
    df = pd.DataFrame([
        line("GOOGL", "Alphabet Inc. Class A Common Stock", cik=1652044, cap=2.0e12),
        line("GOOG", "Alphabet Inc. Class C Capital Stock", cik=1652044, cap=1.9e12),
        line("GOOGN", "Alphabet Inc. Depositary Shares representing a 1/20th Interest in a "
                      "Share of Series B Mandatory Convertible Preferred", cik=1652044),
        line("ABR^D", "Arbor Realty Trust 6.375% Series D Cumulative", raw="ABR^D"),
        line("SPY", "SPDR S&P 500 ETF Trust", is_etf=True),
        line("SE", "Sea Limited American Depositary Shares each representing one Class A "
                   "Ordinary Share", cik=1703399),
        line("PDI", "PIMCO Dynamic Income Fund Common Shares of Beneficial Interest", cik=1510599),
        line("ARCC", "Ares Capital Corporation Common Stock", cik=1287750),
        line("HUBG", "Hub Group Inc. Class A Common Stock", cik=940942, fin_status="E"),
        line("XYZW", "Example Deficient Corp. Common Stock", cik=111, fin_status="D"),
        line("ABCD", "Example REIT Inc. Common Stock", cik=222),
        line("TINY", "Tiny Co Common Stock", cik=333, cap=5e7),
        line("NOCAP", "No Cap Co Common Stock", cik=444, cap=np.nan),
        line("PFBC", "Preferred Bank Common Stock", cik=1492165),
    ])
    out, stats = UF.apply_roster_filters(df, cfg, cef={1510599}, bdc={1287750})
    syms = set(out["symbol"])
    for s, why in [("GOOGN", "a depositary share of a preferred dropped"),
                   ("ABR^D", "Nasdaq's '^' preferred symbol dropped"),
                   ("SPY", "ETF dropped by Nasdaq Trader's flag"),
                   ("SE", "ADR dropped (priced at home)"),
                   ("PDI", "closed-end fund dropped by the SEC register"),
                   ("ARCC", "BDC dropped by the SEC register"),
                   ("HUBG", "Nasdaq status E (filings delinquent) dropped"),
                   ("ABCD", "REIT by name dropped")]:
        ok(K.yahoo_symbol(s) not in syms, why)
    ok({"GOOGL", "GOOG", "XYZW", "PFBC", "TINY", "NOCAP"} <= syms,
       "both Alphabet classes, a merely deficient line and 'Preferred Bank' kept for now")
    ok(int(out.set_index("symbol").loc["GOOGL", "issuer_lines"]) == 2,
       "Alphabet counted as one issuer with two lines")
    pg, _ = UF.pregate(out, cfg)
    ok("TINY" not in set(pg["symbol"]) and "NOCAP" in set(pg["symbol"]),
       "pre-gate: far below the floor not sent; no Nasdaq cap is sent, not guessed small")

    snap = pd.DataFrame([dict(symbol="NOCAP", issuer_lines=1, market_cap_local=np.nan,
                              nasdaq_mcap_usd=9e9, mcap_source=""),
                         dict(symbol="GOOG", issuer_lines=2, market_cap_local=np.nan,
                              nasdaq_mcap_usd=1.9e12, mcap_source="")])
    rep, mstats = UF.repair_market_caps(snap)
    ok(rep.loc[0, "market_cap_local"] == 9e9 and rep.loc[0, "mcap_source"] == "nasdaq"
       and np.isnan(rep.loc[1, "market_cap_local"]) and mstats["mcap_via_nasdaq"] == 1,
       "Nasdaq's cap fills a blank Yahoo cap - never for a multi-class issuer (one class is not the company)")

    priced = pd.DataFrame([
        dict(symbol="BDJ", cik=1332283, sector="Financial Services", industry="Asset Management"),
        dict(symbol="BLK", cik=2012383, sector="Financial Services", industry="Asset Management"),
        dict(symbol="XBDC", cik=999, sector="Financial Services", industry="Asset Management"),
        dict(symbol="AAPL", cik=320193, sector="Technology", industry="Consumer Electronics"),
        dict(symbol="NOSEC", cik=555, sector="", industry=""),
    ])
    cand = UF.fund_check_candidates(priced)
    ok(list(priced.loc[cand, "symbol"]) == ["BDJ", "BLK", "XBDC", "NOSEC"],
       "EDGAR is asked only about financials and sectorless lines")
    kept, fstats = UF.apply_fund_filings(priced, {1332283: "cef", 2012383: "", 999: "bdc"}, cfg, {})
    ok(set(kept["symbol"]) == {"BLK", "AAPL", "NOSEC"}
       and fstats["dropped_closed_end_fund_edgar"] == 1 and fstats["dropped_bdc_edgar"] == 1,
       "a fund the registers missed is dropped by its filings; the asset manager stays")

    lines = pd.DataFrame([dict(ticker="GOOGL", name="Alphabet A", issuer="cik:1652044", adv_local=6e9),
                          dict(ticker="GOOG", name="Alphabet C", issuer="cik:1652044", adv_local=4e9),
                          dict(ticker="META", name="Meta", issuer="cik:1326801", adv_local=5e9)])
    kept, lstats = UF.keep_one_line_per_issuer(lines)
    ok(set(kept["ticker"]) == {"GOOGL", "META"} and lstats["dropped_second_line"] == 1,
       "one line per issuer: Alphabet's more traded class kept, the other dropped")


# ---------------------------------------------------------------------------
# The screens on a planted universe
# ---------------------------------------------------------------------------
def base(**kw):
    r = dict(symbol="", name="", board="NYSE", currency="USD",
             fin_ccy="USD", sector="Industrials", industry="Specialty Industrial Machinery",
             quote_type="EQUITY", market_cap_local=MCAP, adv_local=ADV, close_local=50.0,
             trailing_pe=np.nan, price_to_book=np.nan, ev_to_ebitda=np.nan,
             trailing_eps=4.0, book_value_ps=32.0, div_yield=1.5)
    r.update(kw)
    r["ticker"] = r["symbol"]
    return r


def make_universe() -> pd.DataFrame:
    rows = []
    specs = [("Specialty Industrial Machinery", "Industrials", 22.0, 4.0, 14.0, 18),
             ("Software - Application", "Technology", 35.0, 8.0, 25.0, 14),
             ("Banks - Regional", "Financial Services", 12.0, 1.2, np.nan, 10)]
    n = 0
    for ind, sec, pe, pb, ev, count in specs:
        for _ in range(count):
            n += 1
            rows.append(base(symbol=f"P{n:03d}", name=f"PEER {n}", industry=ind, sector=sec,
                             trailing_pe=pe * rng.uniform(0.93, 1.09),
                             price_to_book=pb * rng.uniform(0.93, 1.09),
                             ev_to_ebitda=ev * rng.uniform(0.93, 1.09),
                             market_cap_local=MCAP * rng.uniform(0.9, 2.5)))
    # PASS: cheap vs machinery peers, good ROE, pays a dividend.
    rows.append(base(symbol="GVAL", name="GENUINE VALUE", trailing_pe=9.0, price_to_book=1.2,
                     ev_to_ebitda=6.0, trailing_eps=5.0, book_value_ps=36.0, div_yield=3.5))
    # FAIL: a SPAC - Yahoo's "Shell Companies", a trust of T-bills.
    rows.append(base(symbol="SPAC", name="EXAMPLE ACQUISITION CORP", sector="Financial Services",
                     industry="Shell Companies", trailing_pe=30.0, price_to_book=1.0))
    # FAIL: a REIT Yahoo names that the roster's name test missed.
    rows.append(base(symbol="PROP", name="EXAMPLE PROPERTIES", sector="Real Estate",
                     industry="REIT - Retail", trailing_pe=30.0, price_to_book=1.3))
    # KEEP: a homebuilder - US GAAP books land at cost, so it is not excluded
    # as Hong Kong's landlords are.
    rows.append(base(symbol="HOME", name="HOMEBUILDER", sector="Consumer Cyclical",
                     industry="Residential Construction", trailing_pe=8.0, price_to_book=1.1,
                     ev_to_ebitda=6.0))
    # FAIL (ROE): a cheap holdco earning 2% on book.
    rows.append(base(symbol="L", name="LOEWS", trailing_pe=11.0,
                     price_to_book=1.0, ev_to_ebitda=7.0, trailing_eps=0.8,
                     book_value_ps=40.0, div_yield=2.5))
    # FAIL: loss-maker. P/E 0 must read as missing.
    rows.append(base(symbol="LOSS", name="LOSS CO", trailing_pe=0.0, price_to_book=0.6,
                     trailing_eps=-2.0, book_value_ps=30.0))
    # A bank below book: EV/EBITDA suppressed, clears the absolute screen via
    # the P/B + ROE carve-out.
    rows.append(base(symbol="BANK", name="CHEAP BANK", sector="Financial Services",
                     industry="Banks - Regional", trailing_pe=7.0, price_to_book=0.85,
                     ev_to_ebitda=3.0, trailing_eps=4.4, book_value_ps=35.0, div_yield=4.0))
    # Berkshire B: Yahoo's book value is per A share, P/B 0.001 - refused by
    # the bound, never read as the cheapest stock in America.
    rows.append(base(symbol="BRK-B", name="BERKSHIRE HATHAWAY", sector="Financial Services",
                     industry="Insurance - Diversified", trailing_pe=13.0, price_to_book=0.001,
                     trailing_eps=36.0, book_value_ps=480000.0))
    # FAIL: an ADR Nasdaq names "Common Stock" - accounts in yen, home
    # market Tokyo. Dropped by its reporting currency.
    rows.append(base(symbol="TM", name="TOYOTA MOTOR CORPORATION COMMON STOCK",
                     sector="Consumer Cyclical", industry="Auto Manufacturers", fin_ccy="JPY",
                     trailing_pe=8.0, price_to_book=0.9, ev_to_ebitda=6.0))
    # Thin industry: one member. Falls back to Yahoo's sector.
    rows.append(base(symbol="LONE", name="LONE MACHINE", industry="Farm & Heavy Construction Machinery",
                     trailing_pe=23.0, price_to_book=4.1, ev_to_ebitda=14.5))
    # FAIL (size).
    rows.append(base(symbol="TINY", name="TINY", market_cap_local=2e8,
                     trailing_pe=5.0, price_to_book=0.5, ev_to_ebitda=3.0))
    return pd.DataFrame(rows)


def check_screens():
    print("\n=== screens ===")
    cfg = ScreenConfig()
    df = make_universe()
    df, ystats = UF.apply_yahoo_filters(df, cfg, {})
    df["market_cap_usd"] = df["market_cap_local"] * USD
    df = df[df["market_cap_usd"] >= cfg.min_market_cap_usd]
    res, _ = run_screen(df, USD, cfg)
    res = UF.add_quality_context(res)
    res = UF.add_valueup_flags(res)
    res, _ = UF.apply_roe_gate(res, cfg)
    res, _ = UF.apply_absolute_screen(res, cfg)
    idx = res.set_index("symbol")

    ok("SPAC" not in idx.index and ystats["dropped_spac"] == 1, "SPAC excluded (Shell Companies)")
    ok("PROP" not in idx.index and ystats["dropped_reit_yahoo"] == 1, "REIT by Yahoo industry excluded")
    ok("TM" not in idx.index and ystats["dropped_foreign_reporter"] == 1,
       "ADR named 'Common Stock' dropped by its reporting currency (yen)")
    df2, s2 = UF.apply_yahoo_filters(make_universe(), ScreenConfig(exclude_foreign_reporters=False), {})
    ok("TM" in set(df2["symbol"]) and s2.get("dropped_foreign_reporter") is None,
       "--include-foreign keeps it (the currency repair then applies)")
    ok("HOME" in idx.index, "homebuilder kept - US GAAP real estate is at cost")
    ok("TINY" not in idx.index, "below the size floor")
    ok(bool(idx.loc["L", "is_holdco"]) and "CNA" in idx.loc["L", "holdco_note"],
       "named holding company tagged with what it holds, not dropped")
    ok(bool(idx.loc["GVAL", "passes"]), "genuinely cheap name passes the peer screen")
    ok(not bool(idx.loc["L", "passes_any"]) and idx.loc["L", "n_metrics_passing"] >= 2,
       "cheap holdco with 2% ROE fails on ROE, not on cheapness")
    ok(pd.isna(idx.loc["LOSS", "trailing_pe"]), "P/E of 0 read as missing")
    ok(pd.isna(idx.loc["BRK-B", "price_to_book"]), "Berkshire B's per-A-share book refused (P/B 0.001)")
    ok(pd.isna(idx.loc["BANK", "ev_to_ebitda"]), "bank's EV/EBITDA suppressed (Yahoo sector)")
    ok(bool(idx.loc["BANK", "abs_passes"]) and bool(idx.loc["BANK", "abs_via_carveout"]),
       "bank clears the absolute screen via the P/B + ROE carve-out")
    ok(idx.loc["LONE", "trailing_pe_peer_basis"] == "sector",
       f"thin industry falls back to Yahoo's sector ({idx.loc['LONE', 'trailing_pe_peer_basis']!r})")
    ok(idx.loc["GVAL", "trailing_pe_peer_basis"] == "industry",
       "a populated industry benchmarks within itself")

    hits = res[res["passes_any"]][["symbol", "name", "industry", "trailing_pe",
                                   "price_to_book", "roe_pct", "screen"]]
    print("\n  passing:")
    print(hits.round(2).to_string(index=False) if not hits.empty else "  (none)")


# ---------------------------------------------------------------------------
# The currency repair
# ---------------------------------------------------------------------------
def check_currency():
    print("\n=== currency repair (USD quote, CAD / EUR / JPY accounts) ===")
    CAD, CAD_TTM, EUR, JPY = 0.725, 0.731, 1.165, 0.0068    # USD per unit: spot, year average
    shares = 1e9
    px = 50.0

    def row(sym, ccy, eps, bps, ni, qeq, ev, ebitda, **kw):
        r = dict(symbol=sym, ticker=sym, currency="USD", fin_ccy=ccy,
                 close_local=px, market_cap_local=px * shares, yf_shares=shares,
                 trailing_eps=eps, trailing_pe=px / eps if eps else np.nan,
                 book_value_ps=bps, price_to_book=px / bps, yf_ni=ni, yf_roe=0.12,
                 yf_ev=ev, yf_ebitda=ebitda, ev_to_ebitda=ev / ebitda,
                 lf_equity=qeq, lf_shares=shares, share_basis_g=1.0)
        r.update(kw)
        return r
    rows = [
        # Royal Bank shape: EPS and book in USD, a bank's EBITDA absent.
        row("RYX", "CAD", eps=4.0 * CAD_TTM, bps=30.0 * CAD, ni=4.0e9, qeq=30.0e9,
            ev=55e9, ebitda=np.nan, sector="Financial Services"),
        # Enbridge shape: USD EV over CAD EBITDA - repaired at the average rate.
        row("ENBX", "CAD", eps=4.0 * CAD_TTM, bps=30.0 * CAD, ni=4.0e9, qeq=30.0e9,
            ev=60e9, ebitda=8e9),
        # A rounded EPS (0.01) - evidence of nothing: P/E refused, P/B kept.
        row("RNDX", "CAD", eps=0.01, bps=30.0 * CAD, ni=4.0e9, qeq=30.0e9,
            ev=60e9, ebitda=8e9),
        # A yen reporter whose book Yahoo left in YEN (k = 1, decisive): P/B repaired.
        row("JPYB", "JPY", eps=4.0 * JPY * 150, bps=4000.0, ni=600e9, qeq=4000e9,
            ev=55e9, ebitda=1000e9),
        # A dollar reporter: untouched.
        row("USDX", "USD", eps=4.0, bps=30.0, ni=4.0e9, qeq=30.0e9, ev=55e9, ebitda=5e9),
        # A euro reporter, EV with nothing to corroborate it: refused.
        row("BADX", "EUR", eps=4.0 * EUR, bps=30.0 * EUR, ni=4.0e9, qeq=30.0e9,
            ev=400e9, ebitda=40e9, lf_debt=1e9, lf_cash=1e9, lf_mi=0.0),
    ]
    q_equity = {r["ticker"]: r["lf_equity"] for r in rows}
    spot = {"USD": 1.0, "CAD": CAD, "EUR": EUR, "JPY": JPY}
    ttm = {"USD": 1.0, "CAD": CAD_TTM, "EUR": EUR, "JPY": JPY}
    df, stats = UF.repair_foreign_reporters(pd.DataFrame(rows), spot, ttm, q_equity)
    i = df.set_index("symbol")
    ok(abs(i.loc["RYX", "price_to_book"] - px / (30.0 * CAD)) < 1e-9
       and i.loc["RYX", "fx_note"] == "",
       "Canadian bank: dollar EPS and book left alone, no EV/EBITDA note")
    ok(abs(i.loc["ENBX", "ev_to_ebitda"] - 60e9 / (8e9 * CAD_TTM)) < 1e-9,
       f"USD EV over CAD EBITDA repaired at the average rate ({i.loc['ENBX', 'ev_to_ebitda']:.2f})")
    ok(np.isnan(i.loc["RNDX", "trailing_pe"]) and pd.notna(i.loc["RNDX", "price_to_book"]),
       "a rounded EPS refuses the P/E only")
    ok(abs(i.loc["JPYB", "price_to_book"] - px / (4000.0 * JPY)) < 1e-9
       and "P/B repaired" in i.loc["JPYB", "fx_note"],
       "yen book value on a decisive currency: P/B repaired")
    ok(i.loc["USDX", "price_to_book"] == px / 30.0 and i.loc["USDX", "fx_note"] == ""
       and i.loc["USDX", "ev_to_ebitda"] == 55e9 / 5e9,
       "dollar reporter untouched")
    ok(np.isnan(i.loc["BADX", "ev_to_ebitda"]) and "EV/EBITDA refused" in i.loc["BADX", "fx_note"],
       "an uncorroborated EV is refused")
    ok(stats["foreign_reporters"] == 5, f"funnel counts the foreign reporters ({stats})")


# ---------------------------------------------------------------------------
# Pyramids
# ---------------------------------------------------------------------------
def check_pyramids():
    print("\n=== pyramids ===")
    cfg = ScreenConfig()

    def row(name, rev, op, eq, mi, ccy="USD", fy="2023,2024,2025",
            industry="Oil & Gas Midstream"):
        return dict(name=name, ticker=name[:4], rev_y3=rev, op_y3=op,
                    lf_equity=eq, lf_mi=mi, fin_ccy=ccy, fin_years=fy, is_holdco=False,
                    sector="Energy", industry=industry)
    df = pd.DataFrame([
        # Teekay over Teekay Tankers: one income statement, the parent's
        # equity largely the subsidiary's other holders.
        row("TEEKAY TANKERS", 951.8, 215.0, 2.04e9, 0.0),
        row("TEEKAY CORP", 949.5, 206.6, 0.72e9, 1.44e9),
        # The first US run's false pair: Brink's and Pool Corp, statements
        # within tolerance, both "Industrials" - different industries.
        row("BRINKS", 5261.2, 588.5, 0.278e9, 0.130e9, industry="Security & Protection Services"),
        row("POOL CORP", 5289.4, 580.2, 1.185e9, 0.0, industry="Industrial Distribution"),
        # Same revenue by chance, nothing else in common.
        row("COINCIDENCE A", 5639.0, 900.0, 4.0e9, 0.3e9),
        row("COINCIDENCE B", 5648.0, 450.0, 2.5e9, 0.05e9),
    ])
    out, stats = UF.flag_pyramids(df, cfg)
    names = set(out["name"])
    ok(stats["pyramid_pairs"] == 1 and "TEEKAY CORP" not in names,
       f"parent consolidating a listed subsidiary dropped ({stats})")
    ok(out.set_index("name").loc["TEEKAY TANKERS", "pyramid_child_of"] == "TEEKAY CORP",
       "subsidiary tagged with its listed parent")
    ok({"COINCIDENCE A", "COINCIDENCE B", "BRINKS", "POOL CORP"} <= names,
       "revenue coincidences, and matching statements in different industries, are not pyramids")


def check_rate_guard():
    from providers_us import RateGuard
    print("\n=== rate guard ===")
    g = RateGuard("test", limit=3, cooldown=0.2, max_cooldowns=1)
    for _ in range(3):
        g.hit()
    t0 = __import__("time").time()
    ok(g.gate() and __import__("time").time() - t0 >= 0.15 and not g.tripped,
       "a run of failures pauses every worker, then lets them resume")
    for _ in range(3):
        g.hit()
    ok(not g.gate() and g.tripped, "failures after the last cooldown stop the pool")
    g2 = RateGuard("test", limit=3, cooldown=0.2, max_cooldowns=1)
    g2.hit(); g2.hit(); g2.ok(); g2.hit(); g2.hit()   # noqa: E702
    ok(g2.resume_at == 0.0, "a success in between resets the count")


# ---------------------------------------------------------------------------
# Filed statements, splits and the history screen (the shared code)
# ---------------------------------------------------------------------------
YEARS = pd.to_datetime(["2021-12-31", "2022-12-31", "2023-12-31", "2024-12-31", "2025-12-31"])


def stmt(rows: dict) -> pd.DataFrame:
    # Yahoo shape: line items as rows, newest year first, oldest column empty.
    df = pd.DataFrame({k: [np.nan] + list(v) for k, v in rows.items()}, index=YEARS).T
    return df[df.columns[::-1]]


def company(ni=(100e6, 110e6, 120e6, 130e6), shares=(1e8,) * 4):
    inc = stmt({"Total Revenue": (1000e6, 1100e6, 1200e6, 1300e6),
                "Operating Income": (150e6, 160e6, 170e6, 180e6),
                "EBITDA": (200e6, 210e6, 220e6, 230e6),
                "Net Income Common Stockholders": ni})
    bs = stmt({"Common Stock Equity": (1000e6,) * 4, "Ordinary Shares Number": shares,
               "Total Debt": (300e6,) * 4, "Cash And Cash Equivalents": (100e6,) * 4})
    return inc, bs


def check_statements():
    from providers_us import build_statement_record, cagr
    print("\n=== filed statements ===")
    px = pd.Series(20.0, index=pd.date_range("2020-01-01", "2026-10-16", freq="B"))

    inc, bs = company()
    rec = build_statement_record(inc, bs, px, None, "USD", "USD", close_now=20.0, mcap_now=2e9)
    ok(rec["hist_years"] == "2022,2023,2024,2025", "Yahoo's empty padded year is not a filed year")
    ok(rec["hist_pbr"] == [2.0] * 4, f"P/B history in USD ({rec['hist_pbr']})")
    ok(rec["fin_years"] == "2023,2024,2025" and rec["rev_y3"] == 1300.0,
       "3-year history is the last three filed years, in millions")
    ok(np.isnan(cagr([-50, 10, 20])), "CAGR undefined on a negative base")

    # A Canadian-dollar reporter quoted in USD: USD 2bn at 1.37 CAD per USD
    # is CAD 2.74bn over CAD 1bn of equity: P/B 2.74.
    fx = pd.Series(1.37, index=px.index)
    rec = build_statement_record(inc, bs, px, fx, "USD", "CAD", close_now=20.0, mcap_now=2e9)
    ok(rec["hist_pbr"] == [2.74] * 4, f"CAD reporter converted at each year-end ({rec['hist_pbr']})")
    rec = build_statement_record(inc, bs, px, None, "USD", "CAD", close_now=20.0, mcap_now=2e9)
    ok(all(v is None for v in rec["hist_pbr"]), "CAD reporter without FX -> no benchmark")

    # Alphabet: Yahoo's cap is every class, and so is the filed count.
    rec = build_statement_record(inc, bs, px, None, "USD", "USD", close_now=20.0, mcap_now=2e9)
    ok(rec["hist_note"] == "", "multi-class: whole-company cap matches the filed count")
    # Berkshire B: the B price against a count in A-equivalents is refused.
    rec = build_statement_record(inc, bs, px, None, "USD", "USD", close_now=20.0, mcap_now=3000e9)
    ok(rec["hist_note"] == "price/share basis mismatch",
       "a price and a share count on different class bases refused by the basis check")

    inc2, bs2 = company(shares=(1e7, 1e7, 1e8, 1e8))
    rec = build_statement_record(inc2, bs2, px, None, "USD", "USD", close_now=20.0, mcap_now=2e9)
    ok(rec["hist_note"] == "share-count break" and rec["hist_pbr"] == [],
       "share split between filings -> no history, not a 90% 'premium'")

    inc4, bs4 = company(ni=(100e6, 100e6, 100e6, 200e6))
    inc4.loc["Normalized Income"] = inc4.loc["Net Income Common Stockholders"]
    inc4.loc["Normalized Income", YEARS[-1]] = 100e6
    inc4.loc["Normalized EBITDA"] = inc4.loc["EBITDA"]
    rec = build_statement_record(inc4, bs4, px, None, "USD", "USD", close_now=20.0, mcap_now=2e9)
    ok(rec["hist_earnings"] == "normalized" and rec["hist_per"][-1] == 20.0,
       "one-off gain: valuation on normalized earnings")


def check_split_basis():
    from providers_us import build_statement_record
    print("\n=== splits since the latest filing ===")
    px = pd.Series(20.0, index=pd.date_range("2020-01-01", "2026-10-16", freq="B"))
    splits = lambda ratio: pd.Series([ratio], index=[pd.Timestamp("2026-06-10")])  # noqa: E731

    # A 10-for-1 split after the latest filing (the NVIDIA / Broadcom shape):
    # prices restated at once, filed shares not yet.
    inc, bs = company(shares=(10e6,) * 4)
    rec = build_statement_record(inc, bs, px, None, "USD", "USD", close_now=20.0,
                                 mcap_now=20.0 * 100e6, splits=splits(10.0))
    ok(rec["hist_note"] == "" and abs(rec["share_basis_g"] - 10.0) < 1e-9
       and rec["hist_pbr"] == [2.0] * 4,
       f"unrestated 10:1 split: history restated by 10 ({rec['hist_pbr']})")
    inc, bs = company(shares=(20e6,) * 4)
    rec = build_statement_record(inc, bs, px, None, "USD", "USD", close_now=20.0,
                                 mcap_now=20.0 * 100e6, splits=None)
    ok(rec["hist_note"] == "price/share basis mismatch" and rec["hist_pbr"] == [],
       "unexplained share-count gap refused, not guessed")

    df = pd.DataFrame([
        dict(share_basis_g=10.0, lf_equity=1000e6, lf_shares=10e6, book_value_ps=100.0,
             trailing_eps=10.0, trailing_pe=2.0, price_to_book=0.2, div_yield=4.0),
        dict(share_basis_g=1.0, lf_equity=1000e6, lf_shares=100e6, book_value_ps=10.0,
             trailing_eps=1.0, trailing_pe=20.0, price_to_book=2.0, div_yield=4.0),
    ])
    out, _ = UF.restate_info_for_splits(df)
    ok(abs(out.loc[0, "price_to_book"] - 2.0) < 1e-9 and out.loc[1, "price_to_book"] == 2.0,
       ".info on the pre-split basis restated (P/B 0.2 -> 2.0); no split, untouched")


def check_history_screen():
    print("\n=== own-history screen ===")
    cfg = ScreenConfig()

    def row(sym, pers, pbrs, evxs, sector="Industrials", roe_ok=True):
        return dict(symbol=sym, ticker=sym, sector=sector, industry="x",
                    hist_per=pers, hist_pbr=pbrs, hist_evx=evxs,
                    market_cap_local=1.0e9, fx_now=1.0, lf_ni=100e6,
                    lf_equity=1000e6, lf_ebitda=200e6, lf_debt=300e6,
                    lf_cash=100e6, lf_mi=0.0, passes=False, abs_passes=False,
                    roe_ok=roe_ok, avg_discount=0.0, hist_note="")
    h = ([20, 21, 19, 20], [2.0, 2.1, 1.9, 2.0], [10, 11, 9, 10])
    df = pd.DataFrame([row("DRTD", *h), row("LOWR", *h, roe_ok=False),
                       row("THIN", [20, None, None, 21], [2.0, None, None, 2.1],
                           [10, None, None, 11]),
                       row("BANK", *h, sector="Financial Services"),
                       row("PROP", *h, sector="Real Estate")])
    res, _ = UF.apply_history_screen(df, cfg)
    idx = res.set_index("symbol")
    d = idx.loc["DRTD"]
    ok(bool(d["hist_passes"]) and d["screen"] == "history", "de-rated name passes on history alone")
    ok(abs(d["per_now"] - 10.0) < 1e-9, "today measured on the history's basis")
    ok(not bool(idx.loc["LOWR", "hist_passes"]), "ROE floor applies to the history screen")
    ok(idx.loc["THIN", "hist_n_valid"] == 0, "fewer than 3 usable years -> no benchmark")
    ok(pd.isna(idx.loc["BANK", "hist_evx_med"]) and pd.isna(idx.loc["BANK", "evx_now"]),
       "financials: EV/EBITDA skipped in history and today")
    ok(pd.notna(idx.loc["PROP", "evx_now"]), "real estate keeps EV/EBITDA (not a financial)")


def main() -> int:
    check_parsers()
    check_roster()
    check_screens()
    check_currency()
    check_pyramids()
    check_rate_guard()
    check_statements()
    check_split_basis()
    check_history_screen()
    print("\n" + ("ALL CHECKS PASSED" if not FAILURES else f"FAILURES: {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
