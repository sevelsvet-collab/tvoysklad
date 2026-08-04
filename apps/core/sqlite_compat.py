"""Регистронезависимый LIKE для кириллицы в SQLite.

Встроенный LIKE в SQLite игнорирует регистр только для ASCII, поэтому
поиск «ромашка» не находил «Ромашка» при локальной разработке.
На PostgreSQL (прод) не влияет.
"""
import re


def _like_to_regex(pattern, escape=None):
    regex = ""
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if escape and char == escape and i + 1 < len(pattern):
            regex += re.escape(pattern[i + 1])
            i += 2
            continue
        if char == "%":
            regex += ".*"
        elif char == "_":
            regex += "."
        else:
            regex += re.escape(char)
        i += 1
    return regex


def _sqlite_like(pattern, value, escape=None):
    if pattern is None or value is None:
        return False
    regex = _like_to_regex(str(pattern), escape)
    return re.fullmatch(regex, str(value), re.IGNORECASE | re.DOTALL) is not None


def install_unicode_like(sender, connection, **kwargs):
    if connection.vendor == "sqlite":
        connection.connection.create_function("LIKE", 2, _sqlite_like)
        connection.connection.create_function("LIKE", 3, _sqlite_like)
