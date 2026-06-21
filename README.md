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
isn't installed, the script stops and prints these exact steps** (it won't auto-install):

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
`longbridge auth login`. Docs: https://open.longbridge.com/zh-CN/skill/ . This uses the
**CLI** (not the Longbridge MCP). No third-party Python packages required. Python 3.10+.

## Usage

```bash
python3 longbridge_tax.py --year 2025 -o out/ --rate 0.90322
```

`--rate` is optional — the HKD→RMB year-end mid-rate (中间价); when given, an RMB column is
added and tax is computed.

### Outputs (UTF-8-BOM, Excel-friendly)

| File | Contents |
|---|---|
| `longbridge_<YEAR>_成交明细.csv` | stock / option / fund trades; `清算金额` already net of fees |
| `longbridge_<YEAR>_股息利息现金流.csv` | dividends (corps), financing interest, withdrawals |
| `longbridge_<YEAR>_已实现盈亏_按标的.csv` | realized P&L per instrument (average-cost) |
| `longbridge_<YEAR>_账户净值.csv` | per-month asset total (cross-check) |
| `longbridge_<YEAR>_税务汇总.csv` | tax summary — capital gains / dividends / interest + tax due |

The script prints realized total, dividends and interest so you can sanity-check.

## How realized P&L is computed

Average-cost per instrument (long side, Longbridge cash account). `clear_amount` is signed
and already net of fees; realized accrues only on SELLs, so positions still held at year-end
contribute 0 (unrealized, not taxable). The prior-December `equity_holdings.cost_price` seeds
carried-in positions. Money-market funds are shown but excluded from the realized headline.
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
