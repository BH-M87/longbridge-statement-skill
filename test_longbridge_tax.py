import csv
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import longbridge_tax


def trade_row(date, code, name, currency, qty, net, kind="stock"):
    return [date, "", code, name, "买入" if net < 0 else "卖出", kind, currency, qty, 0.0, abs(net), 0.0, net]


class CurrencyHandlingTest(unittest.TestCase):
    def test_realized_pnl_keeps_currency_books_separate(self):
        trades = [
            trade_row("2025/01/01", "ABC", "ABC Ltd", "HKD", 1, -100),
            trade_row("2025/01/02", "ABC", "ABC Ltd", "HKD", 1, 110),
            trade_row("2025/01/03", "ABC", "ABC Ltd", "USD", 1, -10),
            trade_row("2025/01/04", "ABC", "ABC Ltd", "USD", 1, 13),
        ]

        book = longbridge_tax.realized_by_ticker(trades, {})

        self.assertEqual(round(book[("HKD", "ABC")]["realized"], 2), 10.0)
        self.assertEqual(round(book[("USD", "ABC")]["realized"], 2), 3.0)

    def test_opening_short_not_covered_is_not_booked_as_profit(self):
        # 开年第一笔就是卖空且全年未平仓:年末为空头,未实现,不应计入已实现盈亏
        trades = [trade_row("2025/01/02", "ABC", "ABC Ltd", "HKD", 100, 1000.0)]

        book = longbridge_tax.realized_by_ticker(trades, {})

        b = book[("HKD", "ABC")]
        self.assertEqual(round(b["realized"], 2), 0.0)
        self.assertLess(b["pos"], 0)          # 仍为空头
        self.assertTrue(b["shorted"])         # 标记为负持仓,供核对

    def test_covered_short_realizes_proceeds_minus_cover_cost(self):
        # 卖空 +1000 后买回 -900,正确已实现应为 100(而非旧逻辑的 1000)
        trades = [
            trade_row("2025/01/02", "ABC", "ABC Ltd", "HKD", 100, 1000.0),
            trade_row("2025/03/02", "ABC", "ABC Ltd", "HKD", 100, -900.0),
        ]

        book = longbridge_tax.realized_by_ticker(trades, {})

        b = book[("HKD", "ABC")]
        self.assertEqual(round(b["realized"], 2), 100.0)
        self.assertAlmostEqual(b["pos"], 0.0)

    def test_oversell_beyond_basis_only_realizes_held_portion(self):
        # 持仓 1 股@100,卖出 2 股@110:仅对持有的 1 股计盈亏(10),剩余翻空
        trades = [trade_row("2025/01/02", "ABC", "ABC Ltd", "HKD", 2, 220.0)]

        book = longbridge_tax.realized_by_ticker(trades, {("HKD", "ABC"): (1.0, 100.0, "ABC Ltd")})

        b = book[("HKD", "ABC")]
        self.assertEqual(round(b["realized"], 2), 10.0)
        self.assertTrue(b["shorted"])

    def _short_statement(self):
        # open short 100@10 (+1000), cover 60@9 (-540) -> realized 60, still short 40 at year-end
        return {
            "stock_trades": [
                {"trade_date": "2025.01.02", "settle_date": "2025.01.05", "code": "HK700",
                 "name": "Tencent", "direction": "SELL", "currency": "HKD", "trade_quantity": "100",
                 "trade_price": "10", "trade_amount": "1000", "clear_amount": "1000"},
                {"trade_date": "2025.03.02", "settle_date": "2025.03.05", "code": "HK700",
                 "name": "Tencent", "direction": "BUY", "currency": "HKD", "trade_quantity": "60",
                 "trade_price": "9", "trade_amount": "540", "clear_amount": "-540"},
            ],
            "corps": [], "interests": [], "asset": [],
        }

    def _run_negpos(self, outdir, extra):
        with (
            patch.object(longbridge_tax, "ensure_cli"),
            patch.object(longbridge_tax, "list_keys", return_value={"202501": "k1"}),
            patch.object(longbridge_tax, "export", return_value=self._short_statement()),
        ):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                longbridge_tax.main(["--year", "2025", "-o", outdir, "--allow-missing-months", *extra])

    def _read(self, outdir, name):
        # the script auto-nests output under <outdir>/<year>/
        with (Path(outdir) / "2025" / name).open(encoding="utf-8-sig", newline="") as fp:
            return list(csv.DictReader(fp))

    def test_negative_position_flag_includes_in_totals_and_warns(self):
        with tempfile.TemporaryDirectory() as outdir:
            self._run_negpos(outdir, [])      # default flag
            realized = self._read(outdir, "longbridge_2025_已实现盈亏_按标的.csv")
            detail = next(r for r in realized if r["代码"] == "HK700")
            self.assertEqual(float(detail["已实现盈亏(原币)"]), 60.0)
            self.assertIn("负持仓", detail["备注"])
            total = next(r for r in realized if r["代码"] == "合计" and r["货币"] == "HKD")
            self.assertEqual(float(total["已实现盈亏(原币)"]), 60.0)
            tax = self._read(outdir, "longbridge_2025_税务汇总.csv")
            cap = next(r for r in tax if r["所得项目"].startswith("财产转让") and r["货币"] == "HKD")
            self.assertEqual(float(cap["金额(原币)"]), 60.0)

    def test_negative_position_exclude_drops_from_totals_and_tax(self):
        with tempfile.TemporaryDirectory() as outdir:
            self._run_negpos(outdir, ["--on-negative-position", "exclude"])
            realized = self._read(outdir, "longbridge_2025_已实现盈亏_按标的.csv")
            detail = next(r for r in realized if r["代码"] == "HK700")
            self.assertEqual(float(detail["已实现盈亏(原币)"]), 60.0)   # still shown for transparency
            self.assertIn("排除", detail["备注"])
            self.assertFalse([r for r in realized if r["代码"] == "合计"])  # nothing left to total
            tax = self._read(outdir, "longbridge_2025_税务汇总.csv")
            self.assertFalse([r for r in tax if r["所得项目"].startswith("财产转让")])

    def test_negative_position_short_includes_without_warning(self):
        with tempfile.TemporaryDirectory() as outdir:
            self._run_negpos(outdir, ["--on-negative-position", "short"])
            realized = self._read(outdir, "longbridge_2025_已实现盈亏_按标的.csv")
            detail = next(r for r in realized if r["代码"] == "HK700")
            self.assertIn("做空", detail["备注"])
            self.assertNotIn("⚠", detail["备注"])
            total = next(r for r in realized if r["代码"] == "合计" and r["货币"] == "HKD")
            self.assertEqual(float(total["已实现盈亏(原币)"]), 60.0)

    def test_reports_group_tax_summary_by_currency_and_fx_rate(self):
        statement = self.multi_currency_statement()

        with tempfile.TemporaryDirectory() as outdir:
            with (
                patch.object(longbridge_tax, "ensure_cli"),
                patch.object(longbridge_tax, "list_keys", return_value={"202501": "k1"}),
                patch.object(longbridge_tax, "export", return_value=statement),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    longbridge_tax.main([
                        "--year",
                        "2025",
                        "-o",
                        outdir,
                        "--fx-rate",
                        "HKD=0.9",
                        "--fx-rate",
                        "USD=7.1",
                        "--allow-missing-months",
                    ])

            realized_path = Path(outdir) / "2025" / "longbridge_2025_已实现盈亏_按标的.csv"
            with realized_path.open(encoding="utf-8-sig", newline="") as fp:
                realized_rows = list(csv.DictReader(fp))
            realized_by_currency = {
                row["货币"]: float(row["已实现盈亏(原币)"])
                for row in realized_rows
                if row["代码"] == "合计"
            }
            self.assertEqual(realized_by_currency, {"HKD": 10.0, "USD": 3.0})

            tax_path = Path(outdir) / "2025" / "longbridge_2025_税务汇总.csv"
            with tax_path.open(encoding="utf-8-sig", newline="") as fp:
                tax_rows = list(csv.DictReader(fp))
            capital_rows = [r for r in tax_rows if r["所得项目"] == "财产转让所得·已实现(本账户股票/期权)"]
            self.assertEqual(
                {(r["货币"], float(r["金额(原币)"]), float(r["金额(RMB)"])) for r in capital_rows},
                {("HKD", 10.0, 9.0), ("USD", 3.0, 21.3)},
            )

            dividend_rows = [r for r in tax_rows if r["所得项目"] == "利息股息红利所得·现金分红(毛额)"]
            self.assertEqual(
                {(r["货币"], float(r["金额(原币)"]), float(r["金额(RMB)"])) for r in dividend_rows},
                {("HKD", 8.0, 7.2), ("USD", 2.0, 14.2)},
            )

    def test_uses_2025_year_end_default_fx_rates_when_none_are_passed(self):
        statement = self.multi_currency_statement()

        with tempfile.TemporaryDirectory() as outdir:
            with (
                patch.object(longbridge_tax, "ensure_cli"),
                patch.object(longbridge_tax, "list_keys", return_value={"202501": "k1"}),
                patch.object(longbridge_tax, "export", return_value=statement),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    longbridge_tax.main(["--year", "2025", "-o", outdir, "--allow-missing-months"])

            tax_path = Path(outdir) / "2025" / "longbridge_2025_税务汇总.csv"
            with tax_path.open(encoding="utf-8-sig", newline="") as fp:
                tax_rows = list(csv.DictReader(fp))
            capital_rows = [r for r in tax_rows if r["所得项目"] == "财产转让所得·已实现(本账户股票/期权)"]
            self.assertEqual(
                {(r["货币"], float(r["金额(原币)"]), float(r["金额(RMB)"])) for r in capital_rows},
                {("HKD", 10.0, 9.03), ("USD", 3.0, 21.09)},
            )

    def multi_currency_statement(self):
        statement = {
            "stock_trades": [
                {
                    "trade_date": "2025.01.01",
                    "settle_date": "2025.01.02",
                    "code": "HK001",
                    "name": "HK Stock",
                    "direction": "BUY",
                    "currency": "HKD",
                    "trade_quantity": "1",
                    "trade_price": "100",
                    "trade_amount": "100",
                    "clear_amount": "-100",
                },
                {
                    "trade_date": "2025.01.03",
                    "settle_date": "2025.01.06",
                    "code": "HK001",
                    "name": "HK Stock",
                    "direction": "SELL",
                    "currency": "HKD",
                    "trade_quantity": "1",
                    "trade_price": "110",
                    "trade_amount": "110",
                    "clear_amount": "110",
                },
                {
                    "trade_date": "2025.02.01",
                    "settle_date": "2025.02.03",
                    "code": "US001",
                    "name": "US Stock",
                    "direction": "BUY",
                    "currency": "USD",
                    "trade_quantity": "1",
                    "trade_price": "10",
                    "trade_amount": "10",
                    "clear_amount": "-10",
                },
                {
                    "trade_date": "2025.02.04",
                    "settle_date": "2025.02.05",
                    "code": "US001",
                    "name": "US Stock",
                    "direction": "SELL",
                    "currency": "USD",
                    "trade_quantity": "1",
                    "trade_price": "13",
                    "trade_amount": "13",
                    "clear_amount": "13",
                },
            ],
            "corps": [
                {"date": "2025.03.01", "currency": "HKD", "amount": "8", "remark": "dividend"},
                {"date": "2025.03.02", "currency": "USD", "amount": "2", "remark": "dividend"},
            ],
            "interests": [
                {"date": "2025.04.01", "currency": "HKD", "total": "-1", "rate": ""},
                {"date": "2025.04.02", "currency": "USD", "total": "-0.5", "rate": ""},
            ],
            "asset": [],
        }
        return statement


class IpoAllotmentTest(unittest.TestCase):
    def test_parses_allotment_remark(self):
        change = {"biz_code": "LIPOALDR", "amount": "-2905.00", "currency": "",
                  "remark": "IPO  6831.HK Allotted Amount (400 Shares @HKD 2,876.00)",
                  "type": "IPO Allotted Amount(Dr)"}
        self.assertEqual(longbridge_tax.ipo_allotment(change), ("HKD", "6831", 400.0, 2905.0))

    def test_subscription_debit_is_not_an_allotment(self):
        # LIPODR (subscription) is refunded later; only LIPOALDR (allotment) is a cost-basis event
        change = {"biz_code": "LIPODR", "amount": "-2905.00", "currency": "",
                  "remark": "IPO 6831.HK @2,905.00 (400 Shares) Financing: 0.00",
                  "type": "IPO Subscription Amount(Dr)"}
        self.assertIsNone(longbridge_tax.ipo_allotment(change))

    def _ipo_statement(self):
        # allotted 100 IPO shares costing 1000 (10/sh), later sold 100 @15/sh -> realized 500
        return {
            "stock_trades": [
                {"trade_date": "2025.06.10", "settle_date": "2025.06.12", "code": "2655",
                 "name": "GUOXIA TECH", "direction": "SELL", "currency": "HKD",
                 "trade_quantity": "100", "trade_price": "15", "trade_amount": "1500",
                 "clear_amount": "1500"},
            ],
            "account_balance_changes": [
                {"biz_code": "LIPOALDR", "amount": "-1000.00", "currency": "", "date": "2025.05.20",
                 "remark": "IPO  2655.HK Allotted Amount (100 Shares @HKD 1,000.00)",
                 "type": "IPO Allotted Amount(Dr)"},
            ],
            "corps": [], "interests": [], "asset": [],
        }

    def test_ipo_allotment_seeds_cost_basis_not_zero_cost_short(self):
        with tempfile.TemporaryDirectory() as outdir:
            with (
                patch.object(longbridge_tax, "ensure_cli"),
                patch.object(longbridge_tax, "list_keys", return_value={"202506": "k1"}),
                patch.object(longbridge_tax, "export", return_value=self._ipo_statement()),
            ):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    longbridge_tax.main(["--year", "2025", "-o", outdir, "--allow-missing-months"])
            with (Path(outdir) / "2025" / "longbridge_2025_已实现盈亏_按标的.csv").open(encoding="utf-8-sig", newline="") as fp:
                rows = list(csv.DictReader(fp))
            detail = next(r for r in rows if r["代码"] == "2655")
            self.assertEqual(float(detail["已实现盈亏(原币)"]), 500.0)   # 1500 - 1000, not the full 1500
            self.assertNotIn("负持仓", detail["备注"])
            with (Path(outdir) / "2025" / "longbridge_2025_股息利息现金流.csv").open(encoding="utf-8-sig", newline="") as fp:
                cash = list(csv.DictReader(fp))
            self.assertFalse([r for r in cash if "Allotted" in r["备注"]])   # cost basis, not cashflow

    def test_same_day_round_trip_not_flagged_as_short(self):
        # statement lists the SELL before the BUY on the same date; must not reconstruct a phantom short
        trades = [
            trade_row("2025/06/04", "9992", "POP MART", "HKD", 200, 47960.0),    # sell line first
            trade_row("2025/06/04", "9992", "POP MART", "HKD", 200, -48160.0),   # buy, same day
        ]
        b = longbridge_tax.realized_by_ticker(trades, {})[("HKD", "9992")]
        self.assertFalse(b["shorted"])
        self.assertAlmostEqual(b["pos"], 0.0)
        self.assertEqual(round(b["realized"], 2), -200.0)


class DividendCurrencyTest(unittest.TestCase):
    def test_settle_ccy_prefers_explicit_then_at_tag_then_market_suffix(self):
        self.assertEqual(longbridge_tax.settle_ccy({"currency": "usd"}, "remark"), "USD")
        self.assertEqual(longbridge_tax.settle_ccy({"currency": "", "remark": "@HKD 1.20/sh"}, "remark"), "HKD")
        self.assertEqual(longbridge_tax.settle_ccy({"currency": "", "symbol": "700.HK"}, "symbol"), "HKD")
        self.assertEqual(longbridge_tax.settle_ccy({"currency": "", "remark": "AAPL.US dividend"}, "remark"), "USD")
        # a bare amount line with no currency clue stays unknown (not silently mislabelled)
        self.assertEqual(longbridge_tax.settle_ccy({"currency": "", "remark": "cash dividend"}, "remark"), "")
        # a date-like dotted token must not be mistaken for a market suffix
        self.assertEqual(longbridge_tax.settle_ccy({"currency": "", "remark": "paid 2025.03"}, "remark"), "")

    def test_blank_currency_dividend_is_taxed_under_market_currency(self):
        # real Longbridge cash dividends often arrive with currency='' (see IpoAllotmentTest),
        # carrying the market only in the remark/symbol. They must still convert to RMB and be
        # taxed under that currency, not vanish into an unknown-currency bucket.
        statement = {
            "stock_trades": [], "interests": [], "asset": [],
            "corps": [
                {"date": "2025.04.01", "symbol": "AAPL.US", "amount": "12.5", "remark": "Cash Dividend"},
            ],
            "account_balance_changes": [
                {"biz_code": "DIVIDEND", "amount": "500", "currency": "", "date": "2025.03.15",
                 "remark": "Cash Dividend 700.HK", "type": "Dividend"},
            ],
        }
        with tempfile.TemporaryDirectory() as outdir:
            with (
                patch.object(longbridge_tax, "ensure_cli"),
                patch.object(longbridge_tax, "list_keys", return_value={"202503": "k1"}),
                patch.object(longbridge_tax, "export", return_value=statement),
            ):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    longbridge_tax.main(["--year", "2025", "-o", outdir, "--allow-missing-months",
                                         "--fx-rate", "HKD=0.9", "--fx-rate", "USD=7.0"])
            tax_path = Path(outdir) / "2025" / "longbridge_2025_税务汇总.csv"
            with tax_path.open(encoding="utf-8-sig", newline="") as fp:
                tax_rows = list(csv.DictReader(fp))
        div_rows = [r for r in tax_rows
                    if r["所得项目"] == "利息股息红利所得·现金分红(毛额)" and float(r["金额(原币)"]) > 0]
        self.assertEqual(
            {(r["货币"], float(r["金额(原币)"]), float(r["金额(RMB)"]), float(r["应纳税额(RMB)"])) for r in div_rows},
            {("HKD", 500.0, 450.0, 90.0), ("USD", 12.5, 87.5, 17.5)},
        )
        self.assertNotIn("", {r["货币"] for r in div_rows})   # no dividend left in the unknown bucket


class InterestCurrencyTest(unittest.TestCase):
    def test_blank_currency_interest_buckets_under_market_currency(self):
        # financing interest can also arrive with currency='' (the market in the symbol/remark);
        # it must bucket under that currency, not vanish into an unknown-currency row.
        statement = {
            "stock_trades": [], "corps": [], "asset": [],
            "interests": [
                {"date": "2025.05.01", "currency": "", "symbol": "700.HK", "total": "-3.5", "rate": "0.05"},
            ],
        }
        with tempfile.TemporaryDirectory() as outdir:
            with (
                patch.object(longbridge_tax, "ensure_cli"),
                patch.object(longbridge_tax, "list_keys", return_value={"202505": "k1"}),
                patch.object(longbridge_tax, "export", return_value=statement),
            ):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    longbridge_tax.main(["--year", "2025", "-o", outdir, "--allow-missing-months", "--fx-rate", "HKD=0.9"])
            cash_path = Path(outdir) / "2025" / "longbridge_2025_股息利息现金流.csv"
            with cash_path.open(encoding="utf-8-sig", newline="") as fp:
                cash_rows = list(csv.DictReader(fp))
            tax_path = Path(outdir) / "2025" / "longbridge_2025_税务汇总.csv"
            with tax_path.open(encoding="utf-8-sig", newline="") as fp:
                tax_rows = list(csv.DictReader(fp))
        interest_cash = [r for r in cash_rows if r["类型"] == "融资利息"]
        self.assertEqual({r["货币"] for r in interest_cash}, {"HKD"})
        interest_tax = [r for r in tax_rows
                        if r["所得项目"] == "(备查)融资利息支出" and float(r["金额(原币)"]) != 0]
        self.assertEqual({r["货币"] for r in interest_tax}, {"HKD"})


class OutputNestingTest(unittest.TestCase):
    _STMT = {"stock_trades": [], "corps": [], "interests": [], "asset": []}

    def _run(self, outdir):
        with (
            patch.object(longbridge_tax, "ensure_cli"),
            patch.object(longbridge_tax, "list_keys", return_value={"202501": "k1"}),
            patch.object(longbridge_tax, "export", return_value=self._STMT),
        ):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                longbridge_tax.main(["--year", "2025", "-o", outdir, "--allow-missing-months"])

    def test_year_is_auto_nested_under_outdir(self):
        with tempfile.TemporaryDirectory() as base:
            acct = str(Path(base) / "out_acctA")
            self._run(acct)
            self.assertTrue((Path(acct) / "2025" / "longbridge_2025_税务汇总.csv").exists())

    def test_outdir_already_ending_in_year_is_not_double_nested(self):
        with tempfile.TemporaryDirectory() as base:
            acct_year = str(Path(base) / "out_acctA" / "2025")
            self._run(acct_year)
            self.assertTrue((Path(acct_year) / "longbridge_2025_税务汇总.csv").exists())
            self.assertFalse((Path(acct_year) / "2025").exists())   # no .../2025/2025

    def test_default_outdir_is_plain_out(self):
        # No -o given: the account number is unknowable from the API, so the default
        # must be a plain `out/<year>/` — never a guessed per-account folder.
        with tempfile.TemporaryDirectory() as base:
            cwd = os.getcwd()
            os.chdir(base)
            try:
                with (
                    patch.object(longbridge_tax, "ensure_cli"),
                    patch.object(longbridge_tax, "list_keys", return_value={"202501": "k1"}),
                    patch.object(longbridge_tax, "export", return_value=self._STMT),
                ):
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        longbridge_tax.main(["--year", "2025", "--allow-missing-months"])
                self.assertTrue((Path(base) / "out" / "2025" / "longbridge_2025_税务汇总.csv").exists())
            finally:
                os.chdir(cwd)


class OptionNetcashTest(unittest.TestCase):
    """Sell-to-open option contracts left 'negative' by the reconstruction: the default
    netcash mode must book their realized P&L as the net option cash flow, cross-checked
    against the official OptionExpire/OptionExercise settlement events."""

    def _stmt(self):
        return {
            "stock_trades": [],
            "option_trades": [
                # sell 1 contract @2.25, never bought back -> expired worthless in-year;
                # option-side realized must be the +225 premium (flag mode books 0)
                {"trade_date": "2025.01.10", "settle_date": "2025.01.13", "code": "NVDL250117C70000",
                 "name": "NVDL 250117 70 Call", "direction": "SELL", "currency": "USD",
                 "trade_quantity": "1", "trade_price": "2.25", "trade_amount": "225",
                 "clear_amount": "225"},
                # sell 1 contract, expiry 2026-01: still open at year-end -> must be EXCLUDED
                {"trade_date": "2025.12.15", "settle_date": "2025.12.16", "code": "NVDL260109C100000",
                 "name": "NVDL 260109 100 Call", "direction": "SELL", "currency": "USD",
                 "trade_quantity": "1", "trade_price": "0.60", "trade_amount": "60",
                 "clear_amount": "59.02"},
            ],
            "equity_holding_changes": [
                {"code": "NVDL250117C70000", "date": "2025.01.20", "market": "US",
                 "name": "NVDL 250117 70 Call", "quantity": "1.00", "type": "OptionExpire"},
                {"code": "NVDL250130P50000", "date": "2025.02.03", "market": "US",
                 "name": "NVDL 250130 50 Put", "quantity": "1.00", "type": "OptionExercise"},
            ],
            "corps": [], "interests": [], "asset": [],
        }

    def _run(self, outdir, extra=()):
        with (
            patch.object(longbridge_tax, "ensure_cli"),
            patch.object(longbridge_tax, "list_keys", return_value={"202501": "k1"}),
            patch.object(longbridge_tax, "export", return_value=self._stmt()),
        ):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                longbridge_tax.main(["--year", "2025", "-o", outdir, "--allow-missing-months", *extra])

    def _read(self, outdir, name):
        with (Path(outdir) / "2025" / name).open(encoding="utf-8-sig", newline="") as fp:
            return list(csv.DictReader(fp))

    def test_expired_worthless_option_realizes_premium_by_default(self):
        with tempfile.TemporaryDirectory() as outdir:
            self._run(outdir)                      # netcash is the default
            realized = self._read(outdir, "longbridge_2025_已实现盈亏_按标的.csv")
            detail = next(r for r in realized if r["代码"] == "NVDL250117C70000")
            self.assertEqual(float(detail["已实现盈亏(原币)"]), 225.0)
            self.assertIn("净现金流", detail["备注"])
            self.assertIn("到期作废", detail["备注"])       # cross-checked vs official event
            total = next(r for r in realized if r["代码"] == "合计" and r["货币"] == "USD")
            self.assertEqual(float(total["已实现盈亏(原币)"]), 225.0)   # 2026 contract excluded

    def test_still_open_option_is_excluded_as_unrealized(self):
        with tempfile.TemporaryDirectory() as outdir:
            self._run(outdir)
            realized = self._read(outdir, "longbridge_2025_已实现盈亏_按标的.csv")
            detail = next(r for r in realized if r["代码"] == "NVDL260109C100000")
            self.assertIn("仍未平仓", detail["备注"])
            tax = self._read(outdir, "longbridge_2025_税务汇总.csv")
            cap = [r for r in tax if r["所得项目"].startswith("财产转让")]
            self.assertEqual([float(r["金额(原币)"]) for r in cap], [225.0])

    def test_option_events_csv_records_official_settlements(self):
        with tempfile.TemporaryDirectory() as outdir:
            self._run(outdir)
            events = self._read(outdir, "longbridge_2025_期权结算事件.csv")
            self.assertEqual({(r["类型"], r["代码"], float(r["数量(张)"])) for r in events},
                             {("到期作废", "NVDL250117C70000", 1.0),
                              ("行权指派", "NVDL250130P50000", 1.0)})

    def test_carried_in_short_option_uses_seed_cost_basis(self):
        # prior-December holds a SHORT 2 contracts @1.61/share premium; 2025 buys both back
        # @0.35/contract -> realized = (161 - 35) * 2 = 252 (seed cost must be x100'd to
        # per-contract units, and the negative qty must NOT be skipped)
        stmt = {
            "stock_trades": [],
            "option_trades": [
                {"trade_date": "2025.01.20", "settle_date": "2025.01.21", "code": "NVDL250124P50000",
                 "name": "NVDL 250124 50 Put", "direction": "BUY", "currency": "USD",
                 "trade_quantity": "2", "trade_price": "0.35", "trade_amount": "70",
                 "clear_amount": "-70"},
            ],
            "corps": [], "interests": [], "asset": [],
        }
        prior = {"equity_holdings": [
            {"code": "NVDL250124P50000", "currency": "USD", "equity_type": "OP",
             "ledger_quantity": "-2", "cost_price": "1.61", "name": "NVDL 250124 50 Put"},
        ], "stock_trades": [], "corps": [], "interests": [], "asset": []}

        def fake_export(file_key, cache_path=None, refresh=False):
            return prior if (cache_path or "").endswith("202412.json") else stmt

        with tempfile.TemporaryDirectory() as outdir:
            with (
                patch.object(longbridge_tax, "ensure_cli"),
                patch.object(longbridge_tax, "list_keys",
                             return_value={"202412": "k0", "202501": "k1"}),
                patch.object(longbridge_tax, "export", side_effect=fake_export),
            ):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    longbridge_tax.main(["--year", "2025", "-o", outdir, "--allow-missing-months"])
            with (Path(outdir) / "2025" / "longbridge_2025_已实现盈亏_按标的.csv").open(
                    encoding="utf-8-sig", newline="") as fp:
                rows = list(csv.DictReader(fp))
            detail = next(r for r in rows if r["代码"] == "NVDL250124P50000")
            self.assertEqual(float(detail["已实现盈亏(原币)"]), 252.0)
            self.assertNotIn("负持仓", detail["备注"])

    def test_flag_mode_keeps_old_behaviour_for_options(self):
        with tempfile.TemporaryDirectory() as outdir:
            self._run(outdir, ["--on-negative-position", "flag"])
            realized = self._read(outdir, "longbridge_2025_已实现盈亏_按标的.csv")
            detail = next(r for r in realized if r["代码"] == "NVDL250117C70000")
            # settlement events now close the expired contract in every mode, so the
            # premium is booked even under the legacy flag mode
            self.assertEqual(float(detail["已实现盈亏(原币)"]), 225.0)
            self.assertIn("负持仓", detail["备注"])


class WithholdingCreditTest(unittest.TestCase):
    def test_withholding_row_is_reported_and_reduces_tax_due(self):
        statement = {
            "stock_trades": [], "interests": [], "asset": [],
            "account_balance_changes": [
                {"biz_code": "LCDIV", "amount": "26.22", "currency": "USD", "date": "2025.01.02",
                 "remark": "TMF.US Cash Dividend: 0.5243 USD per Share , Held:50",
                 "type": "Cash Dividend"},
                {"biz_code": "LCOTHER", "amount": "-2.62", "currency": "USD", "date": "2025.01.02",
                 "remark": "TMF.US Cash Dividend: 0.5243 USD per Share Withholding Tax/Dividend Fee",
                 "type": "CO Other FEE"},
            ],
        }
        with tempfile.TemporaryDirectory() as outdir:
            with (
                patch.object(longbridge_tax, "ensure_cli"),
                patch.object(longbridge_tax, "list_keys", return_value={"202501": "k1"}),
                patch.object(longbridge_tax, "export", return_value=statement),
            ):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    longbridge_tax.main(["--year", "2025", "-o", outdir, "--allow-missing-months", "--fx-rate", "USD=7.0"])
            with (Path(outdir) / "2025" / "longbridge_2025_税务汇总.csv").open(
                    encoding="utf-8-sig", newline="") as fp:
                tax_rows = list(csv.DictReader(fp))
        wh = next(r for r in tax_rows if r["所得项目"] == "(抵免项)境外已预扣税额")
        self.assertEqual(float(wh["金额(原币)"]), -2.62)
        self.assertEqual(float(wh["金额(RMB)"]), -18.34)
        total = next(r for r in tax_rows if r["所得项目"].startswith("合计"))
        # 26.22 * 7.0 * 20% = 36.708 -> 36.71 gross tax, minus 18.34 credit = 18.37
        self.assertEqual(float(total["应纳税额(RMB)"]), 18.37)


class SettlementCurrencyWordTest(unittest.TestCase):
    def test_bare_currency_word_in_remark_is_recognised(self):
        # ISIN-style ids (US8740391003) defeat the CODE.MARKET suffix; the remark still
        # names the currency, and without this the TSM ADR dividends escaped RMB conversion.
        self.assertEqual(
            longbridge_tax.settle_ccy(
                {"currency": "",
                 "remark": "TSM(US8740391003) Cash Dividend USD 0.608106 per Share"}, "remark"),
            "USD")
        self.assertEqual(
            longbridge_tax.settle_ccy({"currency": "", "remark": "利率 5.75%"}, "remark"), "")

    def test_option_expiry_parses_us_option_codes(self):
        self.assertEqual(longbridge_tax.option_expiry("NVDL250117C70000"), (2025, 1, 17))
        self.assertEqual(longbridge_tax.option_expiry("TSM250808C237500"), (2025, 8, 8))
        self.assertIsNone(longbridge_tax.option_expiry("AAPL"))
        self.assertIsNone(longbridge_tax.option_expiry(""))




class DistributionCharacterTest(unittest.TestCase):
    """Distributions are not all dividends: a year-end capital-gain distribution and a
    return of capital must not be booked as 股息, and ROC must reduce the cost basis.
    The character comes from a user file — never from the broker's 摊薄成本."""

    def _stmt(self):
        return {
            "stock_trades": [
                {"trade_date": "2025.01.02", "settle_date": "2025.01.03", "code": "ABC",
                 "name": "ABC ETF", "direction": "BUY", "currency": "USD",
                 "trade_quantity": "10", "trade_price": "100", "trade_amount": "1000",
                 "clear_amount": "-1000"},
                {"trade_date": "2025.06.02", "settle_date": "2025.06.03", "code": "ABC",
                 "name": "ABC ETF", "direction": "SELL", "currency": "USD",
                 "trade_quantity": "10", "trade_price": "110", "trade_amount": "1100",
                 "clear_amount": "1100"},
            ],
            "account_balance_changes": [
                {"date": "2025.03.01", "biz_code": "LCDIV", "type": "Cash Dividend",
                 "currency": "USD", "amount": "100.00",
                 "remark": "ABC.US Cash Dividend: 1.00 USD per Share , Held:10"},
            ],
            "corps": [], "interests": [], "asset": [],
        }

    def _run(self, args, outdir):
        with (
            patch.object(longbridge_tax, "ensure_cli"),
            patch.object(longbridge_tax, "list_keys", return_value={"202501": "k1"}),
            patch.object(longbridge_tax, "export", return_value=self._stmt()),
        ):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                longbridge_tax.main(["--year", "2025", "-o", outdir,
                                     "--allow-missing-months", *args])
        return outdir

    def _rows(self, outdir, name):
        with open(Path(outdir) / "2025" / name, encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh))

    def _dist_file(self, outdir, character):
        path = Path(outdir) / "dist.csv"
        path.write_text(f"date,code,character,amount,currency\n2025-03-01,ABC,{character},100,USD\n",
                        encoding="utf-8")
        return str(path)

    def test_return_of_capital_reduces_cost_and_defers_gain(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(["--distributions", self._dist_file(tmp, "roc")], tmp)
            realized = self._rows(out, "longbridge_2025_已实现盈亏_按标的.csv")[-1]
            self.assertEqual(float(realized["已实现盈亏(原币)"]), 200.0)   # 100 price + 100 deferred ROC
            tax = {r["所得项目"]: r for r in self._rows(out, "longbridge_2025_税务汇总.csv")}
            self.assertEqual(float(tax["利息股息红利所得·现金分红(毛额)"]["金额(原币)"]), 0.0)  # ROC is not income
            dist = self._rows(out, "longbridge_2025_分配明细.csv")
            self.assertEqual(dist[0]["性质"], "资本返还")

    def test_capital_gain_distribution_is_property_income_and_keeps_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(["--distributions", self._dist_file(tmp, "capital_gain")], tmp)
            realized = self._rows(out, "longbridge_2025_已实现盈亏_按标的.csv")[-1]
            self.assertEqual(float(realized["已实现盈亏(原币)"]), 200.0)   # 100 price + 100 distribution
            tax = {r["所得项目"]: r for r in self._rows(out, "longbridge_2025_税务汇总.csv")}
            self.assertIn("  其中：资本利得分配(财产转让)", tax)
            self.assertEqual(float(tax["  其中：资本利得分配(财产转让)"]["金额(原币)"]), 100.0)
            self.assertEqual(float(tax["利息股息红利所得·现金分红(毛额)"]["金额(原币)"]), 0.0)

    def test_default_is_dividend_and_is_listed_as_unclassified(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run([], tmp)
            tax = {r["所得项目"]: r for r in self._rows(out, "longbridge_2025_税务汇总.csv")}
            self.assertEqual(float(tax["利息股息红利所得·现金分红(毛额)"]["金额(原币)"]), 100.0)
            dist = self._rows(out, "longbridge_2025_分配明细.csv")
            self.assertEqual(dist[0]["性质"], "股息")
            self.assertIn("待确认", dist[0]["备注"])

    def test_strict_mode_refuses_to_guess_and_writes_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                self._run(["--on-unclassified-distribution", "error"], tmp)

    def test_roc_excess_over_basis_is_immediate_gain(self):
        trades = [
            trade_row("2025/01/02", "ABC", "ABC ETF", "USD", 10, -100),
        ]
        book = longbridge_tax.realized_by_ticker(
            trades, {}, [{"date": "2025-03-01", "code": "ABC", "currency": "USD", "amount": 150}])
        self.assertAlmostEqual(book[("USD", "ABC")]["realized"], 50.0, places=2)
        self.assertEqual(book[("USD", "ABC")]["avg"], 0.0)

    def test_ambiguous_rule_raises_instead_of_guessing(self):
        dists = [{"date": "2025-01-01", "code": "ABC", "amount": 10.0, "currency": "USD"}]
        rules = [
            {"date": "", "code": "ABC", "amount": None, "character": "dividend"},
            {"date": "", "code": "ABC", "amount": None, "character": "roc"},
        ]
        with self.assertRaises(ValueError):
            longbridge_tax.classify_distributions(dists, rules)

    def test_load_distributions_accepts_aliases_and_rejects_bad_character(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "d.csv"
            good.write_text("date,code,character,amount\n2025-01-01,ABC,capital-gain,10\n", encoding="utf-8")
            rows = longbridge_tax.load_distributions(str(good))
            self.assertEqual(rows[0]["character"], "capital_gain")
            bad = Path(tmp) / "bad.csv"
            bad.write_text("date,code,character\n2025-01-01,ABC,whatever\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                longbridge_tax.load_distributions(str(bad))


class TradeDateAttributionTest(unittest.TestCase):
    """Statements are cut by settlement month: January's statement carries the prior
    December's trades, and 30/31 December trades only show up in the NEXT January's
    statement. Attribution must follow the trade date, or the year is both over- and
    under-stated."""

    def _trade(self, date, side, qty, price, net):
        return {"trade_date": date, "settle_date": date, "code": "ABC", "name": "ABC ETF",
                "direction": side, "currency": "USD", "trade_quantity": str(qty),
                "trade_price": str(price), "trade_amount": str(abs(net)),
                "clear_amount": str(net)}

    def _run(self, outdir, statements):
        def fake_export(key, *args, **kwargs):
            return statements[key]
        with (
            patch.object(longbridge_tax, "ensure_cli"),
            patch.object(longbridge_tax, "list_keys", return_value={k: k for k in statements}),
            patch.object(longbridge_tax, "export", side_effect=fake_export),
        ):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                longbridge_tax.main(["--year", "2025", "-o", outdir, "--allow-missing-months"])

    def _realized(self, outdir):
        with open(Path(outdir) / "2025" / "longbridge_2025_已实现盈亏_按标的.csv",
                  encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        return float(rows[-1]["已实现盈亏(原币)"])

    def test_prior_december_trade_in_january_statement_is_excluded(self):
        stmt = {"stock_trades": [
            self._trade("2024.12.30", "BUY", 10, 50, -500),    # prior year -> ignore
            self._trade("2025.01.02", "BUY", 10, 100, -1000),
            self._trade("2025.06.02", "SELL", 10, 110, 1100),
        ], "corps": [], "interests": [], "asset": []}
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp, {"202501": stmt})
            self.assertEqual(self._realized(tmp), 100.0)       # 1000 -> 1100, not 750 avg

    def test_late_december_trade_from_next_january_is_included(self):
        jan = {"stock_trades": [self._trade("2025.01.02", "BUY", 10, 100, -1000)],
               "corps": [], "interests": [], "asset": []}
        nxt = {"stock_trades": [self._trade("2025.12.31", "SELL", 10, 120, 1200)],
               "corps": [], "interests": [], "asset": []}
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp, {"202501": jan, "202601": nxt})
            self.assertEqual(self._realized(tmp), 200.0)


class ManualAdjustmentTest(unittest.TestCase):
    """Long-tail events (splits, non-cash distributions, broker reclassifications) are not
    enumerated in code: they map onto one of four effects — quantity / cost / realized /
    income — via --adjustments."""

    def _stmt(self, trades):
        return {"stock_trades": trades, "corps": [], "interests": [], "asset": []}

    def _trade(self, date, side, qty, price, net):
        return {"trade_date": date, "settle_date": date, "code": "ABC", "name": "ABC ETF",
                "direction": side, "currency": "USD", "trade_quantity": str(qty),
                "trade_price": str(price), "trade_amount": str(abs(net)),
                "clear_amount": str(net)}

    def _run(self, outdir, trades, adj_rows):
        path = Path(outdir) / "adj.csv"
        path.write_text("date,code,type,amount,currency,character\n" + "\n".join(adj_rows) + "\n",
                        encoding="utf-8")
        stmt = self._stmt(trades)
        with (
            patch.object(longbridge_tax, "ensure_cli"),
            patch.object(longbridge_tax, "list_keys", return_value={"202501": "k1"}),
            patch.object(longbridge_tax, "export", return_value=stmt),
        ):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                longbridge_tax.main(["--year", "2025", "-o", outdir, "--allow-missing-months",
                                     "--adjustments", str(path)])

    def _realized(self, outdir):
        with open(Path(outdir) / "2025" / "longbridge_2025_已实现盈亏_按标的.csv",
                  encoding="utf-8-sig") as fh:
            return float(list(csv.DictReader(fh))[-1]["已实现盈亏(原币)"])

    def test_split_factor_reprices_quantity_and_unit_cost(self):
        trades = [self._trade("2025.01.02", "BUY", 10, 100, -1000),
                  self._trade("2025.06.02", "SELL", 20, 60, 1200)]
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp, trades, ["2025-03-01,ABC,quantity,2,USD,"])
            self.assertEqual(self._realized(tmp), 200.0)      # (60 - 50) * 20

    def test_cost_adjustment_shifts_basis(self):
        trades = [self._trade("2025.01.02", "BUY", 10, 100, -1000),
                  self._trade("2025.06.02", "SELL", 10, 110, 1100)]
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp, trades, ["2025-03-01,ABC,cost,100,USD,non-cash distribution"])
            self.assertEqual(self._realized(tmp), 0.0)        # (110 - 110) * 10

    def test_realized_and_income_enter_the_right_buckets(self):
        trades = [self._trade("2025.01.02", "BUY", 10, 100, -1000),
                  self._trade("2025.06.02", "SELL", 10, 110, 1100)]
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp, trades, ["2025-04-01,ABC,realized,50,USD,reclass",
                                    "2025-05-01,ABC,income,30,USD,dividend"])
            self.assertEqual(self._realized(tmp), 150.0)
            with open(Path(tmp) / "2025" / "longbridge_2025_税务汇总.csv", encoding="utf-8-sig") as fh:
                tax = {r["所得项目"]: r for r in csv.DictReader(fh)}
            self.assertEqual(float(tax["利息股息红利所得·现金分红(毛额)"]["金额(原币)"]), 30.0)

    def test_invalid_adjustment_type_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.csv"
            p.write_text("date,code,type,amount\n2025-01-01,ABC,whatever,1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                longbridge_tax.load_manual_adjustments(str(p))


if __name__ == "__main__":
    unittest.main()
