"""US-specific filters layered on top of the generic screener.

Everything from flag_pyramids down is Hong Kong's, which is France's, which
is Poland's, Germany's, the UK's and Korea's: the quality floor, the absolute
screen and the own-history screen are not country-specific. What is local,
and which earlier market each piece comes from:

  * roster hygiene - exact lookups on the exchanges' and the SEC's own
    labels: ETFs and test issues (Nasdaq Trader's flags), preferreds, baby
    bonds, warrants, rights and SPAC units (the exchange's security name -
    Korea's 우선주 lesson), ADRs (Germany's foreign secondaries, Hong Kong's
    depositary receipts), closed-end funds and BDCs (the SEC's registers by
    CIK - the UK's AIC register), Nasdaq's delinquent and bankrupt statuses
    (Korea's 관리종목)
  * Yahoo hygiene - REITs, SPACs (Yahoo's "Shell Companies"), fund quotes
  * one line per issuer - Germany's, keyed on the SEC's CIK: Alphabet's
    GOOGL and GOOG, Berkshire's A and B, Fox, News Corp, Brown-Forman
  * holding companies - Hong Kong's named list plus France's pyramid test
  * control - France's closely-held flag, from Yahoo's insider holding
  * the currency repair - Hong Kong's with the dollar as the quote, for the
    few companies that list here and report in another currency
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

import config_us as K

log = logging.getLogger(__name__)


def _fin_mask(df: pd.DataFrame) -> pd.Series:
    """Banks, insurers, brokers and asset managers, by Yahoo's sector. Yahoo
    gives real estate its own sector, so no carve-out is needed."""
    sec = df.get("sector", pd.Series("", index=df.index)).fillna("").astype(str)
    return sec.map(K.is_financial).astype(bool)


def _flag(df: pd.DataFrame, col: str) -> pd.Series:
    return df.get(col, pd.Series(False, index=df.index)).fillna(False).astype(bool)


# ---------------------------------------------------------------------------
# Universe hygiene
# ---------------------------------------------------------------------------
def tag_roster(df: pd.DataFrame, cef: set | None = None,
               bdc: set | None = None) -> pd.DataFrame:
    """Flags the exchanges' and the SEC's own data decide on. Pure."""
    df = df.copy()
    cef, bdc = cef or set(), bdc or set()
    name = df["name"].fillna("").astype(str)
    df["kind"] = name.map(K.security_kind)
    # An ACT symbol that marks a class ("JPM$C", "XYZ.U") is not common
    # stock whatever its name says.
    raw = df.get("raw_symbol", pd.Series("", index=df.index)).fillna("").astype(str)
    act = _flag(df, "act_noncommon") | raw.map(lambda x: bool(K.SCREENER_NONCOMMON.search(x)))
    df.loc[act & df["kind"].eq(""), "kind"] = "preferred"
    df["is_fund_line"] = _flag(df, "is_etf") | _flag(df, "is_test")
    df["is_adr"] = df["kind"].eq("adr")
    df["is_non_common"] = df["kind"].isin(["preferred", "debt", "warrant", "right", "unit"])
    status = df.get("fin_status", pd.Series("", index=df.index)).fillna("").astype(str)
    df["is_distressed"] = status.isin(K.DISTRESS_STATUS)
    cik = pd.to_numeric(df.get("cik"), errors="coerce")
    df["is_cef"] = cik.isin(cef)
    df["is_bdc"] = cik.isin(bdc)
    df["is_reit"] = name.map(K.is_reit)
    # The issuer: the SEC's CIK where the line has one, else the name with
    # its class and legal words removed.
    df["issuer"] = [f"cik:{int(c)}" if pd.notna(c) else f"name:{K.issuer_key(n)}"
                    for c, n in zip(cik, name)]
    return df


def apply_roster_filters(df: pd.DataFrame, cfg: K.ScreenConfig, cef: set | None = None,
                         bdc: set | None = None) -> tuple[pd.DataFrame, dict]:
    """What the roster alone can decide, before a single Yahoo call.

      ETF / test issue     Nasdaq Trader's own flags
      non-common           preferreds (incl. depositary shares of one),
                           baby bonds, warrants, rights, SPAC and tangible-
                           equity units - by the exchange's security name
      ADR                  "American Depositary Shares", "Sponsored ADR" -
                           the price is set at home
      closed-end fund      the SEC's CEF register, by CIK
      BDC                  the SEC's BDC register, by CIK
      distressed           Nasdaq's financial status: filings delinquent or
                           in bankruptcy
      REIT                 "REIT" in the name (Yahoo's industry catches the
                           rest once priced)
    """
    df = tag_roster(df, cef, bdc)
    stats = {"listings": len(df)}

    def drop(flag: str, key: str, on: bool):
        nonlocal df
        if on:
            stats[key] = int(df[flag].sum())
            df = df[~df[flag]]

    drop("is_fund_line", "dropped_etf", True)
    drop("is_non_common", "dropped_non_common", True)
    drop("is_adr", "dropped_adr", cfg.exclude_adrs)
    drop("is_cef", "dropped_closed_end_fund", cfg.exclude_investment_companies)
    drop("is_bdc", "dropped_bdc", cfg.exclude_investment_companies)
    drop("is_distressed", "dropped_distressed", cfg.exclude_distressed)
    drop("is_reit", "dropped_reit", cfg.exclude_reits)
    df = df.copy()
    # Lines per issuer that survived - the market-cap repair and the
    # dashboard's class tag read it.
    df["issuer_lines"] = df.groupby("issuer")["issuer"].transform("size")
    return df, stats


def pregate(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Nasdaq's market cap as a cheap first cut, so Yahoo's throttled .info
    only sees lines that could clear the size gate.

    Lines with no Nasdaq figure go through (nothing to judge by), and so does
    anything at or above `pregate_fraction` of the floor - a quarter, not
    the floor itself, because for a multi-class company Nasdaq's figure can
    be one class. The gate proper is Yahoo's market cap, applied later.
    """
    cap = pd.to_numeric(df.get("nasdaq_mcap_usd"), errors="coerce")
    keep = cap.isna() | (cap >= cfg.min_market_cap_usd * cfg.pregate_fraction)
    stats = {"pregate_sent_to_yahoo": int(keep.sum()),
             "pregate_no_nasdaq_cap": int((keep & cap.isna()).sum())}
    return df[keep].copy(), stats


def repair_market_caps(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Nasdaq's market cap where Yahoo has none - single-line issuers only.

    Germany's invariant 11 (a missing market cap is repaired, not read as
    small), with the exchange's figure as the source, as Poland and Hong
    Kong did. For a company with two listed classes Nasdaq's figure can be
    one class, so those stay missing rather than gated on a fraction.
    """
    df = df.copy()
    mc = pd.to_numeric(df.get("market_cap_local"), errors="coerce")
    nq = pd.to_numeric(df.get("nasdaq_mcap_usd"), errors="coerce")
    single = pd.to_numeric(df.get("issuer_lines"), errors="coerce").fillna(1) <= 1
    fill = mc.isna() & nq.notna() & single
    df.loc[fill, "market_cap_local"] = nq[fill]
    df.loc[fill, "mcap_source"] = "nasdaq"
    return df, {"mcap_via_nasdaq": int(fill.sum())}


def attach_classification(df: pd.DataFrame) -> pd.DataFrame:
    """Yahoo's sector and industry as the peer keys, under the names every
    other market uses (`industry` the peer group, `sector` its fallback), so
    screener.py and the dashboard read them unchanged."""
    df = df.copy()
    df["industry"] = df.get("yf_industry", pd.Series("", index=df.index)).fillna("").astype(str)
    df["sector"] = df.get("yf_sector", pd.Series("", index=df.index)).fillna("").astype(str)
    return df


def fund_check_candidates(df: pd.DataFrame) -> pd.Series:
    """Where a fund can hide after the registers: Yahoo's financial sector
    (every listed fund measured is "Asset Management") or no sector at all.
    Only these are looked up on EDGAR."""
    sec = df.get("sector", pd.Series("", index=df.index)).fillna("").astype(str)
    return (_fin_mask(df) | sec.eq("")) & pd.to_numeric(df.get("cik"), errors="coerce").notna()


def apply_fund_filings(df: pd.DataFrame, kinds: dict, cfg: K.ScreenConfig,
                       stats: dict) -> tuple[pd.DataFrame, dict]:
    """Closed-end funds and BDCs the SEC's registers missed, by their own
    EDGAR filings (providers_us.edgar_fund_kinds) - BlackRock's older trusts
    (Enhanced Equity Dividend, Enhanced Large Cap Core, Health Sciences)
    are in none of the registers and trade at 0.85-0.95x NAV."""
    df = df.copy()
    cik = pd.to_numeric(df.get("cik"), errors="coerce")
    kind = cik.map(lambda c: kinds.get(int(c), "") if pd.notna(c) else "")
    df["is_cef_edgar"] = kind.eq("cef")
    df["is_bdc_edgar"] = kind.eq("bdc")
    stats = dict(stats)
    if cfg.exclude_investment_companies:
        for flag, key in (("is_cef_edgar", "dropped_closed_end_fund_edgar"),
                          ("is_bdc_edgar", "dropped_bdc_edgar")):
            stats[key] = int(df[flag].sum())
            df = df[~df[flag]]
    return df.copy(), stats


def apply_yahoo_filters(df: pd.DataFrame, cfg: K.ScreenConfig,
                        stats: dict) -> tuple[pd.DataFrame, dict]:
    """What needs Yahoo's classification, on the priced lines.

    REITs Yahoo names (most say "Trust" or "Properties", not "REIT"), SPACs
    (Yahoo's "Shell Companies": a trust of Treasury bills at ten dollars a
    share), fund quotes the directory did not flag, and companies whose
    accounts are not in dollars - ADRs the exchange names "Common Stock"
    and Canadian interlisted lines, whose home market is elsewhere (see
    config_us.ScreenConfig.exclude_foreign_reporters). Holding companies
    are tagged, never dropped by default.
    """
    df = df.copy()
    name = df["name"].fillna("").astype(str)
    ind = df.get("industry", pd.Series("", index=df.index)).fillna("").astype(str)
    qtype = df.get("quote_type", pd.Series("", index=df.index)).fillna("").astype(str)
    df["is_reit_yahoo"] = pd.Series([K.is_reit(n, i) for n, i in zip(name, ind)],
                                    index=df.index)
    df["is_spac"] = ind.map(K.is_shell)
    df["is_fund_quote"] = qtype.str.upper().isin(["ETF", "MUTUALFUND", "FUND"])
    df["is_holdco"] = df["symbol"].map(K.is_holdco)
    df["holdco_note"] = df["symbol"].map(lambda s: K.KNOWN_HOLDCOS.get(K.yahoo_symbol(s), ""))
    fin_ccy = df.get("fin_ccy", pd.Series("", index=df.index)).fillna("").astype(str)
    df["is_foreign_reporter"] = fin_ccy.ne("") & fin_ccy.ne(K.HOME_CURRENCY)

    stats = dict(stats)
    for flag, key, on in (("is_reit_yahoo", "dropped_reit_yahoo", cfg.exclude_reits),
                          ("is_spac", "dropped_spac", True),
                          ("is_fund_quote", "dropped_fund_quote", True),
                          ("is_foreign_reporter", "dropped_foreign_reporter",
                           cfg.exclude_foreign_reporters),
                          ("is_holdco", "dropped_holdco", cfg.exclude_holdcos)):
        if on:
            stats[key] = int(df[flag].sum())
            df = df[~df[flag]]
    if not cfg.exclude_holdcos:
        stats["flagged_holdco"] = int(df["is_holdco"].sum())
    stats["industry_missing"] = int(df["industry"].eq("").sum())
    return df.copy(), stats


def keep_one_line_per_issuer(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Two share classes of one company: keep the traded line.

    Germany's invariant 1, keyed on the SEC's CIK, which is exact where a
    name is not: GOOGL / GOOG, BRK-A / BRK-B, FOXA / FOX, NWSA / NWS, BF-A /
    BF-B, HEI / HEI-A, LEN / LEN-B. Both lines price one set of earnings, so
    the cheaper would be scored against a cohort holding its sibling. The
    most traded line survives - for Berkshire that is B, whose per-share
    fields Yahoo states per A share and the P/B bound then refuses.
    """
    df = df.copy()
    if "issuer" not in df.columns:
        df["issuer"] = "name:" + df["name"].map(K.issuer_key)
    df["n_lines"] = df.groupby("issuer")["issuer"].transform("size")
    adv = pd.to_numeric(df.get("adv_local"), errors="coerce").fillna(-1.0)
    df["_a"] = adv
    df = df.sort_values(["issuer", "_a"], ascending=[True, False])
    dup = df.duplicated(subset=["issuer"], keep="first")
    dropped = df.loc[dup, "ticker"].tolist()
    if dropped:
        log.info("one line per issuer: dropped %d (%s)", len(dropped),
                 ", ".join(dropped[:30]) + (" ..." if len(dropped) > 30 else ""))
    df = df[~dup].drop(columns=["_a"])
    return df, {"dropped_second_line": len(dropped)}


# ---------------------------------------------------------------------------
# Control and pyramids
# ---------------------------------------------------------------------------
def apply_control(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Closely held: insiders and strategic holders own a majority.

    France's flag. Yahoo's heldPercentInsiders lumps a founder, a family
    and a parent together, so this says the free float is the minority, not
    who controls.
    """
    df = df.copy()
    ins = pd.to_numeric(df.get("insiders_pct"), errors="coerce")
    df["ctrl_pct"] = ins * 100.0
    df["is_controlled"] = ins >= K.CONTROL_MIN_INSIDERS
    stats = {"insiders_known": int(ins.notna().sum()),
             "flagged_majority_insiders": int(df["is_controlled"].sum())}
    if cfg.exclude_controlled:
        stats["dropped_controlled"] = int(df["is_controlled"].sum())
        df = df[~df["is_controlled"]]
    return df, stats


def _latest(row: pd.Series, key: str) -> float:
    for i in (3, 2, 1):
        v = pd.to_numeric(row.get(f"{key}_y{i}"), errors="coerce")
        if pd.notna(v):
            return float(v)
    return np.nan


def flag_pyramids(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """A listed parent that consolidates a listed subsidiary.

    France's test (Christian Dior over LVMH), with Hong Kong's addition.
    Both in one screen counts one set of earnings twice, and the parent -
    which carries a standing holding discount - is scored against a cohort
    that holds its own subsidiary. A pair needs all of
      * revenue within PYRAMID_REVENUE_TOL and operating profit within
        PYRAMID_OP_TOL, same currency and fiscal year - one income statement
      * a parent whose equity is at least PYRAMID_MIN_MI_SHARE minority
        interests (the subsidiary's other shareholders)
      * a child carrying far fewer (PYRAMID_MI_GAP below the parent)
      * the same Yahoo INDUSTRY - one income statement is one business
    Revenue alone matched ten unrelated pairs in France; without a sector
    test Hong Kong paired a coal miner with a hog farmer. The US needs the
    industry: with 2,500 names there are three million pairs, and the first
    run paired Brink's (cash logistics) with Pool Corp (pool supplies) -
    revenue 5,261m against 5,289m, operating profit 589m against 580m,
    Brink's buyback-thinned equity a third minority interests, both in
    Yahoo's Industrials sector. Teekay over Teekay Tankers, the real pair,
    shares an industry. The parent is dropped by default and the subsidiary
    tagged.
    """
    df = df.copy()
    df["pyramid_parent_of"] = ""
    df["pyramid_child_of"] = ""
    rev = df.apply(lambda r: _latest(r, "rev"), axis=1)
    op = df.apply(lambda r: _latest(r, "op"), axis=1)
    fy = df.get("fin_years", pd.Series("", index=df.index)).fillna("").astype(str) \
           .str.split(",").str[-1]
    ccy = df.get("fin_ccy", pd.Series("", index=df.index)).fillna("").astype(str)
    eq = pd.to_numeric(df.get("lf_equity"), errors="coerce")
    mi = pd.to_numeric(df.get("lf_mi"), errors="coerce").fillna(0.0)
    mi_share = mi / (eq + mi)
    sec = df.get("industry", pd.Series("", index=df.index)).fillna("").astype(str)
    idx = [i for i in df.index if pd.notna(rev[i]) and rev[i] > 0]
    pairs = []
    for a_pos, a in enumerate(idx):
        for b in idx[a_pos + 1:]:
            if (fy[a] != fy[b] or ccy[a] != ccy[b] or not fy[a] or not sec[a]
                    or sec[a] != sec[b]):
                continue
            if abs(rev[a] - rev[b]) / max(rev[a], rev[b]) > K.PYRAMID_REVENUE_TOL:
                continue
            if not (pd.notna(op[a]) and pd.notna(op[b]) and op[a] * op[b] > 0
                    and abs(op[a] - op[b]) / max(abs(op[a]), abs(op[b])) <= K.PYRAMID_OP_TOL):
                continue
            parent, child = (a, b) if mi_share[a] >= mi_share[b] else (b, a)
            if (mi_share[parent] >= K.PYRAMID_MIN_MI_SHARE
                    and mi_share[parent] - mi_share[child] >= K.PYRAMID_MI_GAP):
                pairs.append((parent, child))
    for p, c in pairs:
        df.at[p, "pyramid_parent_of"] = str(df.at[c, "name"])
        df.at[c, "pyramid_child_of"] = str(df.at[p, "name"])
        df.at[p, "is_holdco"] = True
        log.info("pyramid: %s consolidates %s (revenue %.0f vs %.0f)",
                 df.at[p, "name"], df.at[c, "name"], rev[p], rev[c])
    parents = df["pyramid_parent_of"].ne("")
    stats = {"pyramid_pairs": len(pairs)}
    if cfg.exclude_pyramid_parents:
        stats["dropped_pyramid_parent"] = int(parents.sum())
        df = df[~parents]
    return df, stats


# ---------------------------------------------------------------------------
# The currency repair
# ---------------------------------------------------------------------------
# Hong Kong's repair with the US dollar as the quote currency. A reporting
# currency whose rate to the dollar is within this factor of 1 cannot be
# told apart from it by magnitude at all. Refused.
FX_MIN_SEPARATION = 1.08
# A ratio within this factor of a hypothesis supports it - Hong Kong's
# measured band (EPS x shares / net income: IQR 0.96-1.01 against the
# year's average rate; the tails were rounded EPS and loss years).
FX_BAND = 1.25
# The reporting-currency reading is only ever accepted where the two
# hypotheses are this far apart. See CLAUDE.md for the US measurement.
FX_DECISIVE = 1.5


def _basis(k: pd.Series, r: pd.Series, decisive: pd.Series) -> pd.Series:
    """'quote' where k is within FX_BAND of r, 'reporting' where it is within
    FX_BAND of 1 and the currencies are decisively apart, '' otherwise
    (untestable, or evidence of neither)."""
    lk = np.log(k.where(k > 0))
    lr = np.log(r.where(r > 0))
    band = np.log(FX_BAND)
    quote = (lk - lr).abs() < band
    rep = decisive & (lk.abs() < band) & ~quote
    return pd.Series(np.where(quote, "quote", np.where(rep, "reporting", "")), index=k.index)


def repair_foreign_reporters(df: pd.DataFrame, usd_per: dict, usd_per_ttm: dict | None = None,
                             q_equity: dict | None = None) -> tuple[pd.DataFrame, dict]:
    """Yahoo's multiples on dollar lines of companies that report in another
    currency - Royal Bank and TD (Canadian dollars), Spotify (euros).

    Hong Kong's repair, where the shape was measured: Yahoo's market cap,
    EV, EPS and book value per share are in the QUOTE currency, its EBITDA
    and net income in the reporting currency, so P/E and P/B are usually
    right as stated and EV/EBITDA is not. Each field is still tested per
    row, never assumed, and against the rate Yahoo actually used:

      EPS  k = EPS x shares / net income         vs the TRAILING-YEAR AVERAGE
           rate - the trailing EPS sums four quarters at four rates
      BPS  k = BPS x shares / latest quarterly equity   vs SPOT
           (no quarter: the filed annual, then Yahoo's own ROE)
      EV   EV / market cap in [0.25, 4], France's band, OR within FX_BAND
           of today's market cap plus filed net debt and minorities in
           dollars: a dollar EV, so EV/EBITDA = EV / (EBITDA x the average
           rate)

    With r the rate (dollars per reporting unit), k near r says dollars
    (kept), k near 1 the reporting currency (repaired - only where
    FX_DECISIVE allows), anything else refuses the multiple it vouches for.
    """
    df = df.copy()
    q_equity = q_equity or {}
    usd_per_ttm = usd_per_ttm or usd_per

    def n(c: str) -> pd.Series:
        if c not in df.columns:
            return pd.Series(np.nan, index=df.index)
        return pd.to_numeric(df[c], errors="coerce")
    fin = df.get("fin_ccy", pd.Series("", index=df.index)).fillna("").astype(str)
    quote = df.get("currency", pd.Series(K.HOME_CURRENCY, index=df.index)) \
              .fillna(K.HOME_CURRENCY).astype(str)
    foreign = fin.ne("") & fin.ne(quote) & quote.eq(K.HOME_CURRENCY)
    r = fin.map(lambda c: usd_per.get(c, np.nan)).astype(float)
    rt = fin.map(lambda c: usd_per_ttm.get(c, np.nan)).astype(float).fillna(r)
    df["fx_rep_to_usd"] = r.where(foreign, 1.0)
    df["fx_note"] = ""
    stats = {"foreign_reporters": int(foreign.sum())}
    if not foreign.any():
        return df, stats

    untestable = foreign & ~(np.abs(np.log(r)) > np.log(FX_MIN_SEPARATION))
    live = foreign & ~untestable
    decisive = np.abs(np.log(r)) >= np.log(FX_DECISIVE)

    eps, bps, sh = n("trailing_eps"), n("book_value_ps"), n("yf_shares")
    ni, roe = n("yf_ni"), n("yf_roe")
    ev, mcap, ebitda = n("yf_ev"), n("market_cap_local"), n("yf_ebitda")

    eps_basis = _basis((eps * sh / ni).where(ni.abs() > 0), rt, decisive)
    eps_rep = live & eps_basis.eq("reporting")
    # Only a positive figure can be vouched for or refused; a loss is
    # already a missing multiple (invariant 2).
    eps_bad = live & eps_basis.eq("") & (eps > 0)
    eps_q = eps.where(~eps_rep, eps * rt)

    qeq = df["ticker"].map(q_equity).astype(float)
    by_quarter = _basis((bps * sh / qeq).where((qeq > 0) & (sh > 0)), r, decisive)
    eq = n("lf_equity")
    lsh = n("lf_shares") * n("share_basis_g").fillna(1.0)
    by_filing = _basis((bps * lsh / eq).where((eq > 0) & (lsh > 0)), r, decisive)
    # Yahoo's ROE is unit-free; EPS over BPS matches it when both are in the
    # quote currency and runs off by r when BPS is in the reporting currency.
    # So r over that ratio lands near r (quote) or near 1 (reporting).
    k_roe = ((eps_q / bps) / roe).where((roe > 0) & (bps > 0) & (eps_q > 0))
    by_roe = _basis(r / k_roe, r, decisive)
    has_q = (qeq > 0) & (sh > 0) & bps.notna()
    has_filing = (eq > 0) & (lsh > 0) & bps.notna()
    bps_basis = by_quarter.where(has_q, by_filing.where(has_filing, by_roe))
    bps_rep = live & bps_basis.eq("reporting")
    bps_bad = live & bps_basis.eq("") & (bps > 0)

    own_ev = mcap + (n("lf_debt") - n("lf_cash") + n("lf_mi").fillna(0.0)) * r
    ev_q = live & ((ev / mcap).between(0.25, 4.0)
                     | (np.log((ev / own_ev).where((ev > 0) & (own_ev > 0))).abs()
                        < np.log(FX_BAND)))
    # Financials have no EV/EBITDA to repair (invariant 6), and a loss-making
    # EBITDA none to vouch for; neither earns an "EV/EBITDA refused" note.
    ev_moot = _fin_mask(df) | ~(ebitda > 0)

    df["trailing_eps"] = eps_q.where(~eps_bad)
    df["trailing_pe"] = n("trailing_pe").where(~eps_rep, n("trailing_pe") / rt).where(~eps_bad)
    df["book_value_ps"] = bps.where(~bps_rep, bps * r).where(~bps_bad)
    df["price_to_book"] = n("price_to_book").where(~bps_rep, n("price_to_book") / r) \
                                            .where(~bps_bad)
    evx = (ev / (ebitda * rt)).where(ebitda > 0)
    df["ev_to_ebitda"] = n("ev_to_ebitda").where(~foreign, evx.where(ev_q))
    for c in ("trailing_pe", "price_to_book", "ev_to_ebitda", "trailing_eps", "book_value_ps"):
        df.loc[untestable, c] = np.nan

    notes = []
    for i in df.index[foreign]:
        if untestable[i]:
            notes.append((i, "fx basis untestable"))
            continue
        bits = []
        if eps_rep[i]:
            bits.append("P/E repaired")
        elif eps_bad[i]:
            bits.append("P/E refused")
        if bps_rep[i]:
            bits.append("P/B repaired")
        elif bps_bad[i]:
            bits.append("P/B refused")
        if not ev_moot[i]:
            bits.append("EV/EBITDA repaired" if ev_q[i] and pd.notna(evx[i])
                        else "EV/EBITDA refused")
        if bits:
            notes.append((i, ", ".join(bits)))
    for i, t in notes:
        df.at[i, "fx_note"] = t

    stats.update({
        "fx_untestable": int(untestable.sum()),
        "fx_eps_on_reporting_basis": int(eps_rep.sum()),
        "fx_pe_refused": int(eps_bad.sum()),
        "fx_pb_repaired": int(bps_rep.sum()),
        "fx_pb_refused": int(bps_bad.sum()),
        "fx_ev_repaired": int((ev_q & evx.notna()).sum()),
    })
    log.info("currency repair: %d foreign reporters (%s)", int(foreign.sum()),
             ", ".join(f"{k} {v}" for k, v in stats.items() if k != "foreign_reporters"))
    return df, stats


def add_valueup_flags(df: pd.DataFrame) -> pd.DataFrame:
    """Low-P/B flags.

    Korea had a live policy catalyst attached to almost exactly this screen -
    KRX publicly identifies firms whose PBR sits in the bottom 20% of their
    industry - and Japan has the TSE's March-2023 request to companies below
    book. The US has no counterpart: no listing rule or disclosure regime is
    keyed to valuation. What closes a US discount is usually an activist or
    a bid. So these two columns are kept for continuity and for sorting, not
    as a catalyst.
    """
    df = df.copy()
    rank = df.get("price_to_book_pct_rank")
    df["pbr_bottom20_industry"] = (rank <= 0.20) if rank is not None else False
    df["pbr_below_1"] = df["price_to_book"] < 1.0
    return df


def add_quality_context(df: pd.DataFrame) -> pd.DataFrame:
    """ROE derived from EPS and BPS rather than taken as a vendor field.

    Same reasoning as Korea: the ratio is then consistent with the very P/E
    and P/B being screened on. It is unit-free only once EPS and BPS are in
    the same currency - Yahoo hands out reporting-currency figures beside
    dollar ones for foreign reporters, so this must run after
    repair_foreign_reporters.
    """
    df = df.copy()
    eps = pd.to_numeric(df.get("trailing_eps"), errors="coerce")
    bps = pd.to_numeric(df.get("book_value_ps"), errors="coerce")
    df["roe_pct"] = np.where((bps > 0) & eps.notna(), eps / bps * 100.0, np.nan)
    df["div_yield"] = pd.to_numeric(df.get("div_yield"), errors="coerce")
    df["pays_dividend"] = df["div_yield"].fillna(0) > 0
    return df


def flag_ttm_vs_filed(df: pd.DataFrame) -> pd.DataFrame:
    """Profitable over the trailing twelve months, loss-making in the last
    filed year. A flag, never a gate.

    The peer and absolute screens use Yahoo's trailing multiples in every
    market, and Yahoo's trailing twelve months is the sum of the last four
    quarters - one-offs included. Germany's K+S is the case that found this:
    two quarters of very large non-operating gains turned a filed loss of
    EUR 1.1bn into TTM net income of +EUR 1.06bn and a P/E of 2.6.

    It is not a sign error - the quarters genuinely sum that way - so the
    trailing figure is left alone and the row is marked instead. The test is
    deliberately narrow: TTM profit against a normalized filed LOSS. A ratio
    test (TTM P/E far below filed P/E) would also catch genuine turnarounds,
    which is noise, not a warning.
    """
    df = df.copy()
    pe = pd.to_numeric(df.get("trailing_pe"), errors="coerce")
    lf = pd.to_numeric(df.get("lf_ni"), errors="coerce")
    df["ttm_vs_filed_loss"] = pe.notna() & (lf < 0)
    return df


def apply_roe_gate(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Require survivors to actually earn something.

    Runs after scoring, never before: it narrows `passes` and leaves
    avg_discount alone, so the peer cohorts still contain the low-ROE names
    that make them representative. Missing ROE fails.
    """
    df = df.copy()
    roe = pd.to_numeric(df.get("roe_pct"), errors="coerce")
    df["roe_ok"] = roe.notna() & (roe >= cfg.min_roe_pct)
    df["roe_tier"] = np.select(
        [roe >= 15.0, roe >= cfg.roe_good_pct, roe >= cfg.min_roe_pct],
        ["strong", "good", "marginal"], default="fail")

    before = int(df["passes"].sum())
    df["passes"] = df["passes"] & df["roe_ok"]
    after = int(df["passes"].sum())
    stats = {f"dropped_roe_below_{cfg.min_roe_pct:g}": before - after,
             "passing_after_roe": after}
    df = df.sort_values(["passes", "avg_discount"], ascending=[False, False])
    return df, stats


def apply_absolute_screen(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Absolute cheapness, independent of the peer comparison.

    Identical in structure to the Korea version, including the financials
    carve-out: EV/EBITDA is suppressed for banks and insurers, so a strict
    both-metrics rule would exclude every US bank and insurer however far
    below book it traded.
    """
    df = df.copy()
    pbr = pd.to_numeric(df.get("price_to_book"), errors="coerce")
    ev = pd.to_numeric(df.get("ev_to_ebitda"), errors="coerce")
    roe = pd.to_numeric(df.get("roe_pct"), errors="coerce")
    dy = pd.to_numeric(df.get("div_yield"), errors="coerce")
    fin = _fin_mask(df)

    df["abs_pbr_ok"] = pbr.notna() & (pbr < cfg.abs_max_pbr)
    df["abs_ev_ok"] = ev.notna() & (ev < cfg.abs_max_ev_ebitda)
    df["abs_ev_missing"] = ev.isna()
    df["abs_roe_ok"] = roe.notna() & (roe >= cfg.min_roe_pct)

    fair_pbr = roe / cfg.abs_cost_of_equity_pct
    df["abs_fair_pbr"] = fair_pbr
    df["abs_pbr_vs_roe_ok"] = pbr.notna() & fair_pbr.notna() & (pbr < fair_pbr)
    df["abs_div_ok"] = dy.notna() & (dy >= cfg.abs_min_div_yield)

    core = df["abs_pbr_ok"] & df["abs_ev_ok"]
    if cfg.abs_financials_pbr_only:
        core = core | (fin & df["abs_pbr_ok"])
        df["abs_via_carveout"] = fin & df["abs_pbr_ok"] & ~df["abs_ev_ok"]
    else:
        df["abs_via_carveout"] = False

    if cfg.abs_require_roe:
        core = core & df["abs_roe_ok"]
    if cfg.abs_require_pbr_vs_roe:
        core = core & df["abs_pbr_vs_roe_ok"]
    if cfg.abs_min_div_yield > 0:
        core = core & df["abs_div_ok"]
    df["abs_passes"] = core

    rel = df["passes"].astype(bool)
    absp = df["abs_passes"].astype(bool)
    df["screen"] = np.select([rel & absp, rel & ~absp, ~rel & absp],
                             ["both", "relative", "absolute"], default="")
    df["passes_any"] = rel | absp

    stats = {
        f"abs_pbr_under_{cfg.abs_max_pbr:g}": int(df["abs_pbr_ok"].sum()),
        f"abs_ev_under_{cfg.abs_max_ev_ebitda:g}": int(df["abs_ev_ok"].sum()),
        "abs_pbr_below_fair": int(df["abs_pbr_vs_roe_ok"].sum()),
        f"abs_div_over_{cfg.abs_min_div_yield:g}pct": int(df["abs_div_ok"].sum()),
        "abs_passing": int(absp.sum()),
        "abs_via_financial_carveout": int((absp & df["abs_via_carveout"]).sum()),
        "abs_new_vs_relative": int((absp & ~rel).sum()),
        "passing_either_screen": int(df["passes_any"].sum()),
    }
    df = df.sort_values(["passes_any", "avg_discount"], ascending=[False, False])
    return df, stats


def restate_info_for_splits(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Yahoo's .info per-share fields on today's share basis, after a split.

    The peer and absolute screens read P/E, P/B, EPS, book value and yield
    straight from .info. Yahoo restates those for splits on its own schedule,
    and not consistently: after Johnson Matthey's Aug 2026 4-for-3
    consolidation its bookValue was already on today's 125.9m shares (16.01 =
    FY2026 equity / 125.9m) while its filed statements were still on the old
    167.9m; in the Japan build bookValue was restated while dividendRate was
    not. So the basis is tested per row, never assumed either way.

    `share_basis_g` comes from build_statement_record: the split / consolidation
    factor since the latest filing that today's market cap demands (1 for
    almost everyone). Where it is not 1, filed equity over (book value x filed
    shares) says which basis .info is on: ~1 means the old one, ~g the new.

      old basis   EPS, book value and yield divided by g; P/E and P/B
                  multiplied by it. ROE, their ratio, does not move.
      new basis   per-share fields are right; the dividend cannot be checked
                  the same way (in Japan it lagged when book value did not),
                  so the yield is left missing rather than trusted.
      neither     P/E, P/B and yield missing - no basis to vouch for.

    EV/EBITDA needs nothing: Yahoo builds EV from today's market cap.
    """
    df = df.copy()
    g = pd.to_numeric(df.get("share_basis_g"), errors="coerce").fillna(1.0)
    eq = pd.to_numeric(df.get("lf_equity"), errors="coerce")
    sh = pd.to_numeric(df.get("lf_shares"), errors="coerce")
    bv = pd.to_numeric(df.get("book_value_ps"), errors="coerce")
    k = eq / (bv * sh)
    moved = np.abs(np.log(g)) > 0.02
    tol = np.log(1.25)
    old = moved & (np.abs(np.log(k)) < tol)
    new = moved & ~old & (np.abs(np.log(k / g)) < tol)
    unclear = moved & ~old & ~new

    for c, op in (("trailing_eps", "div"), ("book_value_ps", "div"), ("div_yield", "div"),
                  ("trailing_pe", "mul"), ("price_to_book", "mul")):
        if c in df.columns:
            v = pd.to_numeric(df[c], errors="coerce")
            df[c] = v.where(~old, v / g if op == "div" else v * g)
    if "div_yield" in df.columns:
        df.loc[new, "div_yield"] = np.nan
    for c in ("trailing_pe", "price_to_book", "div_yield"):
        if c in df.columns:
            df.loc[unclear, c] = np.nan

    df["split_note"] = np.select([old, new, unclear],
                                 ["info restated for split", "yield unverified after split",
                                  "info basis unclear"], default="")
    stats = {"info_restated_for_split": int(old.sum()),
             "info_yield_unverified": int(new.sum()),
             "info_basis_unclear": int(unclear.sum())}
    if moved.any():
        log.info("split basis: %d .info rows restated, %d yields unverified, %d unclear",
                 *stats.values())
    return df, stats


HIST_METRICS = (("per", "trailing_pe"), ("pbr", "price_to_book"),
                ("evx", "ev_to_ebitda"))


def _as_list(v) -> list:
    return list(v) if isinstance(v, (list, tuple, np.ndarray)) else []


def add_history_now(df: pd.DataFrame) -> pd.DataFrame:
    """Today's P/E, P/B and EV/EBITDA on the SAME basis as the history.

    Each historical year is that year-end market value over that year's filed
    totals (providers_us.build_statement_record). Today's value has to be
    built the same way - today's market value over the latest filing - or the
    comparison measures the gap between two definitions rather than a change
    in valuation.

    That is not hypothetical. yfinance's own trailingPE divides by the last
    twelve months, including interims: the UK build measured Shell at 10.5
    against 15.2 on the filed-year basis, a 31% gap that would read as a 31%
    discount to history on its own. Korea learned the same lesson on
    EV/EBITDA, where mixing providers put only 18 of 30 within +/-25%; its fix
    was to rebuild the current value from the history's own source, which is
    what this does.

    So these three columns feed the own-history screen ONLY. The peer and
    absolute screens keep yfinance's figures, because they compare companies
    with each other on a common vendor definition, not a company with itself.

    Out-of-bounds values become NaN here rather than in the screen, so the
    dashboard receives exactly what Python tested and cannot disagree with it.
    """
    df = df.copy()
    n = lambda c: pd.to_numeric(df.get(c), errors="coerce")  # noqa: E731
    mv = n("market_cap_local") * n("fx_now")      # quote-major -> reporting ccy
    ni, eq, eb = n("lf_ni"), n("lf_equity"), n("lf_ebitda")
    mi = n("lf_mi").fillna(0.0)
    df["per_now"] = (mv / ni).where(ni > 0)
    df["pbr_now"] = (mv / eq).where(eq > 0)
    df["evx_now"] = ((mv + n("lf_debt") - n("lf_cash") + mi) / eb).where(eb > 0)
    df.loc[_fin_mask(df), "evx_now"] = np.nan          # invariant 6
    for col, (_, bound_key) in zip(("per_now", "pbr_now", "evx_now"), HIST_METRICS):
        lo, hi = K.METRIC_BOUNDS[bound_key]
        df[col] = df[col].where((df[col] >= lo) & (df[col] <= hi))
    return df


def apply_history_screen(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Cheap against the company's own filed history - the third screen.

    A port of the Korea build's apply_history_screen, held to the same rules:
    the benchmark is a median (invariant 4); a non-positive or out-of-bounds
    multiple is missing, never cheap, in history as well as today
    (invariant 2), so a loss year drops out of the benchmark rather than
    dragging it; EV/EBITDA is skipped for financials (invariant 6); fewer than
    `hist_min_years` usable years is no benchmark; the ROE floor applies.

    One difference, and it is a data limit rather than a choice: Yahoo carries
    FOUR filed years for US companies, as for UK, German and Hong Kong ones, where
    WiseReport gave Korea five, so the benchmark is a four-year median.
    """
    df = add_history_now(df)
    fin = _fin_mask(df)

    disc_cols = []
    for key, bound_key in HIST_METRICS:
        lo, hi = K.METRIC_BOUNDS[bound_key]
        vals = df.get(f"hist_{key}", pd.Series([[]] * len(df), index=df.index)) \
                 .map(_as_list)
        for i in range(5):
            df[f"hist_{key}_y{i + 1}"] = vals.map(
                lambda v, i=i: v[i] if i < len(v) and v[i] is not None else np.nan)

        def bench(v):
            ok = [float(x) for x in v if x is not None and np.isfinite(float(x))
                  and lo <= float(x) <= hi]
            return float(np.median(ok)) if len(ok) >= cfg.hist_min_years else np.nan

        med = vals.map(bench)
        if key == "evx":
            med = med.where(~fin)
        cur = df[f"{key}_now"]
        df[f"hist_{key}_med"] = med
        df[f"hist_{key}_disc"] = (med - cur) / med
        disc_cols.append(f"hist_{key}_disc")

    discs = df[disc_cols]
    df["hist_n_valid"] = discs.notna().sum(axis=1)
    df["hist_n_pass"] = (discs >= cfg.hist_min_discount).sum(axis=1)
    # Averages every metric with data, including the failing ones - the same
    # rule as avg_discount (invariant 5).
    df["hist_avg_disc"] = discs.mean(axis=1, skipna=True)

    roe_ok = df.get("roe_ok", pd.Series(True, index=df.index)).fillna(False).astype(bool)
    hp = df["hist_n_pass"] >= cfg.hist_min_metrics
    if cfg.hist_require_roe:
        hp = hp & roe_ok
    df["hist_passes"] = hp

    # Three screens now, so `screen` names every one a row cleared.
    rel = df["passes"].astype(bool)
    absp = df.get("abs_passes", pd.Series(False, index=df.index)).astype(bool)
    parts = pd.DataFrame({"relative": rel, "absolute": absp, "history": hp})
    df["screen"] = parts.apply(lambda r: " + ".join(k for k, v in r.items() if v), axis=1)
    df["passes_any"] = rel | absp | hp

    note = df.get("hist_note", pd.Series("", index=df.index)).fillna("")
    stats = {
        "hist_with_benchmark": int((df["hist_n_valid"] > 0).sum()),
        # Every name a guard refused a benchmark, by reason - so a data problem
        # shows up as a count in the funnel rather than as names quietly
        # missing from the third screen.
        "hist_share_break": int((note == "share-count break").sum()),
        "hist_basis_mismatch": int((note == "price/share basis mismatch").sum()),
        "hist_stale_filing": int((note == "stale filings").sum()),
        "hist_no_reporting_ccy": int((note == "no reporting currency").sum()),
        "hist_normalized_earnings": int((df.get("hist_earnings", pd.Series("", index=df.index))
                                         == "normalized").sum()),
        f"hist_passing_{cfg.hist_min_discount:.0%}": int(hp.sum()),
        "hist_new_vs_other_screens": int((hp & ~rel & ~absp).sum()),
        "passing_any_screen": int(df["passes_any"].sum()),
    }
    df = df.sort_values(["passes_any", "avg_discount"], ascending=[False, False])
    return df, stats


def us_output_columns(cfg: K.ScreenConfig) -> list[str]:
    cols = ["ticker", "symbol", "cik", "name", "name_exchange", "board", "country",
            "sector", "industry", "yf_country", "market_cap_usd", "mcap_source",
            "nasdaq_mcap_usd", "adv_usd", "close_local", "currency", "fx_rep_to_usd", "fx_note",
            "issuer_lines", "is_controlled", "ctrl_pct", "is_holdco", "holdco_note",
            "pyramid_parent_of", "pyramid_child_of"]
    for m in cfg.metrics:
        cols += [m, f"{m}_peer_median", f"{m}_discount",
                 f"{m}_peer_n", f"{m}_pct_rank", f"{m}_peer_basis"]
    cols += ["hist_years", "hist_note", "hist_earnings", "share_basis_g", "split_note",
             "per_now", "pbr_now", "evx_now",
             "hist_n_valid", "hist_n_pass", "hist_avg_disc", "hist_passes"]
    for m in ("per", "pbr", "evx"):
        cols += [f"hist_{m}_med", f"hist_{m}_disc"]
        cols += [f"hist_{m}_y{i}" for i in range(1, 6)]
    cols += ["fin_years", "fin_n", "fin_ccy"]
    for m in ("rev", "op", "ebitda", "np"):
        cols += [f"{m}_y1", f"{m}_y2", f"{m}_y3", f"{m}_cagr"]
    cols += ["screen", "passes_any", "abs_passes", "abs_pbr_ok", "abs_ev_ok",
             "abs_pbr_vs_roe_ok", "abs_div_ok", "abs_roe_ok", "abs_fair_pbr",
             "abs_via_carveout", "ttm_vs_filed_loss",
             "roe_pct", "roe_ok", "roe_tier", "div_yield", "pays_dividend",
             "pbr_below_1", "pbr_bottom20_industry",
             "n_valid_metrics", "n_metrics_passing", "metrics_passing",
             "avg_discount", "median_pct_rank", "passes"]
    return cols
