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
        [--distributions FILE] [--adjustments FILE] [--opening-adjustments FILE]

Prerequisite: the official Longbridge CLI installed and authenticated, so that
`longbridge statement --type monthly --format json` works in your shell. This script
never handles credentials — it shells out to `longbridge`. See README.

Outputs (CSV, utf-8-sig; no personal data is embedded — everything comes from your account):
    longbridge_<YEAR>_成交明细.csv          stock/option/fund trades (clear_amount = net of fees)
    longbridge_<YEAR>_股息利息现金流.csv      dividends(corps)/interest/withdrawals
    longbridge_<YEAR>_已实现盈亏_按标的.csv    realized P&L per instrument (average-cost)
    longbridge_<YEAR>_期权结算事件.csv        official OptionExpire/OptionExercise settlement rows
    longbridge_<YEAR>_账户净值.csv           per-month asset total (cross-check)
    longbridge_<YEAR>_税务汇总.csv           tax summary: gains/dividends/withheld-tax/interest + tax due
    longbridge_<YEAR>_分配明细.csv           distribution character: dividend / capital gain / ROC
    longbridge_<YEAR>_次年成本加回.csv       next-year basis add-back for capital-gain distributions
    longbridge_<YEAR>_手工调整.csv           manual adjustments (quantity/cost/realized/income)
"""
from __future__ import annotations
import argparse, csv, json, os, re, shutil, subprocess, sys, time
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
# currency tags inside a free-text remark, used when a cashflow line's `currency` field is
# blank (real Longbridge statements often leave it empty — see ipo_allotment): an explicit
# "@HKD" tag, else a "CODE.MARKET" suffix (e.g. 700.HK -> HKD) mapped through MARKET_CCY,
# else a bare ISO currency word in the remark (e.g. "TSM(US8740391003) Cash Dividend USD
# 0.6081/share" — an ISIN, not a CODE.MARKET suffix, but the remark names the currency).
_CCY_AT_RE = re.compile(r"@\s*([A-Za-z]{3})")
_MKT_SUFFIX_RE = re.compile(r"\b[0-9A-Za-z]+\.([A-Za-z]{2})\b")
_CCY_WORD_RE = re.compile(r"\b(USD|HKD|CNY|CNH|SGD|EUR|GBP|JPY|AUD|CAD|CHF)\b")
# ISIN (e.g. US8740391003 / HK0000584737) — Longbridge sometimes identifies an instrument by
# ISIN alone ("TSM(US8740391003) Payment in Lieu of Dividend"), which defeats CODE.MARKET
# matching. Only a safe country->currency subset is mapped (the full ISO scheme is not 1:1).
_ISIN_RE = re.compile(r"\b([A-Z]{2})[A-Z0-9]{9}\d\b")
_ISIN_CCY = {"US": "USD", "HK": "HKD"}

# US-style option code, e.g. NVDL250117C70000 -> ("NVDL", 2025-01-17, "C", 70.0). Used to
# tell option sellers (wheel strategy) apart from stocks with missing cost basis, and to
# know each contract's expiry so an open short can be classified (terminated in-year vs
# still open at year-end).
_OPTION_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d+)$")


def option_expiry(code):
    """'NVDL250117C70000' -> (2025, 1, 17); None if `code` is not a US option symbol."""
    m = _OPTION_RE.match(str(code).upper())
    if not m:
        return None
    ymd = m.group(2)
    try:
        return (2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
    except ValueError:
        return None

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
    cm = _CCY_AT_RE.search(remark)
    currency = ((change.get("currency") or "") or (cm.group(1) if cm else "")
                or MARKET_CCY.get(market, "")).upper()
    return currency, code, qty, abs(num(change.get("amount")))


def settle_ccy(record, *text_fields):
    """Settlement currency for a cashflow record (dividend, interest, balance change).

    Prefer the explicit `currency` field; when it is blank — which real Longbridge
    statements often leave it (the dividend/IPO lines carry currency only inside the
    remark, e.g. "Cash Dividend 700.HK" or "@HKD") — fall back to a currency tag found in
    the given free-text fields. Without this, a blank-currency dividend buckets under an
    unknown currency and silently escapes RMB conversion and the dividend tax. '' if unknown.
    """
    explicit = (record.get("currency") or "").strip().upper()
    if explicit:
        return explicit
    texts = [str(record.get(f) or "") for f in text_fields]
    for t in texts:                                  # explicit "@HKD"-style tag wins
        m = _CCY_AT_RE.search(t)
        if m:
            return m.group(1).upper()
    for t in texts:                                  # else a CODE.MARKET suffix (700.HK -> HKD)
        m = _MKT_SUFFIX_RE.search(t)
        if m and m.group(1).upper() in MARKET_CCY:
            return MARKET_CCY[m.group(1).upper()]
    for t in texts:                                  # else a bare currency word ("... USD 0.60/sh")
        m = _CCY_WORD_RE.search(t)
        if m:
            return m.group(1).upper()
    for t in texts:                                  # else an ISIN country prefix (US8740391003 -> USD)
        m = _ISIN_RE.search(t)
        if m and m.group(1).upper() in _ISIN_CCY:
            return _ISIN_CCY[m.group(1).upper()]
    return ""


# ---------------------------------------------------------------------------
# Distribution characterisation: dividend / capital gain / return of capital
# ---------------------------------------------------------------------------
# The broker's statements label every cash distribution as a "dividend" (corps or a
# Cash Dividend balance change) and its holding `cost_price` is 摊薄成本 — it drops on
# ANY distribution. Neither is tax characterisation. A year-end capital-gain
# distribution and a return of capital look identical in the statement, but:
#   dividend      -> 利息股息红利所得, taxed in the year received, cost unchanged
#   capital_gain  -> 财产转让所得, taxed in the year received, cost unchanged
#   roc           -> not income; reduces cost basis (deferred into the eventual sale)
# Guessing wrong silently misstates the year (and the next year's opening basis), so
# the character is supplied by the user/advisor in a small file, and anything left
# unclassified is reported loudly instead of being assumed silently.
DISTRIBUTION_CHARACTERS = ("dividend", "capital_gain", "roc")
_DIST_ALIASES = {
    "dividend": "dividend", "div": "dividend", "ordinary_dividend": "dividend",
    "普通股息": "dividend", "股息": "dividend",
    "capital_gain": "capital_gain", "capital-gain": "capital_gain", "cg": "capital_gain",
    "capitalgain": "capital_gain", "资本利得": "capital_gain", "资本利得分配": "capital_gain",
    "roc": "roc", "return_of_capital": "roc", "return-of-capital": "roc",
    "资本返还": "roc", "返还本金": "roc",
}
_DIST_CODE_MARKET_RE = re.compile(r"\b([0-9A-Za-z]{1,10})\.([A-Za-z]{2})\b")
_DIST_LEADING_CODE_RE = re.compile(r"^\s*([A-Za-z]{1,6})\s*\(")


def _norm_date(value):
    return str(value or "").strip().replace(".", "-").replace("/", "-")


def distribution_code(record, remark=""):
    """Best-effort instrument code for a distribution row: explicit `code`/`symbol`,
    else a CODE.MARKET suffix in the remark, else the ticker before an ISIN."""
    for field in ("code", "symbol"):
        raw = str(record.get(field) or "").strip().upper()
        if raw:
            return raw.split(".")[0]
    text = str(remark or "")
    m = _DIST_CODE_MARKET_RE.search(text)
    if m:
        return m.group(1).upper()
    m = _DIST_LEADING_CODE_RE.search(text)
    if m:
        return m.group(1).upper()
    return ""


def load_distributions(path):
    """Load the distribution-character file (CSV or JSON).

    Columns / keys: `date`, `code`, `character`, `amount`, `currency`, `source`, `note`.
    `code` and `character` are required; `date`/`amount` are optional matchers (use them
    when the same ticker distributes more than once in the year).
    """
    if str(path).lower().endswith(".json"):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("distributions", []) if isinstance(data, dict) else data
    else:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    out = []
    for i, raw in enumerate(rows, start=2):
        row = {str(k or "").strip().lower(): v for k, v in raw.items()}
        code = str(row.get("code") or "").strip().upper()
        char = _DIST_ALIASES.get(str(row.get("character") or "").strip().lower(),
                                 str(row.get("character") or "").strip().lower())
        if not code:
            raise ValueError(f"{path}: 第 {i} 行缺少 code")
        if char not in DISTRIBUTION_CHARACTERS:
            raise ValueError(f"{path}: 第 {i} 行 character={char!r} 无效，应为 {DISTRIBUTION_CHARACTERS}")
        raw_amount = row.get("amount")
        try:
            amount = float(str(raw_amount).replace(",", "")) if raw_amount not in (None, "") else None
        except ValueError as exc:
            raise ValueError(f"{path}: 第 {i} 行 amount 不是数字: {raw_amount!r}") from exc
        out.append({"date": _norm_date(row.get("date")), "code": code, "character": char,
                    "amount": amount, "currency": str(row.get("currency") or "").strip().upper(),
                    "source": str(row.get("source") or "").strip(),
                    "note": str(row.get("note") or "").strip()})
    return out


def classify_distributions(distributions, rules):
    """Attach a character to every detected distribution from the user's rules.

    Matching order: (code, date) -> (code, amount) -> a wildcard rule, but only when the
    code distributes once that year. A dated rule is never consumed by a different
    distribution of the same ticker (that would mislabel e.g. the 12/19 capital gain as
    the 1/2 dividend). Ambiguous matches raise instead of guessing.
    """
    used = set()
    counts = defaultdict(int)
    for d in distributions:
        counts[d["code"]] += 1
    for d in distributions:
        candidates = [i for i, r in enumerate(rules) if i not in used and r["code"] == d["code"]]
        pick = None
        dated = [i for i in candidates
                 if rules[i].get("date") and rules[i]["date"] == d["date"]]
        if len(dated) > 1:
            raise ValueError(f"分配匹配有歧义: {d['date']} {d['code']} {d['amount']:.2f} — "
                             f"character 文件有多行同日规则")
        if dated:
            pick = dated[0]
        if pick is None:
            by_amount = [i for i in candidates
                         if rules[i].get("amount") is not None
                         and abs(rules[i]["amount"] - d["amount"]) <= 0.01]
            if len(by_amount) > 1:
                raise ValueError(f"分配匹配有歧义: {d['date']} {d['code']} {d['amount']:.2f} — "
                                 f"character 文件有多行同额规则")
            if by_amount:
                pick = by_amount[0]
        if pick is None:
            wild = [i for i in candidates
                    if not rules[i].get("date") and rules[i].get("amount") is None]
            if len(wild) == 1 and counts[d["code"]] == 1:
                pick = wild[0]
            elif len(wild) > 1 and counts[d["code"]] == 1:
                raise ValueError(f"分配匹配有歧义: {d['date']} {d['code']} {d['amount']:.2f} — "
                                 f"该标的有 {len(wild)} 条未限定的定性规则，请补 date 或 amount")
        if pick is not None:
            used.add(pick)
            d["character"] = rules[pick]["character"]
            d["source"] = rules[pick].get("source") or d.get("source", "")
            d["note"] = rules[pick].get("note") or ""
        else:
            d["character"] = None
    unused = [r for i, r in enumerate(rules) if i not in used]
    return distributions, unused


def load_opening_adjustments(path):
    """Load `code,currency,amount[,note]` rows that add back to the opening cost basis.

    Use case: the broker reduced `cost_price` for a distribution that is taxable as a
    capital gain (so it must NOT reduce basis). Run the next year with this file to undo
    the broker's reduction. For options `amount` is per contract (the script's unit).
    """
    if str(path).lower().endswith(".json"):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("adjustments", []) if isinstance(data, dict) else data
    else:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    out = []
    for i, raw in enumerate(rows, start=2):
        row = {str(k or "").strip().lower(): v for k, v in raw.items()}
        code = str(row.get("code") or "").strip().upper()
        ccy = str(row.get("currency") or "USD").strip().upper()
        try:
            amount = float(str(row.get("amount") or 0).replace(",", ""))
        except ValueError as exc:
            raise ValueError(f"{path}: 第 {i} 行 amount 不是数字") from exc
        if not code:
            raise ValueError(f"{path}: 第 {i} 行缺少 code")
        out.append({"code": code, "currency": ccy, "amount": amount,
                    "note": str(row.get("note") or "")})
    return out


def apply_opening_adjustments(opening, rows):
    """Add `amount` to each instrument's opening cost (per unit), in place."""
    for r in rows:
        key = (r["currency"], r["code"])
        if key not in opening:
            continue
        qty, cp, name = opening[key]
        if abs(qty) > 1e-9:
            opening[key] = (qty, cp + r["amount"] / qty, name)
    return opening


def _in_year(value, year):
    """True when a statement row's trade/pay date belongs to `year` (missing date -> keep)."""
    text = _norm_date(value)
    return (not text) or text.startswith(f"{year}-")


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


# The statement API is rate-limited to ~1 request/second (error 429002); a 12-month run
# makes 13+ calls, so without retry/backoff a whole run dies mid-way on one 429.
_RATE_KEYS = ("429002", "rate limit", "ratelimit", "too many request", "429",
              "限流", "限速", "頻繁", "频繁", "稍后再试", "稍後再試")


def lb_json(args, retries=5):
    """Run `longbridge ... --format json`; retry on rate-limit with exponential backoff,
    and on auth failure offer to log in and retry once."""
    global _auth_retried
    attempt = 0
    while True:
        try:
            out = subprocess.run(["longbridge", *args, "--format", "json"],
                                 capture_output=True, text=True)
        except FileNotFoundError:
            _install_guide(); sys.exit(1)
        if out.returncode == 0:
            break
        err = (out.stderr or out.stdout).strip()
        low = err.lower()
        if any(k in low for k in _RATE_KEYS) and attempt < retries:
            wait = 2 ** attempt + 1
            print(f"  ⏳ 触发 API 限流，{wait}s 后重试（{attempt + 1}/{retries}）...",
                  file=sys.stderr)
            time.sleep(wait)
            attempt += 1
            continue
        is_auth = any(k in low for k in _AUTH_KEYS)
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
    want = ({f"{year}{m:02d}" for m in range(1, 13)}
            | {f"{year-1}12", f"{year+1}01"})
    return {r["date"]: r["file_key"] for r in rows if r["date"] in want}


def _alternate_keys(file_key):
    """Longbridge's statement list sometimes hands back a file_key under the wrong CDN
    prefix (`/statement_data/...` vs `/lb-statement/...`); the other prefix often works.
    Yield the alternates so `export` can retry instead of dying on one bad key."""
    parts = str(file_key or "").split("/")
    if len(parts) > 2 and parts[1] in ("statement_data", "lb-statement"):
        for prefix in ("lb-statement", "statement_data"):
            if prefix != parts[1]:
                yield "/".join([parts[0], prefix, *parts[2:]])


def export(file_key, cache_path=None, refresh=False):
    """Export one statement; cache the JSON on disk so re-runs don't re-hit the rate-limited
    API. Statements are immutable history, so an on-disk cache is safe; pass --refresh to
    bypass (e.g. if the broker amends a statement). Retries the alternate CDN prefix when
    the listed key 404s (seen on 202410: the list key was useless, the other prefix worked)."""
    if cache_path and not refresh and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as fp:
                return json.load(fp)
        except json.JSONDecodeError:
            pass                                    # corrupt cache -> refetch
    data, last_exit = None, None
    for key in (file_key, *_alternate_keys(file_key)):
        try:
            data = lb_json(["statement", "export", "--file-key", key])
            break
        except SystemExit as exc:                   # bad key / non-JSON response
            last_exit = exc
    if data is None:
        raise last_exit
    if cache_path:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False)
    return data


def _apply_roc(book, adj, diagnostics):
    """Apply a return-of-capital distribution to the open position's cost basis.

    ROC is not income: it reduces the basis, so the eventual sale books a larger gain
    (deferred, not exempt). Any excess over the remaining basis is a gain immediately
    (the US rule, and the same 20% 财产转让 character in China).
    """
    code = adj["code"]
    ccy = (adj.get("currency") or "").upper()
    amount = float(adj["amount"])
    key = (ccy, code)
    if key not in book and ("", code) in book:
        key = ("", code)
    b = book.get(key)
    if not b or b["pos"] <= 1e-9:
        if diagnostics is not None:
            diagnostics.append(f"ROC 无法应用: {adj['date']} {code} 当日无多头持仓（{amount:.2f}）")
        return
    cost = b["pos"] * b["avg"]
    applied = min(cost, amount)
    b["avg"] = (cost - applied) / b["pos"]
    b["roc"] = b.get("roc", 0.0) + applied
    if amount > cost + 1e-9:
        excess = amount - cost
        b["realized"] += excess
        b["roc_excess"] = b.get("roc_excess", 0.0) + excess


def _apply_settlement(book, event, diagnostics):
    """Close a still-open option position at 0 on its official expiry/assignment date.

    A contract sold last year and held to expiry has no closing trade this year: without
    this event the premium would stay unrealized forever (or be dropped when the seed is
    skipped). Only the still-open quantity is closed, so a position already bought back
    (pos == 0) is left alone and a 0-price settlement already present in the trades is not
    double-counted.
    """
    code = event["code"]
    key = ("", code) if ("", code) in book else None
    if key is None:
        matches = [k for k in book if k[1] == code]
        key = matches[0] if matches else None
    b = book.get(key) if key else None
    if not b or abs(b["pos"]) <= 1e-9:
        return
    qty = min(abs(float(event["qty"])), abs(b["pos"]))
    if b["pos"] > 0:
        b["realized"] += (0.0 - b["avg"]) * qty
        b["pos"] -= qty
    else:
        b["realized"] += (b["avg"] - 0.0) * qty
        b["pos"] += qty
    b["settled"] = b.get("settled", 0.0) + qty


ADJUSTMENT_TYPES = ("quantity", "cost", "realized", "income")


def load_manual_adjustments(path):
    """Load the manual-adjustment ledger (CSV or JSON) for events the model does not cover.

    Columns: `date, code, type, amount[, currency, character, note]`.
    Types:
      quantity  amount = factor (2 for a 2-for-1 split, 0.5 for a 1-for-2 reverse split):
                quantity *= factor, per-unit cost /= factor; total cost unchanged.
      cost      amount = signed change to the open position's TOTAL cost (non-cash
                distribution, merger consideration, tax reclassification, ...).
      realized  amount = signed realized P&L booked directly (broker reclassification).
      income    amount = signed income; `character` = dividend (default) or capital_gain.

    This is the escape hatch that keeps the skill from having to enumerate every product
    and corporate action: map the event to one of these four effects, or leave it out.
    """
    if str(path).lower().endswith(".json"):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("adjustments", []) if isinstance(data, dict) else data
    else:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    out = []
    for i, raw in enumerate(rows, start=2):
        row = {str(k or "").strip().lower(): v for k, v in raw.items()}
        code = str(row.get("code") or "").strip().upper()
        typ = str(row.get("type") or "").strip().lower()
        if not code:
            raise ValueError(f"{path}: 第 {i} 行缺少 code")
        if typ not in ADJUSTMENT_TYPES:
            raise ValueError(f"{path}: 第 {i} 行 type={typ!r} 无效，应为 {ADJUSTMENT_TYPES}")
        try:
            amount = float(str(row.get("amount") or 0).replace(",", ""))
        except ValueError as exc:
            raise ValueError(f"{path}: 第 {i} 行 amount 不是数字") from exc
        if typ == "quantity" and amount <= 0:
            raise ValueError(f"{path}: 第 {i} 行 quantity 的 amount 必须是正数（拆分/并股比例）")
        character = _DIST_ALIASES.get(str(row.get("character") or "").strip().lower(),
                                     str(row.get("character") or "").strip().lower())
        if typ == "income" and character not in ("dividend", "capital_gain"):
            character = "capital_gain"
        out.append({"date": _norm_date(row.get("date")), "code": code, "type": typ,
                    "amount": amount, "currency": str(row.get("currency") or "").strip().upper(),
                    "character": character, "note": str(row.get("note") or "").strip()})
    return out


def _apply_cost_adjustment(book, adj, diagnostics):
    """Apply a quantity (split) or cost adjustment to the open position at that date."""
    code = adj["code"]
    ccy = (adj.get("currency") or "").upper()
    key = (ccy, code)
    if key not in book and ("", code) in book:
        key = ("", code)
    if key not in book:
        matches = [k for k in book if k[1] == code]
        key = matches[0] if matches else None
    b = book.get(key) if key else None
    if not b or abs(b["pos"]) <= 1e-9:
        if diagnostics is not None:
            diagnostics.append(f"手工调整无法应用: {adj['date']} {code} {adj['type']} "
                               f"{adj['amount']:.2f}（当日无持仓）")
        return
    if adj["type"] == "quantity":
        factor = float(adj["amount"])
        b["pos"] *= factor
        b["avg"] /= factor
        b["split"] = b.get("split", 1.0) * factor
    else:                                             # cost (total-cost delta)
        if b["pos"] < 0:
            if diagnostics is not None:
                diagnostics.append(f"手工 cost 调整跳过空头: {adj['date']} {code}")
            return
        total = b["pos"] * b["avg"] + float(adj["amount"])
        b["avg"] = max(total, 0.0) / b["pos"]
        b["cost_adj"] = b.get("cost_adj", 0.0) + float(adj["amount"])


def realized_by_ticker(trades, opening, adjustments=None, diagnostics=None, settlements=None,
                       manual_adjustments=None):
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
    events = [(_norm_date(t[0]), 0 if t[4] == "买入" else 1, "trade", t) for t in trades]
    for a in adjustments or []:
        events.append((_norm_date(a["date"]), 2, "roc", a))
    for ma in manual_adjustments or []:
        if ma["type"] in ("quantity", "cost"):
            events.append((_norm_date(ma["date"]), 2, "cost_adj", ma))
    for st_ev in settlements or []:
        events.append((_norm_date(st_ev["date"]), 3, "settle", st_ev))
    for _date, _order, kind, payload in sorted(events, key=lambda e: (e[0], e[1])):
        if kind == "roc":
            _apply_roc(book, payload, diagnostics)
            continue
        if kind == "settle":
            _apply_settlement(book, payload, diagnostics)
            continue
        if kind == "cost_adj":
            _apply_cost_adjustment(book, payload, diagnostics)
            continue
        t = payload
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
    ap.add_argument("--on-negative-position", choices=["netcash", "flag", "exclude", "short"],
                    default="netcash",
                    help="how to treat instruments whose position goes negative: "
                         "'netcash' (default, for option sellers) = option contracts opened AND "
                         "left unclosed in-year are not missing cost basis — they expired, were "
                         "assigned, or were bought back. Their realized P&L equals their net "
                         "option cash flow (premium in - buyback out), and the stock leg of any "
                         "assignment is already in stock_trades; contracts still open at year-end "
                         "(expiry after Dec 31) contribute 0. Stocks going negative still warn "
                         "(flag behaviour) — that IS usually missing cost basis; "
                         "'flag' = compute & include in totals but warn; "
                         "'exclude' = drop from totals/tax pending manual review; "
                         "'short' = treat as a confirmed genuine short (compute, include, no warning)")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore the on-disk statement cache and re-download all monthly statements")
    ap.add_argument("--base-currency", default="", metavar="CCY",
                    help="account base currency (e.g. HKD); used ONLY to bucket financing-interest "
                         "lines whose statement currency is blank — the statement carries no other "
                         "hint for them")
    ap.add_argument("--distributions", metavar="FILE",
                    help="distribution-character CSV/JSON (date,code,character,amount,currency,source,note); "
                         "character = dividend | capital_gain | roc. Needed whenever a distribution is not "
                         "an ordinary dividend — the broker's statement and 摊薄成本 cannot tell you which")
    ap.add_argument("--on-unclassified-distribution", choices=["dividend", "error"], default="dividend",
                    help="what to do with a distribution absent from --distributions: 'dividend' (default, "
                         "treat as ordinary dividend and list it for review) or 'error' (write "
                         "distributions_template.csv and stop)")
    ap.add_argument("--write-distribution-template", action="store_true",
                    help="write distributions_template.csv (all detected distributions, blank character) "
                         "next to the outputs for review/filling")
    ap.add_argument("--adjustments", metavar="FILE",
                    help="manual-adjustment ledger CSV/JSON (date,code,type,amount[,currency,character,note]); "
                         "type = quantity (split factor) | cost (total-cost delta) | realized | income. "
                         "Use it for corporate actions / reclassifications the event model does not cover")
    ap.add_argument("--opening-adjustments", metavar="FILE",
                    help="CSV/JSON rows code,currency,amount added back to the opening cost basis; use in "
                         "year N+1 to undo a broker's cost reduction for a capital-gain distribution")
    ap.add_argument("--allow-missing-months", action="store_true",
                    help="continue even if some monthly statements are unavailable; the run will be "
                         "understated and the gap is reported loudly")
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
    cachedir = os.path.join(outdir, ".statements")   # raw monthly JSON cache (see export())

    ensure_cli()                                     # offer to install/login if missing (asks first)
    keys = list_keys(year)
    if not keys:
        sys.exit(f"no monthly statements found for {year} — is the Longbridge CLI authenticated?")
    missing_months = [f"{year}{m:02d}" for m in range(1, 13)
                      if f"{year}{m:02d}" not in keys and f"{year}{m:02d}" >= f"{year}01"]
    if missing_months and not args.allow_missing_months:
        sys.exit(
            "缺少月结单: " + ", ".join(missing_months) + "\n"
            "这些月份的成交/分红不会被计入，结果会系统性少报（2024 年真实案例：5–7 月无文档、"
            "10 月 file_key 前缀错误，直接损失 3 个月数据）。\n"
            "处理方式：① 先用 --allow-missing-months 跑一版并核对缺口；② 或用 --refresh 重试；"
            "③ 月结单确实缺失时，改用 cash-flow + 逐笔成交重建（见 issue-option-expire）。")
    if missing_months:
        print(f"  ⚠ 缺失月结单: {', '.join(missing_months)} — 以下结果会少报，已按 --allow-missing-months 继续。",
              file=sys.stderr)

    trades, cashflows, navrows, distributions, settlements = [], [], [], [], []
    opening = {}                                         # (ccy, code) -> (signed qty, cost/unit, name)

    # prior-December closing holdings -> opening cost basis. Signed: SHORT option positions
    # carried into the year (ledger_quantity < 0, equity_type OP) MUST be seeded too — the
    # old `qty <= 0: continue` silently dropped them, so their 2025 buy-to-closes reconstructed
    # as phantom LONGS (realized 0, premium lost). NB cost_price for options is the per-share
    # premium while trades are priced per CONTRACT (x100) — multiply so the engine's units match.
    prior = keys.get(f"{year-1}12")
    if prior:
        for h in export(prior, os.path.join(cachedir, f"{year-1}12.json"), args.refresh).get("equity_holdings", []):
            cp = h.get("cost_price")
            qty = num(h.get("ledger_quantity"))
            if cp in (None, "", "N/A") or abs(qty) <= 1e-9:   # skip funds (cost N/A) & flat lines
                continue
            cost = num(cp)
            if str(h.get("equity_type", "")).upper() == "OP":
                cost *= 100.0
            opening[((h.get("currency") or "").upper(), str(h["code"]))] = (qty, cost, h.get("name", ""))

    option_events = []                                   # official OptionExpire/OptionExercise rows
    opt_events = defaultdict(lambda: {"expire": 0.0, "exercise": 0.0})
    st_dec = None                                        # December statement, for year-end checks
    # Include <year+1>01: statements are cut by settlement month, so trades dated 30/31 Dec
    # only appear in January's statement. Attribute by TRADE DATE and drop rows whose date
    # falls outside the year (the same mechanism that otherwise leaks the prior December's
    # trades into January's statement and double-counts them).
    months = [f"{year}{m:02d}" for m in range(1, 13)] + [f"{year+1}01"]
    for ym in months:
        if ym not in keys:
            continue
        st = export(keys[ym], os.path.join(cachedir, f"{ym}.json"), args.refresh)
        if ym == f"{year}12":
            st_dec = st
        # official settlement events for option sellers: equity_holding_changes records a
        # contract held to expiry as OptionExpire (worthless) or OptionExercise (assigned).
        # Contracts bought back before expiry generate NO event — absence of an event is not
        # a data gap, it means the round trip closed inside the year.
        for e in st.get("equity_holding_changes", []):
            if not _in_year(e.get("date"), year):
                continue
            etyp = str(e.get("type", ""))
            if etyp not in ("OptionExpire", "OptionExercise"):
                continue
            qty = num(e.get("quantity"))
            settlements.append({"date": _norm_date(e.get("date") or ym),
                                "code": str(e.get("code")), "qty": abs(qty)})
            option_events.append([d(e.get("date") or ym),
                                  "到期作废" if etyp == "OptionExpire" else "行权指派",
                                  str(e.get("code")), e.get("name", ""), qty])
            opt_events[str(e.get("code"))]["expire" if etyp == "OptionExpire" else "exercise"] += qty
        for t in st.get("stock_trades", []):
            if not _in_year(t.get("trade_date"), year):
                continue
            net = num(t["clear_amount"]); gross = num(t["trade_amount"])
            fee = round(abs(net) - gross if t["direction"] == "BUY" else gross - net, 2)
            trades.append([d(t["trade_date"]), d(t.get("settle_date")), str(t["code"]), t.get("name", ""),
                           "买入" if t["direction"] == "BUY" else "卖出", "stock", t.get("currency", ""),
                           num(t["trade_quantity"]), num(t["trade_price"]), gross, fee, net])
        for t in st.get("option_trades", []):           # empty in 2025; future-proof
            if not _in_year(t.get("trade_date"), year):
                continue
            net = num(t.get("clear_amount")); gross = num(t.get("trade_amount"))
            fee = round(abs(abs(net) - gross), 2)
            trades.append([d(t.get("trade_date")), d(t.get("settle_date")), str(t.get("code")), t.get("name", ""),
                           "买入" if num(t.get("clear_amount")) < 0 else "卖出", "option", t.get("currency", ""),
                           num(t.get("trade_quantity")), num(t.get("trade_price")), gross, fee, net])
        for f in st.get("fund_trades", []):
            if not _in_year(f.get("confirm_date") or f.get("order_date"), year):
                continue
            amt = num(f["trade_amount"])
            dirn = {"1": "申购", "2": "赎回"}.get(str(f.get("direction")), str(f.get("direction")))
            net = -amt if dirn == "申购" else amt
            trades.append([d(f.get("confirm_date") or f.get("order_date")), "", str(f["code"]), f.get("name", ""),
                           dirn, "fund", settle_ccy(f, "code", "name", "remark"), num(f["trade_quantity"]), num(f.get("price")),
                           amt, 0.0, net])
        # cashflows: dividends (corps) + interest (interests) + meaningful balance changes
        for c in st.get("corps", []):
            if not _in_year(c.get("date") or c.get("ex_date"), year):
                continue
            ccy = settle_ccy(c, "symbol", "code", "remark", "name")
            amt = num(c.get("amount") or c.get("net_amount"))
            remark = c.get("remark") or c.get("name") or json.dumps(c, ensure_ascii=False)
            cashflows.append([d(c.get("date") or c.get("ex_date") or ym), "分红/公司行动",
                              ccy, amt, remark])
            if amt > 0:
                distributions.append({"date": _norm_date(d(c.get("date") or c.get("ex_date") or ym)),
                                      "code": distribution_code(c, remark), "name": c.get("name", ""),
                                      "currency": ccy, "amount": amt, "remark": remark,
                                      "source": "corps", "character": None})
        for it in st.get("interests", []):
            if not _in_year(it.get("date"), year):
                continue
            cashflows.append([d(it.get("date") or ym), "融资利息",
                              settle_ccy(it, "symbol", "code", "remark", "name"),
                              num(it.get("total")), f"利率 {it.get('rate', '')}"])
        for a in st.get("account_balance_changes", []):
            if not _in_year(a.get("date"), year):
                continue
            bc = str(a.get("biz_code", ""))
            remark = str(a.get("remark", "")); typ = str(a.get("type", ""))
            is_div = any(h in (remark + typ).lower() for h in DIV_HINTS)
            if bc in INTERNAL_BIZ and not is_div:
                continue
            ccy = settle_ccy(a, "remark", "type")
            amt = num(a.get("amount"))
            cashflows.append([d(a.get("date")), ("分红" if is_div else typ) or bc, ccy, amt, remark])
            if is_div and amt > 0:
                distributions.append({"date": _norm_date(d(a.get("date"))),
                                      "code": distribution_code(a, remark), "name": typ,
                                      "currency": ccy, "amount": amt, "remark": remark,
                                      "source": "account_balance_changes", "character": None})
        # IPO-allotted shares -> seed opening cost basis (they have no buy in stock_trades)
        for a in st.get("account_balance_changes", []):
            if not _in_year(a.get("date"), year):
                continue
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
        if ym.startswith(str(year)):
            for as_ in st.get("asset", []):
                navrows.append([ym, as_.get("currency", ""), num(as_.get("total"))])

    # ---- distribution characterisation (dividend / capital gain / ROC) ----
    diagnostics = []
    rules = load_distributions(args.distributions) if args.distributions else []
    distributions, unused_rules = classify_distributions(distributions, rules)
    for r in unused_rules:
        print(f"  ⚠ 分配定性文件有一行未匹配到任何分配: {r['date']} {r['code']} {r['character']}",
              file=sys.stderr)
    unclassified = [x for x in distributions if not x["character"]]

    def write_dist_template(path):
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            wr = csv.writer(fh)
            wr.writerow(["date", "code", "character", "amount", "currency", "source", "note", "hint"])
            for x in distributions:
                wr.writerow([x["date"], x["code"], x["character"] or "", f"{x['amount']:.2f}",
                             x["currency"], x["source"], x["remark"],
                             "未分类：请填 dividend / capital_gain / roc"
                             + ("；金额较大，重点核对是否有预扣" if x["amount"] >= 1000 else "")])

    if args.write_distribution_template or (unclassified and args.on_unclassified_distribution == "error"):
        write_dist_template(os.path.join(outdir, "distributions_template.csv"))
    if unclassified and args.on_unclassified_distribution == "error":
        sys.exit(f"有 {len(unclassified)} 笔分配未定性（模板已写到 {outdir}/distributions_template.csv）。"
                 "请填好 character 后用 --distributions 指回；不要用券商的摊薄成本推算税务性质。")
    for x in unclassified:
        x["character"] = "dividend"
        x["note"] = (x.get("note") or "") + "；未定性，按股息处理（待确认）"

    divs = defaultdict(float)                     # 利息股息红利（原币）
    capgain = defaultdict(float)                  # (ccy, code) -> 资本利得分配
    capgain_by_ccy = defaultdict(float)
    roc_by_ccy = defaultdict(float)
    roc_adjustments = []
    for x in distributions:
        ccy = (x["currency"] or "").upper()
        if x["character"] == "dividend":
            divs[ccy] += x["amount"]
            x["applied"] = "股息所得"
        elif x["character"] == "capital_gain":
            capgain[(ccy, x["code"])] += x["amount"]
            capgain_by_ccy[ccy] += x["amount"]
            x["applied"] = "财产转让所得（成本不变）"
        else:                                     # roc
            roc_adjustments.append({"date": x["date"], "code": x["code"],
                                    "currency": ccy, "amount": x["amount"]})
            roc_by_ccy[ccy] += x["amount"]
            x["applied"] = "冲减成本（不计税）"

    # ---- manual-adjustment ledger (long-tail corporate actions) ----------
    manual = load_manual_adjustments(args.adjustments) if args.adjustments else []
    manual_engine = [m for m in manual if m["type"] in ("quantity", "cost")]
    manual_realized = [m for m in manual if m["type"] == "realized"]
    for m in manual:
        ccy = (m["currency"] or "").upper()
        if m["type"] == "income":
            if m["character"] == "dividend":
                divs[ccy] += m["amount"]
                m["applied"] = "股息所得"
            else:
                capgain[(ccy, m["code"])] += m["amount"]
                capgain_by_ccy[ccy] += m["amount"]
                m["applied"] = "财产转让所得（成本不变）"
        elif m["type"] == "realized":
            m["applied"] = "财产转让所得（直接调整）"
        elif m["type"] == "quantity":
            m["applied"] = f"数量×{m['amount']:g}，单位成本÷{m['amount']:g}"
        else:
            m["applied"] = "总成本调整"

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
    if args.opening_adjustments:
        apply_opening_adjustments(opening, load_opening_adjustments(args.opening_adjustments))
    book = realized_by_ticker(trades, opening, roc_adjustments, diagnostics, settlements,
                              manual_engine)
    for (ccy, code), amt in capgain.items():
        b = book.setdefault((ccy, code), {"name": "", "currency": ccy, "pos": 0.0, "avg": 0.0,
                                          "realized": 0.0, "nf": 0, "sumnet": 0.0, "shorted": False})
        b["realized"] += amt
        b["capgain"] = b.get("capgain", 0.0) + amt
    for m in manual_realized:
        key = ((m["currency"] or "").upper(), m["code"])
        b = book.setdefault(key, {"name": "", "currency": key[0], "pos": 0.0, "avg": 0.0,
                                  "realized": 0.0, "nf": 0, "sumnet": 0.0, "shorted": False})
        b["realized"] += m["amount"]
        b["manual"] = b.get("manual", 0.0) + m["amount"]
    opening_keys = set(opening)
    rows, totals = [], defaultdict(float)
    outcome = defaultdict(int)                           # option-seller outcomes, for the summary
    for key in sorted(book, key=lambda c: (c[0], book[c]["realized"])):
        currency, code = key
        b = book[key]
        if b["nf"] == 0 and abs(b["realized"]) < 1e-6:   # untouched carried holding -> skip
            continue
        rz = round(b["realized"], 2)
        excluded = False
        held = abs(b["pos"]) > 1e-6
        notes = []
        if b.get("capgain"):
            notes.append(f"含资本利得分配 {b['capgain']:.2f}（成本不变）")
        if b.get("roc"):
            notes.append(f"含资本返还 {b['roc']:.2f}（已冲减成本，递延）")
        if b.get("manual"):
            notes.append(f"含手工调整 {b['manual']:.2f}")
        if b.get("split") and abs(b["split"] - 1.0) > 1e-9:
            notes.append(f"含拆股/并股 ×{b['split']:g}")
        if b.get("cost_adj"):
            notes.append(f"含成本调整 {b['cost_adj']:.2f}")
        if held:
            notes.append("年末仍有持仓(空头),未实现不计入" if b["pos"] < 0 else "年末仍有持仓,未实现不计入")
        if b["shorted"]:
            exp = option_expiry(code)
            seeded = key in opening_keys
            mode = args.on_negative_position
            if mode == "netcash" and exp is not None:
                if seeded:
                    # carried-in short (prior-December seed): engine books it against last
                    # year's premium basis; any remainder still short at year-end is last
                    # year's income / unrealized, not this year's.
                    notes.append("期初带入的空头期权,按上年成本基础计算(剩余空头部分权利金属上年)")
                    outcome["carried"] += 1
                elif exp <= (year, 12, 31):
                    # sell-to-open with in-year expiry: option-side realized == net cash flow
                    rz = round(b["sumnet"], 2)
                    ev = opt_events.get(code) or {}
                    if ev.get("exercise") and not ev.get("expire"):
                        kind, k = "被行权指派(官方OptionExercise)", "assigned"
                    elif ev.get("expire") and not ev.get("exercise"):
                        kind, k = "到期作废(官方OptionExpire)", "expired"
                    elif ev:
                        kind, k = "部分指派+部分作废(官方结算)", "assigned"
                    else:
                        kind, k = "年内买入平仓(无到期事件)", "closed"
                    notes.append(f"期权卖方·净现金流口径({kind})")
                    outcome[k] += 1
                else:
                    # contract still open at year-end: premium received is unrealized
                    excluded = True
                    notes.append("⚠ 期权年末仍未平仓(到期日在税年后),权利金未实现,已从合计/税务中排除")
                    outcome["open"] += 1
            elif mode == "short":
                notes.append("做空(已确认),已计入")
            elif mode == "exclude":
                excluded = True
                notes.append("⚠ 负持仓:已从合计/税务中排除,待核对成本基础")
            else:                    # flag mode, or netcash on a NON-option (real missing basis)
                notes.append("⚠ 出现负持仓:疑似缺少成本基础(转入/卖出早于买入),请核对")
        if not excluded:
            totals[currency] += rz
        row = [code, b["name"], currency, b["nf"], round(b["sumnet"], 2), rz]
        if fx_rates:
            row.append(rmb(rz, currency))
        row.append(";".join(notes))
        rows.append(row)

    # 3b) 官方期权结算事件 (expiry/assignment audit trail for the netcash notes above)
    w(f"longbridge_{year}_期权结算事件.csv",
      ["日期", "类型", "代码", "名称", "数量(张)"], sorted(option_events))
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
    interests = defaultdict(float)
    withheld = defaultdict(float)      # negative sums: broker-withheld tax on dividends
    # dividend rows carry the currency; withholding fee rows often arrive with currency=''
    # on the same date (e.g. "Withholding Tax/Dividend Fee") — inherit that currency.
    div_ccy_by_date = defaultdict(set)
    div_ccy_all = set()
    for c in cashflows:
        if "分红" in c[1] and c[3] > 0 and (c[2] or "").upper():
            div_ccy_by_date[c[0]].add(c[2].upper())
            div_ccy_all.add(c[2].upper())
    for c in cashflows:
        currency = (c[2] or "").upper()
        typ, amt, remark = c[1], c[3], str(c[4] or "")
        low = (typ + remark).lower()
        if "分红" in typ and amt > 0:
            pass                                    # already aggregated from distributions
        elif amt < 0 and ("withholding" in low or "预扣" in remark or "預扣" in remark
                          or "分红" in typ):
            if not currency:                               # blank-currency withholding row
                cand = div_ccy_by_date.get(c[0], set())
                if len(cand) == 1:
                    currency = next(iter(cand))            # same-date dividend's currency
                elif len(div_ccy_all) == 1:
                    currency = next(iter(div_ccy_all))     # only one dividend currency all year
            withheld[currency] += amt
        if "利息" in typ:
            # financing interest arrives with currency='' and no other hint; the account base
            # currency (--base-currency) is the only sane bucket for it
            interests[currency or args.base_currency.upper()] += amt

    th = ["所得项目", "货币", "金额(原币)"] + (["金额(RMB)", "应纳税额(RMB)"] if fx_rates else []) + ["税率", "备注"]
    trows, tax_total_rmb = [], 0.0
    currencies = sorted(set(totals) | set(divs) | set(interests) | set(capgain_by_ccy) | set(roc_by_ccy))
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
        cg = round(capgain_by_ccy.get(currency, 0.0), 2)
        if cg:
            trows.append(["  其中：资本利得分配(财产转让)", currency, cg]
                         + ([rmb(cg, currency), ""] if fx_rates else [])
                         + ["20%", "基金/ETF 的资本利得分配；按财产转让确认，不冲减成本（已含在上面的合计中）"])
        rocv = round(roc_by_ccy.get(currency, 0.0), 2)
        if rocv:
            trows.append(["(备查)资本返还(冲减成本)", currency, rocv]
                         + ([rmb(rocv, currency), ""] if fx_rates else [])
                         + ["—", "非收入；已冲减成本，卖出时体现为更高的财产转让所得"])
        wh = round(withheld.get(currency, 0.0), 2)
        if wh < 0:                                       # only show real withholdings
            wh_rmb = rmb(wh, currency)
            if isinstance(wh_rmb, float):
                tax_total_rmb += wh_rmb                  # negative -> reduces tax due
            trows.append(["(抵免项)境外已预扣税额", currency, wh]
                         + ([wh_rmb, ""] if fx_rates else [])
                         + ["—", "境外来源已缴税款,可申请境外税收抵免(限额为该项抵免前应纳税额);负数=已预扣"])
        trows.append(["(备查)融资利息支出", currency, interest]
                     + ([interest_rmb, ""] if fx_rates else [])
                     + ["—", "非收入;做财产转让可作合理费用参考"])
    if fx_rates:
        trows.append(["合计·本账户应纳税额(估)", "", "", "", round(tax_total_rmb, 2), "",
                      "= 分红税 + 财产转让税 - 境外预扣抵免(本账户);财产转让最终税额须合并其他账户后确定"])
    else:
        trows.append(["提示", "", "传 --rate <HKD年末中间价> 或 --fx-rate <币种=年末中间价> 可计算人民币与应纳税额"])
    w(f"longbridge_{year}_税务汇总.csv", th, trows)

    # 6) 分配明细（股息 / 资本利得 / 资本返还）与次年成本加回提示
    if distributions:
        label = {"dividend": "股息", "capital_gain": "资本利得", "roc": "资本返还"}
        w(f"longbridge_{year}_分配明细.csv",
          ["日期", "代码", "名称", "货币", "金额", "性质", "应用到", "来源", "备注"],
          [[x["date"], x["code"], x["name"], x["currency"], f"{x['amount']:.2f}",
            label.get(x["character"], x["character"]), x.get("applied", ""), x["source"], x["note"]]
           for x in sorted(distributions, key=lambda r: (r["date"], r["code"]))])
    if manual:
        w(f"longbridge_{year}_手工调整.csv",
          ["日期", "代码", "类型", "金额", "货币", "性质", "应用到", "备注"],
          [[m["date"], m["code"], m["type"], f"{m['amount']:.2f}", m["currency"],
            m.get("character") or "", m.get("applied", ""), m["note"]] for m in manual])
    if capgain:
        w(f"longbridge_{year}_次年成本加回.csv",
          ["code", "currency", "amount", "note"],
          [[code, ccy, f"{amt:.2f}",
            "若券商已把这笔资本利得分配冲减了成本，下一年用 --opening-adjustments 指本文件"]
           for (ccy, code), amt in sorted(capgain.items())])

    print(f"Longbridge {year} -> {outdir}/")
    trade_summary = ", ".join(f"{ccy or 'UNKNOWN'} {total:,.2f}"
                              for ccy, total in sorted(sums_by_currency(trades, 11, 6).items()))
    print(f"  成交明细:        {len(trades)} 笔 (股票/期权/基金), Σ清算净额={trade_summary}")
    print(f"  股息利息现金流:  {len(cashflows)} 行")
    realized_summary = ", ".join(f"{ccy or 'UNKNOWN'} {total:,.2f}" for ccy, total in sorted(totals.items()))
    div_summary = ", ".join(f"{ccy or 'UNKNOWN'} {total:,.2f}" for ccy, total in sorted(divs.items()))
    wh_summary = ", ".join(f"{ccy or 'UNKNOWN'} {total:,.2f}" for ccy, total in sorted(withheld.items()))
    interest_summary = ", ".join(f"{ccy or 'UNKNOWN'} {total:,.2f}" for ccy, total in sorted(interests.items()))
    print(f"  已实现盈亏(股票): Σ={realized_summary or '0.00'}")
    print(f"  分红(毛额):      {div_summary or '0.00'} ;  境外预扣(可抵免): {wh_summary or '0.00'}"
          f" ;  融资利息: {interest_summary or '0.00'}")
    if distributions:
        cnt = {k: sum(1 for x in distributions if x["character"] == k)
               for k in ("dividend", "capital_gain", "roc")}
        print(f"  分配定性:        {len(distributions)} 笔（股息 {cnt['dividend']} / 资本利得 "
              f"{cnt['capital_gain']} / 资本返还 {cnt['roc']}；未定性 {len(unclassified)}）")
        if capgain_by_ccy:
            print("  资本利得分配:    " + ", ".join(f"{c} {v:,.2f}"
                                                    for c, v in sorted(capgain_by_ccy.items())))
        if roc_by_ccy:
            print("  资本返还(冲成本): " + ", ".join(f"{c} {v:,.2f}"
                                                      for c, v in sorted(roc_by_ccy.items())))
    if manual:
        print(f"  手工调整:        {len(manual)} 笔（quantity {sum(1 for m in manual if m['type'] == 'quantity')} / "
              f"cost {sum(1 for m in manual if m['type'] == 'cost')} / "
              f"realized {sum(1 for m in manual if m['type'] == 'realized')} / "
              f"income {sum(1 for m in manual if m['type'] == 'income')}）")
    for msg in diagnostics:
        print(f"  ⚠ {msg}", file=sys.stderr)
    if option_events:
        print(f"  期权结算事件:    {len(option_events)} 行 (官方 OptionExpire/OptionExercise,"
              f" 详见 期权结算事件.csv)")
    if outcome:
        parts = []
        for k, label in (("assigned", "指派"), ("expired", "作废"), ("closed", "年内平仓"),
                         ("carried", "期初带入"), ("open", "年末未平")):
            if outcome.get(k):
                parts.append(f"{label} {outcome[k]}")
        print(f"  期权卖方(netcash): {', '.join(parts)}")
    if fx_rates:
        print(f"  税务汇总:        本账户应纳税额估算 RMB {tax_total_rmb:,.2f}")
    # negative positions that netcash could NOT resolve: non-option shorts (or unparsable
    # codes) — these really do smell like missing cost basis.
    problem_shorts = sorted(
        f"{ccy}:{code}" for (ccy, code), b in book.items()
        if b.get("shorted") and (args.on_negative_position != "netcash"
                                 or option_expiry(code) is None))
    if problem_shorts:
        if args.on_negative_position in ("flag", "netcash"):
            print(f"  ⚠ 负持仓提醒(NEGATIVE_POSITION):  {', '.join(problem_shorts)} 出现负持仓——现金账户通常意味着"
                  f"缺少买入/成本基础(如转入股票、卖出记录早于买入),已按对称做空口径计入合计,可能不准。\n"
                  f"    处理方式:① 补上对应标的的期初成本后重跑(最准);"
                  f"② --on-negative-position=exclude 先从合计/税务中排除待核对;"
                  f"③ 若确实是做空,--on-negative-position=short 确认计入。", file=sys.stderr)
        elif args.on_negative_position == "exclude":
            print(f"  ⚠ 已排除负持仓标的(NEGATIVE_POSITION):  {', '.join(problem_shorts)} 已从合计/税务中排除,待核对成本基础。",
                  file=sys.stderr)
    # closure check: any option still open in the December holdings whose expiry is within
    # the tax year means a trade/settlement record was missed — worth a manual look.
    open_opts = sorted(
        str(h["code"]) for h in (st_dec or {}).get("equity_holdings", [])
        if str(h.get("equity_type", "")).upper() == "OP"
        and abs(num(h.get("ledger_quantity"))) > 1e-9)
    stale = [c for c in open_opts if (option_expiry(c) or (9999, 12, 31)) <= (year, 12, 31)]
    if stale:
        print(f"  ⚠ 年末未平仓期权核对: {', '.join(stale)} 在12月报表仍有持仓但到期日已过——"
              f"可能缺少平仓/结算记录,请人工核对。", file=sys.stderr)
    elif open_opts:
        print(f"  年末未平仓期权(到期日在税年后,今年计0): {', '.join(open_opts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
