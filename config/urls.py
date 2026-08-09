from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

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
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
else:
    # В бою media отдаёт само приложение (за обратным прокси). Объём небольшой —
    # это подписи, печати и логотипы организаций.
    from django.urls import re_path
    from django.views.static import serve as _serve

    urlpatterns += [
        re_path(r"^media/(?P<path>.*)$", _serve, {"document_root": settings.MEDIA_ROOT}),
    ]
