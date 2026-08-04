from django.apps import AppConfig
from django.db.backends.signals import connection_created
from django.db.models.signals import post_migrate


def create_roles(sender, **kwargs):
    from django.contrib.auth.models import Group

    from .roles import ALL_ROLES

    for name in ALL_ROLES:
        Group.objects.get_or_create(name=name)


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    verbose_name = "Основное"

    def ready(self):
        from .sqlite_compat import install_unicode_like

        post_migrate.connect(create_roles, sender=self)
        connection_created.connect(install_unicode_like)
