#!/usr/bin/env python3
"""Pull Longbridge (長橋證券) statements via the official CLI and build tax-ready CSVs.

Companion to futu-statement-skill, for the other broker. Longbridge has an official
command-line tool (`longbridge`, see https://open.longbridge.com/zh-CN/docs/cli/); this
script calls `longbridge statement` to fetch the monthly statements as JSON and computes
the figures you need for individual income tax on foreign income (个税 境外所得), CRS,
or P&L reconciliation.

Like Futu, Longbridge statements have **no "已实现盈亏" (realized P&L) section** — it is
reconstructed here from `stock_trades` (average-cost), seeded with the prior-December
closing holdings (`equity_holdings.cost_price`) so positions carried into the year are
costed correctly. Cash dividends live in `corps` (corporate actions); financing interest
in `interests` (it is a cost you paid, not income).

Usage:
    python3 longbridge_tax.py --year 2025 [-o OUTDIR] [--rate 0.90322] [--fx-rate USD=7.1]

Prerequisite: the official Longbridge CLI installed and authenticated, so that
`longbridge statement --type monthly --format json` works in your shell. This script
never handles credentials — it shells out to `longbridge`. See README.

Outputs (CSV, utf-8-sig; no personal data is embedded — everything comes from your account):
    longbridge_<YEAR>_成交明细.csv          stock/option/fund trades (clear_amount = net of fees)
    longbridge_<YEAR>_股息利息现金流.csv      dividends(corps)/interest/withdrawals
    longbridge_<YEAR>_已实现盈亏_按标的.csv    realized P&L per instrument (average-cost)
    longbridge_<YEAR>_账户净值.csv           per-month asset total (cross-check)
    longbridge_<YEAR>_税务汇总.csv           tax summary: gains/dividends/interest + tax due (--rate/--fx-rate)
"""
from __future__ import annotations
import argparse, csv, json, os, re, shutil, subprocess, sys
from collections import defaultdict

# account_balance_changes biz_codes that are internal/round-trip, excluded from cashflow view.
# LIPOALDR (IPO allotment debit) is the all-in cost of 打新中签 shares — seeded as cost basis
# (see ipo_allotment), so it is hidden from the cashflow view rather than shown as cash out.
INTERNAL_BIZ = {"LMMFP", "LMMFR", "LIPOREFD", "LIPODR", "LIPOALDR", "LINTDR"}
DIV_HINTS = ("股息", "分红", "dividend", "div", "i/d", "f/d")   # to spot dividends anywhere

# market suffix -> settlement currency, for statement lines that omit `currency`
MARKET_CCY = {"HK": "HKD", "US": "USD", "SG": "SGD", "SH": "CNY", "SZ": "CNY"}
# e.g. "IPO  6831.HK Allotted Amount (400 Shares @HKD 2,876.00)" -> code/market/qty
_IPO_ALLOT_RE = re.compile(r"([0-9A-Za-z]+)\.([A-Za-z]{2}).*?\(([\d,]+)\s*Shares", re.I)

# PBOC/CFETS RMB central parity rates on the tax year's final calendar day.
# Source for 2025: https://www.pbc.gov.cn/zhengcehuobisi/125207/125217/125925/2025123109021714424/index.html
DEFAULT_FX_RATES_BY_YEAR = {
    2025: {
        "HKD": 0.90322,
        "USD": 7.0288,
    },
}


def d(s):                                   # '2025.01.07' -> '2025/01/07'
    return (s or "").replace(".", "/")


def num(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def ipo_allotment(change):
    """An 'IPO Allotted Amount(Dr)' balance change -> (currency, code, qty, total_cost) or None.

    IPO-allotted (打新中签) shares arrive via account_balance_changes (biz_code LIPOALDR),
    NOT stock_trades, and carry no buy record. Without seeding their cost here, a later sale
    of them reconstructs as a zero-cost short and overstates the gain. `amount` is the all-in
    debited cost (subscription + fee); code / quantity / currency come from the remark, e.g.
    "IPO  6831.HK Allotted Amount (400 Shares @HKD 2,876.00)".
    """
    if str(change.get("biz_code", "")) != "LIPOALDR":
        return None
    remark = str(change.get("remark", ""))
    m = _IPO_ALLOT_RE.search(remark)
    if not m:
        return None
    code, market, qty = m.group(1), m.group(2).upper(), num(m.group(3))
    if qty <= 0:
        return None
    cm = re.search(r"@\s*([A-Za-z]{3})", remark)
    currency = ((change.get("currency") or "") or (cm.group(1) if cm else "")
                or MARKET_CCY.get(market, "")).upper()
    return currency, code, qty, abs(num(change.get("amount")))


def parse_fx_rates(year, rate, fx_rate_args):
    rates = dict(DEFAULT_FX_RATES_BY_YEAR.get(year, {})) if rate is None and not fx_rate_args else {}
    if rate is not None:
        rates["HKD"] = rate
    for item in fx_rate_args or []:
        if "=" not in item:
            raise argparse.ArgumentTypeError("--fx-rate must be in CCY=RATE format, e.g. USD=7.1")
        ccy, raw_rate = item.split("=", 1)
        ccy = ccy.strip().upper()
        if not ccy:
            raise argparse.ArgumentTypeError("--fx-rate currency cannot be empty")
        try:
            rates[ccy] = float(raw_rate)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"--fx-rate has invalid rate: {item}") from exc
    return rates


def sums_by_currency(rows, amount_index, currency_index):
    totals = defaultdict(float)
    for row in rows:
        totals[(row[currency_index] or "").upper()] += row[amount_index]
    return dict(totals)


def amount_to_rmb(value, currency, fx_rates):
    rate = fx_rates.get((currency or "").upper())
    return round(value * rate, 2) if rate is not None else ""


CLI_DOCS = "https://open.longbridge.com/zh-CN/docs/cli/"
INSTALL = {
    "brew": "brew install --cask longbridge/tap/longbridge-terminal",
    "curl": "curl -sSL https://open.longbridge.com/longbridge/longbridge-terminal/install | sh",
    "scoop": "scoop install https://open.longbridge.com/longbridge/longbridge-terminal/longbridge.json",
}
_AUTH_KEYS = ("auth", "login", "token", "unauthor", "credential", "401", "403",
              "登录", "登錄", "认证", "認證", "授权", "授權", "未登录", "未登錄")
_auth_retried = False


def _confirm(prompt):
    """Ask y/N; False (no action) when not interactive, so automation never hangs."""
    if not sys.stdin.isatty():
        return False
    try:
        return input(prompt).strip().lower() in ("y", "yes", "是")
    except EOFError:
        return False


def _install_guide():
    print(f"""✗ 未检测到 Longbridge CLI（'longbridge'）。本工具依赖官方 CLI。
手动安装（任选其一）：
  • macOS:        {INSTALL['brew']}
  • macOS/Linux:  {INSTALL['curl']}
  • Windows:      {INSTALL['scoop']}
随后登录：longbridge auth login
文档：{CLI_DOCS}""", file=sys.stderr)


def _run(cmd, shell=False):
    print(f"  → 执行: {cmd if isinstance(cmd, str) else ' '.join(cmd)}", file=sys.stderr)
    return subprocess.run(cmd, shell=shell).returncode


def _login(force=False):
    q = ("是否现在登录授权？将运行 `longbridge auth login` 并打开浏览器。[y/N] " if force
         else "检测到未登录/授权失效。是否现在运行 `longbridge auth login`（打开浏览器）？[y/N] ")
    if _confirm("\n" + q):
        _run(["longbridge", "auth", "login"])
        return True
    return False


def ensure_cli():
    """If the CLI is missing, offer to install it (asks first); then offer to log in."""
    if shutil.which("longbridge"):
        return
    _install_guide()
    if not _confirm("\n是否现在帮你自动安装？（需要你确认）[y/N] "):
        sys.exit("已取消。请手动安装后重试。")
    if sys.platform == "darwin":
        cmd = INSTALL["brew"] if shutil.which("brew") else INSTALL["curl"]
    elif sys.platform.startswith("linux"):
        cmd = INSTALL["curl"]
    else:
        sys.exit(f"Windows 请手动安装：{INSTALL['scoop']}")
    print(f"\n即将执行安装命令：\n  {cmd}", file=sys.stderr)
    if not _confirm("确认执行？[y/N] "):
        sys.exit("已取消。")
    _run(cmd, shell=True)
    if not shutil.which("longbridge"):
        sys.exit("安装似乎未完成（PATH 里仍找不到 longbridge）。请重开终端或手动安装后重试。")
    print("✓ Longbridge CLI 已安装。", file=sys.stderr)
    _login(force=True)


def lb_json(args):
    """Run `longbridge ... --format json`; on auth failure, offer to log in and retry once."""
    global _auth_retried
    try:
        out = subprocess.run(["longbridge", *args, "--format", "json"], capture_output=True, text=True)
    except FileNotFoundError:
        _install_guide(); sys.exit(1)
    if out.returncode != 0:
        err = (out.stderr or out.stdout).strip()
        is_auth = any(k in err.lower() for k in _AUTH_KEYS)
        if is_auth and not _auth_retried:
            _auth_retried = True
            print(f"`longbridge {' '.join(args)}` 失败（疑似未登录）:\n{err}", file=sys.stderr)
            if _login():
                return lb_json(args)                 # retry once after login
        hint = (f"\n\n→ 疑似认证问题，请登录后重试: longbridge auth login\n  文档: {CLI_DOCS}"
                if is_auth else "")
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
    """Average-cost realized P&L per instrument (long *and* short side).

    `net` is clear_amount (signed: BUY<0 cash out, SELL>0 cash in), already net of fees.
    `pos` is the signed open quantity (long>0, short<0) and `avg` the average price per
    unit of that open position. Realized P&L accrues only when a trade *reduces* the open
    position (selling a long, or buying back a short); opening or adding to a position just
    re-averages the cost. Whatever position is still open at year-end (long OR short)
    contributes 0 — its gain is unrealized and not taxable this year. `opening` seeds
    carried-in holdings from the prior-December statement so their cost basis is correct.

    NB: a Longbridge cash account cannot truly short, so a position going negative usually
    signals *missing cost basis* (e.g. shares transferred in, or a sell recorded before its
    buy), not a real short. We flag those (`shorted`) so they can be reviewed rather than
    silently booking the whole sale proceeds as profit (the old long-only bug).
    """
    book = {}
    for raw_key, (qty, cp, name) in opening.items():
        if isinstance(raw_key, tuple):
            currency, code = raw_key
        else:
            currency, code = "", raw_key
        key = ((currency or "").upper(), str(code))
        book[key] = {"name": name, "currency": key[0], "pos": qty, "avg": cp,
                     "realized": 0.0, "nf": 0, "sumnet": 0.0, "shorted": False}
    # by trade_date; within a day, opens (买入) before disposals (卖出) so same-day round-trips
    # don't reconstruct as phantom shorts (statements carry no intraday execution time)
    for t in sorted(trades, key=lambda r: (r[0], 0 if r[4] == "买入" else 1)):
        if t[5] not in ("stock", "option"):             # funds handled separately
            continue
        currency, code, name, net, q = (t[6] or "").upper(), t[2], t[3], t[11], t[7]
        if q <= 0:                                       # nothing to do with a 0-qty line
            continue
        key = (currency, code)
        if key not in book and ("", code) in book:
            book[key] = book.pop(("", code))
            book[key]["currency"] = currency
        b = book.setdefault(key, {"name": "", "currency": currency, "pos": 0.0, "avg": 0.0,
                                  "realized": 0.0, "nf": 0, "sumnet": 0.0, "shorted": False})
        if name and not b["name"]:
            b["name"] = name
        b["nf"] += 1; b["sumnet"] += net
        dq = q if net < 0 else -q                        # signed trade qty (BUY>0, SELL<0)
        price = abs(net) / q                             # per-unit cash, fees included
        pos = b["pos"]
        if pos == 0 or (pos > 0) == (dq > 0):            # opening / adding -> re-average
            b["avg"] = (abs(pos) * b["avg"] + q * price) / (abs(pos) + q)
            b["pos"] = pos + dq
        else:                                            # reducing / closing the position
            closed = min(q, abs(pos))
            # long closed by a sell: (sell - avg); short closed by a buy: (avg - buy)
            b["realized"] += (price - b["avg"]) * closed if pos > 0 else (b["avg"] - price) * closed
            b["pos"] = pos + dq
            if q > abs(pos):                             # flipped through zero -> new leg
                b["avg"] = price
        if b["pos"] < -1e-9:                             # went/stayed net short
            b["shorted"] = True
    return book


def main(argv=None):
    ap = argparse.ArgumentParser(description="Longbridge statements -> tax CSVs (via official CLI)")
    ap.add_argument("--year", type=int, required=True, help="tax year, e.g. 2025")
    ap.add_argument("-o", "--outdir", default="out",
                    help="base output directory (default: out); the year is auto-nested as a "
                         "subfolder, e.g. default -> out/<year>/. The Longbridge account number "
                         "is NOT retrievable from the API/statements (only known at CLI login), so "
                         "do not try to name this after the account. If you juggle multiple accounts, "
                         "pass a distinct -o per account yourself, e.g. -o out_acctA -> out_acctA/<year>/")
    ap.add_argument("--rate", type=float, default=None,
                    help="HKD->RMB rate shorthand, same as --fx-rate HKD=RATE")
    ap.add_argument("--fx-rate", action="append", default=[], metavar="CCY=RATE",
                    help="currency-specific RMB FX rate, e.g. HKD=0.90322 or USD=7.10; may repeat; "
                         "defaults to built-in year-end rates when available")
    ap.add_argument("--on-negative-position", choices=["flag", "exclude", "short"], default="flag",
                    help="how to treat instruments whose position goes negative (on a cash account "
                         "this usually means MISSING cost basis, not a real short): "
                         "'flag' (default) = compute & include in totals but warn; "
                         "'exclude' = drop from totals/tax pending manual review; "
                         "'short' = treat as a confirmed genuine short (compute, include, no warning)")
    args = ap.parse_args(argv)
    try:
        fx_rates = parse_fx_rates(args.year, args.rate, args.fx_rate)
    except argparse.ArgumentTypeError as exc:
        ap.error(str(exc))
    year = args.year
    # Nest each year in its own subfolder so different years never overwrite each other:
    # out/<year>/ by default (or <-o>/<year>/). Skip if the path already ends in the year
    # (e.g. -o out/2025) to avoid .../2025/2025.
    outdir = args.outdir
    if os.path.basename(os.path.normpath(outdir)) != str(year):
        outdir = os.path.join(outdir, str(year))
    os.makedirs(outdir, exist_ok=True)

    ensure_cli()                                     # offer to install/login if missing (asks first)
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
            opening[((h.get("currency") or "").upper(), str(h["code"]))] = (qty, num(cp), h.get("name", ""))

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
        # IPO-allotted shares -> seed opening cost basis (they have no buy in stock_trades)
        for a in st.get("account_balance_changes", []):
            al = ipo_allotment(a)
            if not al:
                continue
            currency, code, qty, cost = al
            key = (currency, code)
            if key in opening:
                q0, cp0, nm0 = opening[key]
                nq = q0 + qty
                opening[key] = (nq, (q0 * cp0 + cost) / nq, nm0)
            else:
                opening[key] = (qty, cost / qty, "")
        for as_ in st.get("asset", []):
            navrows.append([ym, as_.get("currency", ""), num(as_.get("total"))])

    def w(name, header, rows):
        with open(os.path.join(outdir, name), "w", newline="", encoding="utf-8-sig") as fp:
            wr = csv.writer(fp); wr.writerow(header); [wr.writerow(r) for r in rows]

    def rmb(v, currency): return amount_to_rmb(v, currency, fx_rates)

    # 1) 成交明细
    trade_total_rows = [["合计", "", "", "", "", "", ccy, "", "", "", "", round(total, 2)]
                        for ccy, total in sorted(sums_by_currency(trades, 11, 6).items())]
    w(f"longbridge_{year}_成交明细.csv",
      ["成交日期", "交收日期", "代码", "名称", "方向", "品类", "货币", "数量", "价格", "成交金额", "手续费", "清算金额(净额)"],
      sorted(trades) + trade_total_rows)

    # 2) 股息利息现金流
    w(f"longbridge_{year}_股息利息现金流.csv", ["日期", "类型", "货币", "金额", "备注"], sorted(cashflows))

    # 3) 已实现盈亏 按标的
    book = realized_by_ticker(trades, opening)
    rows, totals = [], defaultdict(float)
    for key in sorted(book, key=lambda c: (c[0], book[c]["realized"])):
        currency, code = key
        b = book[key]
        if b["nf"] == 0 and abs(b["realized"]) < 1e-6:   # untouched carried holding -> skip
            continue
        rz = round(b["realized"], 2)
        excluded = b["shorted"] and args.on_negative_position == "exclude"
        if not excluded:
            totals[currency] += rz
        held = abs(b["pos"]) > 1e-6
        notes = []
        if held:
            notes.append("年末仍有持仓(空头),未实现不计入" if b["pos"] < 0 else "年末仍有持仓,未实现不计入")
        if b["shorted"]:
            if args.on_negative_position == "short":
                notes.append("做空(已确认),已计入")
            elif excluded:
                notes.append("⚠ 负持仓:已从合计/税务中排除,待核对成本基础")
            else:
                notes.append("⚠ 出现负持仓:疑似缺少成本基础(转入/卖出早于买入),请核对")
        row = [code, b["name"], currency, b["nf"], round(b["sumnet"], 2), rz]
        if fx_rates:
            row.append(rmb(rz, currency))
        row.append(";".join(notes))
        rows.append(row)
    h3 = ["代码", "名称", "货币", "成交笔数", "清算净额合计(原币)", "已实现盈亏(原币)"]
    if fx_rates:
        h3.append("已实现盈亏(RMB)")
    h3.append("备注")
    total_rows = []
    for currency, total in sorted(totals.items()):
        total_rows.append(["合计", "", currency, "", "", round(total, 2)]
                          + ([rmb(total, currency)] if fx_rates else [])
                          + ["股票/期权已实现;货基另计,未并入"])
    w(f"longbridge_{year}_已实现盈亏_按标的.csv", h3, rows + total_rows)

    # 4) 账户净值
    w(f"longbridge_{year}_账户净值.csv", ["月份", "货币", "资产总值(月末)"], navrows)

    # 5) 税务汇总
    divs = defaultdict(float)
    interests = defaultdict(float)
    for c in cashflows:
        currency = (c[2] or "").upper()
        if "分红" in c[1] and c[3] > 0:
            divs[currency] += c[3]
        if "利息" in c[1]:
            interests[currency] += c[3]

    th = ["所得项目", "货币", "金额(原币)"] + (["金额(RMB)", "应纳税额(RMB)"] if fx_rates else []) + ["税率", "备注"]
    trows, tax_total_rmb = [], 0.0
    currencies = sorted(set(totals) | set(divs) | set(interests))
    for currency in currencies:
        total = round(totals.get(currency, 0.0), 2)
        div = round(divs.get(currency, 0.0), 2)
        interest = round(interests.get(currency, 0.0), 2)
        cap_rmb = rmb(total, currency)
        div_rmb = rmb(div, currency)
        interest_rmb = rmb(interest, currency)
        cap_tax = round(cap_rmb * 0.20, 2) if cap_rmb != "" and total > 0 else (0.0 if cap_rmb != "" else "")
        div_tax = round(div_rmb * 0.20, 2) if div_rmb != "" else ""
        if isinstance(cap_tax, float):
            tax_total_rmb += cap_tax
        if isinstance(div_tax, float):
            tax_total_rmb += div_tax
        trows.append(["财产转让所得·已实现(本账户股票/期权)", currency, total]
                     + ([cap_rmb, cap_tax] if fx_rates else [])
                     + ["20%", "盈利才计税且需与其他账户同类所得盈亏合并;本表仅本账户,亏损不计税"])
        trows.append(["利息股息红利所得·现金分红(毛额)", currency, div]
                     + ([div_rmb, div_tax] if fx_rates else [])
                     + ["20%", "单独计税,不可扣成本/不可与亏损相抵;境外已预扣可申请抵免"])
        trows.append(["(备查)融资利息支出", currency, interest]
                     + ([interest_rmb, ""] if fx_rates else [])
                     + ["—", "非收入;做财产转让可作合理费用参考"])
    if fx_rates:
        trows.append(["合计·本账户应纳税额(估)", "", "", "", round(tax_total_rmb, 2), "",
                      "= 分红税 + 财产转让税(本账户);财产转让最终税额须合并其他账户后确定"])
    else:
        trows.append(["提示", "", "传 --rate <HKD年末中间价> 或 --fx-rate <币种=年末中间价> 可计算人民币与应纳税额"])
    w(f"longbridge_{year}_税务汇总.csv", th, trows)

    print(f"Longbridge {year} -> {outdir}/")
    trade_summary = ", ".join(f"{ccy or 'UNKNOWN'} {total:,.2f}"
                              for ccy, total in sorted(sums_by_currency(trades, 11, 6).items()))
    print(f"  成交明细:        {len(trades)} 笔 (股票/期权/基金), Σ清算净额={trade_summary}")
    print(f"  股息利息现金流:  {len(cashflows)} 行")
    realized_summary = ", ".join(f"{ccy or 'UNKNOWN'} {total:,.2f}" for ccy, total in sorted(totals.items()))
    div_summary = ", ".join(f"{ccy or 'UNKNOWN'} {total:,.2f}" for ccy, total in sorted(divs.items()))
    interest_summary = ", ".join(f"{ccy or 'UNKNOWN'} {total:,.2f}" for ccy, total in sorted(interests.items()))
    print(f"  已实现盈亏(股票): Σ={realized_summary or '0.00'}")
    print(f"  分红(corps+):    {div_summary or '0.00'} ;  融资利息: {interest_summary or '0.00'}")
    if fx_rates:
        print(f"  税务汇总:        本账户应纳税额估算 RMB {tax_total_rmb:,.2f}")
    shorted = sorted(f"{ccy}:{code}" for (ccy, code), b in book.items() if b.get("shorted"))
    if shorted and args.on_negative_position == "flag":
        print(f"  ⚠ 负持仓提醒(NEGATIVE_POSITION):  {', '.join(shorted)} 出现负持仓——现金账户通常意味着"
              f"缺少买入/成本基础(如转入股票、卖出记录早于买入),已按对称做空口径计入合计,可能不准。\n"
              f"    处理方式:① 补上对应标的的期初成本后重跑(最准);"
              f"② --on-negative-position=exclude 先从合计/税务中排除待核对;"
              f"③ 若确实是做空,--on-negative-position=short 确认计入。", file=sys.stderr)
    elif shorted and args.on_negative_position == "exclude":
        print(f"  ⚠ 已排除负持仓标的(NEGATIVE_POSITION):  {', '.join(shorted)} 已从合计/税务中排除,待核对成本基础。",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
