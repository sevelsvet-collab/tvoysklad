"""С чем можно связать задачу — явный список.

Тип связи и id приходят из запроса только как ключ этого списка и число:
произвольного обращения к моделям по content_type/id нет. У каждого типа своё
поле задачи (внешний ключ с запретом удаления) — ссылочную целостность держит
база, а не программа.

Право на связанный объект — право открыть его карточку: те же роли
(allowed_roles), которые проверяет представление карточки. Список документов
и печатные формы основанием для доступа к задаче не служат.
"""
from dataclasses import dataclass
from functools import lru_cache

from django.apps import apps
from django.urls import resolve, reverse


@dataclass(frozen=True)
class LinkType:
    code: str           # значение Task.link_type и имя внешнего ключа задачи
    model: str          # "приложение.Модель"
    card_url: str       # маршрут карточки объекта
    label: str          # подпись типа
    label_genitive: str  # «…не может открыть счёт покупателю»

    @property
    def model_class(self):
        return apps.get_model(self.model)

    def card_roles(self):
        return card_roles(self.card_url)

    def url(self, pk):
        return reverse(self.card_url, args=[pk])


LINK_TYPES = (
    LinkType("counterparty", "partners.Counterparty", "counterparty_edit", "Контрагент", "карточку контрагента"),
    LinkType("invoice", "sales.Invoice", "invoice_edit", "Счёт покупателю", "счёт покупателю"),
    LinkType("shipment", "sales.Shipment", "shipment_edit", "Отгрузка", "отгрузку"),
    LinkType("customer_return", "sales.CustomerReturn", "customer_return_edit", "Возврат покупателя",
             "возврат покупателя"),
    LinkType("receipt", "purchases.Receipt", "receipt_edit", "Приёмка", "приёмку"),
    LinkType("supplier_return", "purchases.SupplierReturn", "supplier_return_edit", "Возврат поставщику",
             "возврат поставщику"),
    LinkType("payment", "finance.Payment", "payment_edit", "Платёж", "платёж"),
)
BY_CODE = {link.code: link for link in LINK_TYPES}
LINK_CODES = tuple(BY_CODE)


def for_object(obj):
    """Тип связи для объекта (документа или контрагента) или None."""
    label = obj._meta.label
    return next((link for link in LINK_TYPES if link.model == label), None)


@lru_cache(maxsize=None)
def card_roles(url_name):
    """Роли, которым открыта карточка: allowed_roles её представления.

    Пустой кортеж — карточку открывает любой вошедший (так RoleRequiredMixin
    понимает пустой allowed_roles)."""
    view = resolve(reverse(url_name, args=[1])).func.view_class
    return tuple(view.allowed_roles)
