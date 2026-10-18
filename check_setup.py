"""Pre-flight diagnostic: tests every live call the US screen makes.

    python check_setup.py

Run this before main_us.py. Nearly every failure in this build is a SILENT
one - a missing market cap reads as "too small", a blocked SEC register as
"no closed-end funds" (so every fund at a discount to NAV reads as a cheap
financial), a throttled Yahoo as a market where companies vanished - and a
full run is slow enough that finding out afterwards is expensive. Each check
here names what it protects against.
"""
from __future__ import annotations

import logging
import sys
import time

logging.basicConfig(level=logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str):
    def deco(fn):
        def run():
            t0 = time.time()
            try:
                detail = fn() or ""
                RESULTS.append((name, True, detail))
                print(f"  OK    {name:<44} {detail}  ({time.time() - t0:.1f}s)")
            except Exception as e:                                # noqa: BLE001
                RESULTS.append((name, False, str(e)))
                print(f"  FAIL  {name:<44} {e}")
        return run
    return deco


@check("Nasdaq screener + directory + SEC CIKs")
def roster():
    from config_us import ScreenConfig
    from providers_us import USProvider
    r = USProvider(ScreenConfig()).listing_roster()
    if len(r) < 5000:
        raise RuntimeError(f"only {len(r)} lines - Nasdaq's feed changed?")
    idx = r.set_index("ticker")
    for t in ("AAPL", "JPM", "BRK-B", "GOOGL", "GOOG"):
        if t not in idx.index:
            raise RuntimeError(f"{t} missing from the roster")
    if not idx.loc["AAPL", "nasdaq_mcap_usd"] > 1e12:
        raise RuntimeError("Apple's Nasdaq market cap missing - the pre-gate would send everything")
    if idx.loc["GOOGL", "cik"] != idx.loc["GOOG", "cik"]:
        raise RuntimeError("GOOGL and GOOG do not share a CIK - one-line-per-issuer would "
                           "screen both of Alphabet's classes")
    if idx.loc["JPM", "board"] != "NYSE" or idx.loc["AAPL", "board"] != "Nasdaq":
        raise RuntimeError("exchange flags missing - Nasdaq Trader's directory moved?")
    by = r["board"].replace("", "other").value_counts().to_dict()
    return f"{len(r)} lines {by}, {int(r['cik'].notna().sum())} with a CIK"


@check("SEC closed-end fund and BDC registers")
def registers():
    from config_us import ScreenConfig
    from providers_us import SEC_UA, USProvider
    cef, bdc = USProvider(ScreenConfig()).investment_vehicles()
    if len(cef) < 500 or len(bdc) < 100:
        raise RuntimeError(f"{len(cef)} CEFs / {len(bdc)} BDCs - the SEC refused us "
                           f"(User-Agent {SEC_UA!r}; set SEC_USER_AGENT) or moved the files")
    for cik, nm in ((1510599, "PIMCO Dynamic Income"), (1287750, "Ares Capital"),
                    (1396440, "Main Street Capital")):
        if cik not in cef | bdc:
            raise RuntimeError(f"{nm} missing from the registers")
    return f"{len(cef)} closed-end funds, {len(bdc)} BDCs"


@check("Yahoo .info on a US line (rate limit)")
def info():
    import yfinance as yf
    i = yf.Ticker("JPM").info
    if not i.get("marketCap") or not i.get("trailingPE"):
        raise RuntimeError("fields missing - Yahoo is throttling this IP; wait and retry")
    if i.get("sector") != "Financial Services":
        raise RuntimeError(f"JPM sector {i.get('sector')!r} - the financials carve-out reads it")
    return f"JPM ${i['marketCap'] / 1e9:,.0f}bn, P/E {i['trailingPE']:.1f}"


@check("multi-class market cap is the whole company")
def multiclass():
    import yfinance as yf
    a = yf.Ticker("GOOGL").info.get("marketCap")
    c = yf.Ticker("GOOG").info.get("marketCap")
    if not (a and c) or not 0.9 < a / c < 1.1:
        raise RuntimeError(f"GOOGL {a} vs GOOG {c} - Yahoo's cap is per class now; the size "
                           "gate and the history basis check both assume the whole company")
    return f"Alphabet ${a / 1e12:,.2f}tn on both lines"


@check("currency split on a Canadian interlisted line")
def fxsplit():
    import yfinance as yf
    i = yf.Ticker("ENB").info
    if i.get("currency") != "USD" or i.get("financialCurrency") != "CAD":
        raise RuntimeError(f"ENB {i.get('currency')!r} / {i.get('financialCurrency')!r}, expected "
                           "USD / CAD - the currency repair reads both")
    ev, eb = i.get("enterpriseValue"), i.get("ebitda")
    return f"Enbridge EV/EBITDA as Yahoo states it: {ev / eb:.1f}" if ev and eb else "no EV"


@check("market-cap fallback (fast_info)")
def fast():
    import yfinance as yf
    mc = yf.Ticker("MSFT").fast_info.get("marketCap")
    if not mc or mc < 1e12:
        raise RuntimeError(f"Microsoft fast_info market cap {mc} - the fallback is broken")
    return f"Microsoft ${mc / 1e12:,.2f}tn"


@check("batched price download (liquidity)")
def download():
    import yfinance as yf
    px = yf.download(["AAPL", "JPM", "BRK-B"], period="30d", interval="1d",
                     progress=False, auto_adjust=False, group_by="column")
    if px.empty or "Volume" not in px.columns.get_level_values(0):
        raise RuntimeError("no Close/Volume panel")
    n = int(px["Volume"].notna().sum().min())
    if n < 10:
        raise RuntimeError(f"only {n} sessions of volume")
    return f"{n} sessions for 3 tickers"


@check("filed statements")
def statements():
    import yfinance as yf
    tk = yf.Ticker("AAPL")
    inc, bs = tk.income_stmt, tk.balance_sheet
    need_i = ("Total Revenue", "Net Income Common Stockholders")
    need_b = ("Common Stock Equity", "Ordinary Shares Number")
    miss = [k for k in need_i if k not in inc.index] + [k for k in need_b if k not in bs.index]
    if miss:
        raise RuntimeError(f"rows missing {miss} - Yahoo renamed statement lines")
    return f"{inc.shape[1]} columns, normalized income: {'Normalized Income' in inc.index}"


@check("FX CAD/EUR -> USD")
def fx():
    from config_us import ScreenConfig
    from providers_us import USProvider
    h = USProvider(ScreenConfig()).usd_per(["CAD", "EUR"])
    if not (0.6 < h["CAD"] < 0.85 and 0.95 < h["EUR"] < 1.35):
        raise RuntimeError(f"rates {h} out of range")
    return f"1 CAD = {h['CAD']:.4f} USD, 1 EUR = {h['EUR']:.4f} USD"


def main() -> int:
    print("US screener pre-flight\n")
    for fn in (roster, registers, info, multiclass, fxsplit, fast, download, statements, fx):
        fn()
    bad = [n for n, ok, _ in RESULTS if not ok]
    print("\n" + ("all checks passed - run python main_us.py -v"
                  if not bad else f"{len(bad)} check(s) failed: {', '.join(bad)}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
