"""Superuser provisioning of phone identities and their team access."""
from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import User
from django.db import transaction
from unfold.admin import ModelAdmin, TabularInline
from unfold.forms import UserChangeForm, AdminPasswordChangeForm
from unfold.widgets import UnfoldAdminTextInputWidget, UnfoldAdminSelectWidget

from .models import UserProfile, Team, TeamMembership
from .phone_numbers import normalize_phone


class PhoneUserCreationForm(forms.ModelForm):
    username = forms.CharField(
        label="Телефон", max_length=32, widget=UnfoldAdminTextInputWidget,
    )
    full_name = forms.CharField(
        label="Имя", max_length=255, widget=UnfoldAdminTextInputWidget,
    )
    team = forms.ModelChoiceField(
        label="Команда", queryset=Team.objects.filter(is_active=True),
        widget=UnfoldAdminSelectWidget,
        help_text="Если команды ещё нет, сначала добавьте её в разделе «Команды».",
    )
    role = forms.ChoiceField(
        label="Роль", choices=TeamMembership.ROLES, widget=UnfoldAdminSelectWidget,
    )

    class Meta:
        model = User
        fields = ("username",)

    def clean_username(self):
        phone = normalize_phone(self.cleaned_data["username"])
        if User.objects.filter(username=phone).exists():
            raise forms.ValidationError("Пользователь с этим телефоном уже существует.")
        return phone

    def save(self, commit=True):
        user = super().save(commit=False)
        user.first_name = self.cleaned_data["full_name"][:150]
        user.set_unusable_password()
        if commit:
            user.save()
        return user


class ProfileIdentityForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ("user", "full_name", "phone", "email", "department")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["department"].required = False
        if not self.instance.pk and "user" in self.fields:
            self.fields["user"].queryset = User.objects.filter(profile__isnull=True)

    def clean(self):
        data = super().clean()
        user = self.instance.user if self.instance.pk else data.get("user")
        if user and user.username.isdigit():
            phone = normalize_phone(data.get("phone") or user.username)
            if phone != user.username:
                self.add_error("phone", "Телефон должен совпадать с логином пользователя.")
            data["phone"] = phone
        elif data.get("phone"):
            data["phone"] = normalize_phone(data["phone"])
        return data


class MembershipInline(TabularInline):
    model = TeamMembership
    fields = ("team", "role", "status", "invited_until")
    extra = 0

    def has_add_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False


class PhoneUserAdmin(ModelAdmin, DjangoUserAdmin):
    form = UserChangeForm
    add_form = PhoneUserCreationForm
    change_password_form = AdminPasswordChangeForm
    add_fieldsets = (("Пользователь для входа по WhatsApp", {
        "fields": ("username", "full_name", "team", "role"),
    }),)
    inlines = (MembershipInline,)

    def get_inline_instances(self, request, obj=None):
        return super().get_inline_instances(request, obj) if obj else []

    def get_readonly_fields(self, request, obj=None):
        return ("username",) if obj else ()

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            UserProfile.objects.create(
                user=obj, full_name=form.cleaned_data["full_name"], phone=obj.username,
            )
            TeamMembership.objects.create(
                user=obj, team=form.cleaned_data["team"],
                role=form.cleaned_data["role"], status="active",
            )


admin.site.unregister(User)
admin.site.register(User, PhoneUserAdmin)
