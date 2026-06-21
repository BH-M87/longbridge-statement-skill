---
name: longbridge-statement-tax
description: Use when computing realized P&L, dividends, interest, or tax due from a Longbridge (長橋證券) account — for individual income tax on foreign income (个税 境外所得), CRS reporting, accounting, or broker P&L reconciliation. Pulls the official monthly statements via the Longbridge CLI and reconstructs the figures the statement does not provide. Companion to the Futu statement skill.
---

# Longbridge Statement Tax

## Overview

Longbridge (長橋證券) has an **official command-line tool** (`longbridge`, see
https://open.longbridge.com/zh-CN/skill/). This skill calls `longbridge statement` to
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

Install and authenticate the Longbridge CLI so this works in your shell:

```bash
longbridge statement --type monthly --format json
```

The skill **shells out to `longbridge`** — it never handles your credentials. Set up auth
per https://open.longbridge.com/zh-CN/skill/ (API key / token via env or login). Python 3.10+.

## Quick Start

```bash
python3 longbridge_tax.py --year 2025 -o out/ --rate 0.90322
```

`--rate` (optional) is the HKD→RMB year-end 中间价; it adds an RMB column and computes tax.

| Output (utf-8-sig) | Contents |
|---|---|
| `longbridge_<YEAR>_成交明细.csv` | stock / option / fund trades; `清算金额` already net of fees |
| `longbridge_<YEAR>_股息利息现金流.csv` | dividends (corps), financing interest, withdrawals |
| `longbridge_<YEAR>_已实现盈亏_按标的.csv` | realized P&L per instrument (average-cost) |
| `longbridge_<YEAR>_账户净值.csv` | per-month asset total (cross-check) |
| `longbridge_<YEAR>_税务汇总.csv` | tax summary: gains / dividends / interest + tax due (with `--rate`) |

The script prints realized total, dividends and interest so you can sanity-check.

## How it works

1. `longbridge statement --type monthly --start-date <YEAR-1>-12-01 --limit 14` → file keys
   for the 12 months **plus the prior December** (for opening cost basis).
2. `longbridge statement export --file-key <k>` → each month's full JSON.
3. Reconstruct realized P&L (average-cost, long side) from `stock_trades`, seeded from the
   prior-Dec `equity_holdings`. Dividends from `corps`; interest from `interests`.

## Statement sections used

| Section | Holds | Use for |
|---|---|---|
| `stock_trades` | each trade (`clear_amount` = net of fees, signed) | realized P&L, fees |
| `equity_holdings` | per-holding `cost_price`, quantities (incl. prior-Dec) | opening cost basis |
| `corps` | corporate actions / cash dividends | dividend income |
| `interests` | monthly financing interest (`total` negative = paid) | interest cost |
| `account_balance_changes` | cash movements (withdrawals, etc.) | cash-flow view |
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

## Tax note (个税 境外所得)

Convert HKD→RMB at the year-end 人民币汇率中间价 (`--rate`). `财产转让所得` (capital gains,
20%) nets gains/losses within the same market across accounts; `利息股息红利所得`
(dividends, flat 20%) is taxed standalone with no deductions. Confirm netting scope,
foreign-tax credit, fund-income classification, and FX 口径 with a tax advisor.
