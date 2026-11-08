# Changelog

All notable changes to the Longbridge statement tax skill are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/); the project is
date-versioned (no semver tags).

## 2026-11-08

### Added
- **`--adjustments FILE` — a manual-adjustment ledger for the long tail.** Four effects cover
  events the event model does not enumerate: `quantity` (split/reverse-split factor), `cost`
  (signed total-cost delta for non-cash distributions, merger consideration, basis fixes),
  `realized` (broker reclassification) and `income` (`character` = dividend or capital_gain).
  Applied on their date, after that day's trades; no-position/short cases are reported and
  skipped. Output: `手工调整.csv`.
- **`docs/tax-treatment.md` and `docs/examples.md`** — the normative event model, distribution
  decision tree and data contracts, separated from generic (non-exhaustive) examples.
- **Distribution characterisation — `--distributions FILE`.** Distributions are no longer
  assumed to be dividends. Each one can be `dividend` (利息股息红利), `capital_gain`
  (财产转让, cost unchanged) or `roc` (return of capital: not income, cost basis reduced and
  deferred into the sale). Matching is by `(code, date)` → `(code, amount)` → a wildcard only
  when the ticker distributes once; ambiguous rules raise instead of guessing.
- **`--write-distribution-template`** emits `distributions_template.csv` (all distributions,
  blank character, hints) so the user/advisor can confirm the character from the fund's tax
  supplement or 1042-S.
- **`--on-unclassified-distribution error`** stops the run instead of defaulting unknowns to
  dividends; the default still treats them as dividends but lists them as 未定性/待确认 in
  `分配明细.csv` and the console.
- **ROC support in the realized engine**: a return of capital reduces the open position's cost
  basis (excess over basis becomes an immediate gain), so later sales book the deferred gain.
- **`longbridge_<YEAR>_分配明细.csv`** and a `(备查)资本返还` / `其中：资本利得分配` line in the
  tax summary.
- **`--opening-adjustments FILE`** plus `次年成本加回.csv`: undo a broker's cost reduction for a
  capital-gain distribution in the following year (the broker's 摊薄 cost drops on any
  distribution, which must not change basis for a capital-gain distribution).
- **Data-quality guards**: missing monthly statements now stop the run (with a loud list and
  `--allow-missing-months` override); `export` retries the alternate CDN key prefix
  (`/statement_data/` ↔ `/lb-statement/`) when the listed key is dead.
- **Trade-date attribution**: `<year+1>01` is fetched and every section is filtered to rows whose
  trade/pay date is inside the year. Statements are cut by settlement month, so January's
  statement otherwise leaks the prior December's trades into the year (double count) and 30/31
  December trades are otherwise missed.

### Fixed
- The realized engine now normalises trade and ROC-adjustment dates before sorting (a ROC dated
  `YYYY-MM-DD` sorted before trades dated `YYYY/MM/DD`).
- **Options carried in from the prior December and settled this year are now closed at 0** from
  `equity_holding_changes` (`OptionExpire`/`OptionExercise`). Previously they stayed open and
  their premium was silently dropped (an early-January call and two puts in a real 2025 run,
  about +1.6k USD in total). Trades that already closed the position are left alone (no double
  count), and a position bought back before expiry is unaffected.

## 2026-11-07

### Added
- **Option-seller (wheel) support — new default `--on-negative-position=netcash`.** Options
  sold to open and never bought back used to reconstruct as "negative positions", and all
  three legacy modes (flag/exclude/expiry-estimate) misstated their P&L. In `netcash` mode an
  in-year-expiry short option books its realized P&L as its **net option cash flow** (premium
  received − buybacks paid) — mathematically identical across expired-worthless / assigned /
  bought-back endings — while a contract still open at year-end is excluded as unrealized.
  The stock leg of an assignment needs no special handling (it is delivered at the strike and
  already lands in `stock_trades`). Non-option shorts keep the old warn/exclude/short flow.
- **Official settlement events are now parsed and exported.** Monthly
  `equity_holding_changes` rows `OptionExpire` / `OptionExercise` (the only authoritative
  expiry/assignment record Longbridge provides) are collected into
  `longbridge_<YEAR>_期权结算事件.csv` and used to label each netcash option's ending.
  Contracts bought back before expiry generate no event — absence is normal, not a gap.
- **Withholding tax is now aggregated and shown as a credit line.** Negative
  withholding/dividend-fee rows are summed per currency (blank-currency rows inherit the
  same-date dividend's currency, else the single dividend currency of the year) and reported
  as `(抵免项)境外已预扣税额`, reducing the estimated tax due.
- **`--base-currency CCY`** buckets blank-currency financing-interest lines (the statement
  carries no currency hint for them).
- **`--refresh`** and an on-disk statement cache (`<outdir>/<year>/.statements/`): monthly
  statement JSONs are cached, so re-runs don't re-hit the rate-limited API.
- **Rate-limit retry with exponential backoff** in the CLI wrapper (the statement API allows
  ~1 req/s; error 429002 used to kill a whole 13-request run mid-way).

### Fixed
- **Prior-December seeding of SHORT option positions.** The seeding loop skipped every
  `ledger_quantity <= 0` row, so short options carried into the year were never seeded: their
  2025 buy-to-closes reconstructed as phantom longs (realized 0, premium lost). Negative
  quantities are now seeded as shorts, and option `cost_price` (per-share premium) is
  converted to per-contract units (×100) to match trade pricing.
- **ISIN-identified dividends escaped currency inference.** Remarks like
  `TSM(US8740391003) Payment in Lieu of Dividend` defeat the `CODE.MARKET` suffix match; a
  bare currency word and a safe ISIN country-prefix fallback (`US`→USD, `HK`→HKD) were added
  to `settle_ccy()`, and fund-trade rows now use it too.

## 2026-06-30

### Fixed
- **Cash dividends with a blank `currency` are no longer dropped from RMB conversion and the
  dividend tax.** Real Longbridge statements often leave the `currency` field empty on
  dividend lines (the same way IPO-allotment lines do — see gotcha #9), carrying the market
  only inside the remark/symbol (e.g. `Cash Dividend 700.HK`, `AAPL.US`). Those dividends
  were bucketed under an unknown ("") currency, which has no FX rate, so `金额(RMB)` and
  `应纳税额(RMB)` came out blank — the dividend silently escaped the 20% tax. A new
  `settle_ccy()` helper derives the settlement currency from an `@HKD`-style tag or a
  `CODE.MARKET` suffix (mapped through `MARKET_CCY`) whenever the field is blank, and is now
  applied to both dividend sources (`corps` and dividend-flagged `account_balance_changes`).
- **Financing interest with a blank `currency` now buckets under the right currency too.**
  The same `settle_ccy()` fallback is applied to `interests` lines, so a blank-currency
  interest line that carries the market in its symbol/remark no longer lands in the
  unknown-currency row of the 税务汇总 (interest is a备查 cost, not taxed, but it must still
  reconcile per currency).

### Added
- `DividendCurrencyTest` covering `settle_ccy` precedence (explicit → `@CCY` tag → market
  suffix) and an end-to-end check that blank-currency dividends are taxed under the right
  currency instead of vanishing into the unknown bucket.
- `InterestCurrencyTest` checking a blank-currency financing-interest line buckets under its
  market currency in both the cashflow CSV and the tax summary.

## 2026-06-29

### Changed
- **Default output is now `out/<year>/`; output is no longer labelled by account.** The
  Longbridge account number is not retrievable from the API or the statements — it is known
  only at CLI login (`assets`/`portfolio` don't carry it; `bank-cards` lists external bank
  accounts, not the brokerage account). The previous "name `-o` after the account" guidance
  invited *guessing* the account (e.g. from memory), which silently mislabels tax data. The
  default `-o` changed from `longbridge_parsed` to `out`, and SKILL.md/README now say never to
  guess the account; pass a distinct `-o` per account only if you actually run multiple
  accounts. Year auto-nesting and the double-nest guard are unchanged.

### Added
- `OutputNestingTest.test_default_outdir_is_plain_out` pins the `out/<year>/` default.

## 2026-06-28

### Fixed
- **IPO-allotted shares (打新中签) are now costed correctly.** Allotted shares enter via
  `account_balance_changes` (`biz_code LIPOALDR`, e.g.
  `IPO 6831.HK Allotted Amount (400 Shares @HKD 2,876.00)`), not as a `stock_trades` buy, so
  the reconstruction previously treated a later sale as a zero-cost short and badly overstated
  the gain. New `ipo_allotment()` parses these lines and seeds their cost basis like a
  carried-in holding. (On a real 2025 account this flipped a spurious −9,976.90 HKD *loss*
  into the true +10,292.15 HKD *profit* — 7 IPO names worth +14,787.86 HKD.)
- **Same-day round-trips no longer reconstruct as phantom shorts.** Statements carry no
  intraday execution time and sometimes list the SELL line before the BUY on the same date.
  Reconstruction now orders buys before sells within a trade date, clearing spurious
  `负持仓` (negative-position) flags without changing realized totals.

### Changed
- **Output is auto-nested per year: `-o` is now a per-account base dir and the year is added
  as a subfolder** (`out_<account>/<year>/`, e.g. `out_H10764613_M/2025/`), so different
  accounts and years never overwrite each other. If `-o` already ends in the year it is not
  double-nested. The CLI has no account selector and statements carry no account number, so
  name the `-o` folder after the account and ensure the CLI is logged into that account.
- `LIPOALDR` added to `INTERNAL_BIZ` so the IPO allotment debit is consumed as cost basis
  rather than shown as a cash-out line in the cash-flow CSV.
- `.gitignore` now ignores `out_*/` (per-account output folders) alongside `out/`.

### Added
- Tests for IPO allotment parsing/seeding and same-day-ordering (`IpoAllotmentTest`) and for
  per-year output nesting / double-nest guard (`OutputNestingTest`).
- SKILL.md gotcha #9 and statement-section notes documenting IPO allotment cost basis;
  SKILL.md / README quick-start updated for the `out_<account>/<year>/` convention.

## 2026-06-26

### Added
- `--on-negative-position {flag,exclude,short}` flag with skill-guided confirmation for
  instruments whose reconstructed position goes negative.

## 2026-06-25

### Fixed
- Correct realized P&L for short / oversold positions (no longer books the whole sale as profit).
- Multi-currency tax reporting: amounts bucketed per currency with per-currency FX rates.

## 2026-06-21

### Added
- Initial Longbridge statement tax skill: pulls monthly statements via the official
  `longbridge` CLI and reconstructs realized P&L, dividends, interest, and a tax summary.
- Auto-install / auth guidance for the CLI when missing (with y/N confirmation).
