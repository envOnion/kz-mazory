"""Durable TXT uploads feed the existing history analysis queue without WAHA."""
import hashlib
import json
import uuid
from collections import Counter,defaultdict
from pathlib import PurePath
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists,OuterRef,Q,Max
from django.conf import settings
from django.utils import timezone
from .models import AISettings,AuditEvent,HISTORY_ACTIVE_STATES,HistoryAnalysisItem,RawMessage,WhatsAppConfig,WhatsAppExportEntry,WhatsAppExportUpload,WhatsAppHistoryJob,WhatsAppHistoryRun
from .history_jobs import enqueue_step,source,finish
from .whatsapp_export_parser import MAX_BYTES,ExportError,export_zone,parse_export
from .whatsapp_identity import alias,matching_originals,temporal_originals,whatsapp_scope


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
        limits={name:getattr(cfg,name) for name in ('analysis_input_token_limit','analysis_target_message_limit','analysis_output_token_limit')}
        run=WhatsAppHistoryRun.objects.create(job=job,requested_by=user,import_kind='file',export_upload=upload,state='importing',source_snapshot=snapshot,settings_snapshot={**limits,'analysis_policy':'history-packets-v1','analysis_mode':'reprocess_all','replace_unsent':True,'only_new':False,'analyze_after_import':True,'poll_seconds':5,'request_key':request_key,'existing_entry_ordinal':upload.entries.aggregate(value=Max('ordinal'))['value'] or 0},status_message='Проверяем TXT-экспорт. Подключение WAHA не требуется.')
        enqueue_step(run)
        AuditEvent.objects.create(actor=user,target_type='WhatsAppHistoryRun',target_id=run.id,action='history_export_upload',before_after={'upload_id':upload.id,'bytes':size})
        return run
    except Exception:
        if created:upload.file.delete(save=False)
        raise


def _entries(upload,records,run):
    groups=defaultdict(list)
    for row in records:
        if row.kind=='text':groups[row.fingerprint].append(row.ordinal)
    done=set(upload.entries.values_list('ordinal',flat=True))
    for row in records[run.offset:run.offset+100]:
        if row.ordinal in done:continue
        values=dict(upload=upload,ordinal=row.ordinal,line_start=row.line_start,line_end=row.line_end,sent_at=row.sent_at,time_precision=row.precision,kind=row.kind,fingerprint=row.fingerprint)
        if row.kind!='text':
            reason='export_system' if row.kind=='system' else 'export_media_unavailable'
            WhatsAppExportEntry.objects.create(**values,resolution_state='excluded',reason_code=reason,reason_description='Служебное событие WhatsApp, не бизнес-факт.' if row.kind=='system' else 'В TXT отсутствует содержимое вложения; основание для бизнес-факта недоступно.')
            continue
        revision=hashlib.sha256(row.content.encode()).hexdigest();external=hashlib.sha256(json.dumps([upload.source_key,upload.sha256,upload.timezone,upload.date_order,row.ordinal]).encode()).hexdigest()
        known=alias(run.job.config,'export',external,revision)
        raw=known.raw_message if known else None
        matched=bool(raw)
        if not raw:
            candidates=list(matching_originals(run.job.config,row.sent_at,row.precision,row.sender,row.phone,row.content).filter(Q(raw_payload__export_upload_id__isnull=True)|~Q(raw_payload__export_upload_id=upload.id)))
            if candidates:
                if len(candidates)==len(groups[row.fingerprint]):
                    raw=candidates[groups[row.fingerprint].index(row.ordinal)];matched=True
                else:
                    WhatsAppExportEntry.objects.create(**values,resolution_state='blocked',reason_code='deduplication_ambiguous',reason_description='Число одинаковых сообщений с этим автором и временем не совпадает; TXT не содержит WhatsApp ID для однозначного сопоставления.')
                    continue
            elif temporal_originals(run.job.config,row.sent_at,row.precision,row.sender,row.phone).filter(Q(raw_payload__export_upload_id__isnull=True)|~Q(raw_payload__export_upload_id=upload.id)).exists():
                WhatsAppExportEntry.objects.create(**values,resolution_state='blocked',reason_code='deduplication_ambiguous',reason_description='У этого автора и времени сохранён другой текст; TXT не позволяет отличить редакцию от отдельной реплики. Старые данные сохранены.')
                continue
            else:
                raw=RawMessage.objects.create(source='whatsapp_export',session_name=run.source_snapshot['session_name'],config_id=run.source_snapshot['config_id'],team_id=run.source_snapshot['team_id'],chat_id=run.source_snapshot['chat_id'],message_id='export:'+hashlib.sha256(external.encode()).hexdigest(),source_revision=revision,processing_state='export_staged',timestamp=row.sent_at,sent_at_known=True,sender_name=row.sender,sender_phone=row.phone,content=row.content,raw_payload={'event':'export.import','export_upload_id':upload.id,'export_timezone':upload.timezone,'export_time_precision':row.precision,'export_line_start':row.line_start,'export_line_end':row.line_end})
            alias(run.job.config,'export',external,revision,raw)
        WhatsAppExportEntry.objects.create(**values,raw_message=raw,resolution_state='matched' if matched else 'created',reason_code='exact_original_match' if matched else 'export_new_message',reason_description='Однозначное совпадение текста, автора, времени и количества реплик выбранного чата.' if matched else 'Новый оригинал из TXT-экспорта; сохранены строки источника.')
    run.offset=min(len(records),run.offset+100)
    run.fetched_count=run.offset
    baseline=run.settings_snapshot.get('existing_entry_ordinal',0)
    run.imported_count=upload.entries.filter(resolution_state='created',ordinal__gt=baseline).count()
    run.existing_count=upload.entries.exclude(raw_message=None).filter(Q(ordinal__lte=baseline)|Q(resolution_state='matched')).count()
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
