"""Диагностика возвратов по основанию: только чтение, ничего не записывает.

    python manage.py check_returns

Показывает нарушения, которые могли накопиться до появления проверки:

* по основанию возвращено больше, чем в нём было (основание, товар, количество
  в основании, возвращено, превышение, связанные возвраты);
* организация или контрагент возврата не совпадают с основанием;
* возврат стоит в хронологии не позже своего основания;
* строка проведённого возврата покупателя ссылается на строку не своей
  отгрузки-основания (основание сменили или сняли мимо перепроведения).

Исправляющего режима нет: что делать с лишним возвратом — отменить, уменьшить
или убрать основание — решает человек. Правила работы с такими документами
описаны в docs/returns.md.
"""
from django.core.management.base import BaseCommand

from apps.core.constants import DOC_POSTED
from apps.inventory import return_limits
from apps.inventory.services import _plain


class Command(BaseCommand):
    help = "Показывает возвраты, превышающие количество в основании. Ничего не изменяет."

    def handle(self, *args, **options):
        found = return_limits.violations()
        quantity = [v for v in found if v["kind"] == "quantity"]
        party = [v for v in found if v["kind"] == "party"]
        chronology = [v for v in found if v["kind"] == "chronology"]
        links = [v for v in found if v["kind"] == "line_link"]
        self.stdout.write("Проверка возвратов по основанию (только чтение).")

        if quantity:
            self.stdout.write("")
            self.stdout.write(f"Возвращено больше, чем было в основании: {len(quantity)}")
        for v in quantity:
            config, basis = v["config"], v["basis"]
            state = "" if basis.status == DOC_POSTED else " (НЕ ПРОВЕДЕНА)"
            self.stdout.write(
                f"  {config['basis_title']} {_doc(basis)}{state}, товар {return_limits._product_name(v['product_id'])} "
                f"(id {v['product_id']}): в основании {_plain(v['in_basis'])}, возвращено {_plain(v['returned'])}, "
                f"превышение {_plain(v['excess'])}"
            )
            for ret in v["returns"]:
                amount = return_limits.line_quantities(ret)[v["product_id"]]
                self.stdout.write(f"      {config['title']} {_doc(ret)} — {_plain(amount)}")

        if party:
            self.stdout.write("")
            self.stdout.write(f"Организация или контрагент возврата не совпадают с основанием: {len(party)}")
        for v in party:
            config, basis, ret = v["config"], v["basis"], v["returns"][0]
            partner = config["partner"]
            self.stdout.write(
                f"  {config['title']} {_doc(ret)}: {ret.organization} / {getattr(ret, partner)}; "
                f"{config['basis_title']} {_doc(basis)}: {basis.organization} / {getattr(basis, partner)}"
            )

        if chronology:
            self.stdout.write("")
            self.stdout.write(f"Возврат стоит не позже своего основания: {len(chronology)}")
        for v in chronology:
            config, basis, ret = v["config"], v["basis"], v["returns"][0]
            self.stdout.write(
                f"  {config['title']} {_doc(ret)} — {ret.date:%d.%m.%Y} {ret.time:%H:%M}; "
                f"{config['basis_title']} {_doc(basis)} — {basis.date:%d.%m.%Y} {basis.time:%H:%M}"
            )

        if links:
            self.stdout.write("")
            self.stdout.write(f"Строка возврата ссылается на строку не своей отгрузки-основания: {len(links)}")
        for v in links:
            config, basis, ret, line = v["config"], v["basis"], v["returns"][0], v["line"]
            own = f"{config['basis_title']} {_doc(basis)}" if basis else "основания нет"
            self.stdout.write(
                f"  {config['title']} {_doc(ret)} ({own}): строка «{line.product.name}» (id {line.pk}) — "
                f"строка отгрузки {_doc(line.shipment_line.shipment)}"
            )

        self.stdout.write("")
        if not found:
            self.stdout.write(self.style.SUCCESS("Нарушений нет."))
            return
        self.stdout.write(self.style.WARNING(
            f"Найдено нарушений: {len(found)}. Ничего не изменено. Новые операции не могут увеличить "
            "превышение; отмена, удаление и уменьшение возврата остаются доступны."
        ))


def _doc(doc):
    return f"№ {doc.number} от {doc.date:%d.%m.%Y} (id {doc.pk})"
