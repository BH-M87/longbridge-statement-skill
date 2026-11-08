---
name: longbridge-statement-tax
description: Use when computing realized P&L, dividends, interest, or tax due from a Longbridge (長橋證券) account — for individual income tax on foreign income (个税 境外所得), CRS reporting, accounting, or broker P&L reconciliation. Pulls the official monthly statements via the Longbridge CLI and reconstructs the figures the statement does not provide. Companion to the Futu statement skill.
---

# Longbridge Statement Tax

## Overview

Longbridge (長橋證券) has an **official command-line tool** (`longbridge`, see
https://open.longbridge.com/zh-CN/docs/cli/). This skill calls `longbridge statement` to
fetch the monthly statements as JSON and turns them into tax-ready CSVs.

Like most brokers, Longbridge statements have **no "已实现盈亏" (realized P&L) section**,
so realized P&L is **reconstructed** here (average-cost) from `stock_trades`, seeded with
the **prior-December closing holdings** (`equity_holdings.cost_price`) so positions carried
into the year are costed correctly. Cash dividends live in **`corps` (corporate actions)**;
financing **interest is in `interests` — a cost you paid, not income**.

This is the companion to **futu-statement-skill** (the other broker). Combine the two
brokers' `已实现盈亏` for the final `财产转让所得` (capital-gains) tax.

## When to Use

- Computing 个税 境外所得: realized capital gains + dividends, kept separate
- CRS / annual account reconciliation
- "How much did I make/lose on Longbridge last year, and how much was dividends?"

## Prerequisite: the official Longbridge CLI

This skill **shells out to `longbridge`** — it never handles your credentials. If the CLI
is missing, the script **offers to install it for you (asks for confirmation first), then
offers to run `longbridge auth login`** — both steps require your y/N confirmation, and it
falls back to printing manual steps if you decline or run non-interactively. Manual setup:

```bash
# install (pick your platform)
brew install --cask longbridge/tap/longbridge-terminal                                   # macOS
curl -sSL https://open.longbridge.com/longbridge/longbridge-terminal/install | sh        # macOS/Linux
scoop install https://open.longbridge.com/longbridge/longbridge-terminal/longbridge.json # Windows

# authenticate (opens a browser, OAuth)
longbridge auth login

# verify
longbridge statement --type monthly --format json
```

If a CLI call fails with an auth error, the script tells you to run `longbridge auth login`.
Docs: https://open.longbridge.com/zh-CN/docs/cli/ . Python 3.10+. (Note: this uses the **CLI**,
not the Longbridge MCP.)

## Quick Start

```bash
python3 longbridge_tax.py --year 2025
```

Output lands in `out/<year>/` by default (e.g. `out/2025/`); `-o DIR` overrides the base and
the year is always auto-nested under it. **The Longbridge account number is *not* retrievable
from the API or the statements — it is known only at CLI login time** (the `assets`/`portfolio`
commands don't carry it, and `bank-cards` shows your external bank accounts, not the brokerage
account). So the script does not label output by account, and **you must never guess one** — a
wrong guess (e.g. from memory) silently mislabels someone's tax data. The CLI has no account
selector either: it uses whichever account the token is bound to, so just make sure the CLI is
logged into the account you mean. Only if you genuinely run **multiple** accounts do you need
to separate them — then pass a distinct `-o` per account yourself (e.g. `-o out_acctA`) and
switch CLI logins between runs.

`--rate` (optional) is the HKD→RMB year-end 中间价 shorthand. For multi-currency statements,
pass one `--fx-rate CCY=RATE` per currency, e.g. `--fx-rate HKD=0.90322 --fx-rate USD=7.10`.
Amounts are grouped by currency first; RMB/tax columns are filled only when that currency
has a matching rate.

For `--year 2025`, if no rate is passed, the script uses the 2025-12-31 PBOC/CFETS central
parity defaults: `HKD=0.90322`, `USD=7.0288`. Explicit `--rate` / `--fx-rate` values override
the built-in defaults.

`--on-negative-position {netcash,flag,exclude,short}` controls how negative/short positions are
handled (default `netcash`) — see **Negative positions** below; a `flag`/`netcash` run that
still reports **non-option** negative positions needs user confirmation, not a silent pass.

Other flags: `--refresh` re-downloads all monthly statements (bypassing the on-disk cache in
`<outdir>/<year>/.statements/`); `--base-currency CCY` buckets blank-currency financing-interest
lines (the statement gives no currency hint for them); `--allow-missing-months` lets a run
continue when a monthly statement is unavailable (it warns loudly — results will be understated);
`--distributions FILE` supplies the tax character of non-dividend distributions (see below);
`--opening-adjustments FILE` adds an amount back to an instrument's opening cost (used in year
N+1 after a capital-gain distribution the broker wrongly applied to cost); `--adjustments FILE`
is the manual ledger for events the model does not cover (splits, non-cash distributions,
reclassifications) — see **Manual adjustments** below.

| Output (utf-8-sig) | Contents |
|---|---|
| `longbridge_<YEAR>_成交明细.csv` | stock / option / fund trades; `清算金额` already net of fees |
| `longbridge_<YEAR>_股息利息现金流.csv` | dividends (corps), financing interest, withdrawals |
| `longbridge_<YEAR>_已实现盈亏_按标的.csv` | realized P&L per instrument (average-cost) |
| `longbridge_<YEAR>_期权结算事件.csv` | official OptionExpire/OptionExercise settlement rows |
| `longbridge_<YEAR>_账户净值.csv` | per-month asset total (cross-check) |
| `longbridge_<YEAR>_税务汇总.csv` | tax summary by currency: gains / dividends / capital-gain distributions / ROC memo / withholding credit / interest + tax due |
| `longbridge_<YEAR>_分配明细.csv` | every detected distribution with its character (dividend / capital gain / ROC), source and how it was applied |
| `longbridge_<YEAR>_次年成本加回.csv` | only for capital-gain distributions: feed to next year's `--opening-adjustments` if the broker reduced cost |
| `longbridge_<YEAR>_手工调整.csv` | every `--adjustments` row with its type and how it was applied |

The script prints realized total, dividends and interest so you can sanity-check.

## Negative positions — option sellers are normal; stocks are not

A Longbridge **cash account cannot really short**, so a negative position on a **stock**
almost always means **missing cost basis** (shares transferred in, a sell recorded before its
buy, a `N/A` prior-December cost). BUT an **option contract opened by SELL-TO-OPEN and never
bought back** reconstructs as a negative position too — that is the normal wheel/sell-put/
sell-call pattern, not a data gap. The two cases must not be conflated (older versions of
this script flagged both, which wrecks option sellers' totals).

Default mode `netcash` separates them by instrument type:

- **Option contracts** (codes like `NVDL250117C70000`): whatever their ending — expired
  worthless, assigned, or bought back in-year — the option-side realized P&L **equals their
  net option cash flow** (premium received − buybacks paid, fees included). The script books
  exactly that, and labels each contract's ending from the **official settlement events**
  (`equity_holding_changes` rows `OptionExpire` = expired worthless, `OptionExercise` =
  assigned; absence of an event = bought back before expiry, which is normal, not a data
  gap). A contract still open at year-end (expiry after Dec 31) is **excluded**: its premium
  is unrealized. The **stock leg of an assignment** is already in `stock_trades` (delivered
  exactly at the strike), so it lands in the stock bucket with no special handling.
- **Short option positions carried in from the prior December** are seeded from the
  prior-December `cost_price` (option costs are per-share; the script converts to
  per-contract ×100) and computed by the average-cost engine.
- **Stocks** going negative still warn (`flag` behaviour) — on a cash account that really is
  missing basis. Only then, STOP and confirm with the user:

  1. **补成本基础重跑(最准 / recommended)** — find the correct carried-in cost and re-run.
  2. **先排除待核对** — re-run with `--on-negative-position=exclude`.
  3. **确认是做空** — re-run with `--on-negative-position=short`.

Also check the run summary's 年末未平仓期权 line: any option still held in the December
statement whose expiry is inside the tax year means a settlement record was missed.

## Distributions: dividend vs capital gain vs return of capital

A cash distribution is **not automatically a dividend**, and neither the statement nor the
broker's holding `cost_price` tells you its tax character:

| Character | Tax treatment | Cost basis |
|---|---|---|
| `dividend` | 利息股息红利所得, 20 %, year received | unchanged |
| `capital_gain` | 财产转让所得, 20 %, year received | **unchanged** |
| `roc` (return of capital) | not income | **reduced**; deferred into the eventual sale |

This matters: a year-end ETF capital-gain distribution looks exactly like a dividend in the
statement, and the broker's `cost_price` is **摊薄成本** — it drops on *any* distribution, so
using that drop as "proof of ROC" mislabels the year and the next year's opening basis. In one
real case a four-digit USD distribution on a leveraged single-stock ETF was treated as ROC
solely because `cost_price` fell by the same amount; the fund had actually paid a capital-gain
distribution, which is income in the year received.

Workflow:

1. Run once with `--write-distribution-template`. It writes `distributions_template.csv` listing
   every distribution (date, code, amount, source) with a blank `character` and a hint.
2. Confirm each one from the fund's tax supplement / 1042-S (not from `cost_price`), then fill
   `character` = `dividend` | `capital_gain` | `roc` (aliases and Chinese names accepted) and
   pass the file back with `--distributions`.
3. `--on-unclassified-distribution=error` makes the run stop instead of treating unknowns as
   dividends; the default `dividend` behaviour lists them as "未定性/待确认" in the outputs.

Matching is by `(code, date)`, then `(code, amount)`, then a wildcard only when that ticker
distributes once; ambiguous rules raise rather than guess. For a capital-gain distribution the
script also writes `次年成本加回.csv`: if the broker reduced `cost_price` for it, run next year
with `--opening-adjustments <that file>` so the basis is not understated.

## Manual adjustments (the long tail)

Products are unbounded; taxable events are not. Instead of adding a branch per product, map any
unmodelled event onto one of four effects:

| `type` | `amount` | effect |
|---|---|---|
| `quantity` | split factor (2 = 2-for-1, 0.5 = 1-for-2) | qty × factor, unit cost ÷ factor |
| `cost` | signed total-cost delta | non-cash distribution / merger consideration / basis correction |
| `realized` | signed realized P&L | broker reclassification, booked as 财产转让 |
| `income` | signed income | `character` = `dividend` (default `capital_gain`) |

```csv
date,code,type,amount,currency,character,note
2025-05-01,ABC,quantity,2,USD,,2-for-1 split
2025-06-01,ABC,cost,120,USD,,non-cash distribution added to basis
```

`ls`-style event definitions, the distribution decision tree and every data contract are in
[`docs/tax-treatment.md`](docs/tax-treatment.md); generic worked examples are in
[`docs/examples.md`](docs/examples.md). Those are illustrative, not exhaustive — if an event
does not fit, record it in `--adjustments` rather than guessing.

## How it works

1. `longbridge statement --type monthly --start-date <YEAR-1>-12-01 --limit 14` → file keys
   for the 12 months **plus the prior December** (for opening cost basis).
2. `longbridge statement export --file-key <k>` → each month's full JSON.
3. Reconstruct realized P&L (average-cost, long + short) from `stock_trades`, seeded from the
   prior-Dec `equity_holdings` **and from IPO allotments** (`account_balance_changes` lines with
   `biz_code LIPOALDR` — 打新中签 shares arrive here with their cost, not as a `stock_trades` buy).
   Within a trade date, buys are processed before sells (statements carry no intraday time), so a
   same-day round-trip isn't mistaken for a short. Realized accrues only when a trade reduces the
   open position, so positions still open at year-end (long or short) contribute 0. A genuinely
   negative position is flagged (a cash account can't really short — usually missing cost basis)
   instead of booking the whole sale as profit. Dividends from `corps`; interest from `interests`.

## Statement sections used

| Section | Holds | Use for |
|---|---|---|
| `stock_trades` | each trade (`clear_amount` = net of fees, signed) | realized P&L, fees |
| `equity_holdings` | per-holding `cost_price`, quantities (incl. prior-Dec) | opening cost basis |
| `corps` | corporate actions / cash dividends | dividend income |
| `interests` | monthly financing interest (`total` negative = paid) | interest cost |
| `account_balance_changes` | cash movements (withdrawals, etc.) + IPO allotment debits (`biz_code LIPOALDR`) | cash-flow view; **IPO 打新 cost basis** |
| `asset` | month-end asset total | NAV cross-check |
| `fund_trades` | money-market fund subscribe/redeem | shown; excluded from realized |

## Critical Gotchas

1. **No realized-P&L section** — reconstruct from `stock_trades` (average-cost). Seed the
   opening basis from the **prior-December** statement, or gains on positions bought last
   year are overstated.
2. **`clear_amount` is already net of fees** (signed: BUY < 0, SELL > 0). Per-trade fee =
   `|clear_amount| − trade_amount`.
3. **Dividends are in `corps`, not a "dividend" line.** (In some years `corps` is empty —
   then there were no cash dividends.)
4. **`interests` is a cost you paid** (financing/margin interest), not income.
5. **Money-market funds (`fund_trades`)** are accumulating cash-management vehicles; they
   appear in 成交明细 but are **excluded from the realized-P&L headline** (gain is small and
   the statement gives no fund cost basis). Note it if material.
6. **Capital-gains tax must be combined across accounts.** This skill is Longbridge-only;
   its 税务汇总 shows the standalone figure. Net it against your other brokers (e.g. Futu)
   within `财产转让所得` before computing the final capital-gains tax.
7. **Currencies are not interchangeable.** Keep USD/HKD/etc. in separate buckets for
   average cost, realized P&L, dividends, interest, and RMB conversion. Use `--fx-rate`
   for every currency that needs tax/RMB output. Note that cash-dividend lines (in `corps`
   or as dividend-flagged `account_balance_changes`) often arrive with a **blank `currency`
   field**, carrying the market only in the remark/symbol (e.g. `Cash Dividend 700.HK`); the
   script recovers it via `settle_ccy()` (`@CCY` tag or `CODE.MARKET` suffix) so the dividend
   is still taxed under HKD/USD rather than dropping into an unknown-currency bucket.
8. **Default FX rates are year-scoped.** 2025 has built-in PBOC/CFETS 2025-12-31 defaults
   for HKD and USD. Other years need explicit rates until their defaults are added.
9. **IPO-allotted shares (打新中签) have no buy in `stock_trades`.** Their cost basis is the
   `IPO Allotted Amount(Dr)` line in `account_balance_changes` (`biz_code LIPOALDR`); the script
   seeds it automatically. Without it, selling allotted shares would look like a zero-cost short
   and massively overstate the gain (it once flipped a real +HKD 10k profit into a fake loss).

10. **A distribution's tax character is input, not inference.** `corps` / `Cash Dividend`
    lines and a falling `cost_price` cannot distinguish an ordinary dividend, a capital-gain
    distribution and return of capital. Use `--distributions`; the default treats unknowns as
    dividends and lists them as 待确认.
11. **Trade-date attribution, not statement month.** Statements are cut by settlement month:
    January's statement carries the prior December's trades, and 30/31 December trades only
    appear in the next January's statement. The script now reads `<year+1>01` too and keeps
    only rows whose trade date is inside the year — otherwise a trade is counted twice
    (prior December) or missed (year-end).

## Tax note (个税 境外所得)

Convert each currency to RMB at the year-end 人民币汇率中间价 (`--rate` for HKD, repeated
`--fx-rate CCY=RATE` for other currencies). `财产转让所得` (capital gains,
20%) nets gains/losses within the same market across accounts; `利息股息红利所得`
(dividends, flat 20%) is taxed standalone with no deductions. Confirm netting scope,
foreign-tax credit, fund-income classification, and FX 口径 with a tax advisor.
