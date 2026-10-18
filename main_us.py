"""United States (NYSE + Nasdaq + NYSE American) relative-valuation screener.

  python main_us.py -v                       # full run
  python main_us.py --skip-liquidity         # size alone gates
  python main_us.py --include-adrs           # keep American Depositary Shares
  python main_us.py --include-funds          # keep closed-end funds and BDCs
  python main_us.py --keep-pyramid-parents   # keep a parent next to its listed subsidiary
  python main_us.py --discount 0.15 --min-metrics 1
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime

import pandas as pd

import us_filters as UF
from config_us import ScreenConfig
from providers_us import USProvider
from screener import run_screen

D = ScreenConfig()      # argparse defaults come from here, so the two cannot drift

# Every US line quotes and caps in dollars. Kept as a named scalar so the
# code path is the one every other market runs (screener.apply_usd_conversion).
USD_PER_LOCAL = 1.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="us_screen_results.csv")
    p.add_argument("--min-mcap", type=float, default=D.min_market_cap_usd, help="USD")
    p.add_argument("--min-adv", type=float, default=D.min_adv_usd,
                   help="USD median daily traded value, consolidated tape")
    p.add_argument("--discount", type=float, default=D.discount_threshold)
    p.add_argument("--min-metrics", type=int, default=D.min_metrics_passing)
    p.add_argument("--min-peers", type=int, default=D.min_peers)
    p.add_argument("--peer-keys", default=",".join(D.peer_keys))
    p.add_argument("--fallback-peer-keys", default=",".join(D.fallback_peer_keys))
    p.add_argument("--include-funds", action="store_true",
                   help="keep closed-end funds and BDCs (the SEC's registers)")
    p.add_argument("--include-reits", action="store_true")
    p.add_argument("--include-adrs", action="store_true",
                   help="keep American Depositary Shares (priced at home)")
    p.add_argument("--include-foreign", action="store_true",
                   help="keep companies that report in another currency (ADRs named "
                        "'Common Stock', Canadian interlisted lines); the currency "
                        "repair then applies")
    p.add_argument("--include-distressed", action="store_true",
                   help="keep Nasdaq lines whose filings are delinquent or in bankruptcy")
    p.add_argument("--keep-pyramid-parents", action="store_true",
                   help="keep a parent that consolidates a listed subsidiary")
    p.add_argument("--exclude-holdcos", action="store_true")
    p.add_argument("--exclude-controlled", action="store_true",
                   help="drop companies whose insiders hold a majority")
    p.add_argument("--no-pregate", action="store_true",
                   help="send every line to Yahoo, not only those Nasdaq's market cap "
                        "says could clear the size gate (~2x slower)")
    p.add_argument("--adv-days", type=int, default=D.adv_lookback_days)
    p.add_argument("--skip-liquidity", action="store_true")
    p.add_argument("--min-roe", type=float, default=D.min_roe_pct,
                   help="ROE%% floor applied to survivors; 0 disables")
    p.add_argument("--abs-pbr", type=float, default=D.abs_max_pbr)
    p.add_argument("--abs-ev", type=float, default=D.abs_max_ev_ebitda)
    p.add_argument("--abs-strict-financials", action="store_true")
    p.add_argument("--no-abs-roe", action="store_true")
    p.add_argument("--no-abs-fair-pbr", action="store_true")
    p.add_argument("--coe", type=float, default=D.abs_cost_of_equity_pct)
    p.add_argument("--abs-min-div", type=float, default=D.abs_min_div_yield,
                   help="payout floor %%: shareholder yield (dividends + net buybacks)")
    p.add_argument("--no-oneoff-guard", action="store_true",
                   help="score trailing earnings as reported, one-offs included")
    p.add_argument("--no-financials", action="store_true",
                   help="skip the 3-year revenue/EBITDA/net-profit history")
    p.add_argument("--no-history", action="store_true",
                   help="skip the own-history screen")
    p.add_argument("--hist-discount", type=float, default=D.hist_min_discount)
    p.add_argument("--hist-min-metrics", type=int, default=D.hist_min_metrics)
    p.add_argument("--dashboard", default="us_dashboard.html",
                   help="self-contained HTML dashboard; pass '' to skip")
    p.add_argument("--all", action="store_true",
                   help="write every scored row, not just passes")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


# Yahoo's long names end in the legal form ("Agilent Technologies, Inc.",
# "Loews Corporation"). It says nothing a reader needs.
LEGAL_FORM = re.compile(r"[\s,]+(company limited|co\.?,? ?ltd\.?|limited|ltd\.?|inc\.?|plc"
                        r"|incorporated|corporation|corp\.?|n\.?v\.?|s\.?a\.?|l\.?p\.?"
                        r"|llc|co\.?)\s*$", re.IGNORECASE)
# Nasdaq's names carry the security description ("... Class A Common Stock").
SECURITY_DESC = re.compile(r"\s+(class [a-z] )?(common stock|common shares|ordinary shares"
                           r"|capital stock|common units).*$", re.IGNORECASE)


def display_name(yahoo: str, exchange: str) -> str:
    """Yahoo's readable name over the exchange's security name, legal form
    removed."""
    n = str(yahoo or "").strip() or SECURITY_DESC.sub("", str(exchange or "").strip())
    for _ in range(2):
        n = LEGAL_FORM.sub("", n).strip()
    return n


def main() -> int:
    a = parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    for noisy in ("urllib3", "peewee", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("us")

    keys = lambda s: tuple(k.strip() for k in s.split(",") if k.strip())  # noqa: E731
    cfg = ScreenConfig(
        min_market_cap_usd=a.min_mcap,
        min_adv_usd=0.0 if a.skip_liquidity else a.min_adv,
        discount_threshold=a.discount,
        min_metrics_passing=a.min_metrics,
        min_peers=a.min_peers,
        min_roe_pct=a.min_roe,
        abs_max_pbr=a.abs_pbr,
        abs_max_ev_ebitda=a.abs_ev,
        abs_require_roe=not a.no_abs_roe,
        abs_financials_pbr_only=not a.abs_strict_financials,
        abs_require_pbr_vs_roe=not a.no_abs_fair_pbr,
        abs_cost_of_equity_pct=a.coe,
        abs_min_div_yield=a.abs_min_div,
        oneoff_guard=not a.no_oneoff_guard,
        hist_min_discount=a.hist_discount,
        hist_min_metrics=a.hist_min_metrics,
        peer_keys=keys(a.peer_keys),
        fallback_peer_keys=keys(a.fallback_peer_keys),
        exclude_investment_companies=not a.include_funds,
        exclude_reits=not a.include_reits,
        exclude_adrs=not a.include_adrs,
        exclude_foreign_reporters=not a.include_foreign,
        exclude_distressed=not a.include_distressed,
        exclude_pyramid_parents=not a.keep_pyramid_parents,
        exclude_holdcos=a.exclude_holdcos,
        exclude_controlled=a.exclude_controlled,
        adv_lookback_days=a.adv_days,
        pregate_fraction=0.0 if a.no_pregate else D.pregate_fraction,
    )

    prov = USProvider(cfg)
    days = prov.recent_business_days(cfg.adv_lookback_days)
    asof = days[-1] if days else datetime.now().strftime("%Y-%m-%d")
    log.info("as of %s", asof)

    # 1. roster: Nasdaq's screener, Nasdaq Trader's directory, the SEC's CIKs
    log.info("building roster...")
    roster = prov.listing_roster()
    if roster.empty:
        log.error("empty roster - Nasdaq's screener feed did not answer or did not parse")
        return 1
    log.info("roster: %d lines (%s)", len(roster),
             ", ".join(f"{b or 'other'}={n}" for b, n in roster["board"].value_counts().items()))
    cef, bdc = prov.investment_vehicles()

    # 2. roster hygiene: every rule here is an exchange's or the SEC's own
    #    label, so it runs before a single Yahoo call.
    df, ustats = UF.apply_roster_filters(roster, cfg, cef, bdc)
    log.info("after roster hygiene: %d lines", len(df))

    # 3. the pre-gate on Nasdaq's market caps (invariant 7: the slow
    #    per-ticker work only sees lines that could clear the size gate).
    df, pstats = UF.pregate(df, cfg)
    ustats.update(pstats)
    log.info("pre-gate: %d lines go to Yahoo", len(df))

    # 4. fundamentals: one yfinance .info per line
    log.info("fetching fundamentals for %d tickers...", len(df))
    snap = prov.snapshot(df["ticker"].tolist(), asof)
    df = df.merge(snap, on="ticker", how="left")
    n_priced = int(df["market_cap_local"].notna().sum())
    log.info("  %d of %d lines priced by Yahoo", n_priced, len(df))
    # Refuse rather than screen half the market.
    if n_priced < 0.5 * len(df):
        log.error("only %d of %d lines priced. Yahoo is rate-limiting; the cache "
                  "has kept what arrived, so re-running in a few minutes will fill "
                  "the rest. Refusing to screen a partial universe.", n_priced, len(df))
        return 2
    df, mstats = UF.repair_market_caps(df)
    ustats.update({"priced": n_priced,
                   "mcap_via_fast_info": int((df["mcap_source"] == "fast_info").sum()),
                   **mstats})

    # 5. Yahoo's classification, and the hygiene that needs it
    df = UF.attach_classification(df)
    # 5a. funds the SEC's registers missed, by their own EDGAR filings -
    #     only where Yahoo's sector says one could hide.
    if cfg.exclude_investment_companies:
        cands = df[UF.fund_check_candidates(df)]
        kinds = prov.edgar_fund_kinds(cands["cik"].tolist())
        df, ustats = UF.apply_fund_filings(df, kinds, cfg, ustats)
        ustats["edgar_checked"] = len(kinds)
    df, ustats = UF.apply_yahoo_filters(df, cfg, ustats)
    drops = sum(v for k, v in ustats.items() if k.startswith("dropped_"))
    ustats["after_us_filters"] = len(roster) - drops
    log.info("after US hygiene: %d priced lines", len(df))

    # 6. size gate on Yahoo's market cap - the whole company, every class
    df["market_cap_usd"] = pd.to_numeric(df["market_cap_local"], errors="coerce") * USD_PER_LOCAL
    pre = df[df["market_cap_usd"] >= cfg.min_market_cap_usd].copy()
    ustats["cleared_market_cap"] = len(pre)
    log.info("%d of %d lines cleared USD %.0fm", len(pre), len(df),
             cfg.min_market_cap_usd / 1e6)
    if pre.empty:
        print("Nothing cleared the size gate.")
        return 0

    # 7. liquidity: one batched price download, fetched even with
    #    --skip-liquidity so the measured value reaches the page.
    log.info("fetching %d days of prices for %d names...", cfg.adv_lookback_days, len(pre))
    panel = prov.price_panel(pre["ticker"].tolist(), cfg.adv_lookback_days)
    adv = prov.average_daily_value(panel, pre["ticker"].tolist(), cfg.adv_lookback_days)
    pre = pre.merge(adv, on="ticker", how="left")
    pre["adv_usd"] = pd.to_numeric(pre["adv_local"], errors="coerce") * USD_PER_LOCAL

    # 7b. one line per issuer (GOOGL / GOOG) - before the gate, so the traded
    #     line is kept whichever of them clears it.
    pre, lstats = UF.keep_one_line_per_issuer(pre)
    ustats.update(lstats)
    if not a.skip_liquidity:
        pre = pre[pre["adv_usd"] >= cfg.min_adv_usd]
    ustats["cleared_size_liquidity"] = len(pre)
    log.info("%d cleared size/liquidity", len(pre))
    if pre.empty:
        print("Nothing cleared the size and liquidity gates.")
        return 0

    # 8. control flag (Yahoo's insider holding)
    pre, cstats = UF.apply_control(pre, cfg)
    ustats.update(cstats)

    # 9. filed statements, survivors only (invariant 7)
    if not (a.no_financials and a.no_history):
        from providers_us import fetch_statements
        log.info("fetching filed statements for %d names...", len(pre))
        st = fetch_statements(pre, cfg, asof)
        if len(st.columns) > 1:
            pre = pre.merge(st, on="ticker", how="left")

    # 10. pyramids - a parent consolidating a listed subsidiary. Needs the
    #     filed revenue, so it runs after the statements and before the peer
    #     medians, which must not count one set of earnings twice.
    if "rev_y1" in pre.columns:
        pre, pystats = UF.flag_pyramids(pre, cfg)
        ustats.update(pystats)
    if a.no_financials:
        pre = pre.drop(columns=[c for c in pre.columns
                                if c.startswith(("rev_", "op_", "ebitda_", "np_",
                                                 "fin_years", "fin_n"))])

    # 11. .info onto one basis: splits first (its test reads filed equity in
    #     the filing's currency), then the currency repair.
    if "share_basis_g" in pre.columns:
        pre, sstats = UF.restate_info_for_splits(pre)
        ustats.update(sstats)
    foreign = pre[pre["fin_ccy"].notna() & pre["fin_ccy"].ne(pre["currency"])]
    qeq = prov.quarterly_equity(foreign["ticker"].tolist()) if not foreign.empty else {}
    spot = prov.usd_per(foreign["fin_ccy"].unique())
    pre, fxstats = UF.repair_foreign_reporters(
        pre, spot, prov.usd_per_ttm(foreign["fin_ccy"].unique(), spot), qeq)
    ustats.update(fxstats)

    # 11b. cash flow (P/FCF, shareholder yield) and the one-off guard - both
    #      before the screen, because P/FCF is a peer metric and a capped
    #      P/E must be what the peer medians are built from.
    if "lf_fcf" in pre.columns:
        pre, cfstats = UF.add_cashflow_metrics(pre)
        ustats.update(cfstats)
    pre, ostats = UF.apply_oneoff_guard(pre, cfg)
    ustats.update(ostats)

    # 12. screen
    res, stats = run_screen(pre, USD_PER_LOCAL, cfg)
    if res.empty:
        print("Nothing survived screening.")
        return 0
    res = UF.add_quality_context(res)
    res = UF.add_valueup_flags(res)
    res = UF.flag_ttm_vs_filed(res)
    if cfg.min_roe_pct > 0:
        res, roestats = UF.apply_roe_gate(res, cfg)
        stats = {**stats, **roestats}
    else:
        res["roe_ok"], res["roe_tier"] = True, ""

    res, absstats = UF.apply_absolute_screen(res, cfg)
    stats = {**stats, **absstats}

    if not a.no_history and "hist_pbr" in res.columns:
        res, hstats = UF.apply_history_screen(res, cfg)
        stats = {**stats, **hstats}

    res["name_exchange"] = res["name"]
    yn = res.get("yf_name", pd.Series(index=res.index, dtype=object))
    res["name"] = [display_name(y if isinstance(y, str) else "", g)
                   for y, g in zip(yn, res["name_exchange"])]

    funnel = {**ustats, **stats}
    print("\n--- funnel ---")
    for k, v in funnel.items():
        print(f"  {k:<28} {v}")

    out = res if a.all else res[res["passes_any"]]
    cols = [c for c in UF.us_output_columns(cfg) if c in res.columns]
    out[cols].to_csv(a.out, index=False, encoding="utf-8-sig")

    hits = res[res["passes_any"]]
    print(f"\n--- {len(hits)} stock(s) passing at least one screen ---")
    if not hits.empty:
        show = hits[["symbol", "name", "industry", "market_cap_usd",
                     "trailing_pe", "price_to_book", "roe_pct", "div_yield",
                     "avg_discount", "screen"]].head(50).copy()
        show["mcap_$m"] = (show.pop("market_cap_usd") / 1e6).round(0).astype("Int64")
        show["avg_discount"] = show["avg_discount"].map(
            lambda x: f"{x:.1%}" if pd.notna(x) else "")
        show["name"] = show["name"].str.slice(0, 28)
        show["industry"] = show["industry"].str.slice(0, 24)
        print(show.to_string(index=False))

    meta = {
        "asof": asof, "source": "nasdaq+sec+yfinance", "board": "ALL",
        "roster_asof": prov.roster_asof,
        "sec_registers": prov.register_counts,
        "cmd": "python main_us.py " + " ".join(sys.argv[1:]),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "usd_per_local": USD_PER_LOCAL,
        "funnel": funnel,
        "thresholds": {
            "min_mcap_usd": cfg.min_market_cap_usd,
            "min_adv_usd": 0 if a.skip_liquidity else cfg.min_adv_usd,
            "skip_liquidity": bool(a.skip_liquidity),
            "discount": cfg.discount_threshold,
            "min_metrics": cfg.min_metrics_passing,
            "min_peers": cfg.min_peers,
            "min_valid_metrics": cfg.min_valid_metrics,
            "min_roe_pct": cfg.min_roe_pct,
            "roe_good_pct": cfg.roe_good_pct,
            "abs_max_pbr": cfg.abs_max_pbr,
            "abs_max_ev_ebitda": cfg.abs_max_ev_ebitda,
            "abs_require_roe": cfg.abs_require_roe,
            "abs_financials_pbr_only": cfg.abs_financials_pbr_only,
            "abs_require_pbr_vs_roe": cfg.abs_require_pbr_vs_roe,
            "abs_cost_of_equity_pct": cfg.abs_cost_of_equity_pct,
            "abs_min_div_yield": cfg.abs_min_div_yield,
            "payout_basis": "shareholder_yield",
            "oneoff_guard": cfg.oneoff_guard,
            "oneoff_ratio": cfg.oneoff_ratio,
            "metrics": list(cfg.metrics),
            "hist_min_discount": cfg.hist_min_discount,
            "hist_min_metrics": cfg.hist_min_metrics,
            "hist_min_years": cfg.hist_min_years,
        },
    }
    meta_path = os.path.splitext(a.out)[0] + "_meta.json"
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    print(f"\nwrote {a.out} and {meta_path}")

    if a.dashboard:
        try:
            from dashboard import build_dashboard
            build_dashboard(a.out, meta_path, a.dashboard)
            print(f"wrote {a.dashboard}   <- open this")
        except Exception as e:                            # noqa: BLE001
            log.error("dashboard build failed: %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
