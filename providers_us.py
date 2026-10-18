"""Data providers for the United States (NYSE + Nasdaq) screener.

  roster       Nasdaq's stock screener feed (api.nasdaq.com, one request):
               every NYSE, Nasdaq and NYSE American line with its security
               name, a market cap and a country. Joined to Nasdaq Trader's
               symbol directory - nasdaqlisted.txt and otherlisted.txt, the
               files the exchanges publish for market participants - for the
               listing exchange, the ETF flag, test issues and Nasdaq's
               financial-status code; and to the SEC's company_tickers_exchange
               file for each line's CIK, the issuer key.

  vehicles     The SEC's registers of closed-end funds (file numbers 811-)
               and business development companies (814-), by CIK - every
               listed fund that is not an ETF. The UK's AIC register, the US
               edition: Yahoo calls Ares Capital and PIMCO Dynamic Income
               "Asset Management", as it does BlackRock.

  market caps  Nasdaq's figure is the pre-gate (invariant 7, as Korea, Japan
               and Hong Kong read it) and the fallback when Yahoo has a
               price and no cap - only for a single-line issuer, because for
               a multi-class company it can be one class (GOOG beside GOOGL).

  fundamentals yfinance .info, one call per pre-gate survivor, fast_info as
               the market-cap fallback - Hong Kong's snapshot(), unchanged.

  liquidity    one batched yf.download for the size survivors: median daily
               traded value over the lookback. Yahoo's US volume is the
               consolidated tape - every exchange and off-exchange venue - so
               unlike Xetra or Euronext it is the whole market's.

  statements   Yahoo annual income statement + balance sheet + seven years of
               daily closes. build_statement_record is the UK build's,
               unchanged since Poland.

THE CURRENCY TRAP, SMALL: the home currency is the dollar, so it reaches only
companies that list here and report in another - Canada's interlisted banks,
pipelines and railways (Royal Bank, TD, Enbridge: Canadian dollars), Spotify
(euros). us_filters.repair_foreign_reporters is Hong Kong's repair with the
dollar as the quote currency, tested per row.
"""
from __future__ import annotations

import concurrent.futures as cf
import io
import json
import logging
import os
import re
import threading
import time
from datetime import date

import numpy as np
import pandas as pd
import requests

import config_us as K
from config_us import ScreenConfig

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en"}

SCREENER_URL = ("https://api.nasdaq.com/api/screener/stocks?tableonly=true"
                "&limit=25&offset=0&download=true")
NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_CEF = ("https://www.sec.gov/files/investment/data/other/closed-end-fund-information/"
           "closed-end-investment-company-{y}.csv")
SEC_BDC = ("https://www.sec.gov/files/investment/data/other/business-development-company-report/"
           "business-development-company-{y}.csv")
# The SEC refuses requests without a declared contact (HTTP 403 to a browser
# UA, a bare product name, or a GitHub noreply address - measured
# 2026-10-18), and asks automated users to identify themselves. The default
# is the identity this repo's commits use; set SEC_USER_AGENT to your own.
SEC_UA = os.environ.get("SEC_USER_AGENT", "us-screener research noreply@anthropic.com")
# The SEC's registers list the funds that FILED in the year, so a fund whose
# latest filing fell in the previous year is only in that year's file. Three
# years are unioned - and that is still not every fund: BlackRock's older
# trusts (BDJ, CII, BME, DSU) are in none of them. So the registers are the
# cheap first cut, before Yahoo, and EDGAR's filing history is the exact
# second one (see edgar_fund_kinds).
SEC_REGISTER_YEARS = 3
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
# A registered fund's own reports: shareholder reports (N-CSR, N-CSRS),
# monthly holdings (NPORT-P) and the annual census (N-CEN). An asset
# MANAGER files none of these - BlackRock files N-PX (its proxy votes) and
# 10-Ks - so the form list separates PIMCO Dynamic Income from BlackRock,
# which Yahoo's "Asset Management" does not.
FUND_FORMS = {"N-CSR", "N-CSRS", "NPORT-P", "N-CEN"}
# A business development company elects under the 1940 Act and is given an
# 814- file number (Ares Capital: 814-00663) beside its 10-K filings.
BDC_FILE_PREFIX = "814-"
BDC_OWN_FORMS = {"10-K", "10-Q"}
EDGAR_CACHE_VERSION = 2
EDGAR_CACHE_DAYS = 30

# Every field snapshot() promises to return. Declared rather than inferred so a
# fully throttled fetch still produces a correctly-shaped frame.
SNAPSHOT_FIELDS = (
    "yf_name", "currency", "market_cap_local", "mcap_source", "close_local",
    "trailing_pe", "price_to_book", "ev_to_ebitda", "trailing_eps",
    "book_value_ps", "div_yield", "yf_sector", "yf_industry", "yf_country",
    "quote_type", "yf_shares",
    # The currency the ACCOUNTS are in - Royal Bank quotes in USD on the NYSE
    # and reports in Canadian dollars.
    "fin_ccy",
    # The currency repair's evidence - see us_filters.repair_foreign_reporters.
    "yf_ev", "yf_ebitda", "yf_ni", "yf_roe",
    # Share of the company held by insiders (founders, a parent) - the
    # control flag, config_us.CONTROL_MIN_INSIDERS.
    "insiders_pct",
)


def is_rate_limit(e: Exception) -> bool:
    return "RateLimit" in type(e).__name__ or "Too Many Requests" in str(e)


class RateGuard:
    """Stops a worker pool after a run of rate-limit errors.

    The UK build's defences assumed a throttled run fails at the start; the
    US universe is three times Hong Kong's, and the first measured run was
    cut off at 1,500 of 3,800 names. Yahoo's block is per IP and lasts
    minutes, so every request after it fails - and each one deepens it.
    After `limit` failures with no success in between, every worker pauses
    for `cooldown` seconds and the pool resumes - up to `max_cooldowns`
    times, then it stops asking; the cache keeps what arrived, the run
    refuses to screen a partial universe, and a re-run fills the gap.

    A failure is a rate-limit error OR, for the statement endpoints, an
    empty frame: on the first full run Yahoo answered 473 of 2,514
    statement requests with empty tables and no error (a stretch of 400
    names fetched at twice the normal speed), and every one of those names
    fetched normally ten minutes later.
    """

    def __init__(self, what: str, limit: int = 25, cooldown: float = 120.0,
                 max_cooldowns: int = 3):
        self.what, self.limit = what, limit
        self.cooldown, self.max_cooldowns = cooldown, max_cooldowns
        self.n, self.used, self.tripped = 0, 0, False
        self.resume_at = 0.0
        self._lock = threading.Lock()

    def gate(self) -> bool:
        """Called before each request: waits out a cooldown, False once tripped."""
        while True:
            with self._lock:
                if self.tripped:
                    return False
                wait = self.resume_at - time.time()
            if wait <= 0:
                return True
            time.sleep(min(wait, 5.0))

    def ok(self) -> None:
        with self._lock:
            self.n = 0

    def hit(self) -> None:
        with self._lock:
            self.n += 1
            if self.n < self.limit or self.tripped or time.time() < self.resume_at:
                return
            if self.used < self.max_cooldowns:
                self.used += 1
                self.n = 0
                self.resume_at = time.time() + self.cooldown
                log.warning("Yahoo: %d %s requests failed in a row - pausing %.0fs "
                            "(cooldown %d of %d)", self.limit, self.what, self.cooldown,
                            self.used, self.max_cooldowns)
            else:
                self.tripped = True
                log.error("Yahoo is rate-limiting this IP: %s requests still failing "
                          "after %d cooldowns. Stopping - the cache keeps what arrived; "
                          "re-run in a few minutes to fill the rest.", self.what, self.used)


def out_schema(df: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    if df is None or df.empty:
        df = pd.DataFrame({"ticker": tickers})
    df = df.reindex(columns=["ticker"] + list(SNAPSHOT_FIELDS))
    return df[df["ticker"].isin(tickers)].reset_index(drop=True)


def _money(v) -> float:
    """'$175.55', '49492610320.00', '' -> float; blank is missing, never 0."""
    s = str(v if v is not None else "").replace("$", "").replace(",", "").strip()
    try:
        f = float(s)
    except ValueError:
        return np.nan
    return f if np.isfinite(f) and f > 0 else np.nan


def parse_screener(payload: dict) -> pd.DataFrame:
    """Nasdaq's screener JSON -> symbol, name, market cap, country. Pure."""
    cols = ["symbol", "name", "nasdaq_mcap_usd", "country", "last_usd"]
    try:
        rows = payload["data"]["rows"]
    except (KeyError, TypeError):
        return pd.DataFrame(columns=cols)
    out = [{"symbol": str(r.get("symbol") or "").strip(),
            "name": " ".join(str(r.get("name") or "").split()),
            "nasdaq_mcap_usd": _money(r.get("marketCap")),
            "country": str(r.get("country") or "").strip(),
            "last_usd": _money(r.get("lastsale"))} for r in rows]
    df = pd.DataFrame(out, columns=cols)
    return df[df["symbol"].ne("")].drop_duplicates(subset=["symbol"])


def parse_symbol_directory(nasdaq_txt: str, other_txt: str) -> pd.DataFrame:
    """Nasdaq Trader's two pipe-delimited files -> one row per symbol. Pure.

    The last line of each file is "File Creation Time: ..." and is dropped.
    otherlisted.txt's symbol is the ACT symbol ("BRK.B", "JPM$C"), whose
    punctuation marks the class; both files' symbols are mapped to Yahoo's
    spelling for the join.
    """
    cols = ["ysym", "exch_symbol", "board", "is_etf", "is_test", "fin_status",
            "act_noncommon", "dir_name"]

    def read(txt):
        try:
            t = pd.read_csv(io.StringIO(txt), sep="|", dtype=str, keep_default_na=False)
        except Exception:                                 # noqa: BLE001
            return pd.DataFrame()
        first = t.columns[0]
        return t[~t[first].str.startswith("File Creation Time")]

    parts = []
    nl = read(nasdaq_txt)
    if not nl.empty and "Symbol" in nl.columns:
        parts.append(pd.DataFrame({
            "exch_symbol": nl["Symbol"].str.strip(),
            "board": K.NASDAQ_BOARD,
            "is_etf": nl.get("ETF", "").eq("Y"),
            "is_test": nl.get("Test Issue", "").eq("Y"),
            "fin_status": nl.get("Financial Status", "").str.strip(),
            "act_noncommon": False,
            "dir_name": nl.get("Security Name", "").str.strip(),
        }))
    ol = read(other_txt)
    if not ol.empty and "ACT Symbol" in ol.columns:
        sym = ol["ACT Symbol"].str.strip()
        parts.append(pd.DataFrame({
            "exch_symbol": sym,
            "board": ol.get("Exchange", "").map(lambda c: K.board_of(c, False)),
            "is_etf": ol.get("ETF", "").eq("Y"),
            "is_test": ol.get("Test Issue", "").eq("Y"),
            "fin_status": "",
            "act_noncommon": sym.map(lambda s: bool(K.ACT_NONCOMMON.search(s))),
            "dir_name": ol.get("Security Name", "").str.strip(),
        }))
    if not parts:
        return pd.DataFrame(columns=cols)
    df = pd.concat(parts, ignore_index=True)
    df["ysym"] = df["exch_symbol"].map(K.yahoo_symbol)
    return df.drop_duplicates(subset=["ysym"])[cols].reset_index(drop=True)


def parse_sec_tickers(payload: dict) -> pd.DataFrame:
    """SEC company_tickers_exchange.json -> Yahoo symbol, CIK, SEC exchange. Pure."""
    cols = ["ysym", "cik", "sec_exchange"]
    try:
        df = pd.DataFrame(payload["data"], columns=payload["fields"])
    except (KeyError, TypeError, ValueError):
        return pd.DataFrame(columns=cols)
    df["ysym"] = df["ticker"].map(K.yahoo_symbol)
    df["cik"] = pd.to_numeric(df["cik"], errors="coerce").astype("Int64")
    df["sec_exchange"] = df["exchange"].fillna("").astype(str)
    return df.drop_duplicates(subset=["ysym"])[cols].reset_index(drop=True)


def parse_sec_register(text: str) -> set[int]:
    """CIKs from one of the SEC's investment-company registers (CEF or BDC
    CSV). The header carries a byte-order mark; the CIK column is found by
    name, never by position."""
    try:
        t = pd.read_csv(io.StringIO(text.lstrip("﻿")), dtype=str)
    except Exception:                                     # noqa: BLE001
        return set()
    col = next((c for c in t.columns if c.strip().upper() in ("CIK", "CIK NUMBER")), None)
    if col is None:
        return set()
    return {int(c) for c in pd.to_numeric(t[col], errors="coerce").dropna()}


def parse_submissions(payload: dict) -> str:
    """EDGAR submissions JSON -> 'bdc', 'cef' or '' (an operating company).
    Pure. Reads the recent-filings block only, which covers years of
    filings for any fund (they file monthly)."""
    try:
        rec = payload["filings"]["recent"]
    except (KeyError, TypeError):
        return ""
    forms = [str(f) for f in rec.get("form", [])]
    files = [str(f or "") for f in rec.get("fileNumber", [])]
    # The 814- number must be on the company's OWN periodic reports.
    # Blackstone, Brookfield and Assured Guaranty carry 814- numbers too, on
    # the ownership reports they file about BDCs they hold - measured
    # 2026-10-18, all three would otherwise have been dropped as BDCs.
    if any(fo.split("/")[0] in BDC_OWN_FORMS and fn.startswith(BDC_FILE_PREFIX)
           for fo, fn in zip(forms, files)):
        return "bdc"
    return "cef" if {f.split("/")[0] for f in forms} & FUND_FORMS else ""


class USProvider:
    def __init__(self, cfg: ScreenConfig):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.roster_asof = ""
        self.register_counts: dict[str, int] = {}

    # -- calendar ----------------------------------------------------------
    def recent_business_days(self, n: int) -> list[str]:
        """Real NYSE sessions, read off SPY - US holidays (Good Friday,
        Juneteenth) match no generic business-day calendar."""
        import yfinance as yf
        try:
            h = yf.Ticker("SPY").history(period=f"{max(n * 2, 120)}d")
            if not h.empty:
                return [d.strftime("%Y-%m-%d") for d in h.index[-n:]]
        except Exception as e:                            # noqa: BLE001
            log.warning("calendar via yfinance failed: %s", e)
        days = pd.bdate_range(end=pd.Timestamp.today(), periods=n)
        return [d.strftime("%Y-%m-%d") for d in days]

    def _sec(self, url: str, timeout: int = 60) -> requests.Response:
        r = self.s.get(url, headers={"User-Agent": SEC_UA,
                                     "Accept-Encoding": "gzip, deflate"}, timeout=timeout)
        r.raise_for_status()
        return r

    # -- roster ------------------------------------------------------------
    def listing_roster(self) -> pd.DataFrame:
        """Every line Nasdaq's screener lists, with exchange flags and CIK.

        The screener is the universe and carries the market cap; the symbol
        directory and the SEC file are joined on, and a line either of them
        lacks keeps blanks (never dropped for a failed join - a missing CIK
        only means the name-based issuer key is used instead).
        """
        cols = ["ticker", "symbol", "raw_symbol", "name", "board", "country", "nasdaq_mcap_usd",
                "last_usd", "cik", "is_etf", "is_test", "fin_status", "act_noncommon"]
        try:
            r = self.s.get(SCREENER_URL, headers={"Accept": "application/json"}, timeout=90)
            r.raise_for_status()
            scr = parse_screener(r.json())
        except Exception as e:                            # noqa: BLE001
            log.error("Nasdaq screener feed failed: %s", e)
            return pd.DataFrame(columns=cols)
        if scr.empty:
            log.error("Nasdaq screener feed returned no rows - layout changed?")
            return pd.DataFrame(columns=cols)
        self.roster_asof = date.today().isoformat()
        scr["ticker"] = scr["symbol"].map(K.yahoo_symbol)

        try:
            nl = self.s.get(NASDAQ_LISTED, timeout=60).text
            ol = self.s.get(OTHER_LISTED, timeout=60).text
            sd = parse_symbol_directory(nl, ol)
        except Exception as e:                            # noqa: BLE001
            log.warning("Nasdaq Trader symbol directory failed (%s) - no ETF / "
                        "exchange flags", e)
            sd = parse_symbol_directory("", "")
        log.info("  symbol directory: %d symbols", len(sd))

        try:
            sec = parse_sec_tickers(self._sec(SEC_TICKERS).json())
        except Exception as e:                            # noqa: BLE001
            log.warning("SEC ticker file failed (%s) - issuers grouped by name only", e)
            sec = parse_sec_tickers({})
        log.info("  SEC tickers: %d", len(sec))

        df = scr.merge(sd.rename(columns={"ysym": "ticker"}), on="ticker", how="left") \
                .merge(sec.rename(columns={"ysym": "ticker"}), on="ticker", how="left")
        for c in ("is_etf", "is_test", "act_noncommon"):
            df[c] = df[c].astype("boolean").fillna(False).astype(bool)
        df["fin_status"] = df["fin_status"].fillna("")
        df["board"] = df["board"].fillna("")
        # A line the directory missed: the SEC's exchange, where it names one.
        miss = df["board"].eq("")
        df.loc[miss, "board"] = df.loc[miss, "sec_exchange"].map(
            {"NYSE": "NYSE", "Nasdaq": K.NASDAQ_BOARD}).fillna("")
        df["raw_symbol"] = df["symbol"]
        df["symbol"] = df["ticker"]
        log.info("  Nasdaq screener: %d lines, %d with a CIK, %d on the directory",
                 len(df), int(df["cik"].notna().sum()), int(df["exch_symbol"].notna().sum()))
        return df[cols].reset_index(drop=True)

    def investment_vehicles(self) -> tuple[set[int], set[int]]:
        """CIKs of closed-end funds and BDCs, from the SEC's own registers.

        Empty sets on failure, and the run says so: without them every
        closed-end fund at a discount to NAV would read as a cheap
        financial (the UK's investment-trust lesson)."""
        this = date.today().year
        out = {}
        for kind, url in (("cef", SEC_CEF), ("bdc", SEC_BDC)):
            ciks: set[int] = set()
            for y in range(this, this - SEC_REGISTER_YEARS, -1):
                try:
                    ciks |= parse_sec_register(self._sec(url.format(y=y)).text)
                except Exception as e:                    # noqa: BLE001
                    log.debug("SEC %s register %d: %s", kind, y, e)
            out[kind] = ciks
            self.register_counts[kind] = len(ciks)
            if not ciks:
                log.warning("SEC %s register unavailable - those funds cannot be "
                            "excluded and will be screened as companies", kind.upper())
        log.info("  SEC registers: %d closed-end funds, %d BDCs",
                 len(out["cef"]), len(out["bdc"]))
        return out["cef"], out["bdc"]

    def edgar_fund_kinds(self, ciks) -> dict[int, str]:
        """'cef' / 'bdc' / '' per CIK, from each company's EDGAR filing history.

        The exact test the registers are not: a fund files its own reports
        and a BDC has an 814- file number. One request per CIK, so the
        caller sends only the names a fund could hide among (Yahoo's
        financial sector, or no sector at all). The SEC allows ten requests
        a second; four workers with a pause stay well under it. Answers are
        cached for EDGAR_CACHE_DAYS - a fund does not stop being one.
        A CIK whose lookup fails is absent from the result, never ''.
        """
        path = os.path.join(self.cfg.cache_dir, f"edgar_kinds_v{EDGAR_CACHE_VERSION}.json")
        cache: dict = {}
        try:
            with open(path, encoding="utf-8") as fh:
                cache = json.load(fh)
        except Exception:                                 # noqa: BLE001
            cache = {}
        today = date.today()

        def fresh(c):
            e = cache.get(str(c))
            return e and (today - date.fromisoformat(e["d"])).days <= EDGAR_CACHE_DAYS

        want = sorted({int(c) for c in pd.to_numeric(pd.Series(list(ciks)), errors="coerce")
                       .dropna()})
        todo = [c for c in want if not fresh(c)]

        def one(c: int):
            time.sleep(0.3)
            for attempt in (0, 1):
                try:
                    r = self._sec(SEC_SUBMISSIONS.format(cik=c), timeout=30)
                    return c, parse_submissions(r.json())
                except Exception as e:                    # noqa: BLE001
                    log.debug("EDGAR %d attempt %d: %s", c, attempt, e)
                    time.sleep(1.5)
            return c, None

        if todo:
            log.info("  EDGAR filing histories: %d to fetch (%d cached)",
                     len(todo), len(want) - len(todo))
            with cf.ThreadPoolExecutor(max_workers=4) as ex:
                for c, kind in ex.map(one, todo):
                    if kind is not None:
                        cache[str(c)] = {"k": kind, "d": today.isoformat()}
            try:
                os.makedirs(self.cfg.cache_dir, exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(cache, fh)
            except Exception as e:                        # noqa: BLE001
                log.warning("  could not write EDGAR cache: %s", e)
        out = {c: cache[str(c)]["k"] for c in want if str(c) in cache}
        missing = len(want) - len(out)
        if missing:
            log.warning("  EDGAR: %d of %d filing histories unavailable - those names "
                        "are screened unchecked", missing, len(want))
        return out

    # -- fundamentals ------------------------------------------------------
    def _cache_path(self, asof: str) -> str:
        return os.path.join(self.cfg.cache_dir, f"snapshot_{asof or 'latest'}.csv")

    def snapshot(self, tickers: list[str], asof: str = "") -> pd.DataFrame:
        """One yfinance .info per ticker, fast_info when .info has no market cap.

        Cached per session date for the reason the UK build found: Yahoo
        throttles by returning dicts with fields MISSING, not by raising, so
        successive runs must fill gaps rather than re-ask for everything.
        """
        import yfinance as yf

        cached = pd.DataFrame()
        path = self._cache_path(asof)
        if os.path.exists(path):
            age_h = (time.time() - os.path.getmtime(path)) / 3600.0
            if age_h <= self.cfg.cache_ttl_hours:
                try:
                    cached = pd.read_csv(path)
                    log.info("  cache: %d rows from %s (%.1fh old)",
                             len(cached), os.path.basename(path), age_h)
                except Exception as e:                    # noqa: BLE001
                    log.warning("  cache unreadable (%s), refetching", e)
                stale = [f for f in SNAPSHOT_FIELDS if f not in cached.columns]
                if stale and not cached.empty:
                    log.info("  cache predates field(s) %s - refetching",
                             ", ".join(stale))
                    cached = pd.DataFrame()

        have = set()
        if not cached.empty and "market_cap_local" in cached.columns:
            have = set(cached.loc[cached["market_cap_local"].notna(), "ticker"])
        todo = [t for t in tickers if t not in have]
        if not todo:
            log.info("  all %d tickers served from cache", len(tickers))
            return out_schema(cached, tickers)

        # The limit is global (it rejects the crumb request), so probe once
        # single-threaded before opening the pool.
        try:
            yf.Ticker(todo[0]).info
        except Exception as e:                            # noqa: BLE001
            if "RateLimit" in type(e).__name__ or "Too Many Requests" in str(e):
                log.error("Yahoo is rate-limiting this IP (%s). Nothing was "
                          "fetched; wait a few minutes and re-run - the cache "
                          "keeps whatever has already arrived.", type(e).__name__)
                return out_schema(cached, tickers)
            log.debug("probe ticker %s failed non-fatally: %s", todo[0], e)

        guard = RateGuard("snapshot")

        def one(t: str) -> dict:
            rec = {"ticker": t}
            i = {}
            if self.cfg.request_delay:
                time.sleep(self.cfg.request_delay)
            for attempt in (0, 1):
                if not guard.gate():
                    return rec
                try:
                    i = yf.Ticker(t).info or {}
                    if i.get("marketCap") is not None or i.get("regularMarketPrice"):
                        guard.ok()
                        break
                except Exception as e:                    # noqa: BLE001
                    log.debug("%s attempt %d: %s", t, attempt, e)
                    if is_rate_limit(e):
                        guard.hit()     # the retry waits out any cooldown
                        continue
                if attempt == 0:
                    time.sleep(0.5 + self.cfg.request_delay)
            if not i:
                return rec
            price = i.get("regularMarketPrice") or i.get("currentPrice")
            mcap, src = i.get("marketCap"), "info"
            if mcap is None and price:
                try:
                    mcap = yf.Ticker(t).fast_info.get("marketCap")
                    src = "fast_info" if mcap else ""
                except Exception as e:                    # noqa: BLE001
                    log.debug("%s fast_info: %s", t, e)
                    src = ""
            rec.update({
                "yf_name": i.get("longName") or i.get("shortName"),
                "currency": i.get("currency"),
                "market_cap_local": mcap,
                "mcap_source": src if mcap else "",
                "close_local": price,
                "trailing_pe": i.get("trailingPE"),
                "price_to_book": i.get("priceToBook"),
                "ev_to_ebitda": i.get("enterpriseToEbitda"),
                "trailing_eps": i.get("trailingEps"),
                "book_value_ps": i.get("bookValue"),
                # Percent (3.96 = 3.96%), the same scale as the 2% floor.
                "div_yield": i.get("dividendYield"),
                "yf_sector": i.get("sector"),
                "yf_industry": i.get("industry"),
                "yf_country": i.get("country"),
                "quote_type": i.get("quoteType"),
                # The count Yahoo's market cap is built on. For Alphabet that
                # is every class (GOOGL's marketCap is the whole company).
                "yf_shares": i.get("impliedSharesOutstanding") or i.get("sharesOutstanding"),
                "fin_ccy": i.get("financialCurrency"),
                "yf_ev": i.get("enterpriseValue"),
                "yf_ebitda": i.get("ebitda"),
                "yf_ni": i.get("netIncomeToCommon"),
                "yf_roe": i.get("returnOnEquity"),
                "insiders_pct": i.get("heldPercentInsiders"),
            })
            return rec

        log.info("  fetching %d tickers (%d already cached)", len(todo), len(have))
        rows: list[dict] = []
        with cf.ThreadPoolExecutor(max_workers=self.cfg.max_workers) as ex:
            for n, rec in enumerate(ex.map(one, todo), 1):
                rows.append(rec)
                if n % 100 == 0:
                    log.info("  fetched %d/%d", n, len(todo))
                # Save as we go: a run killed half-way (a CI timeout, a
                # closed laptop) keeps what it fetched.
                if n % 500 == 0:
                    self._save_snapshot(cached, rows, asof)

        out = self._save_snapshot(cached, rows, asof)
        out = out[out["ticker"].isin(tickers)].reset_index(drop=True)
        got = int(out["market_cap_local"].notna().sum())
        fb = int((out["mcap_source"] == "fast_info").sum())
        log.info("  %d/%d tickers have a market cap (%d via fast_info)",
                 got, len(tickers), fb)
        return out

    def _save_snapshot(self, cached: pd.DataFrame, rows: list[dict], asof: str) -> pd.DataFrame:
        fresh = pd.DataFrame(rows)
        parts = [d for d in (cached, fresh) if not d.empty]
        out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        out = out.reindex(columns=["ticker"] + list(SNAPSHOT_FIELDS))
        # Priced rows first so a cached hit beats a fresh throttled blank.
        out = out.sort_values("market_cap_local", na_position="last")
        out = out.drop_duplicates(subset=["ticker"], keep="first")
        try:
            os.makedirs(self.cfg.cache_dir, exist_ok=True)
            out.to_csv(self._cache_path(asof), index=False, encoding="utf-8")
        except Exception as e:                            # noqa: BLE001
            log.warning("  could not write cache: %s", e)
        return out

    def quarterly_equity(self, tickers: list[str]) -> dict[str, float]:
        """Latest quarterly common equity, in the reporting currency.

        France's reason: Yahoo's bookValue is the latest quarter's equity
        over today's shares, and the annual figure drifts from it.
        """
        import yfinance as yf
        out: dict[str, float] = {}

        def one(t: str):
            if self.cfg.request_delay:
                time.sleep(self.cfg.request_delay)
            try:
                q = yf.Ticker(t).quarterly_balance_sheet
                for row in ("Common Stock Equity", "Stockholders Equity"):
                    if q is not None and row in q.index:
                        s = pd.to_numeric(q.loc[row], errors="coerce").dropna()
                        if not s.empty:
                            s.index = pd.to_datetime(s.index)
                            return t, float(s.sort_index().iloc[-1])
            except Exception as e:                        # noqa: BLE001
                log.debug("%s quarterly balance sheet: %s", t, e)
            return t, None

        with cf.ThreadPoolExecutor(max_workers=self.cfg.max_workers) as ex:
            for t, v in ex.map(one, tickers):
                if v is not None:
                    out[t] = v
        log.info("  quarterly equity for %d of %d foreign reporters", len(out), len(tickers))
        return out

    # -- liquidity ---------------------------------------------------------
    def price_panel(self, tickers: list[str], days: int, chunk: int = 100) -> pd.DataFrame:
        """Daily close and volume for every ticker at once (batched endpoint,
        not .info's throttle)."""
        import yfinance as yf
        period = f"{max(int(days * 1.6), 30)}d"
        frames = []
        for i in range(0, len(tickers), chunk):
            part = tickers[i:i + chunk]
            try:
                px = yf.download(part, period=period, interval="1d", progress=False,
                                 auto_adjust=False, actions=False, threads=True,
                                 group_by="column")
            except Exception as e:                        # noqa: BLE001
                log.warning("price chunk %d failed: %s", i // chunk, e)
                continue
            if px is not None and not px.empty:
                frames.append(px)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, axis=1)

    @staticmethod
    def average_daily_value(panel: pd.DataFrame, tickers: list[str],
                            days: int) -> pd.DataFrame:
        """Median daily traded value in USD over the last `days` sessions.

        Median, not mean: one index-rebalance day (the Russell
        reconstitution, a quarterly S&P rebalance) can be twenty times a
        name's normal volume.
        """
        if panel.empty:
            return pd.DataFrame(columns=["ticker", "adv_local", "adv_sessions"])
        try:
            close, vol = panel["Close"], panel["Volume"]
        except KeyError:
            return pd.DataFrame(columns=["ticker", "adv_local", "adv_sessions"])
        if isinstance(close, pd.Series):                  # single ticker
            close, vol = close.to_frame(tickers[0]), vol.to_frame(tickers[0])
        rows = []
        for t in tickers:
            if t not in close.columns or t not in vol.columns:
                continue
            c, v = close[t], vol[t]
            if isinstance(c, pd.DataFrame):               # duplicate column
                c, v = c.iloc[:, 0], v.iloc[:, 0]
            tv = (pd.to_numeric(c, errors="coerce")
                  * pd.to_numeric(v, errors="coerce")).dropna().tail(days)
            if tv.empty:
                continue
            rows.append({"ticker": t, "adv_local": float(tv.median()),
                         "adv_sessions": int(len(tv))})
        return pd.DataFrame(rows)

    # -- FX ----------------------------------------------------------------
    def _rate(self, base: str, quote: str, lo: float, hi: float) -> float:
        """Units of `quote` per one `base`: Yahoo, then Frankfurter (ECB)."""
        import yfinance as yf
        try:
            h = yf.Ticker(f"{base}{quote}=X").history(period="5d")
            if not h.empty:
                rate = float(h["Close"].iloc[-1])
                if lo < rate < hi:
                    return rate
        except Exception as e:                            # noqa: BLE001
            log.info("FX %s/%s via Yahoo failed (%s)", base, quote, e)
        try:
            r = self.s.get(f"https://api.frankfurter.app/latest?from={base}&to={quote}",
                           timeout=20)
            rate = float(r.json()["rates"][quote])
            if lo < rate < hi:
                log.info("FX %s/%s from Frankfurter (ECB reference)", base, quote)
                return rate
        except Exception as e:                            # noqa: BLE001
            log.warning("FX %s/%s via Frankfurter failed: %s", base, quote, e)
        return float("nan")

    def usd_per(self, currencies) -> dict[str, float]:
        """USD per one unit of each reporting currency, for the currency
        repair. A rate that cannot be found is NaN, and the repair then
        refuses the affected multiples rather than guessing."""
        out = {"USD": 1.0}
        for c in sorted({str(c) for c in currencies if isinstance(c, str) and c}):
            if c not in out:
                out[c] = self._rate(c, "USD", 1e-5, 1e4)
                log.info("  FX: 1 %s = %.4f USD", c, out[c])
        return out

    def usd_per_ttm(self, currencies, spot: dict) -> dict[str, float]:
        """USD per reporting unit, averaged over the last year of sessions.

        Yahoo converts a trailing EPS - four quarters' earnings - at about
        the year's average rate, not today's (Hong Kong's measurement, where
        the renminbi moved 7% in a year). Falls back to spot.
        """
        import yfinance as yf
        out = {"USD": 1.0}
        for c in sorted({str(c) for c in currencies if isinstance(c, str) and c}):
            if c in out:
                continue
            out[c] = spot.get(c, float("nan"))
            try:
                h = yf.Ticker(f"{c}USD=X").history(period="1y")["Close"].dropna()
                if len(h) > 150:
                    out[c] = float(h.mean())
            except Exception as e:                        # noqa: BLE001
                log.info("FX %s/USD history failed (%s) - spot used for the EPS test", c, e)
        return out


# ---------------------------------------------------------------------------
# Filed statements: three-year history and the own-history benchmark
# ---------------------------------------------------------------------------
# Ported from the UK build unchanged in logic, via Germany. The worked examples
# in the comments below (NatWest, Reckitt, Shell, Craneware) were measured in
# London; the rules they justify are Yahoo's, so they hold for US lines too.
# The US cases that exercise them are in test_us.py.
# Row names Yahoo uses in its annual statements, first match wins. Net income
# is taken to COMMON shareholders where Yahoo separates it: NatWest's differs
# by ~6% because of AT1 coupons, and a P/E is a price for the common equity.
IS_ROWS = {
    "rev": ("Total Revenue", "Operating Revenue"),
    "op": ("Operating Income", "Total Operating Income As Reported"),
    "ebitda": ("EBITDA", "Normalized EBITDA"),
    "np": ("Net Income Common Stockholders", "Net Income"),
    # Yahoo's "normalized" lines strip exactly its Total Unusual Items row. The
    # 3-year growth history stays on the REPORTED rows above, as Korea's does
    # - that is what happened. The own-history VALUATION uses these instead,
    # because a one-off is not a change in what the business is worth: Reckitt
    # sold Essential Home in 2025, reported EBITDA jumped 4,760 vs 3,966
    # normalized and net income 3,182 vs 2,535, and on reported figures it
    # passed the history screen on P/E and EV/EBITDA while its P/B - which a
    # disposal gain does not move - sat only 15% below its history. For an
    # ordinary year the two lines agree to within a few percent.
    "np_norm": ("Normalized Income",),
    "ebitda_norm": ("Normalized EBITDA",),
}
# Cash-flow rows, latest filed year only: free cash flow for P/FCF, and what
# the company paid its shareholders for the shareholder yield. Yahoo signs
# outflows negative (dividends, repurchases, capex); they are stored as
# positive amounts. "Free Cash Flow" is Yahoo's operating cash flow less
# capital expenditure; where it is absent the two parts are used.
CF_ROWS = {
    "fcf": ("Free Cash Flow",),
    "ocf": ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"),
    "capex": ("Capital Expenditure",),
    "div_paid": ("Cash Dividends Paid", "Common Stock Dividend Paid"),
    "buyback": ("Repurchase Of Capital Stock", "Common Stock Payments"),
    "issuance": ("Issuance Of Capital Stock", "Common Stock Issuance"),
}
BS_ROWS = {
    "equity": ("Common Stock Equity", "Stockholders Equity"),
    "shares": ("Ordinary Shares Number", "Share Issued"),
    "debt": ("Total Debt",),
    "cash": ("Cash And Cash Equivalents",
             "Cash Cash Equivalents And Short Term Investments"),
    "mi": ("Minority Interest",),
}

# Quote currencies Yahoo reports in minor units, and the major unit each is.
MINOR_CCY = {"GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0),
             "ZAc": ("ZAR", 100.0), "ILA": ("ILS", 100.0)}

# Consecutive filed share counts outside this band are treated as a corporate
# action (consolidation, split, rights issue) rather than buybacks. Shell's
# heavy buybacks move ~7% a year, so the band is wide enough to leave them
# alone. It cannot catch small consolidations; it catches the ones that would
# otherwise manufacture a 30%+ "discount" out of a unit change.
SHARE_BREAK_BAND = (0.67, 1.5)
# Today's price x latest filed shares must land near today's market cap,
# once any split or consolidation since the filing is allowed for. If it does
# not, the price series and the share count are on different bases and every
# historical market value built from them is wrong by the same factor.
#
# Measured 2026-10-06 on names with no split since their filing: UK 1st-99th
# percentile 0.75-1.07 (max 1.12), Germany 0.91-1.10. Below 1 is shares issued
# since the filing (IQE 0.73, Rockhopper 0.79), above it buybacks. The old
# 0.6-1.6 admitted an unrestated 3:2 split (0.67) or 4:3 consolidation (1.33)
# - a fake 25-33% move in every past valuation. 0.7-1.25 refuses both and
# keeps every healthy name measured.
NOW_BASIS_BAND = (0.7, 1.25)


def _splits_after(splits, when) -> list:
    """Split / consolidation ratios Yahoo recorded after `when`, oldest first.

    Yahoo reports a consolidation as a ratio below one (Johnson Matthey's
    4-for-3 in Aug 2026 is 0.75) and a demerger price adjustment the same way,
    so the caller decides what a ratio means by whether it explains the share
    count, never by its size alone.
    """
    if splits is None or len(splits) == 0:
        return []
    s = pd.to_numeric(splits, errors="coerce")
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize() \
        if getattr(s.index, "tz", None) is not None else pd.to_datetime(s.index).normalize()
    s = s[(s.index > when) & (s > 0) & (s != 1)].sort_index()
    return [float(v) for v in s.tolist()]
# A latest filing older than this is not the latest filing - Yahoo has missed
# one. "Today" is then today's price over earnings from two years ago, while
# yfinance's own twelve-month figure knows better: Craneware read 52.6x on
# the same basis against 28.6x trailing, and passed the history screen on that
# gap. 13 of 251 survivors (Sept 2026) had a newest filed year of 2024. Eighteen
# months leaves room for a late filer without admitting a skipped year.
MAX_FILING_AGE_DAYS = 548

# The statements cache stores DERIVED records, not raw frames (seven years of
# daily prices per name would be ~40x larger). So a change to
# build_statement_record does not reach names already cached - bump this and
# the next run refetches instead of serving numbers the new code would not
# produce.
STATEMENTS_CACHE_VERSION = 6        # 5: cash-flow fields; 6: four-quarter TTM


def cagr(values: list) -> float:
    """Compound annual growth across the span the values actually cover.

    Identical to the Korea build: undefined when the base is zero or negative.
    A company that lost money three years ago has no meaningful growth RATE,
    and inventing one is worse than reporting nothing. The yearly figures
    always ship alongside, so nothing is hidden by this.
    """
    vals = [v for v in values if v is not None and np.isfinite(v)]
    if len(vals) < 2 or vals[0] <= 0 or vals[-1] <= 0:
        return np.nan
    return (vals[-1] / vals[0]) ** (1.0 / (len(vals) - 1)) - 1.0


def major_ccy(ccy) -> tuple[str, float]:
    """(major currency, divisor) for a Yahoo quote currency."""
    c = str(ccy or "")
    return MINOR_CCY.get(c, (c, 1.0))


def _row(df: pd.DataFrame, names) -> pd.Series:
    """First matching statement row, indexed by fiscal year-end, oldest first."""
    if df is None or df.empty:
        return pd.Series(dtype=float)
    for n in names:
        if n in df.index:
            s = pd.to_numeric(df.loc[n], errors="coerce")
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            return s.sort_index()
    return pd.Series(dtype=float)


def _at(series: pd.Series, when: pd.Timestamp, tolerance_days: int = 10) -> float:
    """Last value on or before `when`, if it is within `tolerance_days`. A
    fiscal year-end that falls in a data gap is missing, not the nearest price
    from months earlier."""
    if series is None or series.empty:
        return np.nan
    s = series.loc[:when].dropna()
    if s.empty or (when - s.index[-1]).days > tolerance_days:
        return np.nan
    return float(s.iloc[-1])


def _num(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return np.nan
    return f if np.isfinite(f) else np.nan


def build_statement_record(inc: pd.DataFrame, bs: pd.DataFrame,
                           px: pd.Series, fx: pd.Series | None,
                           quote_ccy: str, fin_ccy: str,
                           close_now: float, mcap_now: float,
                           fin_years: int = 3, max_hist: int = 5,
                           asof: str | pd.Timestamp | None = None,
                           splits: pd.Series | None = None,
                           cf: pd.DataFrame | None = None,
                           qinc: pd.DataFrame | None = None) -> dict:
    """Everything the two statement-based features need, from raw frames.

    Pure: no network, so test_us.py can exercise every trap offline. `px` is
    the daily close in QUOTE units (dollars for every US line); `fx` converts the
    quote's MAJOR currency into the reporting currency (None when they are the
    same); `close_now` and `mcap_now` are in the quote's major currency.

    Every multiple is a market value over a reported total, never a price over
    a per-share figure. Yahoo's per-share rows are in reporting-currency units
    against a price in the quote currency (Royal Bank: Canadian dollars against US dollars), and
    its EPS row is frequently rounded to zero; totals over totals sidestep both.

    Three-year history: the last `fin_years` filed years, oldest first, in
    millions of the REPORTING currency. Deliberately not converted - a growth
    rate should describe the business, not the Canadian dollar against the US dollar.

    Own history: for each filed year, that year-end market value over that
    year's filed figures, converted into the reporting currency at that
    year-end rate. Plus the latest filing's components, so the screen can put
    TODAY's market value over them on exactly the same definitions - see
    us_filters.apply_history_screen for why that matters.
    """
    rec: dict = {"fin_ccy": fin_ccy or ""}

    rows = {k: _row(inc, v) for k, v in IS_ROWS.items()}
    rows.update({k: _row(bs, v) for k, v in BS_ROWS.items()})

    # A fiscal year counts as filed when it has revenue or net income. Yahoo
    # pads the frame with an extra, entirely empty oldest column (2021 in
    # every UK name checked, and in German, Polish, French, Hong Kong and US ones too), and that must not be read
    # as a zero year.
    dates = sorted(set(rows["rev"].dropna().index) | set(rows["np"].dropna().index))
    if not dates:
        rec["hist_note"] = "no filed years"
        return rec

    # ---- three-year history ---------------------------------------------
    fy = dates[-fin_years:]
    rec["fin_years"] = ",".join(d.strftime("%Y") for d in fy)
    rec["fin_n"] = len(fy)
    for key in ("rev", "op", "ebitda", "np"):
        series = [_num(rows[key].get(d)) / 1e6 for d in fy]
        for i, v in enumerate(series, 1):
            rec[f"{key}_y{i}"] = round(v, 1) if np.isfinite(v) else np.nan
        rec[f"{key}_cagr"] = cagr(series)

    # ---- own history ------------------------------------------------------
    hy = dates[-max_hist:]
    rec["hist_years"] = ",".join(d.strftime("%Y") for d in hy)
    q_major, q_div = major_ccy(quote_ccy)
    same_ccy = bool(fin_ccy) and fin_ccy == q_major
    # No reporting currency is not the same as US dollars. Assuming it would
    # treat a dollar reporter's accounts as pounds and scale every historical
    # multiple by the exchange rate - a fake discount or premium of 20-35%.
    # Missing means no benchmark (invariant 2), never a guess.
    if not fin_ccy:
        rec["hist_note"] = "no reporting currency"

    def fx_at(when):
        if same_ccy:
            return 1.0
        return _at(fx, when) if fx is not None else np.nan

    shares = [_num(rows["shares"].get(d)) for d in hy]
    lf = hy[-1]

    # Earnings basis for the VALUATION: normalized where Yahoo has it for
    # every year in the window, reported otherwise - never a mix within one
    # company's series, because a history that switches definition part-way
    # compares a year with itself on two different measures (see IS_ROWS).
    def pick(norm_key, rep_key):
        n = rows[norm_key]
        if all(np.isfinite(_num(n.get(d))) for d in hy):
            return n, "normalized"
        return rows[rep_key], "reported"
    ni_row, ni_basis = pick("np_norm", "np")
    eb_row, _ = pick("ebitda_norm", "ebitda")
    rec["hist_earnings"] = ni_basis

    # Guard 0: a stale latest filing. Checked first, because every other
    # number in the record would be struck against it.
    if asof is not None and "hist_note" not in rec:
        age = (pd.Timestamp(asof).normalize() - lf).days
        if age > MAX_FILING_AGE_DAYS:
            rec["hist_note"] = "stale filings"

    # Guard 1: a share count that jumps between filings is a corporate action.
    # The price series is split-adjusted while filed counts are not
    # necessarily, so a market value built across the break is wrong by the
    # split ratio - which the screen would read as a huge discount.
    valid = [s for s in shares if np.isfinite(s) and s > 0]
    for a, b in zip(valid, valid[1:]):
        if not (SHARE_BREAK_BAND[0] <= b / a <= SHARE_BREAK_BAND[1]):
            rec["hist_note"] = "share-count break"
            break

    # Guard 2: today's price x latest filed shares against today's market cap,
    # allowing for a split or consolidation Yahoo has not restated yet.
    #
    # Yahoo's price series is adjusted for every split it records; its filed
    # share counts catch up later. Johnson Matthey consolidated 4-for-3 in Aug
    # 2026: the prices were rescaled at once, the FY2026 filing still carried
    # the old 167.9m shares against 125.9m today. The ratio here read 1.33,
    # inside the old 0.6-1.6 band, so every past market value was a third too
    # high and the history screen showed a ~20% discount to its own history
    # that did not exist. A 3:2 split would do the same in the other
    # direction. So, as the Japan build does: among "no change" and each
    # suffix of the ratios recorded since the filing, take the one that
    # explains today's count, restate every filed year by it, and refuse a gap
    # nothing explains.
    s_lf = _num(rows["shares"].get(lf))
    g = 1.0
    if "hist_note" not in rec and np.isfinite(s_lf) and s_lf > 0:
        mc = _num(mcap_now)
        ratio = (_num(close_now) * s_lf / mc) if mc else np.nan
        rec["basis_ratio"] = ratio
        if np.isfinite(ratio) and ratio > 0:
            fs = _splits_after(splits, lf)
            cands = [1.0] + [float(np.prod(fs[k:])) for k in range(len(fs))]
            g = min(cands, key=lambda c: abs(np.log(ratio * c)))
        if not (np.isfinite(ratio) and NOW_BASIS_BAND[0] <= ratio * g <= NOW_BASIS_BAND[1]):
            rec["hist_note"] = "price/share basis mismatch"
            g = 1.0
    # The factor the latest filing's per-share basis is off by: also what the
    # screen uses to check Yahoo's .info per-share fields (see the filters).
    rec["share_basis_g"] = g
    rec["lf_shares"] = s_lf
    shares = [s * g for s in shares]

    per, pbr, evx = [], [], []
    for d, sh in zip(hy, shares):
        p = _at(px, d) / q_div if px is not None else np.nan
        mv = p * sh * fx_at(d)        # year-end market value, reporting ccy
        ni, eq = _num(ni_row.get(d)), _num(rows["equity"].get(d))
        eb = _num(eb_row.get(d))
        debt, cash = _num(rows["debt"].get(d)), _num(rows["cash"].get(d))
        mi = _num(rows["mi"].get(d))
        mi = 0.0 if not np.isfinite(mi) else mi
        # Non-positive denominators become NaN here; bounds are applied by the
        # screen, which is also where a loss year drops out of the benchmark.
        per.append(mv / ni if np.isfinite(mv) and ni > 0 else np.nan)
        pbr.append(mv / eq if np.isfinite(mv) and eq > 0 else np.nan)
        ev = mv + debt - cash + mi
        evx.append(ev / eb if np.isfinite(ev) and eb > 0 else np.nan)

    if "hist_note" in rec:
        per = pbr = evx = []
    rec["hist_per"] = [round(v, 2) if np.isfinite(v) else None for v in per]
    rec["hist_pbr"] = [round(v, 3) if np.isfinite(v) else None for v in pbr]
    rec["hist_evx"] = [round(v, 2) if np.isfinite(v) else None for v in evx]

    # Latest filing's components, for today's multiples on the same basis.
    fx_last = np.nan
    if same_ccy:
        fx_last = 1.0
    elif fx is not None and not fx.dropna().empty:
        fx_last = float(fx.dropna().iloc[-1])
    rec.update({
        # On the same earnings basis as the history, or today's value would be
        # compared across two definitions - the thing this whole design avoids.
        "lf_ni": _num(ni_row.get(lf)), "lf_equity": _num(rows["equity"].get(lf)),
        "lf_ebitda": _num(eb_row.get(lf)), "lf_debt": _num(rows["debt"].get(lf)),
        "lf_cash": _num(rows["cash"].get(lf)), "lf_mi": _num(rows["mi"].get(lf)),
        # Today's quote-major -> reporting-currency rate, carried so the screen
        # does not have to look it up again.
        "fx_now": fx_last,
    })
    rec.update(cashflow_fields(cf, lf))
    # The latest filed year's reported operating and net profit - the one-off
    # guard's fallback where four quarters are not available.
    rec["lf_op"] = _num(rows["op"].get(lf))
    rec["lf_np_rep"] = _num(rows["np"].get(lf))
    rec.update(ttm_fields(qinc, asof))
    rec.setdefault("hist_note", "")
    return rec


# Trailing operating and net profit from the last four QUARTERS, both from the
# same statement. Not .info's operatingMargins: that is the latest quarter's
# margin, and times trailing revenue it put AECOM's and Primoris's operating
# profit below zero and Intuit's at 0.61 of the year (its seasonal quarter) -
# 141 one-off flags on the first run, most of them false.
TTM_ROWS = {"op": ("Operating Income", "Total Operating Income As Reported"),
            "ni": ("Net Income Common Stockholders", "Net Income")}
# Four quarters spanning no more than this many days from first to last
# period-end (a missing quarter would stretch it), the latest no older than
# MAX_TTM_AGE_DAYS. Half-yearly reporters (much of London, Paris, Hong Kong)
# rarely have four, and fall back to the filed year.
TTM_MAX_SPAN_DAYS = 300
MAX_TTM_AGE_DAYS = 200


def ttm_fields(qinc: pd.DataFrame | None, asof=None) -> dict:
    """Sum of the last four quarters' operating and net profit. Pure."""
    out = {"ttm_op": np.nan, "ttm_ni": np.nan}
    if qinc is None or qinc.empty:
        return out
    op, ni = _row(qinc, TTM_ROWS["op"]), _row(qinc, TTM_ROWS["ni"])
    both = pd.concat([op, ni], axis=1, keys=["op", "ni"]).dropna()
    if len(both) < 4:
        return out
    last4 = both.sort_index().tail(4)
    if (last4.index[-1] - last4.index[0]).days > TTM_MAX_SPAN_DAYS:
        return out
    if asof is not None and (pd.Timestamp(asof).normalize() - last4.index[-1]).days > MAX_TTM_AGE_DAYS:
        return out
    out.update({"ttm_op": float(last4["op"].sum()), "ttm_ni": float(last4["ni"].sum())})
    return out


def cashflow_fields(cf: pd.DataFrame | None, lf: pd.Timestamp) -> dict:
    """Free cash flow and shareholder payouts for the latest filed year, in
    the REPORTING currency, as positive amounts. Pure.

    Read at the same fiscal year-end as the income statement (`lf`), so
    P/FCF and the shareholder yield are struck on the year the history's
    "today" uses. A year with no cash-flow column is missing, never zero -
    but within a filed column, a payout row Yahoo leaves out (a company that
    paid no dividend has no dividend row) is zero, because the statement
    itself says nothing was paid.
    """
    out = {"lf_fcf": np.nan, "lf_div_paid": np.nan, "lf_buyback": np.nan,
           "lf_issuance": np.nan}
    if cf is None or cf.empty:
        return out
    rows = {k: _row(cf, v) for k, v in CF_ROWS.items()}
    ocf = _num(rows["ocf"].get(lf))
    if not np.isfinite(ocf):
        return out                       # no cash-flow statement for that year
    fcf = _num(rows["fcf"].get(lf))
    if not np.isfinite(fcf):
        capex = _num(rows["capex"].get(lf))
        fcf = ocf + (capex if np.isfinite(capex) else np.nan)

    def paid(key):
        v = _num(rows[key].get(lf))
        return abs(v) if np.isfinite(v) else 0.0
    out.update({"lf_fcf": fcf, "lf_div_paid": paid("div_paid"),
                "lf_buyback": paid("buyback"), "lf_issuance": paid("issuance")})
    return out


def _json_safe(rec: dict) -> dict:
    out = {}
    for k, v in rec.items():
        if isinstance(v, (float, np.floating)):
            out[k] = float(v) if np.isfinite(float(v)) else None
        elif isinstance(v, np.integer):
            out[k] = int(v)
        else:
            out[k] = v
    return out


def _save_json(path: str, obj: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
    except Exception as e:                                # noqa: BLE001
        log.warning("  could not write statements cache: %s", e)


def fetch_statements(snap: pd.DataFrame, cfg: ScreenConfig,
                     asof: str = "") -> pd.DataFrame:
    """Filed statements for the size-gated survivors. Invariant 7: this is the
    slowest work in the run, so it only ever sees names that already cleared
    every cheap filter.

    Three Yahoo calls per ticker (income statement, balance sheet, seven years
    of daily closes) plus one FX history per foreign reporting currency, paced
    like snapshot() and cached per session date for the same reason: a
    throttled statement call returns an empty frame, not an error, and an
    empty frame reads as "no history" - which quietly removes a name from the
    third screen instead of failing loudly.
    """
    import yfinance as yf

    tickers = snap["ticker"].tolist()
    path = os.path.join(cfg.cache_dir,
                        f"statements_v{STATEMENTS_CACHE_VERSION}_{asof or 'latest'}.json")
    cached: dict = {}
    if os.path.exists(path):
        age_h = (time.time() - os.path.getmtime(path)) / 3600.0
        if age_h <= cfg.cache_ttl_hours:
            try:
                with open(path, encoding="utf-8") as fh:
                    cached = json.load(fh)
            except Exception as e:
                log.warning("  statements cache unreadable (%s), refetching", e)

    # FX: one history per reporting currency that differs from the quote's.
    pairs = set()
    for _, r in snap.iterrows():
        q_major, _ = major_ccy(r.get("currency"))
        f = r.get("fin_ccy")
        if isinstance(f, str) and f and f != q_major:
            pairs.add((q_major, f))
    fxs: dict = {}
    for q, f in sorted(pairs):
        try:
            h = yf.Ticker(f"{q}{f}=X").history(period="7y", interval="1d")
            s = h["Close"].copy()
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            fxs[(q, f)] = s
            log.info("  FX history %s->%s: %d days", q, f, len(s))
        except Exception as e:
            log.warning("  FX history %s->%s failed: %s - those reporters get "
                        "no own-history benchmark", q, f, e)

    todo = [t for t in tickers if t not in cached]
    rows = {t: r for t, r in snap.set_index("ticker").iterrows()}

    guard = RateGuard("statement")

    def one(t: str):
        if cfg.request_delay:
            time.sleep(cfg.request_delay)
        r = rows[t]
        inc = cf = qinc = None
        for attempt in (0, 1):
            if not guard.gate():
                return t, None
            try:
                tk = yf.Ticker(t)
                inc, bs = tk.income_stmt, tk.balance_sheet
                cf = tk.cashflow
                qinc = tk.quarterly_income_stmt
                h = tk.history(period="7y", interval="1d", auto_adjust=False)
            except Exception as e:
                log.debug("%s statements attempt %d: %s", t, attempt, e)
                inc = None
                if is_rate_limit(e):
                    guard.hit()
                continue
            if inc is not None and not inc.empty:
                guard.ok()
                break
            # Empty with no error: a company with no filings, or Yahoo
            # shedding load. Counted, so a run of them triggers a cooldown;
            # one retry, after it if one started.
            guard.hit()
        if inc is None or inc.empty:
            return t, None
        px = pd.Series(dtype=float)
        if h is not None and not h.empty:
            px = h["Close"].copy()
            px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
        q_major, _ = major_ccy(r.get("currency"))
        f = r.get("fin_ccy") if isinstance(r.get("fin_ccy"), str) else ""
        rec = build_statement_record(
            inc, bs, px, fxs.get((q_major, f)), r.get("currency"), f,
            _num(r.get("close_local")), _num(r.get("market_cap_local")),
            asof=asof or None,
            splits=(h["Stock Splits"] if h is not None and "Stock Splits" in h else None),
            cf=cf, qinc=qinc)
        return t, _json_safe(rec)

    if todo:
        log.info("  statements: fetching %d tickers (%d cached)", len(todo),
                 len(tickers) - len(todo))
        with cf.ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
            for n, (t, rec) in enumerate(ex.map(one, todo), 1):
                if rec is not None:
                    cached[t] = rec
                if n % 100 == 0:
                    log.info("  statements %d/%d", n, len(todo))
                # Save as we go - the US run is ~2,000 names and a killed run
                # should keep what it fetched.
                if n % 400 == 0:
                    _save_json(path, cached)
        _save_json(path, cached)

    got = [t for t in tickers if t in cached]
    log.info("  statements for %d/%d survivors", len(got), len(tickers))
    if len(got) < 0.8 * len(tickers):
        log.warning("  only %.0f%% have statements - Yahoo is probably "
                    "throttling. The two statement features will be thin; "
                    "re-run to fill the gaps from cache.",
                    100.0 * len(got) / max(len(tickers), 1))
    if not got:
        return pd.DataFrame(columns=["ticker"])
    out = pd.DataFrame([{"ticker": t, **cached[t]} for t in got])
    for k in ("hist_per", "hist_pbr", "hist_evx"):
        if k in out.columns:
            out[k] = out[k].map(lambda v: v if isinstance(v, list) else [])
    # fin_ccy also comes from the snapshot, and the snapshot's is the one kept.
    return out.drop(columns=["fin_ccy"], errors="ignore")
