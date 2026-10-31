from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, re_path

from apps.core.uploads import media_file

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("apps.core.urls")),
    path("", include("apps.partners.urls")),
    path("", include("apps.catalog.urls")),
    path("", include("apps.inventory.urls")),
    path("", include("apps.purchases.urls")),
    path("", include("apps.sales.urls")),
    path("", include("apps.documents.urls")),
    path("", include("apps.finance.urls")),
    path("", include("apps.reports.urls")),
    # media всегда отдаёт приложение: оно решает, что можно показать в браузере
    re_path(r"^media/(?P<path>.*)$", media_file),
]
