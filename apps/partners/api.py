"""JSON API контрагентов для «живого» поиска в документах."""
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from apps.core import roles
from apps.core.permissions import role_required

from .models import Counterparty
from .services import InnLookupError, lookup_inn

SEARCH_LIMIT = 15
EDIT_ROLES = [roles.ROLE_ADMIN, roles.ROLE_MANAGER, roles.ROLE_ACCOUNTANT]


def _filter_by_type(qs, partner_type):
    """customer/supplier → включая «Покупатель и поставщик»."""
    if partner_type in (Counterparty.TYPE_CUSTOMER, Counterparty.TYPE_SUPPLIER):
        return qs.filter(partner_type__in=[partner_type, Counterparty.TYPE_BOTH])
    return qs


def _serialize(cp):
    details = []
    if cp.inn:
        details.append(f"ИНН {cp.inn}")
    if cp.phone:
        details.append(cp.phone)
    return {
        "id": cp.pk,
        "name": cp.name,
        "label": cp.name,
        "details": " · ".join(details),
        "inn": cp.inn,
    }


@login_required
def counterparty_search(request):
    # Поиск по id — для подстановки имени в предзаполненное поле (?id=<pk>)
    id_ = request.GET.get("id")
    if id_:
        cp = Counterparty.objects.filter(pk=id_).first()
        return JsonResponse({"results": [_serialize(cp)] if cp else []})
    q = request.GET.get("q", "").strip()
    qs = Counterparty.objects.filter(is_active=True)
    qs = _filter_by_type(qs, request.GET.get("type", ""))
    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(full_name__icontains=q)
            | Q(inn__icontains=q) | Q(phone__icontains=q) | Q(email__icontains=q)
        )
    results = [_serialize(cp) for cp in qs.order_by("name")[:SEARCH_LIMIT]]
    return JsonResponse({"results": results, "can_create": request.user.has_role(*EDIT_ROLES)})


def _partner_type(request):
    partner_type = request.POST.get("type") or Counterparty.TYPE_CUSTOMER
    return partner_type if partner_type in dict(Counterparty.TYPE_CHOICES) else Counterparty.TYPE_CUSTOMER


@require_POST
@login_required
@role_required(*EDIT_ROLES)
def counterparty_quick_create(request):
    """Создаёт контрагента «на лету».

    Если введён ИНН (10 или 12 цифр) — подтягивает реквизиты через DaData
    прямо из документа. Иначе создаёт по наименованию (дозаполнить можно в карточке).
    """
    name = request.POST.get("name", "").strip()
    if not name:
        return JsonResponse({"ok": False, "error": "Введите наименование или ИНН"}, status=400)

    partner_type = _partner_type(request)

    if name.isdigit() and len(name) in (10, 12):
        existing = Counterparty.objects.filter(inn=name).first()
        if existing:
            return JsonResponse({"ok": True, "created": False, "counterparty": _serialize(existing)})

        try:
            d = lookup_inn(name)
            cp = Counterparty.objects.create(
                name=d["name"] or name, full_name=d["full_name"], kind=d["kind"],
                inn=d["inn"], kpp=d["kpp"], ogrn=d["ogrn"], okpo=d["okpo"],
                legal_address=d["legal_address"], director_name=d["director_name"],
                partner_type=partner_type,
            )
            return JsonResponse({"ok": True, "created": True, "by_inn": True, "counterparty": _serialize(cp)})
        except InnLookupError as exc:
            # Ключ DaData не настроен или ИНН не найден — заведём с ИНН, реквизиты дозаполнят вручную
            cp = Counterparty.objects.create(name=name, inn=name, partner_type=partner_type)
            return JsonResponse({
                "ok": True, "created": True, "by_inn": False,
                "warning": str(exc), "counterparty": _serialize(cp),
            })

    existing = Counterparty.objects.filter(name__iexact=name, is_active=True).first()
    if existing:
        return JsonResponse({"ok": True, "created": False, "counterparty": _serialize(existing)})

    cp = Counterparty.objects.create(name=name, partner_type=partner_type)
    return JsonResponse({"ok": True, "created": True, "counterparty": _serialize(cp)})
