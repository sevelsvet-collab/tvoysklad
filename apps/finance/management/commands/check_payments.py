"""Диагностика платежей и оплаты счетов. По умолчанию только читает.

    python manage.py check_payments

Показывает:

* счета покупателям, у которых сохранённое «Оплачено» не равно сумме
  проведённых входящих платежей по ним;
* платежи, у которых счёт или касса принадлежит другой организации;
* платежи, у которых организация или контрагент не совпадают со связанным
  счётом покупателю (и исходящие платежи, связанные со счётом);
* платежи с отрицательной суммой (любой статус) и проведённые платежи
  с нулевой суммой. Нулевой черновик ошибкой не считается;
* корректировки остатков, у которых касса или счёт принадлежит другой
  организации (проведённая такая корректировка меняет остаток чужой кассы).
  Их снимают с проведения, удаляют или исправляют связь вручную — это всегда
  разрешено; команда их не меняет;
* корректировки остатков, у которых записанные расчётный остаток и разница
  не совпадают с правилом «остаток на момент» (факт на дату и время
  документа), и кассы, у которых из-за этого другой остаток: «записано /
  по правилу».

Пересчёт корректировок по правилу включается отдельно:

    python manage.py check_payments --fix-corrections --i-have-a-backup

Он пересчитывает расчётные остатки и разницы корректировок — каждая касса
целиком в своей транзакции — и больше ничего не меняет: введённые
фактические остатки, даты, время и платежи остаются прежними.

Недопустимые суммы команда не исправляет: знак не меняется, входящий платёж
не превращается в исходящий. Такой платёж снимают с проведения и исправляют
или удаляют вручную.

Несовместимые связи команда не исправляет никогда: деньги между организациями
автоматически не переносятся, решение — за человеком.

Пересчёт «Оплачено» включается только явно, двумя ключами сразу:

    python manage.py check_payments --fix-paid-amount --i-have-a-backup

Он записывает в счёт фактическую сумму проведённых платежей и больше ничего
не трогает. Сначала — резервная копия и запуск без ключей.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.finance import services


class Command(BaseCommand):
    help = "Сверяет оплату счетов с платежами и ищет несовместимые связи (без ключей ничего не меняет)."

    def add_arguments(self, parser):
        parser.add_argument("--fix-paid-amount", action="store_true",
                            help="Записать в счета фактическую сумму проведённых платежей")
        parser.add_argument("--fix-corrections", action="store_true",
                            help="Пересчитать разницы корректировок остатков по правилу «остаток на момент»")
        parser.add_argument("--i-have-a-backup", action="store_true", dest="backup",
                            help="Подтверждение, что резервная копия базы сделана (обязательно с --fix-…)")

    def handle(self, *args, **options):
        fix = options["fix_paid_amount"]
        fix_corrections = options["fix_corrections"]
        if (fix or fix_corrections) and not options["backup"]:
            raise CommandError(
                "Исправление меняет «Оплачено» в счетах покупателям или разницы корректировок остатков. "
                "Сделайте резервную копию базы и повторите с ключом --i-have-a-backup."
            )
        modes = [name for name, on in (("ПЕРЕСЧЁТ ОПЛАТЫ СЧЕТОВ", fix), ("ПЕРЕСЧЁТ КОРРЕКТИРОВОК", fix_corrections)) if on]
        mode = " и ".join(modes) if modes else "только проверка, запись выключена"
        self.stdout.write(f"Проверка платежей ({mode}).")

        differences = services.paid_amount_differences()
        if differences:
            self.stdout.write("")
            self.stdout.write(f"«Оплачено» в счёте не равно сумме проведённых платежей: {len(differences)}")
        for invoice, stored, actual in differences:
            self.stdout.write(
                f"  счёт № {invoice.number} от {invoice.date:%d.%m.%Y} (id {invoice.pk}), {invoice.customer}: "
                f"в счёте {stored:f}, по платежам {actual:f}, разница {stored - actual:f}"
            )

        incompatible = services.incompatible_payments()
        if incompatible:
            self.stdout.write("")
            self.stdout.write(f"Платежи с несовместимыми связями (не исправляются автоматически): {len(incompatible)}")
        for payment, errors in incompatible:
            state = "проведён" if payment.is_posted else "черновик"
            for text in errors.values():
                self.stdout.write(
                    f"  {payment.get_kind_display().lower()} № {payment.number} от {payment.date:%d.%m.%Y} "
                    f"(id {payment.pk}, {state}, {payment.amount:f}): {text}"
                )

        invalid = services.invalid_amounts()
        if invalid:
            self.stdout.write("")
            self.stdout.write(f"Платежи с недопустимой суммой (не исправляются автоматически): {len(invalid)}")
        for payment in invalid:
            state = "проведён" if payment.is_posted else "черновик"
            problem = "отрицательная сумма" if payment.amount < 0 else "проведён с нулевой суммой"
            self.stdout.write(
                f"  {payment.get_kind_display().lower()} № {payment.number} от {payment.date:%d.%m.%Y} "
                f"(id {payment.pk}, {state}): {problem} {payment.amount:f}"
            )

        corrections = services.incompatible_corrections()
        if corrections:
            self.stdout.write("")
            self.stdout.write(
                f"Корректировки остатков с кассой или счётом другой организации "
                f"(не исправляются автоматически): {len(corrections)}"
            )
        for correction, errors in corrections:
            state = "проведена" if correction.is_posted else "черновик"
            for text in errors.values():
                self.stdout.write(
                    f"  корректировка остатка № {correction.number} от {correction.date:%d.%m.%Y} "
                    f"(id {correction.pk}, {state}, {correction.amount:f}): {text}"
                )

        history = services.correction_history_differences()
        if history:
            self.stdout.write("")
            self.stdout.write(f"Корректировки остатков: записано не по правилу «остаток на момент» "
                              f"(касс: {len(history)})")
        for account, stored_balance, expected_balance, rows in history:
            self.stdout.write(
                f"  {account} ({account.organization}, id {account.pk}): остаток записан {stored_balance:f}, "
                f"по правилу {expected_balance:f}"
            )
            for correction, (stored_before, stored_amount), (before, amount) in rows:
                state = "проведена" if correction.is_posted else "черновик, предварительно"
                self.stdout.write(
                    f"    корректировка № {correction.number} от {correction.date:%d.%m.%Y} "
                    f"{correction.time:%H:%M} (id {correction.pk}, {state}, факт {correction.actual_balance:f}): "
                    f"записано — расчётный {stored_before:f}, разница {stored_amount:f}; по правилу — "
                    f"расчётный {before:f}, разница {amount:f}"
                )

        self.stdout.write("")
        if not differences and not incompatible and not invalid and not corrections and not history:
            self.stdout.write(self.style.SUCCESS("Расхождений нет."))
            return
        if fix_corrections and history:
            fixed = services.fix_correction_history([account.pk for account, _, _, _ in history])
            self.stdout.write(self.style.SUCCESS(f"Пересчитана история касс: {len(fixed)}."))
        elif history:
            self.stdout.write(self.style.WARNING(
                f"Корректировок не по правилу «остаток на момент»: касс — {len(history)}. Ничего не изменено. "
                "Пересчёт: --fix-corrections --i-have-a-backup (факты, даты и платежи не меняются)."
            ))
        if fix:
            with transaction.atomic():
                services.recompute_invoices([invoice.pk for invoice, _, _ in differences])
            self.stdout.write(self.style.SUCCESS(f"Пересчитано счетов: {len(differences)}."))
        elif differences or incompatible or invalid or corrections:
            self.stdout.write(self.style.WARNING(
                f"Найдено: расхождений оплаты — {len(differences)}, несовместимых связей — {len(incompatible)}, "
                f"недопустимых сумм — {len(invalid)}. Ничего не изменено."
            ))
        if invalid:
            self.stdout.write(self.style.WARNING(
                "Недопустимые суммы исправляются вручную: снимите платёж с проведения, укажите сумму больше "
                "нуля (вид платежа задаёт направление) и проведите снова — или удалите платёж."
            ))
        if incompatible:
            self.stdout.write(self.style.WARNING(
                "Несовместимые связи нужно исправить вручную: открыть платёж и выбрать счёт или кассу своей "
                "организации либо отвязать платёж от счёта покупателю."
            ))
        if corrections:
            self.stdout.write(self.style.WARNING(
                "Корректировку с чужой кассой исправляют вручную: снимите с проведения и выберите кассу своей "
                "организации (разница пересчитается при сохранении) — или удалите корректировку."
            ))
