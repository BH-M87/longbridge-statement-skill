# Tax treatment model

This is the normative reference: **what the script models, what it refuses to guess, and how
to extend it without editing code.** Examples live in [`examples.md`](examples.md); they are
illustrations, not an exhaustive catalogue.

> Not tax advice. Confirm character, netting scope, foreign-tax credit and filing deadlines
> with a tax adviser.

## 1. Events, not products

Products are unbounded (stocks, ETFs, leveraged/inverse ETFs, options, money-market funds,
REITs, bonds, structured notes, SPACs, ...). Taxable **events** are not. The script models
these:

| Event | Input | Treatment |
|---|---|---|
| Buy / sell | `stock_trades`, `option_trades`, `fund_trades` | average cost; realized only when the position is reduced |
| Cash distribution | `corps`, `Cash Dividend` balance changes + `--distributions` | character-dependent (below) |
| Option expiry / assignment | `equity_holding_changes` `OptionExpire` / `OptionExercise` | close at 0; premium/loss realized this year; stock leg at strike is in `stock_trades` |
| Fee / interest | trade `clear_amount`, `interests` | fees netted into cost/proceeds; financing interest is a cost, not income |
| IPO allotment | `account_balance_changes` `LIPOALDR` | seed cost basis (no buy trade exists) |
| Withholding | negative `CO Other FEE` / `Withholding` rows | credit against dividend tax |
| Split / merger / non-cash / reclassification | `--adjustments` | one of four effects: `quantity`, `cost`, `realized`, `income` |
| Transfer in/out | not modelled | use `--opening-adjustments` (cost) or `--adjustments`; flag manually |

Anything that cannot be mapped to an event is **out of scope by design** — record it with
`--adjustments` and a note, rather than waiting for a product-specific branch.

## 2. Tax characters

| Character | Chinese item | Rate | Year | Cost basis |
|---|---|---|---|---|
| `dividend` | 利息股息红利所得 | 20 % | received | unchanged |
| `capital_gain` | 财产转让所得 | 20 % | received | **unchanged** |
| `roc` (return of capital) | not income | — | deferred | **reduced**; excess over basis is a gain now |

Why the distinction matters: a fund's year-end **capital-gain distribution** and a **return of
capital** look identical in a broker statement (both are positive cash, both labelled
"dividend"), but one is income now and the other reduces basis. The broker's holding
`cost_price` is 摊薄成本 — it drops on *any* distribution — so a falling cost is **not**
evidence of ROC.

## 3. Data contracts

### `distributions.csv` (`--distributions`)

```csv
date,code,character,amount,currency,source,note
2025-12-10,XYZ,capital_gain,1000,USD,fund 2025 tax supplement,paid 12/17
2025-03-01,ABC,roc,250,USD,1042-S box 3,
```

- `code` and `character` required (`dividend` / `capital_gain` / `roc`; Chinese aliases and
  `cg`/`div`/`return_of_capital` are accepted).
- `date` / `amount` optional matchers. Matching order: `(code, date)` → `(code, amount)` → a
  wildcard only when that ticker distributes once. Ambiguous matches **raise**.
- `source` is free text and is copied to the output for audit (fund tax supplement, 1042-S,
  broker tax report, adviser email). Keep the document.

### `adjustments.csv` (`--adjustments`)

```csv
date,code,type,amount,currency,character,note
2025-05-01,ABC,quantity,2,USD,,2-for-1 split
2025-06-01,ABC,cost,120,USD,,non-cash distribution added to basis
2025-07-01,ABC,realized,-500,USD,,broker reclassification
2025-08-01,ABC,income,30,USD,dividend,special dividend
```

| type | `amount` means | effect |
|---|---|---|
| `quantity` | split factor (2 = 2-for-1, 0.5 = 1-for-2) | `qty *= factor`, unit cost `/= factor`; total cost unchanged |
| `cost` | signed **total** cost delta | `avg = (qty*avg + amount) / qty`; long positions only |
| `realized` | signed realized P&L | added to 财产转让所得 for the year |
| `income` | signed income | `character` = `dividend` (default `capital_gain`) |

Adjustments apply on their date, after that day's trades. A quantity/cost adjustment with no
open position, or a `cost` adjustment on a short, is reported as a diagnostic and skipped.

### `opening-adjustments.csv` (`--opening-adjustments`)

```csv
code,currency,amount,note
XYZ,USD,1000,undo broker cost reduction for a capital-gain distribution
```

Adds `amount` back to the opening cost basis (per unit: `amount / qty`). Use in year N+1 when
the broker reduced cost for a distribution that is taxable as a capital gain, so basis must not
change. The script writes `次年成本加回.csv` for this purpose.

## 4. Decision tree for a cash distribution

1. Is it cash? Non-cash distributions → `--adjustments type=cost` (or a note if immaterial).
2. Is it a return of capital (fund tax supplement / 1042-S box 3, or basis reduction by the
   fund)? → `roc`.
3. Is it a capital-gain distribution (fund 19a-1 / tax supplement, usually a separate December
   ex-date, often no withholding)? → `capital_gain`.
4. Otherwise it is an ordinary dividend → `dividend`.
5. Cannot decide? Leave it out and run with `--on-unclassified-distribution error`; the script
   writes `distributions_template.csv` and stops instead of guessing.

## 5. Cross-year consistency

- **ROC** reduces cost this year, so the sale books a larger gain later. The next year seeds
  from the broker's `cost_price`, which (for ROC) is already reduced — do not apply it twice.
- **Capital gain** must not change cost. If the broker reduced `cost_price` anyway, run the next
  year with `--opening-adjustments <次年成本加回.csv>`.
- **Realized** is attributed by **trade date**, not statement month. The script reads
  `<year+1>01` for 30/31 December trades and drops rows dated outside the year (January's
  statement otherwise carries the prior December's trades).

## 6. Failure modes (fail closed)

| Situation | Behaviour |
|---|---|
| Missing monthly statement | stop, list the months; `--allow-missing-months` to override with a loud warning |
| Distribution not in `--distributions` | default `dividend`, listed as 未定性/待确认; `error` mode stops and writes a template |
| Ambiguous distribution match | raise; ask for `date` or `amount` |
| Negative position on a stock | warn/flag (cash account cannot truly short — usually missing basis) |
| ROC / cost adjustment with no position | diagnostic, skipped |
| Statement key 404 | retry the alternate CDN prefix (`/statement_data/` ↔ `/lb-statement/`) |

## 7. Out of scope (today)

- Automatic retrieval of fund 19a-1 / tax supplements (the character is user-supplied).
- Non-cash distributions valued at fair value, mergers with mixed consideration (use `cost`).
- Short-selling margin interest and stock-borrow fees beyond `interests`.
- Multi-account consolidation and cross-broker netting (run per account, combine manually).
- Cash-flow fallback when statements are missing (the run stops; rebuild from cash-flow by hand).
