from unfold.admin import ModelAdmin
from . import access


class ScopedReadOnlyAdmin(ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        return {}

    def get_inline_instances(self, request, obj=None):
        # Full traces can contain other chat content; dedicated trace permissions apply.
        return []

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        user = request.user
        if user.is_superuser:
            return qs
        name = self.model.__name__
        if name == "Project":
            return qs.filter(pk__in=access.projects_for(user))
        if name == "UserProfile":
            return qs.filter(pk__in=access.profiles_for(user))
        if name == "Company":
            return qs.filter(projects__in=access.projects_for(user)).distinct()
        if name == "RawMessage":
            return qs.filter(pk__in=access.messages_for(user))
        if name == "MessageProcessingTrace":
            return qs.filter(raw_message__in=access.messages_for(user))
        if name == "Commitment":
            return qs.filter(pk__in=access.commitments_for(user))
        if name in ("FinancialRecord", "BusinessEvent", "BitrixDealChangeLog"):
            return qs.filter(project__in=access.projects_for(user))
        return qs.none()


class IntegrationAdmin(ModelAdmin):
    def save_model(self, request, obj, form, change):
        from .models import AuditEvent

        super().save_model(request, obj, form, change)
        AuditEvent.objects.create(
            actor=request.user,
            target_type=obj.__class__.__name__,
            target_id=obj.pk,
            action="integration_config",
            before_after={"fields": list(form.changed_data)},
        )

    actions = None

    def has_view_permission(self, request, obj=None):
        return access.integration_allowed(request.user) and super().has_view_permission(
            request, obj
        )

    def has_change_permission(self, request, obj=None):
        return access.integration_allowed(
            request.user
        ) and super().has_change_permission(request, obj)

    def has_add_permission(self, request):
        return access.integration_allowed(request.user) and super().has_add_permission(
            request
        )

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        return {}


class SuperuserAdmin(ModelAdmin):
    def save_model(self, request, obj, form, change):
        from .models import AuditEvent, Team

        if isinstance(obj, Team) and change:
            old = Team.objects.get(pk=obj.pk)
            obj.rules_version = old.rules_version + 1
        super().save_model(request, obj, form, change)
        AuditEvent.objects.create(
            actor=request.user,
            target_type=obj.__class__.__name__,
            target_id=obj.pk,
            action="admin_update" if change else "admin_create",
            before_after={"fields": list(form.changed_data)},
        )

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False
