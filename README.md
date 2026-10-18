# United States (NYSE + Nasdaq) Relative Valuation Screener

**Live:** https://shkon1215-netizen.github.io/us-screener/ · rebuilt each
weekday after the US close

Finds US-listed stocks trading at a discount to their industry peers on P/E,
P/B and EV/EBITDA — and, independently, stocks that are cheap in absolute
terms, and stocks that are cheap against their own filed history. Each row
also carries three years of revenue, EBITDA and net profit.

**Defaults:** market cap ≥ USD 600M · median daily traded value ≥ USD 4M
(consolidated tape) · ≥20% below peer median on ≥2 of 3 metrics · ROE ≥ 5%.

The eighth market after [Korea](https://github.com/shkon1215-netizen/korea-screener),
the UK, Japan, Germany, Poland, France and Hong Kong. `screener.py` is the
same maths; what is local is the data layer and the universe hygiene.

## Setup

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
python test_us.py        # offline logic check, no network needed
python check_setup.py    # tests every live call individually
python main_us.py -v     # full run, ~45 minutes cold, ~3 warm
```

```bash
python main_us.py --min-adv 1e6                # a looser liquidity bar
python main_us.py --skip-liquidity             # size alone gates
python main_us.py --discount 0.15 --min-metrics 1
python main_us.py --include-adrs --all         # see what ADRs would add
python main_us.py --include-funds              # keep closed-end funds and BDCs
```

Results cache to `.us_cache/` per session date, so re-runs are fast — and so
a throttled Yahoo cannot silently shrink the universe.

The SEC asks automated users to identify themselves and refuses requests
that do not. Set `SEC_USER_AGENT` to your own name and e-mail address.

## The dashboard

Every run writes a self-contained `us_dashboard.html`. Open it directly, or
run `python serve.py` (on Windows, double-click `dashboard.cmd`) to make the
**Refresh** button work. Every threshold on the page is re-evaluated in the
browser; peer medians and sub-floor market caps cannot be, and the page says
so.

## Where the data comes from

| | source |
|---|---|
| roster, security names, pre-gate market caps | Nasdaq's stock screener feed — every NYSE, Nasdaq and NYSE American line, one request |
| exchange, ETF and test-issue flags, Nasdaq status | Nasdaq Trader's symbol directory |
| issuer (CIK) | the SEC's `company_tickers_exchange.json` |
| closed-end funds and BDCs | the SEC's registers, then each candidate's own EDGAR filing history |
| multiples, EPS, book, yield, industry | Yahoo `.info`, `fast_info` when the market cap is blank |
| liquidity | one batched Yahoo price download, 60-session median traded value |
| filed statements | Yahoo annual income statement and balance sheet |

## US traps this handles

**The roster lists securities, not companies.** Nasdaq's screener carries
preferreds, depositary shares of a preferred, baby bonds, warrants, rights and
SPAC units beside the common stock. Each goes by the exchange's own security
name — and "Preferred Bank Common Stock" stays.

**Closed-end funds and BDCs.** Yahoo files PIMCO Dynamic Income and Ares
Capital under "Asset Management", beside BlackRock. The SEC's registers
remove most; the rest (BlackRock's older trusts, in no register) are found by
their own EDGAR filings: a fund files N-CSR and N-PORT reports, a BDC's 10-K
carries an 814- file number.

**Two classes, one company.** GOOGL and GOOG, BRK-A and BRK-B, FOXA and FOX
share an SEC CIK; only the most traded line is screened, and Yahoo's market
cap on it is the whole company.

**ADRs and other foreign secondaries** are excluded: by name ("American
Depositary Shares", "New York Registry Shares"), and — because Nasdaq names
many ADRs plain "Common Stock" (TSMC, Toyota, HDFC Bank, Novo Nordisk) — by
accounts in another currency, which also takes Canada's interlisted banks and
pipelines. The cost is a handful of foreign companies listed only here
(Spotify, On, Birkenstock); `--include-foreign` keeps them all and repairs
Yahoo's mixed-currency EV/EBITDA. REITs, SPACs and Nasdaq lines whose filings
are delinquent or in bankruptcy go too.

Research tool, not investment advice.
