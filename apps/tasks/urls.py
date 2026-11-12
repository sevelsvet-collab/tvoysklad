from django.urls import path

from . import views

urlpatterns = [
    path("tasks/", views.TaskListView.as_view(mode="mine"), name="task_mine"),
    path("tasks/delegated/", views.TaskListView.as_view(mode="delegated"), name="task_delegated"),
    path("tasks/review/", views.TaskListView.as_view(mode="review"), name="task_review"),
    path("tasks/all/", views.TaskListView.as_view(mode="all"), name="task_all"),
    path("tasks/new/", views.TaskCreateView.as_view(), name="task_create"),
    path("tasks/<int:pk>/", views.TaskDetailView.as_view(), name="task_detail"),
    path("tasks/<int:pk>/edit/", views.TaskEditView.as_view(), name="task_edit"),
    path("tasks/<int:pk>/do/<str:name>/", views.task_action, name="task_action"),
    path("tasks/<int:pk>/reassign/", views.task_reassign, name="task_reassign"),
    path("tasks/<int:pk>/checklist/<int:item_pk>/", views.task_check, name="task_check"),
    path("tasks/<int:pk>/comment/", views.task_comment, name="task_comment"),
    path("tasks/<int:pk>/read/", views.task_read, name="task_read"),
    path("tasks/notifications/", views.NotificationListView.as_view(), name="task_notifications"),
    path("tasks/notifications/<int:pk>/open/", views.notification_open, name="notification_open"),
    path("tasks/notifications/read/", views.notifications_read, name="notifications_read"),
]
