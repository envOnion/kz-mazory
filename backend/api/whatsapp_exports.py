"""Durable TXT uploads feed the existing history analysis queue without WAHA."""
import hashlib
import json
import uuid
from collections import Counter,defaultdict
from datetime import timedelta
from pathlib import PurePath
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count,Exists,OuterRef,Q,Max
from django.conf import settings
from django.utils import timezone
from .models import AISettings,AuditEvent,HISTORY_ACTIVE_STATES,HistoryAnalysisItem,RawMessage,WhatsAppConfig,WhatsAppExportEntry,WhatsAppExportUpload,WhatsAppHistoryJob,WhatsAppHistoryRun,WhatsAppMessageAlias
from .history_jobs import enqueue_step,source,finish
from .whatsapp_export_parser import MAX_BYTES,ExportError,export_zone,parse_export
from .whatsapp_identity import alias,whatsapp_scope


def _original_filter(row, *, with_content):
    if row.precision == 'minute':
        start=row.sent_at.replace(second=0,microsecond=0)
        query=Q(timestamp__gte=start,timestamp__lt=start+timedelta(minutes=1))
    else:
        query=Q(timestamp=row.sent_at)
    if row.phone:
        query &= (Q(sender_phone__in=[row.phone,row.phone.lstrip('+')])
                  | Q(sender_phone='',sender_name=row.sender))
    elif row.sender:
        query &= Q(sender_name=row.sender)
    else:
        return Q(pk__in=[])
    if with_content:
        query &= Q(content=row.content)
    return query


def _same_original(candidate, row, *, with_content):
    if row.precision == 'minute':
        start=row.sent_at.replace(second=0,microsecond=0)
        if not start <= candidate['timestamp'] < start+timedelta(minutes=1):
            return False
    elif candidate['timestamp'] != row.sent_at:
        return False
    if row.phone:
        same_author=candidate['sender_phone'] in [row.phone,row.phone.lstrip('+')] or (
            not candidate['sender_phone'] and candidate['sender_name']==row.sender
        )
    else:
        same_author=bool(row.sender) and candidate['sender_name']==row.sender
    return same_author and (not with_content or candidate['content']==row.content)


@transaction.atomic
def start_file(job_id,file,zone,date_order,user,request_key):
    job=WhatsAppHistoryJob.objects.select_for_update().get(pk=job_id)
    config=WhatsAppConfig.objects.select_for_update(of=('self',)).select_related('team').get(pk=job.config_id)
    snapshot=source(config)
    if not file or not 0<file.size<=MAX_BYTES:raise ValidationError('Выберите TXT-файл размером не более 20 МиБ.')
    name=PurePath(file.name.replace('\\','/')).name
    if not name.lower().endswith('.txt'):raise ValidationError('Поддерживается только TXT-экспорт WhatsApp.')
    export_zone(zone)
    if date_order not in ('DMY','MDY'):raise ValidationError('Выберите порядок даты.')
    try:request_key=str(uuid.UUID(request_key))
    except (ValueError,TypeError,AttributeError):raise ValidationError('Обновите страницу перед отправкой файла.') from None
    digest=hashlib.sha256();size=0
    for chunk in file.chunks():
        size+=len(chunk)
        if size>MAX_BYTES:raise ValidationError('Размер TXT не должен превышать 20 МиБ.')
        digest.update(chunk)
    file.seek(0)
    source_key=hashlib.sha256(json.dumps(snapshot,sort_keys=True).encode()).hexdigest()
    upload=WhatsAppExportUpload.objects.filter(job=job,source_key=source_key,sha256=digest.hexdigest(),timezone=zone,date_order=date_order).first()
    if upload:
        same=upload.runs.filter(settings_snapshot__request_key=request_key).first()
        if same:return same
        active=upload.runs.filter(state__in=HISTORY_ACTIVE_STATES).first()
        if active:return active
    active=job.runs.filter(run_kind='full',state__in=HISTORY_ACTIVE_STATES).first()
    if active:raise ValidationError(f'Уже выполняется полный запуск #{active.id}. Он не был отменён.')
    created=False
    if not upload:
        file.name=uuid.uuid4().hex+'.txt'
        upload=WhatsAppExportUpload.objects.create(job=job,uploaded_by=user,file=file,original_name=name[:255],sha256=digest.hexdigest(),source_key=source_key,timezone=zone,date_order=date_order,source_snapshot=snapshot)
        created=True
    try:
        cfg=AISettings.get_active()
        from .processing_attempts import ANALYSIS_CONFIG_FIELDS
        limits={name:getattr(cfg,name) for name in ('analysis_input_token_limit','analysis_target_message_limit','analysis_output_token_limit', *ANALYSIS_CONFIG_FIELDS)}
        run=WhatsAppHistoryRun.objects.create(job=job,requested_by=user,import_kind='file',export_upload=upload,state='importing',source_snapshot=snapshot,settings_snapshot={**limits,'analysis_policy':'history-packets-v1','full_history_policy':'thread-context-v2','analysis_mode':'reprocess_all','replace_unsent':True,'only_new':False,'analyze_after_import':True,'poll_seconds':5,'request_key':request_key,'existing_entry_ordinal':upload.entries.aggregate(value=Max('ordinal'))['value'] or 0},status_message='Проверяем TXT-экспорт. Подключение WAHA не требуется.')
        enqueue_step(run)
        AuditEvent.objects.create(actor=user,target_type='WhatsAppHistoryRun',target_id=run.id,action='history_export_upload',before_after={'upload_id':upload.id,'bytes':size})
        return run
    except Exception:
        if created:upload.file.delete(save=False)
        raise


def _entries(upload,records,run):
    from .participants import Sender, register_sender
    from .phone_numbers import normalize_phone
    authors = {(row.sender, row.phone) for row in records[run.offset:run.offset+100] if row.sender}
    for name, phone in authors:
        normalized = ''
        if phone:
            try: normalized = normalize_phone(phone)
            except ValidationError: pass
        register_sender(run.job.config, Sender(phone=normalized, name=name))
    groups=defaultdict(list)
    for row in records:
        if row.kind=='text':groups[row.fingerprint].append(row.ordinal)
    page = records[run.offset:run.offset+100]
    done=set(upload.entries.filter(ordinal__in=[row.ordinal for row in page]).values_list('ordinal',flat=True))
    export_keys = {
        row.ordinal: (
            hashlib.sha256(json.dumps([upload.source_key,upload.sha256,upload.timezone,upload.date_order,row.ordinal]).encode()).hexdigest(),
            hashlib.sha256(row.content.encode()).hexdigest(),
        )
        for row in page if row.kind == 'text'
    }
    aliases = {
        (item['external_id'], item['source_revision']): item['raw_message_id']
        for item in WhatsAppMessageAlias.objects.filter(
            config=run.job.config, session_name=run.job.config.session_name,
            chat_id=run.job.config.group_jid, namespace='export',
            external_id__in=[value[0] for value in export_keys.values()],
        ).values('external_id','source_revision','raw_message_id')
    }
    resolve_rows=[row for row in page if row.kind=='text' and row.ordinal not in done
                  and (export_keys[row.ordinal][0],export_keys[row.ordinal][1]) not in aliases]
    exact_filter=Q(pk__in=[])
    for row in resolve_rows:
        exact_filter |= _original_filter(row,with_content=True)
    original_scope=whatsapp_scope(run.job.config,include_staged=True).filter(
        Q(raw_payload__export_upload_id__isnull=True)|~Q(raw_payload__export_upload_id=upload.id)
    )
    originals=list(original_scope.filter(exact_filter).values(
        'id','timestamp','sender_name','sender_phone','content'
    )) if resolve_rows else []
    exact_by_ordinal={row.ordinal:sorted(
        [candidate for candidate in originals if _same_original(candidate,row,with_content=True)],
        key=lambda candidate:(candidate['timestamp'],candidate['id']),
    ) for row in resolve_rows}
    temporal_rows=[row for row in resolve_rows if not exact_by_ordinal[row.ordinal]]
    temporal_filter=Q(pk__in=[])
    for row in temporal_rows:
        temporal_filter |= _original_filter(row,with_content=False)
    temporal_originals=list(original_scope.filter(temporal_filter).values(
        'timestamp','sender_name','sender_phone','content'
    )) if temporal_rows else []
    temporal_by_ordinal={row.ordinal:any(
        _same_original(candidate,row,with_content=False) for candidate in temporal_originals
    ) for row in temporal_rows}
    entries_to_create=[]
    aliases_to_create=[]
    alias_expectations=[]
    for row in page:
        if row.ordinal in done:continue
        values=dict(upload=upload,ordinal=row.ordinal,line_start=row.line_start,line_end=row.line_end,sent_at=row.sent_at,time_precision=row.precision,kind=row.kind,fingerprint=row.fingerprint)
        if row.kind!='text':
            reason='export_system' if row.kind=='system' else 'export_media_unavailable'
            entries_to_create.append(WhatsAppExportEntry(**values,resolution_state='excluded',reason_code=reason,reason_description='Служебное событие WhatsApp, не бизнес-факт.' if row.kind=='system' else 'В TXT отсутствует содержимое вложения; основание для бизнес-факта недоступно.'))
            continue
        external,revision=export_keys[row.ordinal]
        known=aliases.get((external,revision))
        raw_message_id=known
        matched=bool(raw_message_id)
        if not raw_message_id:
            candidates=exact_by_ordinal.get(row.ordinal,[])
            if candidates:
                if len(candidates)==len(groups[row.fingerprint]):
                    raw_message_id=candidates[groups[row.fingerprint].index(row.ordinal)]['id'];matched=True
                else:
                    entries_to_create.append(WhatsAppExportEntry(**values,resolution_state='blocked',reason_code='deduplication_ambiguous',reason_description='Число одинаковых сообщений с этим автором и временем не совпадает; TXT не содержит WhatsApp ID для однозначного сопоставления.'))
                    continue
            elif temporal_by_ordinal.get(row.ordinal,False):
                entries_to_create.append(WhatsAppExportEntry(**values,resolution_state='blocked',reason_code='deduplication_ambiguous',reason_description='У этого автора и времени сохранён другой текст; TXT не позволяет отличить редакцию от отдельной реплики. Старые данные сохранены.'))
                continue
            else:
                raw=RawMessage.objects.create(source='whatsapp_export',session_name=run.source_snapshot['session_name'],config_id=run.source_snapshot['config_id'],team_id=run.source_snapshot['team_id'],chat_id=run.source_snapshot['chat_id'],message_id='export:'+hashlib.sha256(external.encode()).hexdigest(),source_revision=revision,processing_state='export_staged',timestamp=row.sent_at,sent_at_known=True,sender_name=row.sender,sender_phone=row.phone,content=row.content,raw_payload={'event':'export.import','export_upload_id':upload.id,'export_timezone':upload.timezone,'export_time_precision':row.precision,'export_line_start':row.line_start,'export_line_end':row.line_end})
                raw_message_id=raw.id
            if not known:
                aliases_to_create.append(WhatsAppMessageAlias(config=run.job.config,raw_message_id=raw_message_id,session_name=run.job.config.session_name,chat_id=run.job.config.group_jid,namespace='export',external_id=external,source_revision=revision))
                alias_expectations.append((external,revision,raw_message_id))
        entries_to_create.append(WhatsAppExportEntry(**values,raw_message_id=raw_message_id,resolution_state='matched' if matched else 'created',reason_code='exact_original_match' if matched else 'export_new_message',reason_description='Однозначное совпадение текста, автора, времени и количества реплик выбранного чата.' if matched else 'Новый оригинал из TXT-экспорта; сохранены строки источника.'))
    if aliases_to_create:
        created_at=timezone.now()
        for item in aliases_to_create:
            item.created_at=item.updated_at=created_at
        WhatsAppMessageAlias.objects.bulk_create(aliases_to_create,ignore_conflicts=True,batch_size=100)
        saved={(item['external_id'],item['source_revision']):item['raw_message_id'] for item in WhatsAppMessageAlias.objects.filter(
            config=run.job.config,session_name=run.job.config.session_name,chat_id=run.job.config.group_jid,
            namespace='export',external_id__in=[key for key,_,_ in alias_expectations],
        ).values('external_id','source_revision','raw_message_id')}
        if any(saved.get((external,revision))!=raw_id for external,revision,raw_id in alias_expectations):
            raise ValueError('whatsapp_alias_conflict')
    created_at=timezone.now()
    for item in entries_to_create:
        item.created_at=item.updated_at=created_at
    WhatsAppExportEntry.objects.bulk_create(entries_to_create,batch_size=100)
    run.offset=min(len(records),run.offset+100)
    run.fetched_count=run.offset
    baseline=run.settings_snapshot.get('existing_entry_ordinal',0)
    totals=upload.entries.aggregate(
        imported=Count('id',filter=Q(resolution_state='created',ordinal__gt=baseline)),
        existing=Count('id',filter=~Q(raw_message_id=None)&(Q(ordinal__lte=baseline)|Q(resolution_state='matched'))),
    )
    run.imported_count=totals['imported']
    run.existing_count=totals['existing']
    run.status_message=f'Разобрано {run.offset} / {len(records)} записей TXT. Новых сообщений: {run.imported_count}; сопоставлено: {run.existing_count}.'
    run.save()


def process_file_step(payload):
    initial=WhatsAppHistoryRun.objects.select_related('export_upload').get(pk=payload['history_run_id'])
    if initial.step!=payload['step'] or initial.state not in ('importing','analyzing'):return
    upload=initial.export_upload
    if not upload:raise ExportError('export_missing','У запуска отсутствует исходный файл.')
    records=None
    if initial.state=='importing':
        try:
            with upload.file.open('rb') as stream:data=stream.read(MAX_BYTES+1)
            if hashlib.sha256(data).hexdigest()!=upload.sha256:raise ExportError('export_checksum_changed','Исходный TXT изменился после загрузки.')
            records,encoding=parse_export(data,upload.timezone,upload.date_order)
        except (ExportError,OSError) as exc:
            code=exc.code if isinstance(exc,ExportError) else 'export_file_unavailable'
            description=exc.description if isinstance(exc,ExportError) else 'Исходный TXT недоступен в закрытом хранилище.'
            line=exc.line if isinstance(exc,ExportError) else 0
            with transaction.atomic():
                run=WhatsAppHistoryRun.objects.select_for_update().get(pk=initial.pk)
                if run.step!=payload['step'] or run.state!='importing':return
                upload.state='failed';upload.diagnostics={'error':code,'description':description,'line':line};upload.save()
                run.error_code=code;finish(run,'failed',description+(f' Строка {line}.' if line else ''))
            return
    with transaction.atomic():
        WhatsAppHistoryJob.objects.select_for_update().get(pk=initial.job_id)
        config=WhatsAppConfig.objects.select_for_update(of=('self',)).select_related('team').get(pk=initial.job.config_id)
        run=WhatsAppHistoryRun.objects.select_for_update().get(pk=initial.pk)
        if run.step!=payload['step'] or run.state not in ('importing','analyzing'):return
        if source(config)!=upload.source_snapshot:
            run.error_code='history_source_changed';finish(run,'failed','Источник задания изменился; TXT не импортирован в другой чат.');return
        if run.state=='importing':
            upload.state='parsing';upload.diagnostics={'encoding':encoding,'total':len(records),'categories':dict(Counter(row.kind for row in records)),'first_at':min(row.sent_at for row in records).isoformat(),'last_at':max(row.sent_at for row in records).isoformat()};upload.save()
            _entries(upload,records,run)
            if run.offset<len(records):enqueue_step(run);return
            upload.state='ready';upload.diagnostics['blocked']=upload.entries.filter(resolution_state='blocked').count();upload.save()
            from .participants import enqueue_participants
            enqueue_participants(config)
            RawMessage.objects.filter(raw_payload__export_upload_id=upload.id,processing_state='export_staged').update(processing_state='received')
            scope=whatsapp_scope(config)
            newer=scope.filter(source=OuterRef('source'),message_id=OuterRef('message_id'),id__gt=OuterRef('id'))
            originals=scope.annotate(has_newer=Exists(newer)).filter(has_newer=False)
            run.messages.set(originals)
            run.cutoff_at=timezone.now()
            run.settings_snapshot['snapshot_max_id']=originals.aggregate(last=Max('id'))['last'] or 0
            run.state='analyzing';run.save()
            from .history_analysis import schedule
            schedule(run)
            enqueue_step(run,5)
        else:
            from .history_analysis import counts,summary
            value=counts(run)
            if value['pending']:run.status_message=summary(value);enqueue_step(run,5)
            else:
                incomplete=value['errors'] or value['insufficient_data'] or value.get('blocked',0) or upload.diagnostics.get('blocked',0)
                finish(run,'completed_with_errors' if incomplete else 'completed',('Полный проход завершён; есть ошибки или данные, требующие уточнения. ' if incomplete else 'Полный проход завершён. ')+summary(value))
