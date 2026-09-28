import re
from urllib.parse import urlsplit

from django import forms
from unfold.widgets import INPUT_CLASSES

from .models import BitrixSettings
from .providers import ProviderUnavailable, checked_url


class BitrixSettingsForm(forms.ModelForm):
    new_webhook_url = forms.CharField(
        label="Новый REST Webhook URL",
        required=False,
        max_length=255,
        widget=forms.PasswordInput(
            render_value=False,
            attrs={"class": " ".join(INPUT_CLASSES), "autocomplete": "new-password"},
        ),
        help_text=(
            "Вставьте полный адрес https://ваш-портал/rest/ID/ключ/. "
            "Оставьте поле пустым, чтобы сохранить текущий адрес. "
            "Новый адрес применяется со следующей фоновой операции."
        ),
    )

    class Meta:
        model = BitrixSettings
        exclude = ("webhook_url", "inbound_token")

    def clean_new_webhook_url(self):
        value = self.cleaned_data["new_webhook_url"]
        if not value:
            return value
        try:
            parsed = urlsplit(value)
            valid = (
                not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and re.fullmatch(r"/rest/[1-9][0-9]*/[A-Za-z0-9_-]+/?", parsed.path)
            )
            if not valid:
                raise ValueError
            checked_url(value)
        except ProviderUnavailable:
            raise forms.ValidationError(
                "Используйте HTTPS и разрешённый портал Bitrix24. "
                "Для нового портала добавьте его хост в PROVIDER_ALLOWED_HOSTS на сервере."
            ) from None
        except ValueError:
            raise forms.ValidationError(
                "Укажите полный REST Webhook URL вида https://портал/rest/ID/ключ/, "
                "без параметров запроса и имени метода."
            ) from None
        normalized = value.rstrip("/") + "/"
        if len(normalized) > 255:
            raise forms.ValidationError(
                "Адрес Webhook не должен превышать 255 символов."
            )
        return normalized

    def save(self, commit=True):
        if self.cleaned_data.get("new_webhook_url"):
            self.instance.webhook_url = self.cleaned_data["new_webhook_url"]
        return super().save(commit=commit)
