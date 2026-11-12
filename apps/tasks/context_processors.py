def task_notifications(request):
    """Непрочитанные уведомления для значка в верхней панели — только те,
    что пользователь вправе видеть сейчас (по текущим ролям)."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    from .access import viewer_for
    from .services import unread_count

    viewer = viewer_for(request)
    if not viewer.is_staff_member:
        return {}
    return {"task_unread": unread_count(viewer), "tasks_enabled": True}
