# Examples

Generic, deliberately non-exhaustive. Amounts and tickers are illustrative. The normative
rules are in [`tax-treatment.md`](tax-treatment.md).

## 1. A fund pays a capital-gain distribution (not a dividend)

A leveraged single-stock ETF pays 2.00/share on 500 shares = **1,000** in December, separate
from its quarterly dividend, with no withholding. The broker calls it "Cash Dividend" and its
摊薄 `cost_price` drops by 2.00 — neither is tax characterisation.

```csv
# distributions.csv
date,code,character,amount,currency,source,note
2025-12-10,XYZ,capital_gain,1000,USD,fund 2025 tax supplement,paid 12/17
```

```bash
python3 longbridge_tax.py --year 2025 --distributions distributions.csv
```

Effect: +1,000 to 财产转让所得 in 2025; cost basis unchanged. The tax summary shows a
`其中：资本利得分配(财产转让)` memo line, and `次年成本加回.csv` is written — if the broker
reduced cost for it, run 2026 with `--opening-adjustments`.

## 2. The same cash is a return of capital

A bond ETF distribution is reported by the fund as return of capital (1042-S box 3). It is not
income; it reduces basis and defers into the sale.

```csv
date,code,character,amount,currency,source,note
2025-06-30,BNDX,roc,250,USD,1042-S box 3,
```

Effect: no dividend tax; cost reduced by 250; the eventual sale books 250 more gain. The tax
summary shows a `(备查)资本返还(冲减成本)` line.

## 3. 2-for-1 split

No cash, no trade — the broker just doubles the share count and halves the price.

```csv
# adjustments.csv
date,code,type,amount,currency,character,note
2025-05-01,ABC,quantity,2,USD,,2-for-1 split
```

```bash
python3 longbridge_tax.py --year 2025 --adjustments adjustments.csv
```

Effect: 10 shares at 100 become 20 at 50; a later sale at 60 realizes 200 (not 0/−400 under a
naive reconstruction). `手工调整.csv` records the event.

## 4. Non-cash distribution or merger consideration

A fund distributes securities worth 120 instead of cash, or a merger adds 120 of boot. The
basis increases by 120 (the new securities are not in `stock_trades`).

```csv
date,code,type,amount,currency,character,note
2025-06-01,ABC,cost,120,USD,,non-cash distribution added to basis
```

## 5. Broker reclassification

The broker reclassifies a prior year's item and books ±500 of realized P&L that is not in any
trade. Record it directly, with a note:

```csv
date,code,type,amount,currency,character,note
2025-07-01,ABC,realized,-500,USD,,broker reclassification per support ticket 123
```

## 6. An option sold last year, expired this year

Last December you sold a call; it expires worthless this January. There is no closing trade in
2025 — the premium only becomes income when the contract expires.

No flag needed: the script reads `OptionExpire` / `OptionExercise` from
`equity_holding_changes`, closes the carried-in position at 0, and books the premium in the
expiry year. (Before this behaviour existed the premium stayed unrealized forever.)

## 7. A distribution is ambiguous

Two distributions from the same ticker in one year, and you are not sure which is which. Do not
guess — let the script stop and produce a template:

```bash
python3 longbridge_tax.py --year 2025 --on-unclassified-distribution error
# writes distributions_template.csv and exits
```

Fill `character` from the fund's tax supplement or 1042-S, then rerun with `--distributions`.

## 8. A monthly statement is missing

The run stops and lists the missing months. Only override after checking the gap:

```bash
python3 longbridge_tax.py --year 2024 --allow-missing-months
# ⚠ 缺失月结单: 202405, 202406, 202407 — results will be understated
```

For a real gap, rebuild those months from `longbridge cash-flow` + `order executions
--history` (trade date, not statement month), then merge with `--adjustments`.
