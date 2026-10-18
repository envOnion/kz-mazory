"""Explicit, read-only diagnostic views. No automatic model registry or payload dump."""
from django.db import models
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from decimal import Decimal
from rest_framework.exceptions import ValidationError
from .communication import sanitize_data
from .messenger_api import decode_cursor, encode_cursor
from .models import (AuditEvent, AsyncOperation, CommunicationEvent, CommunicationRecord,
                     Notification, OutboxEvent, ProviderReservation, ProviderUsage,
                     RawMessage, TemporalEntityRevision)


def diagnostics(request):
    from django_q.models import Task
    resources = {
        'audit': (AuditEvent, 'Аудит действий', 'Кто изменил настройки или подтвердил действие. Значения секретов исключены.',
            ('id','actor_id','action','target_type','target_id','created_at')),
        'reservations': (ProviderReservation, 'Резервы провайдера', 'Состояния резервов токенов и сроков аренды для диагностики ожиданий.',
            ('id','config_id','usage_id','purpose','operation','reserved_tokens','state','lease_until','created_at','updated_at')),
        'usage': (ProviderUsage, 'Расход провайдера', 'Сохранённые фактические токены, длительность и стоимость внешних вызовов.',
            ('id','outbox_event_id','operation','api_format','model_name','duration_ms','succeeded','input_tokens','output_tokens','cost_usd','error_code','created_at')),
        'versions': (TemporalEntityRevision, 'Версии данных', 'Неизменяемая история бизнес-данных: время события и время сохранения.',
            ('id','entity_type','entity_id','team_id','project_id','raw_message_id','actor_id','revision','event_kind','effective_at','recorded_at','time_precision','changes','snapshot')),
        'operations': (AsyncOperation, 'Фоновые операции', 'Состояния пользовательских операций и сроки доступности. Входные запросы и результаты открываются в своём рабочем разделе.',
            ('id','requested_by_id','operation_type','status','error_code','created_at','updated_at','expires_at')),
        'q2': (Task, 'Результаты Q2', 'Фактически завершённые задачи воркера. Аргументы, результаты и секретные payload не выдаются.',
            ('id','name','func','success','started','stopped')),
        'coverage': (None, 'Полнота журнала', 'Числа записей и отсутствующих проекций по настоящей БД. Эта проверка не запускает реконструкцию.',
            ('raw_total','raw_missing','notifications_missing','auth_missing','records','events','estimated_events')),
    }
    labels = {'id':'ID','actor_id':'Пользователь ID','action':'Действие','target_type':'Тип объекта','target_id':'Объект ID',
        'config_id':'Настройка ID','usage_id':'Расход ID','purpose':'Назначение','operation':'Операция','reserved_tokens':'Резерв токенов',
        'state':'Состояние','lease_until':'Аренда до','created_at':'Создано','updated_at':'Обновлено','outbox_event_id':'Outbox ID',
        'api_format':'Формат API','model_name':'Модель','duration_ms':'Длительность, мс','succeeded':'Успешно','input_tokens':'Входные токены',
        'output_tokens':'Выходные токены','cost_usd':'Стоимость, USD','error_code':'Код ошибки','entity_type':'Сущность','entity_id':'Сущность ID',
        'team_id':'Команда ID','project_id':'Проект ID','raw_message_id':'Источник ID','revision':'Версия','event_kind':'Тип события',
        'effective_at':'Время события','recorded_at':'Время сохранения','time_precision':'Точность времени','changes':'Изменения',
        'snapshot':'Сохранённое состояние','requested_by_id':'Инициатор ID','operation_type':'Тип операции','status':'Статус',
        'expires_at':'Доступно до','name':'Название','func':'Функция','success':'Успешно','started':'Начало','stopped':'Окончание',
        'raw_total':'Исходные сообщения','raw_missing':'Без проекции сообщения','notifications_missing':'Уведомления без проекции',
        'auth_missing':'Отправки OTP без проекции','records':'Записи журнала','events':'События журнала','estimated_events':'События с оценочным временем'}
    resource = request.query_params.get('resource','audit')
    if resource not in resources:
        raise ValidationError({'error':'Неизвестный раздел диагностики.'})
    model, title, description, fields = resources[resource]
    cursor = decode_cursor(request.query_params.get('cursor')) or {}
    object_id = request.query_params.get('object','')
    next_cursor = None
    if model is None:
        if object_id or cursor:
            raise ValidationError({'error':'Полнота журнала не имеет отдельных ID или страниц.'})
        items = [{'raw_total':RawMessage.objects.count(), 'raw_missing':RawMessage.objects.filter(communication_record__isnull=True).count(),
            'notifications_missing':Notification.objects.filter(communication_record__isnull=True).count(),
            'auth_missing':OutboxEvent.objects.filter(event_type='otp',communication_record__isnull=True).count(),
            'records':CommunicationRecord.objects.count(), 'events':CommunicationEvent.objects.count(),
            'estimated_events':CommunicationEvent.objects.filter(time_is_estimated=True).count()}]
    else:
        numeric = isinstance(model._meta.pk,models.IntegerField)
        def identifier(value):
            if numeric:
                if type(value) is int and value>0:
                    return value
                raise ValidationError({'error':'Курсор содержит неверный целочисленный ID.'})
            if not isinstance(value,str) or len(value)>model._meta.pk.max_length:
                raise ValidationError({'error':'Курсор содержит неверный ID задачи.'})
            return value
        qs = model.objects.all()
        if object_id:
            if numeric and (not object_id.isdecimal() or len(object_id)>18 or int(object_id)<1):
                raise ValidationError({'error':'Укажите положительный целочисленный ID.'})
            if not numeric and len(object_id)>model._meta.pk.max_length:
                raise ValidationError({'error':'Неверный ID задачи Q2.'})
            qs = qs.filter(pk=int(object_id) if numeric else object_id)
        if cursor:
            pk = identifier(cursor.get('id'))
            if resource == 'q2':
                moment = cursor.get('t')
                if moment is not None:
                    try:
                        parsed = parse_datetime(moment) if isinstance(moment,str) else None
                    except ValueError:
                        parsed = None
                    if parsed is None:
                        raise ValidationError({'error':'Курсор содержит неверное время задачи.'})
                qs = qs.filter(Q(started__lt=moment)|Q(started=moment,id__lt=pk)|Q(started__isnull=True)) if isinstance(moment,str) else qs.filter(started__isnull=True,id__lt=pk)
            else:
                qs = qs.filter(pk__lt=pk)
        qs = qs.order_by(F('started').desc(nulls_last=True),'-id') if resource=='q2' else qs.order_by('-id')
        rows = list(qs.values(*fields)[:51])
        items = rows[:50]
        if len(rows)>50:
            last=items[-1]
            next_cursor=encode_cursor({'id':last['id'],**({'t':last['started'].isoformat() if last['started'] else None} if resource=='q2' else {})})
    return {'resource':resource,'title':title,'description':description,
        'resources':[{'id':key,'label':value[1]} for key,value in resources.items()],
        'columns':[{'key':key,'label':labels[key]} for key in fields],
        'items':sanitize_data([{key:str(value) if isinstance(value,Decimal) else value for key,value in row.items()} for row in items]),
        'next_cursor':next_cursor,'as_of':timezone.now(),'coverage':'scoped'}
