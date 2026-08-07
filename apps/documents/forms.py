from django import forms


class InvoiceEmailForm(forms.Form):
    """Письмо со счётом на email контрагента (тема/текст редактируются в окне)."""

    to_email = forms.CharField(
        label="Кому (email)",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "client@example.com"}),
        help_text="Можно несколько адресов через запятую.",
    )
    subject = forms.CharField(
        label="Тема",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    message = forms.CharField(
        label="Сообщение",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 7}),
    )

    def clean_to_email(self):
        raw = self.cleaned_data["to_email"].replace(";", ",")
        emails = [part.strip() for part in raw.split(",") if part.strip()]
        if not emails:
            raise forms.ValidationError("Укажите хотя бы один адрес.")
        for email in emails:
            forms.EmailField().clean(email)  # валидирует формат каждого адреса
        return emails
