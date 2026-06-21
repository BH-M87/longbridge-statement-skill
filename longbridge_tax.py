#!/usr/bin/env python3
"""Pull Longbridge (長橋證券) statements via the official CLI and build tax-ready CSVs.

Companion to futu-statement-skill, for the other broker. Longbridge has an official
command-line tool (`longbridge`, see https://open.longbridge.com/zh-CN/skill/); this
script calls `longbridge statement` to fetch the monthly statements as JSON and computes
the figures you need for individual income tax on foreign income (个税 境外所得), CRS,
or P&L reconciliation.

Like Futu, Longbridge statements have **no "已实现盈亏" (realized P&L) section** — it is
reconstructed here from `stock_trades` (average-cost), seeded with the prior-December
closing holdings (`equity_holdings.cost_price`) so positions carried into the year are
costed correctly. Cash dividends live in `corps` (corporate actions); financing interest
in `interests` (it is a cost you paid, not income).

Usage:
    python3 longbridge_tax.py --year 2025 -o OUTDIR [--rate 0.90322]

Prerequisite: the official Longbridge CLI installed and authenticated, so that
`longbridge statement --type monthly --format json` works in your shell. This script
never handles credentials — it shells out to `longbridge`. See README.

Outputs (CSV, utf-8-sig; no personal data is embedded — everything comes from your account):
    longbridge_<YEAR>_成交明细.csv          stock/option/fund trades (clear_amount = net of fees)
    longbridge_<YEAR>_股息利息现金流.csv      dividends(corps)/interest/withdrawals
    longbridge_<YEAR>_已实现盈亏_按标的.csv    realized P&L per instrument (average-cost)
    longbridge_<YEAR>_账户净值.csv           per-month asset total (cross-check)
    longbridge_<YEAR>_税务汇总.csv           tax summary: gains/dividends/interest + tax due (--rate)
"""
from __future__ import annotations
import argparse, csv, json, os, subprocess, sys

# account_balance_changes biz_codes that are internal/round-trip, excluded from cashflow view
INTERNAL_BIZ = {"LMMFP", "LMMFR", "LIPOREFD", "LIPODR", "LINTDR"}
DIV_HINTS = ("股息", "分红", "dividend", "div", "i/d", "f/d")   # to spot dividends anywhere


def d(s):                                   # '2025.01.07' -> '2025/01/07'
    return (s or "").replace(".", "/")


def num(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


INSTALL_HELP = """\
✗ Longbridge CLI ('longbridge') not found. This skill drives the official CLI — install it:

  • macOS (Homebrew):
      brew install --cask longbridge/tap/longbridge-terminal
  • macOS / Linux (script):
      curl -sSL https://open.longbridge.com/longbridge/longbridge-terminal/install | sh
  • Windows (Scoop):
      scoop install https://open.longbridge.com/longbridge/longbridge-terminal/longbridge.json

Then log in (opens a browser for OAuth):
      longbridge auth login

Verify it works, then re-run this script:
      longbridge statement --type monthly --format json

Docs: https://open.longbridge.com/zh-CN/skill/"""

AUTH_HINT = ("\n\n→ This looks like an authentication problem. Log in and retry:\n"
             "      longbridge auth login\n"
             "  Docs: https://open.longbridge.com/zh-CN/skill/")
_AUTH_KEYS = ("auth", "login", "token", "unauthor", "credential", "401", "403",
              "登录", "登錄", "认证", "認證", "授权", "授權", "未登录", "未登錄")


def lb_json(args):
    """Run `longbridge ... --format json` and parse stdout; guide install/auth on failure."""
    try:
        out = subprocess.run(["longbridge", *args, "--format", "json"],
                             capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit(INSTALL_HELP)                       # CLI not installed -> full install guide
    if out.returncode != 0:
        err = (out.stderr or out.stdout).strip()
        hint = AUTH_HINT if any(k in err.lower() for k in _AUTH_KEYS) else ""
        sys.exit(f"`longbridge {' '.join(args)}` failed:\n{err}{hint}")
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        sys.exit(f"could not parse JSON from `longbridge {' '.join(args)}`:\n{out.stdout[:400]}")


def list_keys(year):
    """Return {YYYYMM: file_key} for the year's 12 months + the prior December."""
    rows = lb_json(["statement", "--type", "monthly",
                    "--start-date", f"{year-1}-12-01", "--limit", "14"])
    want = {f"{year}{m:02d}" for m in range(1, 13)} | {f"{year-1}12"}
    return {r["date"]: r["file_key"] for r in rows if r["date"] in want}


def export(file_key):
    return lb_json(["statement", "export", "--file-key", file_key])


def realized_by_ticker(trades, opening):
    """Average-cost realized P&L per instrument (long-side; Longbridge cash account).

    `net` is clear_amount (signed: BUY<0 cash out, SELL>0 cash in), already net of fees.
    Realized accrues only on SELLs, so positions still held at year-end contribute 0
    (their gain is unrealized and not taxable this year). `opening` seeds carried-in
    holdings from the prior-December statement so their cost basis is correct.
    """
    book = {}
    for code, (qty, cp, name) in opening.items():
        book[code] = {"name": name, "pos": qty, "cost": qty * cp,
                      "realized": 0.0, "nf": 0, "sumnet": 0.0}
    for t in sorted(trades, key=lambda r: r[0]):        # by trade_date
        if t[5] not in ("stock", "option"):             # funds handled separately
            continue
        code, name, net, q = t[2], t[3], t[11], t[7]
        b = book.setdefault(code, {"name": "", "pos": 0.0, "cost": 0.0,
                                   "realized": 0.0, "nf": 0, "sumnet": 0.0})
        if name and not b["name"]:
            b["name"] = name
        b["nf"] += 1; b["sumnet"] += net
        if net < 0:                                     # BUY -> add to cost basis
            b["cost"] += -net; b["pos"] += q
        else:                                           # SELL -> realize vs average cost
            avg = b["cost"] / b["pos"] if b["pos"] > 1e-9 else 0.0
            b["realized"] += net - avg * q
            b["cost"] -= avg * q; b["pos"] -= q
    return book


def main(argv=None):
    ap = argparse.ArgumentParser(description="Longbridge statements -> tax CSVs (via official CLI)")
    ap.add_argument("--year", type=int, required=True, help="tax year, e.g. 2025")
    ap.add_argument("-o", "--outdir", default="longbridge_parsed", help="output directory")
    ap.add_argument("--rate", type=float, default=None,
                    help="HKD->RMB rate (e.g. year-end 中间价 0.90322); adds an RMB column")
    args = ap.parse_args(argv)
    year, rate = args.year, args.rate
    os.makedirs(args.outdir, exist_ok=True)

    keys = list_keys(year)
    if not keys:
        sys.exit(f"no monthly statements found for {year} — is the Longbridge CLI authenticated?")

    trades, cashflows, navrows = [], [], []
    opening = {}                                         # code -> (qty, cost_price, name)

    # prior-December closing holdings -> opening cost basis
    prior = keys.get(f"{year-1}12")
    if prior:
        for h in export(prior).get("equity_holdings", []):
            cp = h.get("cost_price")
            qty = num(h.get("ledger_quantity"))
            if cp in (None, "", "N/A") or qty <= 0:      # skip funds (cost N/A) & flat lines
                continue
            opening[str(h["code"])] = (qty, num(cp), h.get("name", ""))

    for m in range(1, 13):
        ym = f"{year}{m:02d}"
        if ym not in keys:
            continue
        st = export(keys[ym])
        for t in st.get("stock_trades", []):
            net = num(t["clear_amount"]); gross = num(t["trade_amount"])
            fee = round(abs(net) - gross if t["direction"] == "BUY" else gross - net, 2)
            trades.append([d(t["trade_date"]), d(t.get("settle_date")), str(t["code"]), t.get("name", ""),
                           "买入" if t["direction"] == "BUY" else "卖出", "stock", t.get("currency", ""),
                           num(t["trade_quantity"]), num(t["trade_price"]), gross, fee, net])
        for t in st.get("option_trades", []):           # empty in 2025; future-proof
            net = num(t.get("clear_amount")); gross = num(t.get("trade_amount"))
            fee = round(abs(abs(net) - gross), 2)
            trades.append([d(t.get("trade_date")), d(t.get("settle_date")), str(t.get("code")), t.get("name", ""),
                           "买入" if num(t.get("clear_amount")) < 0 else "卖出", "option", t.get("currency", ""),
                           num(t.get("trade_quantity")), num(t.get("trade_price")), gross, fee, net])
        for f in st.get("fund_trades", []):
            amt = num(f["trade_amount"])
            dirn = {"1": "申购", "2": "赎回"}.get(str(f.get("direction")), str(f.get("direction")))
            net = -amt if dirn == "申购" else amt
            trades.append([d(f.get("confirm_date") or f.get("order_date")), "", str(f["code"]), f.get("name", ""),
                           dirn, "fund", f.get("currency", ""), num(f["trade_quantity"]), num(f.get("price")),
                           amt, 0.0, net])
        # cashflows: dividends (corps) + interest (interests) + meaningful balance changes
        for c in st.get("corps", []):
            cashflows.append([d(c.get("date") or c.get("ex_date") or ym), "分红/公司行动",
                              c.get("currency", ""), num(c.get("amount") or c.get("net_amount")),
                              c.get("remark") or c.get("name") or json.dumps(c, ensure_ascii=False)])
        for it in st.get("interests", []):
            cashflows.append([d(it.get("date") or ym), "融资利息", it.get("currency", ""),
                              num(it.get("total")), f"利率 {it.get('rate', '')}"])
        for a in st.get("account_balance_changes", []):
            bc = str(a.get("biz_code", ""))
            remark = str(a.get("remark", "")); typ = str(a.get("type", ""))
            is_div = any(h in (remark + typ).lower() for h in DIV_HINTS)
            if bc in INTERNAL_BIZ and not is_div:
                continue
            cashflows.append([d(a.get("date")), ("分红" if is_div else typ) or bc,
                              a.get("currency", ""), num(a.get("amount")), remark])
        for as_ in st.get("asset", []):
            navrows.append([ym, as_.get("currency", ""), num(as_.get("total"))])

    def w(name, header, rows):
        with open(os.path.join(args.outdir, name), "w", newline="", encoding="utf-8-sig") as fp:
            wr = csv.writer(fp); wr.writerow(header); [wr.writerow(r) for r in rows]

    def rmb(v): return round(v * rate, 2) if rate else ""

    # 1) 成交明细
    w(f"longbridge_{year}_成交明细.csv",
      ["成交日期", "交收日期", "代码", "名称", "方向", "品类", "货币", "数量", "价格", "成交金额", "手续费", "清算金额(净额)"],
      sorted(trades) + [["合计", "", "", "", "", "", "", "", "", "", "",
                         round(sum(t[11] for t in trades), 2)]])

    # 2) 股息利息现金流
    w(f"longbridge_{year}_股息利息现金流.csv", ["日期", "类型", "货币", "金额", "备注"], sorted(cashflows))

    # 3) 已实现盈亏 按标的
    book = realized_by_ticker(trades, opening)
    rows, total = [], 0.0
    for code in sorted(book, key=lambda c: book[c]["realized"]):
        b = book[code]
        if b["nf"] == 0 and abs(b["realized"]) < 1e-6:   # untouched carried holding -> skip
            continue
        rz = round(b["realized"], 2); total += rz
        held = b["pos"] > 1e-6
        row = [code, b["name"], b["nf"], round(b["sumnet"], 2), rz]
        if rate is not None:
            row.append(rmb(rz))
        row.append("年末仍有持仓,未实现不计入" if held else "")
        rows.append(row)
    h3 = ["代码", "名称", "成交笔数", "清算净额合计(HKD)", "已实现盈亏(HKD)"]
    if rate is not None:
        h3.append(f"已实现盈亏(RMB,×{rate})")
    h3.append("备注")
    tot3 = ["合计", "", "", "", round(total, 2)] + ([rmb(total)] if rate else []) + ["股票/期权已实现;货基另计,未并入"]
    w(f"longbridge_{year}_已实现盈亏_按标的.csv", h3, rows + [tot3])

    # 4) 账户净值
    w(f"longbridge_{year}_账户净值.csv", ["月份", "货币", "资产总值(月末)"], navrows)

    # 5) 税务汇总
    div = sum(c[3] for c in cashflows if "分红" in c[1] and c[3] > 0)
    interest = sum(c[3] for c in cashflows if "利息" in c[1])
    r = rate or 0
    div_tax = round(div * r * 0.20, 2) if rate else ""
    cap_tax = (round(total * r * 0.20, 2) if (rate and total > 0) else (0.0 if rate else ""))
    th = ["所得项目", "金额(HKD)"] + (["金额(RMB)", "应纳税额(RMB)"] if rate else []) + ["税率", "备注"]
    trows = [
        ["财产转让所得·已实现(本账户股票/期权)", round(total, 2)] + ([rmb(total), cap_tax] if rate else [])
        + ["20%", "盈利才计税且需与其他账户同类所得盈亏合并;本表仅本账户,亏损不计税"],
        ["利息股息红利所得·现金分红(毛额)", round(div, 2)] + ([rmb(div), div_tax] if rate else [])
        + ["20%", "单独计税,不可扣成本/不可与亏损相抵;境外已预扣可申请抵免"],
        ["(备查)融资利息支出", round(interest, 2)] + ([rmb(interest), ""] if rate else [])
        + ["—", "非收入;做财产转让可作合理费用参考"],
    ]
    if rate:
        trows.append(["合计·本账户应纳税额(估)", "", "", round((div_tax or 0) + (cap_tax or 0), 2), "",
                      "= 分红税 + 财产转让税(本账户);财产转让最终税额须合并其他账户后确定"])
    else:
        trows.append(["提示", "传 --rate <年末中间价> 可计算人民币与应纳税额"])
    w(f"longbridge_{year}_税务汇总.csv", th, trows)

    print(f"Longbridge {year} -> {args.outdir}/")
    print(f"  成交明细:        {len(trades)} 笔 (股票/期权/基金), Σ清算净额={sum(t[11] for t in trades):,.2f}")
    print(f"  股息利息现金流:  {len(cashflows)} 行")
    print(f"  已实现盈亏(股票): Σ={total:,.2f} HKD" + (f" = RMB {total*rate:,.2f}" if rate else ""))
    print(f"  分红(corps+):    {div:,.2f} HKD ;  融资利息: {interest:,.2f} HKD")
    if rate:
        print(f"  税务汇总:        分红税 RMB {div_tax:,.2f}"
              + (f" + 财产转让税 RMB {cap_tax:,.2f}" if total > 0 else " (财产转让本账户亏损/无盈利,不计税)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
