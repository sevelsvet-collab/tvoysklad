"""Сумма прописью и склонения для печатных форм."""
from decimal import Decimal

from num2words import num2words

CENTS = Decimal("0.01")


def _plural(number, forms):
    """forms = (для 1, для 2-4, для 5-0). Русские правила склонения."""
    n = abs(int(number)) % 100
    if 11 <= n <= 14:
        return forms[2]
    d = n % 10
    if d == 1:
        return forms[0]
    if 2 <= d <= 4:
        return forms[1]
    return forms[2]


def rubles_kopecks(amount):
    amount = Decimal(amount).quantize(CENTS)
    rub = int(amount)
    kop = int((amount - rub) * 100)
    return rub, kop


def amount_to_words(amount):
    """1234.56 → «Одна тысяча двести тридцать четыре рубля 56 копеек»."""
    rub, kop = rubles_kopecks(amount)
    words = num2words(rub, lang="ru")
    words = words[0].upper() + words[1:]
    rub_word = _plural(rub, ("рубль", "рубля", "рублей"))
    kop_word = _plural(kop, ("копейка", "копейки", "копеек"))
    return f"{words} {rub_word} {kop:02d} {kop_word}"


def amount_short(amount):
    """1234.56 → «1234 руб. 56 коп.» для строки итога."""
    rub, kop = rubles_kopecks(amount)
    return f"{rub} руб. {kop:02d} коп."
