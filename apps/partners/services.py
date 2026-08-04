"""Подтягивание реквизитов организации/ИП по ИНН через API DaData.

Нужен бесплатный API-ключ: зарегистрируйтесь на https://dadata.ru,
ключ — в личном кабинете (https://dadata.ru/profile/#info), затем
добавьте в .env строку DADATA_API_KEY=ваш_ключ.
Бесплатный лимит — 10 000 запросов в сутки.
"""
import json
import urllib.error
import urllib.request

from django.conf import settings

from .models import Counterparty

DADATA_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"


class InnLookupError(Exception):
    pass


def lookup_inn(inn: str) -> dict:
    """Возвращает реквизиты по ИНН или бросает InnLookupError с понятным текстом."""
    api_key = getattr(settings, "DADATA_API_KEY", "")
    if not api_key:
        raise InnLookupError(
            "Не настроен ключ DaData. Получите бесплатный ключ на dadata.ru "
            "и добавьте в файл .env строку DADATA_API_KEY=ваш_ключ, затем перезапустите сервер."
        )
    inn = inn.strip()
    if not inn.isdigit() or len(inn) not in (10, 12):
        raise InnLookupError("ИНН должен состоять из 10 цифр (ЮЛ) или 12 цифр (ИП)")

    request = urllib.request.Request(
        DADATA_URL,
        data=json.dumps({"query": inn}).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Token {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise InnLookupError("DaData отклонил ключ API — проверьте DADATA_API_KEY в .env") from exc
        raise InnLookupError(f"Ошибка сервиса DaData: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise InnLookupError(f"Нет связи с сервисом DaData: {exc.reason}") from exc

    suggestions = payload.get("suggestions") or []
    if not suggestions:
        raise InnLookupError(f"Организация с ИНН {inn} не найдена")

    data = suggestions[0].get("data") or {}
    name = data.get("name") or {}
    is_entrepreneur = data.get("type") == "INDIVIDUAL"

    result = {
        "name": name.get("short_with_opf") or name.get("full_with_opf") or "",
        "full_name": name.get("full_with_opf") or "",
        "kind": Counterparty.KIND_ENTREPRENEUR if is_entrepreneur else Counterparty.KIND_LEGAL,
        "inn": data.get("inn") or inn,
        "kpp": data.get("kpp") or "",
        "ogrn": data.get("ogrn") or "",
        "okpo": data.get("okpo") or "",
        "legal_address": (data.get("address") or {}).get("unrestricted_value") or "",
        "director_name": "",
    }
    management = data.get("management") or {}
    if management.get("name"):
        result["director_name"] = management["name"]
    elif is_entrepreneur:
        result["director_name"] = name.get("full") or ""
    return result
