"""Detect financial claims without treating dates, IDs or list numbers as money."""

import re

FINANCIAL_VALUE = re.compile(
    r"\d[\d\s.,]*\s*(?:₸|\$|€|%|KZT\b|USD\b|EUR\b|RUB\b|тенге\b|руб(?:лей|ля)?\b|млн\b|млрд\b|миллион\w*|миллиард\w*)",
    re.IGNORECASE,
)


def financial_values(text):
    return bool(FINANCIAL_VALUE.search(text))
