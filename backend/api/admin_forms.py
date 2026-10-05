import re
from urllib.parse import urlsplit

from django import forms
from django.views.decorators.debug import sensitive_variables
from unfold.widgets import INPUT_CLASSES

from .bitrix_config import checked_bitrix_webhook_base
from .models import AISettings, BitrixSettings
from .providers import ProviderUnavailable, checked_base_url


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
            normalized = checked_bitrix_webhook_base(value)
        except ProviderUnavailable as exc:
            if str(exc) == "bitrix_webhook_invalid":
                message = (
                    "Укажите полный REST Webhook URL вида "
                    "https://портал/rest/ID/ключ/, без параметров запроса "
                    "и имени метода."
                )
            else:
                message = (
                    "Используйте HTTPS, порт 443 и адрес без логина и пароля."
                )
            raise forms.ValidationError(message) from None
        if len(normalized) > 255:
            raise forms.ValidationError(
                "Адрес Webhook не должен превышать 255 символов."
            )
        return normalized

    def clean_deal_object_field_code(self):
        value = (
            self.cleaned_data.get("deal_object_field_code") or ""
        ).strip().upper()
        if value and not re.fullmatch(r"UF_CRM_[A-Z0-9_]+", value):
            raise forms.ValidationError(
                "Укажите код пользовательского поля вида UF_CRM_* либо оставьте поле пустым."
            )
        return value

    def save(self, commit=True):
        if self.cleaned_data.get("new_webhook_url"):
            self.instance.webhook_url = self.cleaned_data["new_webhook_url"]
        return super().save(commit=commit)


class AISettingsForm(forms.ModelForm):
    new_chat_api_key = forms.CharField(
        label="Новый Chat API key",
        required=False,
        strip=False,
        max_length=4096,
        widget=forms.PasswordInput(
            render_value=False,
            attrs={"class": " ".join(INPUT_CLASSES), "autocomplete": "new-password"},
        ),
        help_text=(
            "Оставьте пустым, чтобы сохранить текущий ключ. Значение шифруется "
            "и после сохранения больше не отображается."
        ),
    )
    clear_chat_api_key = forms.BooleanField(
        label="Удалить Chat API key",
        required=False,
        help_text="Следующие Chat-запросы завершатся безопасной ошибкой настройки.",
    )
    new_embedding_api_key = forms.CharField(
        label="Новый Embeddings API key",
        required=False,
        strip=False,
        max_length=4096,
        widget=forms.PasswordInput(
            render_value=False,
            attrs={"class": " ".join(INPUT_CLASSES), "autocomplete": "new-password"},
        ),
        help_text=(
            "Оставьте пустым, чтобы сохранить текущий ключ. Значение шифруется "
            "и после сохранения больше не отображается."
        ),
    )
    clear_embedding_api_key = forms.BooleanField(
        label="Удалить Embeddings API key",
        required=False,
        help_text="Следующие запросы embeddings завершатся безопасной ошибкой настройки.",
    )

    class Meta:
        model = AISettings
        exclude = (
            "chat_api_key",
            "chat_api_key_encrypted",
            "embedding_api_key",
            "embedding_api_key_encrypted",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._encrypted_replacements = {}
        self._previous_chat_api_format = None
        self._previous_chat_provider_url = ""
        self._previous_embedding_provider_url = ""
        if self.instance and self.instance.pk:
            previous = AISettings.objects.filter(pk=self.instance.pk).only(
                "chat_api_format", "chat_provider_url", "embedding_provider_url"
            ).first()
            if previous:
                self._previous_chat_api_format = previous.chat_api_format
                self._previous_chat_provider_url = previous.chat_provider_url
                self._previous_embedding_provider_url = (
                    previous.embedding_provider_url
                )

    @staticmethod
    def _validate_key(value, field_name, form):
        if value and not value.strip():
            form.add_error(field_name, "Ключ не может состоять только из пробелов.")
            return False
        return bool(value)

    @staticmethod
    def _host(value):
        try:
            return (urlsplit(value).hostname or "").lower()
        except (TypeError, ValueError):
            return ""

    def _clean_base_url(self, field_name):
        value = self.cleaned_data.get(field_name)
        if not value:
            return
        try:
            self.cleaned_data[field_name] = checked_base_url(value)
        except ProviderUnavailable:
            self.add_error(
                field_name,
                "Используйте HTTPS Base URL с хостом, без логина, параметров, "
                "фрагмента и нестандартного порта.",
            )

    @sensitive_variables()
    def clean(self):
        cleaned_data = super().clean()
        chat_format = cleaned_data.get("chat_api_format")
        self._clean_base_url("chat_provider_url")
        self._clean_base_url("embedding_provider_url")

        replacements = (
            ("chat", "new_chat_api_key", "clear_chat_api_key"),
            ("embedding", "new_embedding_api_key", "clear_embedding_api_key"),
        )
        for purpose, new_field, clear_field in replacements:
            new_value = cleaned_data.get(new_field, "")
            clear = bool(cleaned_data.get(clear_field))
            has_replacement = self._validate_key(new_value, new_field, self)
            if has_replacement and clear:
                self.add_error(
                    clear_field,
                    "Выберите одно действие: заменить ключ или удалить его.",
                )
                continue
            if has_replacement:
                from .ai_credentials import CredentialError, encrypt_credential

                try:
                    self._encrypted_replacements[purpose] = encrypt_credential(
                        new_value,
                        purpose=purpose,
                        api_format=(
                            chat_format
                            if purpose == "chat"
                            else AISettings.ChatApiFormat.OPENAI_COMPATIBLE
                        ),
                    )
                except CredentialError:
                    self.add_error(
                        new_field,
                        "Шифрование ключей сейчас недоступно. Проверьте серверную "
                        "настройку AI_CREDENTIAL_ENCRYPTION_KEY.",
                    )

        if self.instance and self.instance.pk:
            new_url = cleaned_data.get("chat_provider_url", "")
            format_changed = (
                chat_format
                and self._previous_chat_api_format
                and chat_format != self._previous_chat_api_format
            )
            host_changed = (
                new_url
                and self._host(new_url) != self._host(self._previous_chat_provider_url)
            )
            if (
                (format_changed or host_changed)
                and not cleaned_data.get("new_chat_api_key")
                and not cleaned_data.get("clear_chat_api_key")
            ):
                self.add_error(
                    "new_chat_api_key",
                    "При смене формата API или hostname введите новый Chat API key "
                    "либо явно удалите сохранённый ключ.",
                )
            new_embedding_url = cleaned_data.get("embedding_provider_url", "")
            embedding_host_changed = (
                new_embedding_url
                and self._host(new_embedding_url)
                != self._host(self._previous_embedding_provider_url)
            )
            if (
                embedding_host_changed
                and not cleaned_data.get("new_embedding_api_key")
                and not cleaned_data.get("clear_embedding_api_key")
            ):
                self.add_error(
                    "new_embedding_api_key",
                    "При смене hostname Embeddings введите новый Embeddings API key "
                    "либо явно удалите сохранённый ключ.",
                )
        return cleaned_data

    @sensitive_variables()
    def save(self, commit=True):
        instance = super().save(commit=False)
        if "chat" in self._encrypted_replacements:
            instance.chat_api_key_encrypted = self._encrypted_replacements["chat"]
        elif self.cleaned_data.get("clear_chat_api_key"):
            instance.clear_chat_api_key()
        if "embedding" in self._encrypted_replacements:
            instance.embedding_api_key_encrypted = self._encrypted_replacements[
                "embedding"
            ]
        elif self.cleaned_data.get("clear_embedding_api_key"):
            instance.clear_embedding_api_key()
        instance.chat_api_key = ""
        instance.embedding_api_key = ""
        if commit:
            instance.save()
            self.save_m2m()
        return instance
