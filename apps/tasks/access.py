"""Права на задачи — по ролям пользователя на момент запроса.

Видеть задачу может:
- администратор — любую;
- остальные сотрудники — только где они постановщик или текущий исполнитель.
Для связанной задачи дополнительно нужно право открыть карточку связанного
объекта (links.card_roles). Без него задача закрыта целиком: карточка,
комментарии, журнал, списки, поиск, счётчики и уведомления. Это касается и
постановщика, и исполнителя; администратор видит такие задачи как проблемные.

Разграничения по организациям в программе нет, и задачи его не добавляют:
права на документы зависят только от ролей.

Одни и те же правила работают в двух видах: для одной задачи (can_view) и
условием запроса (visible_q) — для списков, поиска и счётчиков. Тесты
сверяют их между собой и с ответами карточек документов.
"""
from django.contrib.auth import get_user_model
from django.db.models import Exists, OuterRef, Q

from apps.core import roles

from .links import BY_CODE, LINK_TYPES

DELEGATE_ROLES = (roles.ROLE_ADMIN, roles.ROLE_MANAGER)    # ставят задачи другим сотрудникам


class Viewer:
    def __init__(self, user, roles=None):
        """roles — заранее прочитанные роли (для списков: один запрос на страницу)."""
        self.user = user
        self.pk = user.pk if user.is_authenticated else None
        self.active = bool(user.is_authenticated and user.is_active)
        self.superuser = bool(self.active and user.is_superuser)
        if not self.active:
            self.roles = frozenset()
        elif roles is not None:
            self.roles = frozenset(roles)
        else:
            self.roles = frozenset(user.groups.values_list("name", flat=True))

    def has_role(self, *names):
        return self.superuser or bool(self.roles & set(names))

    @property
    def is_staff_member(self):
        """Сотрудник — активный пользователь с ролью из системы ролей (или суперпользователь)."""
        return self.active and self.has_role(*roles.ALL_ROLES)

    @property
    def is_admin(self):
        return self.active and self.has_role(roles.ROLE_ADMIN)

    @property
    def can_delegate(self):
        return self.active and self.has_role(*DELEGATE_ROLES)

    # ----- связанный объект

    def link_allowed(self, code):
        if not code:
            return True
        if not self.is_staff_member:
            return False
        allowed = BY_CODE[code].card_roles()
        return not allowed or self.has_role(*allowed)

    def allowed_link_codes(self):
        return [link.code for link in LINK_TYPES if self.link_allowed(link.code)]

    # ----- одна задача

    def can_view(self, task):
        if not self.is_staff_member or not self.link_allowed(task.link_type):
            return False
        return self.is_admin or self.pk in (task.creator_id, task.assignee_id)

    def can_manage(self, task):
        """Содержание, срок, исполнитель, чек-лист, приёмка, возврат, отмена, возобновление."""
        return self.can_view(task) and (self.is_admin or task.creator_id == self.pk)

    def is_executor(self, task):
        return self.can_view(task) and task.assignee_id == self.pk

    # ----- условия запросов

    def link_q(self, prefix=""):
        return Q(**{f"{prefix}link_type": ""}) | Q(**{f"{prefix}link_type__in": self.allowed_link_codes()})

    def visible_q(self, prefix=""):
        """Условие для Task (prefix="") или для модели со ссылкой на задачу (prefix="task__")."""
        if not self.is_staff_member:
            return Q(**{f"{prefix}pk__in": []})
        condition = self.link_q(prefix)
        if self.is_admin:
            return condition
        return condition & (Q(**{f"{prefix}creator_id": self.pk}) | Q(**{f"{prefix}assignee_id": self.pk}))


def viewer_for(request):
    """Права пользователя запроса — один запрос ролей на запрос страницы."""
    viewer = getattr(request, "_task_viewer", None)
    if viewer is None or viewer.user is not request.user:
        viewer = Viewer(request.user)
        request._task_viewer = viewer
    return viewer


def roles_of(user_ids):
    """{id пользователя: роли} одним запросом."""
    User = get_user_model()
    result = {pk: set() for pk in user_ids}
    rows = User.groups.through.objects.filter(user_id__in=list(user_ids)).values_list("user_id", "group__name")
    for user_id, name in rows:
        result[user_id].add(name)
    return result


def staff_users():
    """Активные сотрудники — кому можно поставить задачу."""
    User = get_user_model()
    return (User.objects.filter(is_active=True)
            .filter(Q(is_superuser=True) | Q(groups__name__in=roles.ALL_ROLES))
            .distinct().order_by("first_name", "last_name", "username"))


def lacks_link_access(field):
    """Условие для Task: пользователь из поля field (assignee / creator) не может
    открыть карточку связанного объекта. Для фильтра проблемных задач у администратора."""
    User = get_user_model()
    condition = Q(pk__in=[])
    for link in LINK_TYPES:
        allowed = link.card_roles()
        if not allowed:
            continue
        member = User.groups.through.objects.filter(user_id=OuterRef(f"{field}_id"), group__name__in=allowed)
        condition |= Q(link_type=link.code) & Q(**{f"{field}__is_superuser": False}) & ~Exists(member)
    return condition
