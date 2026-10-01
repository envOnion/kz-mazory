import re

from django.core.exceptions import ValidationError


def normalize_phone(value):
    digits = re.sub(r"[^0-9]", "", value)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if not re.fullmatch(r"[1-9][0-9]{9,14}", digits):
        raise ValidationError("Укажите корректный номер телефона.")
    return digits
