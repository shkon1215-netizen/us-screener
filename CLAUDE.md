# CLAUDE.md — United States (NYSE + Nasdaq) Valuation Screener

Eighth market after Korea, the UK, Japan, Germany, Poland, France and Hong
Kong, built on Hong Kong's copy. Same maths (`screener.py`), same three
screens (vs peers, outright, vs own history), same preference floors (ROE 5%,
yield 2%, cost of equity 10%); the absolute-screen valuation levels are
re-measured (P/B < 1.5, EV/EBITDA < 9). Universe: every NYSE, Nasdaq and NYSE
American line on Nasdaq's screener (7,002 on 2026-10-18).

## Run order

```bash
python test_us.py         # offline, must pass
python check_setup.py     # nine live checks
python main_us.py -v      # full run: ~45 min cold (.info x3,800, statements x2,300), ~5 warm
python serve.py           # dashboard with working Refresh (dashboard.cmd)
python sync_dashboard_from_korea.py ../Korea/dashboard.py   # dashboard.py is generated
```

```bash
python main_us.py --include-foreign   # keep non-dollar reporters (ADRs named "Common Stock", Canadians)
python main_us.py --include-adrs      # keep lines named American Depositary Shares
python main_us.py --include-funds     # keep closed-end funds and BDCs
python main_us.py --min-adv 1e6       # looser liquidity bar
python main_us.py --no-pregate        # send every line to Yahoo
```

**Never make Yahoo calls by hand while a run is fetching.** The first run was
cut off at 1,500 of 3,800 names; a handful of probe calls on top of the
pool's pace tipped it.

## Files

| File | Role |
|---|---|
| `screener.py` | Core engine, Korea's maths. FX leg is 1.0 (USD). Do not edit the maths here first. |
| `config_us.py` | Thresholds, bounds, security-type markers (from the exchange's own names), Nasdaq status codes, named holdcos. |
| `providers_us.py` | Nasdaq screener + Nasdaq Trader directory + SEC CIKs, SEC fund registers, EDGAR filing histories, Yahoo snapshot, liquidity, FX, filed statements (UK's `build_statement_record`, unchanged), `RateGuard`. |
| `us_filters.py` | Roster hygiene, pre-gate, market-cap repair, EDGAR fund check, Yahoo hygiene, one line per issuer, control, pyramids, the currency repair, then the shared screens. |
| `main_us.py` | CLI. Orders the gates so per-ticker work runs last. |
| `dashboard.py` | Generated — do not edit. Run `sync_dashboard_from_korea.py` (73 substitutions, each must match once). |
| `serve.py`, `build_site.py`, `dashboard.cmd` | Local server, GitHub Pages site, double-click launcher. |
| `check_setup.py`, `test_us.py` | Live pre-flight; offline tests with planted traps. Keep green. |

## Where each piece came from

| piece | from | US change |
|---|---|---|
| roster from the operator | Germany, France, Hong Kong | Nasdaq's screener feed (one request) + Nasdaq Trader's directory + SEC CIKs |
| cheap pre-gate on the operator's caps | Korea, Japan, Hong Kong | Nasdaq's caps; a quarter of the floor (one real company sits at 0.26x Yahoo's cap) |
| classification | UK, Hong Kong (Yahoo industry → sector) | Nasdaq's own sector field is wrong (Agilent: "Industrials") |
| share classes | Korea (우선주), Japan (5-character codes) | exchange security names + ACT / screener symbol marks |
| investment vehicles | UK (AIC register) | SEC CEF + BDC registers, then each candidate's EDGAR filings |
| one line per issuer | Germany | keyed on the SEC's CIK |
| distress | Korea (관리종목), Japan | Nasdaq Financial Status E/Q/G/H/J/K |
| foreign secondaries | Germany, France, Hong Kong | ADR names + accounts not in dollars |
| pyramids, holdcos, control | France, Hong Kong | pyramids need the same Yahoo INDUSTRY |
| currency repair | Poland → France → Hong Kong | quote currency USD; off by default (foreign reporters excluded) |
| rate limiting | UK | `RateGuard`: cooldowns, empty frames count as failures |
| splits, TTM-vs-filed flag | UK, Germany | none |

## US invariants

1. **The roster lists securities, not companies.** 1,097 lines of 7,002 are
   preferreds, depositary shares of a preferred, baby bonds, warrants, rights,
   SPAC and tangible-equity units. `config_us.security_kind` reads the
   exchange's own name, in a fixed order: ADR, warrant, right, unit (an MLP's
   "Common Units representing limited partner interests" excepted), then *a
   line the exchange calls common is common* ("Preferred Bank Common Stock",
   Nasdaq's "Bank Nova Scotia Halifax Pfd 3 Ordinary Shares"), then
   preferred and debt. Nasdaq's `^` (ABR^D) and the ACT `$` mark preferreds.
2. **Funds hide under "Asset Management".** Yahoo files PIMCO Dynamic Income,
   Ares Capital and BlackRock in one industry. The SEC's CEF and BDC registers
   (three years unioned, by CIK) remove 366 before Yahoo is asked. They are
   not complete: BlackRock's older trusts (BDJ, CII, BME, DSU) are in none of
   them. So every financial or sectorless line is checked against its own
   EDGAR filings: a fund files N-CSR / N-CSRS / NPORT-P / N-CEN (a manager
   files N-PX and 10-Ks), a BDC's own 10-K / 10-Q carries an 814- file
   number. **Only its own 10-K:** Blackstone, Brookfield and Assured Guaranty
   carry 814- numbers on 13G filings about BDCs they hold, and an any-filing
   test dropped all three. 25 CEFs and 5 BDCs found this way on 2026-10-18.
   Answers cached 30 days in `.us_cache/edgar_kinds_v2.json`.
3. **One line per issuer by CIK.** GOOGL/GOOG, BRK-A/BRK-B, FOXA/FOX,
   NWSA/NWS, BF-A/BF-B, HEI/HEI-A, LBTYA/B/K: 23 dropped. The most traded line
   stays. Yahoo's market cap on each line is the whole company (`check_setup`
   asserts it on Alphabet), and the history screen's basis check refuses a
   price and a share count on different class bases. Berkshire B's Yahoo book
   value is per A share (P/B 0.001) and the P/B bound refuses it.
4. **Nasdaq's market cap fills a Yahoo blank only for a single-line issuer** —
   for a multi-class company it can be one class.
5. **Foreign reporters are excluded by default** (155 lines). Nasdaq names
   many ADRs plain "Common Stock" (TSMC, Toyota, HDFC Bank, Novo Nordisk, KB
   Financial, BBVA), and Canada's interlisted banks, pipelines and miners
   report in Canadian dollars. Their home market is elsewhere — and covered by
   the sibling screeners — and Yahoo's per-share data on an ADR mixes the ADR
   ratio with the currency. The cost: foreign companies listed only here
   (Spotify, On, Birkenstock, StoneCo, XP) go too. `--include-foreign` keeps
   them all with Hong Kong's repair; measured on that run, 59 EV/EBITDAs
   repaired, 18 P/Es and 20 P/Bs refused, EPS never on the reporting basis.
6. **Property is not excluded**, unlike Hong Kong and Germany: their reason
   was fair-value investment property (IAS 40 / HKAS 40); US GAAP books real
   estate at cost. REITs (145 by Yahoo industry, 20 by name) and SPACs (299,
   Yahoo's "Shell Companies") go.
7. **Pyramids need the same Yahoo industry.** With 2,500 names there are three
   million pairs: the first run paired Brink's with Pool Corp — revenue
   5,261m vs 5,289m, operating profit 589m vs 580m, Brink's buyback-thinned
   equity a third minorities, both "Industrials". The real pairs found:
   Plains GP / Plains All American, Teekay / Teekay Tankers.
8. **Yahoo throttles statements silently.** The first full run got empty
   income statements, with no error, for 473 of 2,514 names — a stretch of
   400 fetched at twice the normal speed — and all of them fetched normally
   ten minutes later. `RateGuard` counts empty frames and rate-limit errors
   alike; 25 in a row pause every worker for two minutes (three times), then
   the pool stops and the cache keeps what arrived. Re-run: 2,390 of 2,393.
9. **The SEC refuses anonymous requests** (HTTP 403 to a browser UA, a bare
   product name or a GitHub noreply address). `SEC_USER_AGENT` sets the
   contact; the default is this repo's commit identity.
10. **Financials = Yahoo sector "Financial Services"**; real estate is its own
    sector, so no carve-out. Germany's invariants 2, 4, 5, 7, 8, 11 and 14
    hold unchanged.

## Liquidity gate: USD 4m, measured 2026-10-16

| ADV floor | names | relative / absolute / history / any |
|---|---|---|
| none | 2,387 | 362 / 48 / 113 / 432 |
| USD 1m | 2,362 | 360 / 48 / 113 / 430 |
| USD 4m (default) | 2,280 | 348 / 46 / 112 / 416 |
| USD 10m | 2,040 | 312 / 39 / 108 / 377 |

(Peer medians from the no-gate cohort, so approximate.) Yahoo's US volume is
the consolidated tape, so it is the whole market's. p5 of ADV past USD 600m
is USD 4.3m: unlike France and Hong Kong, the 1–4m band holds thin, closely
held small caps, not ordinary large companies. The cross-market bar stays.

## Calibration, measured rather than inherited

2,280 names past the default gates, 2026-10-16:

| | p10 | p25 | median | p75 | p90 |
|---|---|---|---|---|---|
| P/E | 9.42 | 13.10 | **20.67** | 33.63 | 56.0 |
| P/B | 1.07 | 1.53 | **2.68** | 5.14 | 9.41 |
| EV/EBITDA | 6.76 | 9.03 | **13.23** | 19.89 | 31.4 |
| ROE % | −28.2 | 1.2 | **10.5** | 19.1 | 34.2 |
| Yield % | 0.00 | 0.00 | **0.18** | 2.02 | 3.59 |

Korea's P/B < 1 and EV/EBITDA < 8 land at ~p7 and ~p17 — deep value, not the
cheapest quarter — so both moved to their p25: **P/B < 1.5, EV/EBITDA < 9**.
The preference floors stay. What binds is fair P/B (ROE ÷ 10%) and the 2%
yield, which most US companies do not pay: at P/B 1.5 / EV 10, 364 names are
cheap on both, ROE leaves 259, fair P/B 89, the yield 46. Cost of equity 8%
would give 112.

**Board:** median P/B NYSE 2.38, Nasdaq 3.09, NYSE American 2.65 — nothing
like KOSPI/KOSDAQ's 5x, so peers are Yahoo industries across exchanges, with
a board filter on the one page. 2,125 of 2,280 scored within their industry
on P/E.

## Results, reference run (2026-10-16 close)

```
7,002 lines (Nasdaq 4,024, NYSE 2,330, NYSE American 274, unlisted on the directory 373)
  4,538 after roster + EDGAR + Yahoo hygiene
  3,802 sent to Yahoo by the pre-gate, 3,787 priced
  2,416 cleared USD 600m
  2,285 cleared USD 4m/day (after 23 second-class lines)
    359 cheap vs peers (after ROE >= 5%)
     46 cheap outright (22 financials via the P/B + ROE carve-out)
    112 cheap vs own history (52 found by nothing else)
    425 passing at least one screen
```

Browser verdict at default thresholds equals Python's: 425 / 359 / 46 / 112,
and every per-test count (P/B < 1.5 500, EV < 9 383, fair 189, yield 578,
ROE 1,444, carve-out 22, history 183 / 252 / 147).

## Known gaps (ranked by value of fixing)

1. **Trailing one-offs.** Lyft's P/E 2.5 and ROE 85% are a tax
   valuation-allowance release; Hyperliquid Strategies' P/E 2.8 is crypto
   marked to market. `ttm vs fy` only fires on a filed LOSS.
2. **Digital-asset treasury companies** (Hyperliquid Strategies, Strive) are
   priced on their coin holdings, like a fund, and no exchange or SEC label
   names them. A named list would be the honest fix.
3. **Foreign companies listed only in the US** go with the foreign reporters
   (invariant 5).
4. **No GICS.** Peers are Yahoo industries.
5. **NYSE publishes no distress status** in the directory; only Nasdaq's
   delinquent / bankrupt codes are applied.
6. Four filed years for the history screen; trailing multiples only.

## Likely first failures

- **Yahoo throttles**, loudly or silently (invariant 8). The run refuses to
  screen under half priced; the per-day cache fills gaps on re-run.
- **The SEC answers 403** — set `SEC_USER_AGENT`. `check_setup.py` checks the
  registers and the CIK join.
- **Nasdaq's screener feed changes shape** — `check_setup.py` asserts Apple,
  JPM, Berkshire, Alphabet's shared CIK and the exchange flags.
- **The statements cache stores derived records.** Bump
  `STATEMENTS_CACHE_VERSION` when `build_statement_record` changes; bump
  `EDGAR_CACHE_VERSION` when `parse_submissions` does.
- **pandas 3.x** — pinned `<3`.

## Keeping in step with Korea

```bash
python sync_dashboard_from_korea.py ../Korea/dashboard.py
```

Every substitution must match exactly once; misses exit 1. Then grep for
Korean text, KOSPI, PER/PBR and `ticker`, and confirm in a browser that the
page's verdict equals the Python funnel at default thresholds.

## Publishing

`.github/workflows/screen.yml` runs at 22:30 UTC on weekdays (the close is
20:00 UTC in daylight saving, 21:00 in winter), builds `site/` and deploys to
GitHub Pages, with two retries after pauses for Yahoo throttling. Timeout
150 minutes — the cold run is ~45.

A docs-only push does not trigger a run (`paths-ignore: **.md`); start one
with `gh workflow run screen.yml`.

## Interpretation

Sort by `avg_discount`, then read `roe_pct` immediately, then look for tags.
`N classes` means another listed class was dropped and the multiples are the
whole company's; `close` means the free float is the minority; `holdco`
means a portfolio discount may be structural; `ttm vs fy` means read the P/E
twice. Low P/B with low ROE is arithmetic, not opportunity.

Research tool, not investment advice.
