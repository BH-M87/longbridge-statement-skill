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
                longbridge_tax.main(["--year", "2025", "-o", outdir, *extra])

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
                    longbridge_tax.main(["--year", "2025", "-o", outdir])

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
                    longbridge_tax.main(["--year", "2025", "-o", outdir])
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
                    longbridge_tax.main(["--year", "2025", "-o", outdir,
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
                    longbridge_tax.main(["--year", "2025", "-o", outdir, "--fx-rate", "HKD=0.9"])
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
                longbridge_tax.main(["--year", "2025", "-o", outdir])

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
                        longbridge_tax.main(["--year", "2025"])
                self.assertTrue((Path(base) / "out" / "2025" / "longbridge_2025_税务汇总.csv").exists())
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
