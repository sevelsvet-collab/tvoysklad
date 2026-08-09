"""Миксин постраничного вывода с выбором размера страницы.

Добавляет к ListView: размер страницы из ?per_page=, безопасный выбор из
допустимого списка, и данные для шаблона пагинации (partials/_pagination.html).
"""

PER_PAGE_OPTIONS = [10, 25, 50, 100, 250, 500]


class PageSizeMixin:
    paginate_by = 50
    per_page_options = PER_PAGE_OPTIONS

    def get_paginate_by(self, queryset):
        try:
            per_page = int(self.request.GET.get("per_page", self.paginate_by))
        except (TypeError, ValueError):
            return self.paginate_by
        return per_page if per_page in self.per_page_options else self.paginate_by

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["per_page"] = self.get_paginate_by(None)
        ctx["per_page_options"] = self.per_page_options
        page_obj = ctx.get("page_obj")
        if page_obj is not None:
            # Номера страниц с многоточиями вокруг текущей (метод требует
            # аргументы — вычисляем здесь, в шаблоне вызвать нельзя).
            ctx["page_range"] = page_obj.paginator.get_elided_page_range(
                page_obj.number, on_each_side=1, on_ends=1,
            )
            ctx["page_ellipsis"] = page_obj.paginator.ELLIPSIS
        return ctx
