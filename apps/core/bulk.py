"""Действия с отмеченными строками списка (чекбоксы)."""
from django.contrib import messages
from django.db import transaction
from django.db.models import ProtectedError
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .permissions import role_required


def selected_ids(request):
    return [int(i) for i in request.POST.getlist("ids") if str(i).isdigit()]


def back(request, fallback_url):
    """Назад в список с теми же фильтрами и страницей, если это возможно."""
    referer = request.META.get("HTTP_REFERER", "")
    if referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}):
        return redirect(referer)
    return redirect(fallback_url)


def bulk_delete_view(model, list_url_name, roles, noun="документов"):
    """Представление «удалить отмеченные»: проведённые сначала снимаются
    с проведения. Документ, удаление которого нарушило бы учёт (остаток ушёл
    бы в минус, сломалась бы история серийного номера) или на который ссылаются
    другие, остаётся нетронутым — с объяснением причины."""

    @require_POST
    @role_required(*roles)
    def view(request):
        from apps.inventory.services import StockError

        ids = selected_ids(request)
        if not ids:
            messages.warning(request, "Ничего не выбрано")
            return back(request, reverse(list_url_name))
        deleted, failed = 0, []
        for obj in model.objects.filter(pk__in=ids).order_by("pk"):
            try:
                # Каждый документ — своя транзакция и те же правила, что при
                # одиночном удалении: складские документы проверяют остатки
                # и серийные номера в собственном delete().
                with transaction.atomic():
                    if getattr(obj, "is_posted", False) and hasattr(obj, "unpost"):
                        obj.unpost()
                    obj.delete()
                deleted += 1
            except ProtectedError:
                failed.append(f"{obj}: на документ ссылаются другие документы")
            except StockError as exc:
                failed.append(f"{obj}: {exc}")
        if deleted:
            messages.success(request, f"Удалено {noun}: {deleted}")
        for reason in failed[:5]:
            messages.error(request, f"Не удалён — {reason}")
        if len(failed) > 5:
            messages.error(request, f"…и ещё не удалено: {len(failed) - 5}")
        return back(request, reverse(list_url_name))

    return view
