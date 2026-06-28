# longbridge-statement-skill

*English | [中文](README.zh-CN.md)*

A Claude/agent **skill** + standalone Python tool that pulls **Longbridge (長橋證券)**
account statements via the **official Longbridge CLI** and builds tax-ready CSVs — for
**individual income tax on foreign income (个税 境外所得)**, CRS reporting, and broker P&L
reconciliation.

Companion to **[futu-statement-skill](https://github.com/BH-M87/futu-statement-skill)** (the
other broker). Combine both brokers' realized P&L for the final capital-gains figure.

## Why

Like most brokers, Longbridge statements have:

- **no realized-P&L section** — reconstructed here (average-cost) from `stock_trades`, seeded
  with the prior-December closing holdings so carried-in positions are costed correctly;
- **dividends inside `corps`** (corporate actions), not a "dividend" line;
- **interest in `interests`** — a financing cost you paid, not income.

Method is documented in [`SKILL.md`](SKILL.md).

## Prerequisite: official Longbridge CLI

This tool **shells out to `longbridge`** — it never handles your credentials. **If the CLI
isn't installed, the script offers to install it for you (after you confirm), then offers to
run `longbridge auth login`** — each step asks for y/N first (and falls back to printing the
manual steps if you decline or run it non-interactively). Manual setup:

```bash
# install (pick your platform)
brew install --cask longbridge/tap/longbridge-terminal                                   # macOS
curl -sSL https://open.longbridge.com/longbridge/longbridge-terminal/install | sh        # macOS/Linux
scoop install https://open.longbridge.com/longbridge/longbridge-terminal/longbridge.json # Windows

# authenticate (opens a browser, OAuth)
longbridge auth login

# verify it works
longbridge statement --type monthly --format json
```

If a CLI call fails because you're not logged in, the script points you to
`longbridge auth login`. Docs: https://open.longbridge.com/zh-CN/docs/cli/ . This uses the
**CLI** (not the Longbridge MCP). No third-party Python packages required. Python 3.10+.

## Usage

```bash
python3 longbridge_tax.py --year 2025 -o out_<account>/ --rate 0.90322 --fx-rate USD=7.10
```

`-o` is the per-account base directory; the year is auto-nested as a subfolder, so output
lands in `out_<account>/<year>/` (e.g. `out_H10764613_M/2025/`). Use a distinct `-o` per
account so different accounts and years never overwrite each other. The CLI uses whichever
account its token is bound to (statements carry no account number), so name the `-o` folder
after the account yourself and confirm the CLI is logged into that account.

`--rate` is kept as the shorthand for the HKD→RMB year-end mid-rate (中间价). If your
Longbridge statements include more than one currency, pass one `--fx-rate CCY=RATE` per
currency, such as `--fx-rate HKD=0.90322 --fx-rate USD=7.10`. Amounts are grouped by
currency first; RMB/tax columns are filled only when that currency has a matching rate.

For `--year 2025`, if you do not pass any rate, the script uses the 2025-12-31 PBOC/CFETS
central parity defaults: `HKD=0.90322`, `USD=7.0288`. Passing `--rate` or `--fx-rate`
overrides the built-in defaults.

### Outputs (UTF-8-BOM, Excel-friendly)

| File | Contents |
|---|---|
| `longbridge_<YEAR>_成交明细.csv` | stock / option / fund trades; `清算金额` already net of fees |
| `longbridge_<YEAR>_股息利息现金流.csv` | dividends (corps), financing interest, withdrawals |
| `longbridge_<YEAR>_已实现盈亏_按标的.csv` | realized P&L per instrument (average-cost) |
| `longbridge_<YEAR>_账户净值.csv` | per-month asset total (cross-check) |
| `longbridge_<YEAR>_税务汇总.csv` | tax summary by currency — capital gains / dividends / interest + tax due |

The script prints realized total, dividends and interest so you can sanity-check.

## How realized P&L is computed

Average-cost per instrument and currency. `clear_amount` is signed and already net of fees;
realized P&L accrues only when a trade *reduces* the open position (selling a long, or buying
back a short), so whatever position is still open at year-end — long **or** short — contributes
0 (unrealized, not taxable). The prior-December `equity_holdings.cost_price` seeds carried-in
positions. A position going negative (a cash account can't truly short — this usually means
missing cost basis, e.g. shares transferred in or a sell recorded before its buy) is flagged
for review rather than booking the full sale proceeds as profit. Money-market funds are shown
but excluded from the realized headline.
**Capital-gains tax must be combined across brokers** — the standalone figure here nets
against e.g. Futu within `财产转让所得`. Details in [`SKILL.md`](SKILL.md).

## Use as a Claude Code skill

Drop this folder into your skills directory (e.g. `~/.claude/skills/longbridge-statement-tax/`)
or install via your plugin manager. Claude loads [`SKILL.md`](SKILL.md) when you ask it to
compute Longbridge P&L / dividends / tax.

## Privacy

This repo contains **no personal data**. It pulls live from your authenticated Longbridge
CLI and writes CSVs locally. `.gitignore` blocks `*.csv`/`*.json` so you can't accidentally
commit results.

## License

MIT
