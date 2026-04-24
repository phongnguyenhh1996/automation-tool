import unittest

from automation_tool.tradingview_symbol_last import parse_tv_symbol_last_value


class TestParseTvSymbolLastValue(unittest.TestCase):
    def test_none(self) -> None:
        self.assertIsNone(parse_tv_symbol_last_value(""))
        self.assertIsNone(parse_tv_symbol_last_value("   "))

    def test_single_token_with_commas(self) -> None:
        self.assertEqual(parse_tv_symbol_last_value("4,709.0"), 4709.0)
        self.assertEqual(parse_tv_symbol_last_value("-1,234.50"), -1234.5)

    def test_nested_span_digits_concat(self) -> None:
        # Common inner_text renderings when last digits are in a nested <span>
        self.assertEqual(parse_tv_symbol_last_value("4,709.0\n60"), 4709.06)
        self.assertEqual(parse_tv_symbol_last_value("4,709.0 60"), 4709.06)
        self.assertEqual(parse_tv_symbol_last_value("4,709.060"), 4709.06)

    def test_fallback_first_token(self) -> None:
        # If tokenization doesn't match the nested-span pattern, fall back to the first token.
        self.assertEqual(parse_tv_symbol_last_value("4709 60"), 4709.0)

