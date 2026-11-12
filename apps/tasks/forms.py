"""Формы задач. Это только разбор ввода: все правила проверяет services."""
from django import forms
from django.contrib.auth import get_user_model

from .access import staff_users
from .links import LINK_CODES
from .models import Task
from .services import ITEM_MAX, NewTask, TaskChanges

DATETIME_FORMAT = "%Y-%m-%dT%H:%M"

# Поля, которые форма не принимает никогда: их подмена в POST отклоняет запрос
# целиком, без частичного сохранения.
NEVER_POSTED = ("creator", "creator_id", "status", "version_override", "result", "id", "pk",
                "created_at", "submitted_at", "completed_at", "cancelled_at")
FIXED_AFTER_CREATE = ("assignee", "assignee_id", "link_type", "link_id", "link_pk",
                      *LINK_CODES, *[f"{code}_id" for code in LINK_CODES])


def forged_fields(post, extra=()):
    return sorted(name for name in (*NEVER_POSTED, *extra) if name in post)


class UserChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.display_name if obj.is_active else f"{obj.display_name} (отключён)"


def due_field():
    return forms.DateTimeField(
        label="Срок", required=False,
        input_formats=[DATETIME_FORMAT, "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local", "class": "form-control"},
                                   format=DATETIME_FORMAT),
    )


def _lines(text):
    return [line for line in (text or "").splitlines() if line.strip()]


class TaskCreateForm(forms.Form):
    title = forms.CharField(label="Заголовок", max_length=200,
                            widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}))
    description = forms.CharField(label="Описание", required=False,
                                  widget=forms.Textarea(attrs={"class": "form-control", "rows": 4}))
    assignee = UserChoiceField(label="Исполнитель", queryset=get_user_model().objects.none(),
                               widget=forms.Select(attrs={"class": "form-select"}))
    due_at = due_field()
    priority = forms.ChoiceField(label="Приоритет", choices=Task.PRIORITY_CHOICES, initial=Task.PRIORITY_NORMAL,
                                 widget=forms.Select(attrs={"class": "form-select"}))
    requires_review = forms.BooleanField(label="Требуется проверка результата", required=False,
                                         widget=forms.CheckboxInput(attrs={"class": "form-check-input"}))
    checklist = forms.CharField(label="Чек-лист", required=False,
                                help_text="Каждый пункт — с новой строки",
                                widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}))
    link_type = forms.ChoiceField(choices=[("", "")] + [(code, code) for code in LINK_CODES], required=False,
                                  widget=forms.HiddenInput)
    link_pk = forms.IntegerField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        # все действующие сотрудники; можно ли поручить выбранному — решает services
        self.fields["assignee"].queryset = staff_users()
        if user is not None and not self.is_bound:
            self.fields["assignee"].initial = user.pk

    def new_task(self):
        data = self.cleaned_data
        return NewTask(
            title=data["title"], description=data["description"], assignee=data["assignee"],
            due_at=data["due_at"], priority=data["priority"], requires_review=data["requires_review"],
            checklist=_lines(data["checklist"]), link_code=data["link_type"] or "", link_id=data["link_pk"],
        )


class TaskEditForm(forms.Form):
    title = TaskCreateForm.base_fields["title"]
    description = TaskCreateForm.base_fields["description"]
    due_at = due_field()
    priority = TaskCreateForm.base_fields["priority"]
    requires_review = TaskCreateForm.base_fields["requires_review"]
    new_items = forms.CharField(label="Добавить пункты", required=False, help_text="Каждый пункт — с новой строки",
                                widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}))
    version = forms.IntegerField(widget=forms.HiddenInput)

    def __init__(self, *args, task, **kwargs):
        # начальные значения нужны и отправленной форме: недоступное поле
        # (disabled) берёт значение из них, а не из POST
        kwargs["initial"] = {
            "title": task.title, "description": task.description, "due_at": task.due_at,
            "priority": task.priority, "requires_review": task.requires_review, "version": task.version,
        }
        super().__init__(*args, **kwargs)
        self.task = task
        self.items = list(task.checklist.filter(is_removed=False))
        for item in self.items:
            self.fields[f"item_{item.pk}"] = forms.CharField(
                required=False, max_length=ITEM_MAX, initial=item.text,
                widget=forms.TextInput(attrs={"class": "form-control form-control-sm"}))
            self.fields[f"remove_{item.pk}"] = forms.BooleanField(
                required=False, widget=forms.CheckboxInput(attrs={"class": "form-check-input"}))
        if task.is_self_task:
            self.fields["requires_review"].disabled = True
        if task.status not in Task.BEFORE_REVIEW:
            self.fields["requires_review"].disabled = True
            self.fields["new_items"].disabled = True
            for item in self.items:
                self.fields[f"item_{item.pk}"].disabled = True
                self.fields[f"remove_{item.pk}"].disabled = True

    def item_rows(self):
        return [(item, self[f"item_{item.pk}"], self[f"remove_{item.pk}"]) for item in self.items]

    def changes(self):
        data = self.cleaned_data
        renamed = {item.pk: data.get(f"item_{item.pk}") or item.text for item in self.items}
        removed = {item.pk for item in self.items if data.get(f"remove_{item.pk}")}
        return TaskChanges(
            title=data["title"], description=data["description"], due_at=data["due_at"],
            priority=data["priority"], requires_review=data["requires_review"],
            renamed=renamed, removed=removed, added=_lines(data["new_items"]),
        )


class ReassignForm(forms.Form):
    assignee = UserChoiceField(label="Новый исполнитель", queryset=get_user_model().objects.none(),
                               widget=forms.Select(attrs={"class": "form-select form-select-sm"}))
    reason = forms.CharField(label="Причина", widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}))
    version = forms.IntegerField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assignee"].queryset = staff_users()
