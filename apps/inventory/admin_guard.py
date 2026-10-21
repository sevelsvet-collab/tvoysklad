"""Защита складского учёта в административной панели.

Движения, остатки и серийные номера — производные записи: их создаёт
проведение документов. Прямая правка в админке незаметно разошлась бы
с документами, поэтому здесь они только для просмотра. Проведённый документ
в админке тоже не редактируется: его строки нельзя менять в обход
перепроведения. Менять — в самой программе, там действуют все проверки.

Черновик сохраняется через тот же механизм, что форма документа и
change_organization() (PostableMixin.editing, только черновики): строка
документа блокируется и статус берётся из базы. Если документ успели провести,
изменения не сохраняются — ни шапка, ни строки.
"""
from django.contrib import admin, messages
from django.http import HttpResponseRedirect

from .services import StockError


class ReadOnlyAdmin(admin.ModelAdmin):
    """Производные записи: смотреть можно, менять нельзя."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class StockDocumentAdmin(admin.ModelAdmin):
    """Складской документ: черновик правится, проведённый — только просмотр."""

    def get_readonly_fields(self, request, obj=None):
        # статус меняют только проведение и его отмена
        return (*super().get_readonly_fields(request, obj), "status")

    def has_change_permission(self, request, obj=None):
        if obj is not None and obj.is_posted:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.is_posted:
            return False
        return super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if not change:
            return super().save_model(request, obj, form, change)
        # блокировка строки документа держится до конца транзакции админки,
        # поэтому и строки (save_related) пишутся в черновик
        with obj.editing(drafts_only=True):
            super().save_model(request, obj, form, change)

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except StockError as exc:
            # транзакция админки откатилась целиком
            self.message_user(request, f"Изменения не сохранены: {exc}", messages.ERROR)
            return HttpResponseRedirect(request.path)

    def get_actions(self, request):
        # массовое удаление админки идёт мимо delete() модели и её проверок
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions
