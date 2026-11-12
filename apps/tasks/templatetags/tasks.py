"""Вкладка «Задачи» в карточке документа или контрагента.

Список и счётчик — только задачи, которые текущий пользователь вправе
видеть: вкладка не открывает чужих задач по документу."""
from django import template
from django.urls import reverse

from ..access import viewer_for
from ..links import for_object
from ..models import Task

register = template.Library()


@register.simple_tag(takes_context=True)
def linked_tasks(context, obj):
    request = context.get("request")
    link = for_object(obj) if obj is not None and getattr(obj, "pk", None) else None
    if request is None or link is None or not request.user.is_authenticated:
        return {"tasks": [], "count": 0, "enabled": False}
    viewer = viewer_for(request)
    if not viewer.is_staff_member or not viewer.link_allowed(link.code):
        return {"tasks": [], "count": 0, "enabled": False}
    tasks = list(Task.objects.filter(viewer.visible_q()).filter(**{link.code: obj})
                 .select_related("assignee").order_by("status", "-pk")[:100])
    order = {status: index for index, (status, _) in enumerate(Task.STATUS_CHOICES)}
    tasks.sort(key=lambda task: (task.status in Task.FINAL, order[task.status], -task.pk))
    return {
        "tasks": tasks,
        "count": len(tasks),
        "active": sum(1 for task in tasks if task.status in Task.ACTIVE),
        "enabled": True,
        "create_url": f"{reverse('task_create')}?link={link.code}:{obj.pk}",
    }
