import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "a-share-financial-report"
    / "scripts"
    / "analyze_a_share_report.py"
)

spec = importlib.util.spec_from_file_location("analyze_a_share_report", SCRIPT_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class AnalyzeAShareReportTest(unittest.TestCase):
    def test_normalize_symbol_variants(self):
        self.assertEqual(module.normalize_symbol("600519").em_symbol, "SH600519")
        self.assertEqual(module.normalize_symbol("600519.SH").sina_symbol, "sh600519")
        self.assertEqual(module.normalize_symbol("sz000001").em_symbol, "SZ000001")
        self.assertEqual(module.normalize_symbol("BJ430047").sina_symbol, "bj430047")

    def test_missing_akshare_message(self):
        def fake_import(name):
            if name == "akshare":
                raise ImportError(name)
            return object()

        with self.assertRaises(module.FriendlyError) as ctx:
            module.import_runtime_dependencies(fake_import)

        self.assertIn("akshare", str(ctx.exception))
        self.assertIn("python -m pip install -U akshare", str(ctx.exception))

    def test_cash_flow_mismatch_is_red_signal(self):
        rows = [
            {
                "report_date": "2025-12-31",
                "parent_net_profit": 100_000_000.0,
                "operating_cash_flow": -20_000_000.0,
                "cfo_to_profit": -0.2,
            }
        ]

        signals = module.assess_financial_signals(rows)

        self.assertTrue(
            any(signal["level"] == "red" and signal["topic"] == "现金流质量" for signal in signals)
        )

    def test_financial_profile_skips_inventory_signal(self):
        rows = [
            {
                "report_date": "2025-12-31",
                "revenue_yoy": 0.02,
                "inventory_yoy": 0.80,
            }
        ]

        signals = module.assess_financial_signals(rows, industry_profile="financial")

        self.assertFalse(any(signal["metric"] == "存货同比" for signal in signals))

    def test_auto_profile_can_use_company_name(self):
        profile = module.classify_industry("auto", {"股票简称": "平安银行"})

        self.assertEqual(profile, "financial")

    def test_disclosure_keyword_signal(self):
        signals = module.disclosure_risk_signals(
            [
                {
                    "公告标题": "关于前期会计差错更正及补充公告",
                    "公告时间": "2025-04-30",
                    "公告链接": "https://example.invalid/report.pdf",
                }
            ]
        )

        self.assertEqual(signals[0]["level"], "yellow")
        self.assertEqual(signals[0]["topic"], "披露风险")


if __name__ == "__main__":
    unittest.main()
