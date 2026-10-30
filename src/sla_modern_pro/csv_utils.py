"""Escrita de CSV protegida contra injeção de fórmulas (CSV/Formula Injection).

Nomes de host, item, trigger e estado vêm do Zabbix. Um valor como "=HYPERLINK(...)"
viraria fórmula ao abrir o CSV no Excel/LibreOffice, então células que começam com
caractere de fórmula recebem um apóstrofo na frente (o Excel exibe o texto literal).
"""
from __future__ import annotations

from typing import Any, Iterable, TextIO
import csv

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def sanitize_cell(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


class SafeCsvWriter:
    def __init__(self, handle: TextIO, **kwargs: Any) -> None:
        self._writer = csv.writer(handle, **kwargs)

    def writerow(self, row: Iterable[Any]) -> Any:
        return self._writer.writerow([sanitize_cell(value) for value in row])

    def writerows(self, rows: Iterable[Iterable[Any]]) -> None:
        for row in rows:
            self.writerow(row)


def writer(handle: TextIO, **kwargs: Any) -> SafeCsvWriter:
    return SafeCsvWriter(handle, **kwargs)
