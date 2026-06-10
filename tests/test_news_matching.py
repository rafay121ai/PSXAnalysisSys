"""Regression tests for strict stock-name matching in news text."""

import unittest

from scrapers.news import _mentioned_symbols


class NewsMatchingTests(unittest.TestCase):
    def test_kohat_textile_mills_does_not_match_hirat_class_name(self) -> None:
        stocks = [{"symbol": "HIRAT", "company_name": "Hira Textile Mills Limited"}]

        self.assertEqual(_mentioned_symbols("Kohat Textile Mills posts results", stocks), [])

    def test_k_electric_matches_after_trailing_legal_suffix_is_stripped(self) -> None:
        stocks = [{"symbol": "KEL", "company_name": "K-Electric Limited"}]

        self.assertEqual(_mentioned_symbols("K-Electric announces tariff update", stocks), ["KEL"])


if __name__ == "__main__":
    unittest.main()
