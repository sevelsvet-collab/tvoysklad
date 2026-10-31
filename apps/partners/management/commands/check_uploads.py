"""Диагностика сканов договоров: только чтение, ничего не записывает.

    python manage.py check_uploads

Показывает сканы, загруженные до появления проверки типа: файл другого типа,
содержимое не соответствует расширению, файла нет на диске.

Исправляющего режима нет: скан может быть единственной копией подписанного
договора, что с ним делать — решает человек. Пока файл на месте, программа отдаёт
его только на скачивание, если его тип нельзя показывать в браузере.
"""
from django.core.management.base import BaseCommand

from apps.core.uploads import scan_problem
from apps.partners.models import Contract


class Command(BaseCommand):
    help = "Показывает сканы договоров недопустимого типа. Ничего не изменяет."

    def handle(self, *args, **options):
        self.stdout.write("Проверка сканов договоров (только чтение).")
        found = 0
        checked = 0
        for contract in Contract.objects.exclude(scan="").order_by("pk"):
            checked += 1
            problem = _problem(contract.scan)
            if problem:
                found += 1
                self.stdout.write(f"  Договор {contract} (id {contract.pk}): {contract.scan.name} — {problem}")

        self.stdout.write("")
        self.stdout.write(f"Проверено сканов: {checked}.")
        if found:
            self.stdout.write(self.style.WARNING(f"Найдено: {found}. Ничего не изменено."))
        else:
            self.stdout.write(self.style.SUCCESS("Замечаний нет."))


def _problem(scan):
    try:
        with scan.open("rb") as stream:
            return scan_problem(scan.name, stream)
    except FileNotFoundError:
        return "файла нет на диске"
