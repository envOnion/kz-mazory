"""Read scopes for operations; membership alone never grants chat access."""
from django.db import models
from django.db.models import Q
from django.db.models.functions import Cast
from django.db.models.fields.json import KeyTextTransform

from . import access
from .messenger_api import records_scope
from .models import (CommunicationEvent, CrmDelivery, NotificationDelivery, OutboxEvent,
                     WhatsAppHistoryRun, AsyncOperation, RawMessage, WhatsAppConfig, Project, FactCandidate)


def crm_scope(user):
    qs = CrmDelivery.objects.all()
    if user.is_superuser:
        return qs
    return qs.filter(Q(fact_event__decision__candidate__in=access.candidates_for(user)) |
        Q(fact_event__project__in=access.projects_for(user)))


def outbox_scope(user):
    qs = OutboxEvent.objects.all()
    if user.is_superuser:
        return qs
    records = records_scope(user)
    delivery_ids = NotificationDelivery.objects.filter(notification__recipient=user).values('id')
    history_ids = WhatsAppHistoryRun.objects.filter(job__config__in=access.configs_for(user)).values('id')
    # Payload identifiers are internal numeric foreign keys written by the
    # durable command code, never user-supplied strings or provider IDs.
    for key in ('raw_id', 'delivery_id', 'history_run_id', 'project_id', 'config_id', 'candidate_id', 'operation_id'):
        name = '_text_' + key
        qs = qs.alias(**{name: KeyTextTransform(key, 'payload')}).annotate(**{
            '_scope_' + key: models.Case(models.When(**{name + '__regex': r'^[0-9]{1,18}$'},
                then=Cast(KeyTextTransform(key, 'payload'), models.BigIntegerField())),
                default=None, output_field=models.BigIntegerField())})
    condition = Q(id__in=CommunicationEvent.objects.filter(record__in=records).values('outbox_event_id'))
    condition |= Q(id__in=records.values('otp_outbox_id'))
    condition |= Q(_scope_raw_id__in=access.messages_for(user).values('id'))
    condition |= Q(_scope_delivery_id__in=delivery_ids)
    condition |= Q(_scope_history_run_id__in=history_ids)
    condition |= Q(_scope_project_id__in=access.projects_for(user).values('id'))
    condition |= Q(_scope_candidate_id__in=access.candidates_for(user).values('id'))
    condition |= Q(_scope_config_id__in=access.configs_for(user).values('id'))
    condition |= Q(_scope_operation_id__in=AsyncOperation.objects.filter(requested_by=user).values('id'))
    condition |= Q(id__in=crm_scope(user).values('outbox_event_id'))
    # The outer queryset traverses only OutboxEvent's single-valued FK to
    # BusinessEvent; all other branches are IN subqueries. This cannot fan out
    # rows, so DISTINCT only adds a sort/hash to a potentially large scan.
    return qs.filter(condition)


def outbox_team_scope(qs, team_id):
    """Narrow an already authorized queryset using actual FKs or typed payload IDs."""
    for key in ('raw_id','delivery_id','history_run_id','project_id','config_id','candidate_id','team_id'):
        name = '_team_text_' + key
        qs = qs.alias(**{name:KeyTextTransform(key,'payload')}).alias(**{
            '_team_'+key:models.Case(models.When(**{name+'__regex':r'^[0-9]{1,18}$'},
                then=Cast(KeyTextTransform(key,'payload'),models.BigIntegerField())),
                default=None,output_field=models.BigIntegerField())})
    raw = RawMessage.objects.filter(Q(team_id=team_id)|Q(team__isnull=True,config__team_id=team_id))
    # Every outer relation here is a ForeignKey (BusinessEvent -> Project);
    # all other ownership checks are subqueries, so the result is already
    # unique by OutboxEvent.pk and does not need a DISTINCT pass.
    return qs.filter(
        Q(business_event__project__team_id=team_id) |
        Q(id__in=CommunicationEvent.objects.filter(record__team_id=team_id).values('outbox_event_id')) |
        Q(_team_raw_id__in=raw.values('id')) |
        Q(_team_delivery_id__in=NotificationDelivery.objects.filter(
            Q(notification__project__team_id=team_id)|Q(notification__communication_record__team_id=team_id)).values('id')) |
        Q(_team_history_run_id__in=WhatsAppHistoryRun.objects.filter(job__config__team_id=team_id).values('id')) |
        Q(_team_project_id__in=Project.objects.filter(team_id=team_id).values('id')) |
        Q(_team_config_id__in=WhatsAppConfig.objects.filter(team_id=team_id).values('id')) |
        Q(_team_candidate_id__in=FactCandidate.objects.filter(team_id=team_id).values('id')) |
        Q(_team_team_id=team_id) |
        Q(id__in=CrmDelivery.objects.filter(external_object_link__team_id=team_id).values('outbox_event_id'))
    )
