# Changelog

All notable changes to the Longbridge statement tax skill are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/); the project is
date-versioned (no semver tags).

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

### Added
- `DividendCurrencyTest` covering `settle_ccy` precedence (explicit → `@CCY` tag → market
  suffix) and an end-to-end check that blank-currency dividends are taxed under the right
  currency instead of vanishing into the unknown bucket.

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
