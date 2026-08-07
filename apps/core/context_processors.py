import time

from django.conf import settings
from django.urls import reverse

from . import roles


def assets(request):
    """В разработке добавляет ?v=... к своим CSS/JS, чтобы браузер не держал старую версию.

    В проде версионирование делает ManifestStaticFilesStorage (хэш в имени файла).
    """
    return {"asset_version": str(int(time.time())) if settings.DEBUG else ""}


def navigation(request):
    """Верхнее меню в стиле МойСклад: разделы + подменю активного раздела.

    Каждый модуль добавляет сюда свои пункты. Пункт с url=None — модуль ещё не готов.
    """
    if not request.user.is_authenticated:
        return {}

    url_name = request.resolver_match.url_name if request.resolver_match else ""

    def section(code, title, icon, submenu, admin_only=False):
        items = []
        for sub_title, sub_url_name, active_prefixes in submenu:
            items.append({
                "title": sub_title,
                "url": reverse(sub_url_name) if sub_url_name else None,
                "active": any(url_name.startswith(p) for p in active_prefixes) if active_prefixes else False,
            })
        return {
            "code": code, "title": title, "icon": icon,
            "url": next((i["url"] for i in items if i["url"]), None),
            "active": any(i["active"] for i in items),
            "submenu": items,
            "admin_only": admin_only,
        }

    nav = [
        section("dashboard", "Показатели", "bi-graph-up", [
            ("Показатели", "dashboard", ["dashboard"]),
        ]),
        section("purchases", "Закупки", "bi-cart-plus", [
            ("Приёмки", "receipt_list", ["receipt_"]),
            ("Возвраты поставщикам", "supplier_return_list", ["supplier_return_"]),
        ]),
        section("sales", "Продажи", "bi-cart-check", [
            ("Счета покупателям", "invoice_list", ["invoice_"]),
            ("Отгрузки", "shipment_list", ["shipment_"]),
            ("Возвраты покупателей", "customer_return_list", ["customer_return_"]),
        ]),
        section("catalog", "Товары", "bi-box-seam", [
            ("Товары и услуги", "product_list", ["product_", "group_"]),
            ("Импорт", "catalog_import", ["catalog_import"]),
        ]),
        section("stock", "Склад", "bi-hdd-stack", [
            ("Остатки", "balance_list", ["balance_"]),
            ("Перемещения", "transfer_list", ["transfer_"]),
            ("Оприходования", "adjustment_list_income", ["adjustment_list_income", "adjustment_create_income"]),
            ("Списания", "adjustment_list_expense", ["adjustment_list_expense", "adjustment_create_expense"]),
        ]),
        section("partners", "Контрагенты", "bi-people", [
            ("Контрагенты", "counterparty_list", ["counterparty_"]),
            ("Импорт", "partners_import", ["partners_import"]),
        ]),
        section("money", "Деньги", "bi-cash-stack", [
            ("Платежи", "payment_list", ["payment_"]),
            ("Счета и кассы", "account_list", ["account_"]),
            ("Взаиморасчёты", "settlements", ["settlements"]),
            ("Корректировки", "correction_list", ["correction_list", "cash_correction_", "bank_correction_", "account_correction_", "settlement_correction_"]),
        ]),
        section("reports", "Отчёты", "bi-bar-chart", [
            ("Продажи и прибыль", "report_sales", ["report_sales"]),
            ("Движение денег", "report_cashflow", ["report_cashflow"]),
        ]),
        section("settings", "Настройки", "bi-gear", [
            ("Организации", "organization_list", ["organization_"]),
            ("Склады", "warehouse_list", ["warehouse_"]),
            ("Пользователи", "user_list", ["user_list"]),
        ], admin_only=True),
    ]

    if not request.user.has_role(roles.ROLE_ADMIN):
        nav = [s for s in nav if not s["admin_only"]]

    active_section = next((s for s in nav if s["active"]), None)
    return {"nav_sections": nav, "nav_active": active_section}
