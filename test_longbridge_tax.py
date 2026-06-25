import csv
import contextlib
import io
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

            realized_path = Path(outdir) / "longbridge_2025_已实现盈亏_按标的.csv"
            with realized_path.open(encoding="utf-8-sig", newline="") as fp:
                realized_rows = list(csv.DictReader(fp))
            realized_by_currency = {
                row["货币"]: float(row["已实现盈亏(原币)"])
                for row in realized_rows
                if row["代码"] == "合计"
            }
            self.assertEqual(realized_by_currency, {"HKD": 10.0, "USD": 3.0})

            tax_path = Path(outdir) / "longbridge_2025_税务汇总.csv"
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

            tax_path = Path(outdir) / "longbridge_2025_税务汇总.csv"
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


if __name__ == "__main__":
    unittest.main()
