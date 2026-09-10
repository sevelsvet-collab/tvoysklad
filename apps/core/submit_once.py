"""Защита от повторной отправки формы создания документа (двойной клик).

В форму кладётся одноразовый токен. Первый запрос «забирает» токен, второй
с тем же токеном документ не создаёт и ведёт на уже созданный. Уникальный
индекс в БД срабатывает даже при одновременных запросах: второй ждёт, пока
первый завершит транзакцию, и получает конфликт.
"""
import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.shortcuts import redirect
from django.utils import timezone

from .models import SubmitToken

FIELD = "submit_token"


def new_token():
    return uuid.uuid4().hex


def claim(request):
    """Забирает токен запроса. Если его уже забрали — ответ-переадресация
    на созданный документ, иначе None (можно сохранять)."""
    token = request.POST.get(FIELD, "").strip()[:64]
    if not token:
        return None
    try:
        with transaction.atomic():
            SubmitToken.objects.create(token=token)
    except IntegrityError:
        done = SubmitToken.objects.filter(token=token).first()
        return redirect(done.url if done and done.url else request.path)
    # попутно чистим старые токены
    SubmitToken.objects.filter(created_at__lt=timezone.now() - timedelta(days=2)).delete()
    return None


def remember(request, url):
    """Запоминает, куда ведёт токен, — туда отправим повторный запрос."""
    token = request.POST.get(FIELD, "").strip()[:64]
    if token:
        SubmitToken.objects.filter(token=token).update(url=url)
