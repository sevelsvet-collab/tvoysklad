"""Экраны задач. Права и переходы проверяет services на каждый запрос;
недоступная задача для пользователя не существует (404) — и по прямой
ссылке, и в POST. GET ничего не меняет."""
from functools import cached_property

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, F, Q
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import ListView, TemplateView

from apps.core import roles, submit_once
from apps.core.listing import ChoiceFilter, FilteredListMixin, PeriodFilter
from apps.core.pagination import PageSizeMixin
from apps.core.permissions import RoleRequiredMixin, role_required

from . import services
from .access import Viewer, lacks_link_access, roles_of, viewer_for
from .display import describe, notice_change, notice_text, timezone_label
from .forms import FIXED_AFTER_CREATE, ReassignForm, TaskCreateForm, TaskEditForm, forged_fields
from .links import BY_CODE, LINK_CODES
from .models import Task

TASK_ROLES = roles.ALL_ROLES
FEED_PAGE = 50


def _visible_task(request, pk):
    viewer = viewer_for(request)
    task = (Task.objects.select_related("creator", "assignee", *LINK_CODES).filter(pk=pk).first())
    if task is None or not viewer.can_view(task):
        raise Http404("Задача не найдена")
    return task


def _fail(request, exc, task_pk=None):
    """Отказ services → ответ: 404, 403 или сообщение и возврат на карточку."""
    if isinstance(exc, services.TaskNotFound):
        raise Http404(exc.message)
    if isinstance(exc, services.TaskForbidden):
        raise PermissionDenied(exc.message)
    messages.error(request, exc.message)
    return redirect("task_detail", pk=task_pk) if task_pk else redirect("task_mine")


def _forged(request, extra=()):
    names = forged_fields(request.POST, extra)
    if names:
        return HttpResponseBadRequest("Запрос отклонён: эти поля задачи так не меняются — " + ", ".join(names))
    return None


# ---------- списки

def _users_for_filter():
    from django.contrib.auth import get_user_model

    users = (get_user_model().objects.filter(Q(is_superuser=True) | Q(groups__name__in=TASK_ROLES))
             .distinct().order_by("first_name", "last_name", "username"))
    return [(u.pk, u.display_name if u.is_active else f"{u.display_name} (отключён)") for u in users]


def _by_show(qs, value):
    if value == "closed":
        return qs.filter(status__in=Task.FINAL)
    if value == "all":
        return qs
    return qs.filter(status__in=Task.ACTIVE)


def _by_overdue(qs, value):
    late = Q(due_at__lt=timezone.now(), status__in=Task.ACTIVE)
    if value == "yes":
        return qs.filter(late)
    if value == "no":
        return qs.exclude(late)
    return qs


def _by_problem(qs, value):
    if value == "inactive":
        return qs.filter(assignee__is_active=False, status__in=Task.ACTIVE)
    if value == "no_access":
        return qs.filter(lacks_link_access("assignee") | lacks_link_access("creator"))
    return qs


MODES = {
    "mine": "Мне",
    "delegated": "Порученные мной",
    "review": "На проверке",
    "all": "Все задачи",
}


class TaskListView(FilteredListMixin, RoleRequiredMixin, ListView):
    allowed_roles = TASK_ROLES
    template_name = "tasks/task_list.html"
    context_object_name = "tasks"
    mode = "mine"
    search_placeholder = "Номер, заголовок или описание"

    @cached_property
    def viewer(self):
        return viewer_for(self.request)

    def dispatch(self, request, *args, **kwargs):
        if self.mode == "all" and request.user.is_authenticated and not viewer_for(request).is_admin:
            raise PermissionDenied("Все задачи видит только администратор")
        return super().dispatch(request, *args, **kwargs)

    def get_list_filters(self):
        filters = [
            ChoiceFilter("show", "Показывать", choices=[("active", "Активные"),
                                                        ("closed", "Завершённые и отменённые"),
                                                        ("all", "Все")],
                         default="active", empty_label=None, apply=_by_show),
            ChoiceFilter("status", "Статус", choices=Task.STATUS_CHOICES),
            PeriodFilter("due", "Срок", field="due_at__date"),
            ChoiceFilter("overdue", "Просрочка", choices=[("yes", "Просроченные"), ("no", "Без просрочки")],
                         apply=_by_overdue),
        ]
        if self.mode != "mine":
            filters.append(ChoiceFilter("assignee", "Исполнитель", "assignee_id", _users_for_filter))
        if self.mode == "all":
            filters.append(ChoiceFilter("problem", "Проблемы", choices=[
                ("inactive", "Исполнитель отключён"), ("no_access", "Нет доступа к документу")], apply=_by_problem))
        return filters

    def mode_q(self):
        viewer = self.viewer
        if self.mode == "mine":
            return Q(assignee_id=viewer.pk)
        if self.mode == "delegated":
            return Q(creator_id=viewer.pk) & ~Q(assignee_id=viewer.pk)
        if self.mode == "review":
            return Q(status=Task.REVIEW) if viewer.is_admin else Q(status=Task.REVIEW, creator_id=viewer.pk)
        return Q()

    def filter_queryset(self, qs):
        # поиск и фильтры — только внутри уже доступных задач (get_queryset)
        qs = super().filter_queryset(qs)
        q = self.request.GET.get("q", "").strip()
        if q:
            cond = Q(title__icontains=q) | Q(description__icontains=q)
            number = q.lstrip("№ ").strip()
            if number.isdigit():
                cond |= Q(pk=int(number))
            qs = qs.filter(cond)
        return qs

    def get_queryset(self):
        viewer = self.viewer
        qs = (Task.objects.filter(viewer.visible_q()).filter(self.mode_q())
              .select_related("creator", "assignee", *LINK_CODES)
              .annotate(items_total=Count("checklist", filter=Q(checklist__is_removed=False)),
                        items_done=Count("checklist", filter=Q(checklist__is_removed=False,
                                                                checklist__is_done=True)))
              .order_by(F("due_at").asc(nulls_last=True), "-pk"))
        return self.filter_queryset(qs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        now = timezone.now()
        viewer = self.viewer
        tasks = list(ctx["tasks"])
        people = roles_of({t.assignee_id for t in tasks} | {t.creator_id for t in tasks}) if self.mode == "all" else {}
        for task in tasks:
            task.overdue = bool(task.due_at and task.status in Task.ACTIVE and task.due_at < now)
            if self.mode == "all":
                task.problems = _problems(task, people)
        ctx.update({
            "mode": self.mode, "mode_title": MODES[self.mode], "modes": MODES, "is_admin": viewer.is_admin,
            "tz_label": timezone_label(),
        })
        if self.mode == "all":
            ctx["inactive_count"] = Task.objects.filter(assignee__is_active=False, status__in=Task.ACTIVE).count()
        return ctx


def _problems(task, people):
    """Что мешает работе над задачей — для администратора (роли — заранее, без запроса на строку)."""
    problems = []
    if task.status in Task.ACTIVE and not task.assignee.is_active:
        problems.append("исполнитель отключён")
    if task.link_type:
        assignee = Viewer(task.assignee, people.get(task.assignee_id, ()))
        creator = Viewer(task.creator, people.get(task.creator_id, ()))
        if task.assignee.is_active and not assignee.link_allowed(task.link_type):
            problems.append("исполнитель не видит документ")
        if task.creator.is_active and not creator.link_allowed(task.link_type):
            problems.append("постановщик не видит документ")
    return problems


# ---------- карточка

class TaskDetailView(RoleRequiredMixin, TemplateView):
    allowed_roles = TASK_ROLES
    template_name = "tasks/task_detail.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        task = _visible_task(self.request, kwargs["pk"])
        viewer = viewer_for(self.request)
        events = (task.events.select_related("author", "old_user", "new_user", "comment")
                  .order_by("created_at", "pk"))
        paginator = Paginator(events, FEED_PAGE)
        page_number = self.request.GET.get("history") or paginator.num_pages
        feed = paginator.get_page(page_number)
        transitions = [(name, services.TRANSITIONS[name]) for name in services.allowed_transitions(viewer, task)]
        can_manage = viewer.can_manage(task)
        link = task.link
        ctx.update({
            "task": task,
            "link": link[0] if link else None,
            "link_object": link[1] if link else None,
            "link_url": link[0].url(link[1].pk) if link else "",
            "items": list(task.checklist.filter(is_removed=False).select_related("done_by")),
            "can_check": viewer.is_executor(task) and task.status in Task.BEFORE_REVIEW,
            "can_manage": can_manage,
            "can_edit": can_manage and task.status not in Task.FINAL,
            "can_reassign": can_manage and viewer.can_delegate and task.status in Task.BEFORE_REVIEW,
            "transitions": transitions,
            "feed": [(event, describe(event)) for event in feed],
            "feed_page": feed,
            "reassign_form": ReassignForm(initial={"version": task.version}),
            "submit_token": submit_once.new_token(),
            "unread": services.visible_notifications(viewer).filter(task_id=task.pk, read_at__isnull=True).count(),
            "tz_label": timezone_label(),
            "is_overdue": task.is_overdue,
        })
        return ctx


# ---------- создание и правка

def _parse_link(raw):
    """«invoice:15» → (тип, id); неизвестный тип или не число — 404."""
    code, _, pk = (raw or "").partition(":")
    if code not in BY_CODE or not pk.isdigit():
        raise Http404("Связанный объект не найден")
    return code, int(pk)


class TaskCreateView(RoleRequiredMixin, TemplateView):
    allowed_roles = TASK_ROLES
    template_name = "tasks/task_form.html"

    def link_context(self, code, pk):
        if not code:
            return {}
        viewer = viewer_for(self.request)
        link = BY_CODE.get(code)
        if link is None:
            raise Http404("Связанный объект не найден")
        if not viewer.link_allowed(code):
            raise PermissionDenied("Нет доступа к связанному документу")
        obj = link.model_class.objects.filter(pk=pk).first()
        if obj is None:
            raise Http404("Связанный объект не найден")
        return {"link": link, "link_object": obj, "link_url": link.url(obj.pk)}

    def render_form(self, form, link_ctx, status=200):
        viewer = viewer_for(self.request)
        ctx = self.get_context_data(form=form, creating=True, can_delegate=viewer.can_delegate,
                                    submit_token=submit_once.new_token(), tz_label=timezone_label(), **link_ctx)
        return self.render_to_response(ctx, status=status)

    def get(self, request, *args, **kwargs):
        initial = {}
        link_ctx = {}
        if request.GET.get("link"):
            code, pk = _parse_link(request.GET["link"])
            link_ctx = self.link_context(code, pk)
            initial = {"link_type": code, "link_pk": pk}
        return self.render_form(TaskCreateForm(user=request.user, initial=initial), link_ctx)

    def post(self, request, *args, **kwargs):
        forged = _forged(request, ("version",))
        if forged:
            return forged
        form = TaskCreateForm(request.POST, user=request.user)
        link_ctx = {}
        if form.is_valid():
            if form.cleaned_data["link_type"]:
                link_ctx = self.link_context(form.cleaned_data["link_type"], form.cleaned_data["link_pk"] or 0)
            try:
                with transaction.atomic():
                    duplicate = submit_once.claim(request)
                    if duplicate:
                        return duplicate        # повторная отправка — задача уже создана
                    task = services.create_task(request.user, form.new_task())
                    url = reverse("task_detail", args=[task.pk])
                    submit_once.remember(request, url)
            except services.TaskForbidden as exc:
                form.add_error(exc.field if exc.field in form.fields else None, exc.message)
                return self.render_form(form, link_ctx, status=403)
            except services.TaskError as exc:
                form.add_error(exc.field if exc.field in form.fields else None, exc.message)
                return self.render_form(form, link_ctx)
            messages.success(request, f"Задача № {task.number} поставлена")
            return redirect(url)
        if form.data.get("link_type") in BY_CODE and str(form.data.get("link_pk", "")).isdigit():
            link_ctx = self.link_context(form.data["link_type"], int(form.data["link_pk"]))
        return self.render_form(form, link_ctx)


class TaskEditView(RoleRequiredMixin, TemplateView):
    allowed_roles = TASK_ROLES
    template_name = "tasks/task_form.html"

    def setup_task(self, request, pk):
        task = _visible_task(request, pk)
        viewer = viewer_for(request)
        if not viewer.can_manage(task):
            raise PermissionDenied("Менять задачу может постановщик или администратор")
        return task

    def render_form(self, task, form, status=200):
        link = task.link
        ctx = self.get_context_data(form=form, task=task, creating=False, tz_label=timezone_label(),
                                    link=link[0] if link else None, link_object=link[1] if link else None,
                                    link_url=link[0].url(link[1].pk) if link else "")
        return self.render_to_response(ctx, status=status)

    def get(self, request, pk):
        task = self.setup_task(request, pk)
        if task.status in Task.FINAL:
            messages.info(request, "Завершённую или отменённую задачу сначала возобновите")
            return redirect("task_detail", pk=pk)
        return self.render_form(task, TaskEditForm(task=task))

    def post(self, request, pk):
        forged = _forged(request, FIXED_AFTER_CREATE)
        if forged:
            return forged
        task = self.setup_task(request, pk)
        form = TaskEditForm(request.POST, task=task)
        if not form.is_valid():
            return self.render_form(task, form)
        try:
            task, changed = services.update_task(request.user, task.pk, form.cleaned_data["version"], form.changes())
        except services.TaskConflict as exc:
            form.add_error(None, exc.message)
            return self.render_form(task, form, status=409)
        except (services.TaskNotFound, services.TaskForbidden) as exc:
            return _fail(request, exc, pk)
        except services.TaskError as exc:
            form.add_error(exc.field if exc.field in form.fields else None, exc.message)
            return self.render_form(task, form)
        messages.success(request, "Задача сохранена" if changed else "Изменений нет")
        return redirect("task_detail", pk=pk)


# ---------- действия на карточке

@require_POST
@role_required(*TASK_ROLES)
def task_action(request, pk, name):
    if name not in services.TRANSITIONS:
        raise Http404("Неизвестное действие")
    try:
        task, changed = services.transition(request.user, pk, name, request.POST.get("text", ""))
    except services.TaskError as exc:
        return _fail(request, exc, pk)
    rule = services.TRANSITIONS[name]
    if changed:
        messages.success(request, f"{rule.title}: готово — задача «{services.status_label(task.status)}»")
    else:
        messages.info(request, f"Уже выполнено: задача «{services.status_label(task.status)}»")
    return redirect("task_detail", pk=pk)


@require_POST
@role_required(*TASK_ROLES)
def task_reassign(request, pk):
    forged = _forged(request)
    if forged:
        return forged
    form = ReassignForm(request.POST)
    if not form.is_valid():
        _visible_task(request, pk)          # недоступная задача — 404, а не сообщение о форме
        messages.error(request, "Выберите нового исполнителя и укажите причину")
        return redirect("task_detail", pk=pk)
    try:
        task, changed = services.reassign(request.user, pk, form.cleaned_data["version"],
                                          form.cleaned_data["assignee"], form.cleaned_data["reason"])
    except services.TaskError as exc:
        return _fail(request, exc, pk)
    if changed:
        messages.success(request, f"Исполнитель изменён: {task.assignee.display_name}. Задача снова «Новая»")
    else:
        messages.info(request, "Этот сотрудник уже исполнитель задачи")
    if not viewer_for(request).can_view(task):
        return redirect("task_mine")
    return redirect("task_detail", pk=pk)


@require_POST
@role_required(*TASK_ROLES)
def task_check(request, pk, item_pk):
    try:
        services.set_item_done(request.user, pk, item_pk, request.POST.get("done") == "1")
    except services.TaskError as exc:
        return _fail(request, exc, pk)
    return redirect(reverse("task_detail", args=[pk]) + "#checklist")


@require_POST
@role_required(*TASK_ROLES)
def task_comment(request, pk):
    try:
        with transaction.atomic():
            duplicate = submit_once.claim(request)
            if duplicate:
                _visible_task(request, pk)
                return duplicate            # повторная отправка — комментарий уже добавлен
            services.add_comment(request.user, pk, request.POST.get("text", ""))
            submit_once.remember(request, reverse("task_detail", args=[pk]) + "#feed-end")
    except services.TaskError as exc:
        return _fail(request, exc, pk)
    return redirect(reverse("task_detail", args=[pk]) + "#feed-end")


@require_POST
@role_required(*TASK_ROLES)
def task_read(request, pk):
    _visible_task(request, pk)
    services.mark_read(viewer_for(request), task_id=pk)
    return redirect("task_detail", pk=pk)


# ---------- уведомления

class NotificationListView(PageSizeMixin, RoleRequiredMixin, ListView):
    allowed_roles = TASK_ROLES
    template_name = "tasks/notifications.html"
    context_object_name = "notifications"

    def get_queryset(self):
        viewer = viewer_for(self.request)
        return (services.visible_notifications(viewer)
                .select_related("task", "event", "event__author")
                .order_by("-pk"))

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        viewer = viewer_for(self.request)
        rows = []
        for item in ctx["notifications"]:
            open_task = viewer.can_view(item.task)
            rows.append({"n": item, "open": open_task, "text": notice_text(item), "change": notice_change(item)})
        ctx["rows"] = rows
        return ctx


@require_POST
@role_required(*TASK_ROLES)
def notification_open(request, pk):
    viewer = viewer_for(request)
    item = services.visible_notifications(viewer).select_related("task").filter(pk=pk).first()
    if item is None:
        raise Http404("Уведомление не найдено")
    services.mark_read(viewer, notification_ids=[pk])
    if viewer.can_view(item.task):
        return redirect("task_detail", pk=item.task_id)
    return redirect("task_notifications")


@require_POST
@role_required(*TASK_ROLES)
def notifications_read(request):
    count = services.mark_read(viewer_for(request))
    messages.info(request, f"Отмечено прочитанными: {count}" if count else "Новых уведомлений нет")
    return redirect("task_notifications")

