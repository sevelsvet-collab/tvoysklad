from functools import wraps

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied


def role_required(*allowed_roles):
    """Декоратор для функций-представлений (в т.ч. JSON API): проверяет роль пользователя."""

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                raise PermissionDenied("Требуется вход в систему")
            if allowed_roles and not request.user.has_role(*allowed_roles):
                raise PermissionDenied("Недостаточно прав")
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator


class RoleRequiredMixin(LoginRequiredMixin):
    """Доступ только пользователям с одной из ролей (суперпользователь проходит всегда).

    Пустой allowed_roles — достаточно быть залогиненным.
    """

    allowed_roles: list = []

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if self.allowed_roles and not request.user.has_role(*self.allowed_roles):
            raise PermissionDenied("Недостаточно прав для этого раздела")
        return super().dispatch(request, *args, **kwargs)
