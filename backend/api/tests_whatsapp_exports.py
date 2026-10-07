import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections,transaction
from django.db.models import Max,Sum
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.test import Client,SimpleTestCase,TestCase,TransactionTestCase,override_settings,skipUnlessDBFeature
from django.urls import reverse
from .whatsapp_export_parser import ExportError,parse_export
from .whatsapp_exports import start_file,process_file_step
from .whatsapp_identity import ingest_waha,ingest_waha_batch
from .history_analysis import counts,schedule
from .history_jobs import start_job,control_run,finish
from .models import AuditEvent,Commitment,CrmDelivery,ExternalObjectLink,FactCandidate,FactDecision,FactEvent,FactEvidence,FinancialRecord,HistoryAnalysisItem,MessageProcessingTrace,OutboxEvent,Project,RawMessage,Team,WhatsAppConfig,WhatsAppExportEntry,WhatsAppHistoryJob,WhatsAppHistoryRun,WhatsAppMessageAlias
from .replay_replacement import guard,replace_unsent
from .tests_gemma_context import config as gemma_config
from .tests_gemma_context import endpoint
from .context_tokens import gemma_counter
from .ai_service import AIService
from .tasks import run_outbox
from .providers import ProviderUnavailable


class ExportParserTests(SimpleTestCase):
    def test_android_preserves_multiline_and_source_lines(self):
        rows,_=parse_export('19.08.2026, 18:30 - Оператор: Отчёт\r\n1. Объект Альфа\r\n\r\n2. Объект Бета\r\n19.08.2026, 18:31 - Служебное событие\r\n'.encode(),'Asia/Almaty')
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0].content,'Отчёт\n1. Объект Альфа\n\n2. Объект Бета')
        self.assertEqual((rows[0].line_start,rows[0].line_end),(1,4))
        self.assertEqual(rows[0].sent_at.utcoffset().total_seconds(),18000)
        self.assertEqual(rows[1].kind,'system')

    def test_ios_utf16_and_12_hour_seconds(self):
        rows,encoding=parse_export('\ufeff[08/19/26, 6:30:05 PM] +7 701 000 00 00: Получил\n'.encode('utf-16'),'UTC+03:00','MDY')
        self.assertEqual(encoding,'utf-16');self.assertEqual(rows[0].phone,'+77010000000')
        self.assertEqual((rows[0].sent_at.day,rows[0].sent_at.hour,rows[0].precision),(19,18,'second'))

    def test_system_and_media_have_no_invented_author(self):
        rows,_=parse_export('01.09.2026, 12:00 - Сообщения защищены\n01.09.2026, 12:01 - Оператор: <Без медиафайлов>\n01.09.2026, 12:02 - Оператор: Фото (файл добавлен)\nПодпись: поступило 100 тенге\n'.encode(),'UTC')
        self.assertEqual([row.kind for row in rows],['system','media','text'])
        self.assertEqual(rows[0].sender,'');self.assertIn('Подпись',rows[2].content)

    def test_corruption_is_rejected_before_partial_import(self):
        for content,code in [(b'PK\x00\x01','export_binary_content'),(b'\xffbad','export_encoding_invalid'),('31.02.2026, 12:00 - Оператор: Отчёт'.encode(),'export_date_invalid'),('01.09.2026, 12:00 - Оператор: Текст\n01.09.2026, xx:yy - Ошибка'.encode(),'export_header_invalid'),('Случайный текст\n01.09.2026, 12:00 - Оператор: Текст'.encode(),'export_preamble_unrecognized')]:
            with self.subTest(code=code),self.assertRaises(ExportError) as caught:parse_export(content,'UTC')
            self.assertEqual(caught.exception.code,code)

    def test_ambiguous_or_nonexistent_dst_date_is_rejected(self):
        for stamp in ('25.10.2026, 02:30','29.03.2026, 02:30'):
            with self.assertRaises(ExportError):parse_export((stamp+' - Оператор: Текст').encode(),'Europe/Berlin')


class ExportFixtures(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.settings=override_settings(MEDIA_ROOT=self.temp.name);self.settings.enable();self.addCleanup(self.settings.disable)
        self.user=User.objects.create_superuser('exports','exports@example.test','password')
        self.team=Team.objects.create(name='Export team')
        self.config=WhatsAppConfig.objects.create(team=self.team,group_jid='export@g.us',snapshot={'timezone':'UTC'})
        self.job=WhatsAppHistoryJob.objects.create(config=self.config)
        self.cfg=gemma_config(is_active=True);self.cfg.save()
        self.client.force_login(self.user)

    def upload(self,body='01.09.2026, 12:00 - Оператор: Поступило 100000 тенге\n',request_key=None):
        return start_file(self.job.id,SimpleUploadedFile('chat.txt',body.encode(),content_type='text/plain'),'UTC','DMY',self.user,request_key or str(uuid.uuid4()))

    def import_all(self,run):
        for _ in range(20):
            run.refresh_from_db()
            if run.state!='importing':return
            process_file_step({'history_run_id':run.id,'step':run.step})
        self.fail('File import did not finish')


class ExportFlowTests(ExportFixtures):
    def test_native_history_page_keeps_bounded_sql_and_transport_aliases(self):
        items=[{'message_id':f'bulk:{i}','content':f'Report {i}','timestamp':datetime(2026,9,1,12,0,tzinfo=dt_timezone.utc)} for i in range(100)]
        with CaptureQueriesContext(connection) as queries:
            saved=ingest_waha_batch(self.config,items)
        self.assertLessEqual(len(queries),10)
        self.assertEqual(sum(created for raw,created in saved),100)
        repeated=ingest_waha_batch(self.config,items)
        self.assertFalse(any(created for raw,created in repeated))
        self.assertEqual(RawMessage.objects.count(),100);self.assertEqual(WhatsAppMessageAlias.objects.count(),100)

    def test_import_without_waha_uses_shared_analysis_and_all_records_accounted(self):
        run=self.upload('01.09.2026, 12:00 - Служебное событие\n01.09.2026, 12:01 - Оператор: Отчёт\nОбъект Альфа\n01.09.2026, 12:02 - Оператор: <Без медиафайлов>\n')
        with patch('api.tasks.waha_request',side_effect=AssertionError('WAHA must not be called')):self.import_all(run)
        run.refresh_from_db();self.assertEqual(run.state,'analyzing')
        self.assertEqual(run.export_upload.entries.count(),3)
        self.assertEqual(run.messages.count(),1);self.assertEqual(RawMessage.objects.get().content,'Отчёт\nОбъект Альфа')
        self.assertEqual(counts(run)['pending'],1)
        self.assertTrue(OutboxEvent.objects.filter(event_type='extract_message',payload__history_run_id=run.id).exists())

    def test_same_post_and_new_run_do_not_duplicate_raw_messages(self):
        key=str(uuid.uuid4());run=self.upload(request_key=key)
        self.assertEqual(self.upload(request_key=key).id,run.id)
        self.import_all(run);control_run(run.id,'cancel',self.user)
        self.assertEqual(self.upload(request_key=key).id,run.id)
        second=self.upload();self.import_all(second)
        second.refresh_from_db();self.assertEqual((second.imported_count,second.existing_count),(0,1))
        self.assertEqual(RawMessage.objects.count(),1);self.assertEqual(second.export_upload_id,run.export_upload_id)
        self.assertEqual(second.messages.get().id,run.messages.get().id)

    def test_earlier_waha_and_later_waha_use_one_canonical_original(self):
        stamp=datetime(2026,9,1,12,0,23,tzinfo=dt_timezone.utc)
        with transaction.atomic():raw,_=ingest_waha(self.config,message_id='real:1',content='Поступило 100000 тенге',timestamp=stamp,sender_name='Оператор')
        run=self.upload();self.import_all(run)
        self.assertEqual(run.messages.get().id,raw.id);self.assertEqual(RawMessage.objects.count(),1)
        self.assertEqual(run.export_upload.entries.get().resolution_state,'matched')
        control_run(run.id,'cancel',self.user)
        later=self.upload('02.09.2026, 12:00 - Оператор: Новое сообщение\n');self.import_all(later)
        exported=RawMessage.objects.get(source='whatsapp_export')
        with transaction.atomic():canonical,created=ingest_waha(self.config,message_id='real:2',content=exported.content,timestamp=exported.timestamp.replace(second=20),sender_name='Оператор')
        self.assertFalse(created);self.assertEqual(canonical.id,exported.id)
        self.assertEqual(WhatsAppMessageAlias.objects.filter(raw_message=exported).count(),2)

    def test_identical_occurrences_are_not_collapsed_and_partial_overlap_is_blocked(self):
        body='01.09.2026, 12:00 - Оператор: Повтор\n'*2
        run=self.upload(body);self.import_all(run)
        self.assertEqual(RawMessage.objects.count(),2)
        control_run(run.id,'cancel',self.user)
        other=self.upload('01.09.2026, 12:00 - Оператор: Повтор\n');self.import_all(other)
        self.assertEqual(RawMessage.objects.count(),2)
        self.assertEqual(other.export_upload.entries.get().reason_code,'deduplication_ambiguous')

    def test_monitor_and_full_file_run_can_coexist(self):
        self.job.only_new=True;self.job.save()
        monitor=start_job(self.job.id,self.user)
        run=self.upload();self.import_all(run)
        self.assertEqual(monitor.run_kind,'monitor');self.assertEqual(run.run_kind,'full')
        self.job.refresh_from_db();self.assertTrue(self.job.only_new)
        control_run(run.id,'cancel',self.user);monitor.refresh_from_db();self.assertEqual(monitor.state,'waiting_connection')

    def test_two_full_runs_cannot_overlap(self):
        self.upload()
        with self.assertRaises(ValidationError):self.upload('02.09.2026, 12:00 - Оператор: Другой файл\n')

    def test_file_completion_keeps_native_schedule(self):
        planned=datetime(2026,10,8,12,0,tzinfo=dt_timezone.utc)
        self.job.interval_minutes=30;self.job.next_run_at=planned;self.job.save()
        run=self.upload()
        finish(run,'failed','Synthetic file failure')
        self.job.refresh_from_db()
        self.assertEqual(self.job.next_run_at,planned)

    def test_invalid_file_does_not_create_raw_or_ai_jobs(self):
        run=self.upload('Некорректный файл')
        self.import_all(run);run.refresh_from_db()
        self.assertEqual(run.state,'failed');self.assertEqual(run.error_code,'export_preamble_unrecognized')
        self.assertFalse(RawMessage.objects.exists());self.assertFalse(OutboxEvent.objects.filter(event_type='extract_message').exists())

    def test_admin_upload_invalid_form_and_download_permissions(self):
        url=reverse('admin:api_whatsapphistoryjob_import_txt',args=[self.job.id])
        response=self.client.post(url,{'file':SimpleUploadedFile('chat.zip',b'PKfake'),'timezone':'UTC','date_order':'DMY','request_key':str(uuid.uuid4())})
        self.assertEqual(response.status_code,200);self.assertContains(response,'Поддерживается только TXT')
        self.assertFalse(self.job.exports.exists())
        response=self.client.post(url,{'file':SimpleUploadedFile('chat.txt',b'01.09.2026, 12:00 - Test: text'),'timezone':'UTC','date_order':'DMY','request_key':str(uuid.uuid4())})
        self.assertEqual(response.status_code,302)
        run=self.job.runs.get();self.import_all(run)
        url=reverse('admin:api_whatsapphistoryjob_export_download',args=[self.job.id,run.export_upload_id])
        response=self.client.get(url);self.assertEqual(response.status_code,200);self.assertIn('private',response['Cache-Control']);self.assertIn('no-store',response['Cache-Control'])
        self.client.logout();self.assertEqual(self.client.get(url).status_code,302)

    def test_large_multipart_export_is_allowed_only_on_upload_route(self):
        url=reverse('admin:api_whatsapphistoryjob_import_txt',args=[self.job.id])
        data=b'01.09.2026, 12:00 - Test: report\n'+b'x'*(2*1024*1024)
        fields={'file':SimpleUploadedFile('large.txt',data),'timezone':'UTC','date_order':'DMY','request_key':str(uuid.uuid4())}
        self.assertEqual(self.client.post(url,fields).status_code,302)
        self.assertEqual(self.job.exports.count(),1)
        self.assertFalse(RawMessage.objects.exists())
        self.assertEqual(self.client.post(reverse('admin:api_whatsapphistoryjob_change',args=[self.job.id]),{'file':SimpleUploadedFile('large.txt',data)}).status_code,413)

    def test_corresponding_source_scopes_and_frozen_snapshot_are_shared(self):
        from .message_context import source_scope
        from .dialogue_threads import source_key
        run=self.upload();self.import_all(run);raw=run.messages.get()
        with transaction.atomic():native,_=ingest_waha(self.config,message_id='after',content='Позднее сообщение',timestamp=raw.timestamp,sender_name='Другой автор')
        self.assertEqual(source_key(native),source_key(raw));self.assertEqual(source_scope(raw).count(),2)
        trace=run.analysis_items.get().trace
        self.assertLess(trace.context_metadata['snapshot_max_id'],native.id)

    def test_changed_export_is_held_without_creating_another_payment_source(self):
        run=self.upload();self.import_all(run);control_run(run.id,'cancel',self.user)
        second=self.upload('01.09.2026, 12:00 - Оператор: Поступило 200000 тенге\n');self.import_all(second)
        self.assertEqual(RawMessage.objects.count(),1)
        self.assertEqual(second.export_upload.entries.get().reason_code,'deduplication_ambiguous')

    def test_changed_waha_revision_supersedes_file_original(self):
        run=self.upload();self.import_all(run);original=run.messages.get()
        with transaction.atomic():ingest_waha(self.config,message_id='edited',content=original.content,timestamp=original.timestamp,sender_name=original.sender_name)
        with transaction.atomic():new,_=ingest_waha(self.config,message_id='edited',content='Изменённый отчёт',timestamp=original.timestamp,sender_name=original.sender_name)
        original.refresh_from_db();self.assertEqual(original.processing_state,'superseded')
        self.assertNotEqual(new.pk,original.pk)

    def test_chunk_retry_and_cancellation_resume_without_staged_context(self):
        body=''.join(f'01.09.2026, 12:00 - Оператор: Запись {i}\n' for i in range(101))
        run=self.upload(body);process_file_step({'history_run_id':run.id,'step':run.step})
        run.refresh_from_db();self.assertEqual(run.offset,100)
        from .message_context import history_queryset
        first=RawMessage.objects.first()
        self.assertFalse(history_queryset(first,first.id)[0].exists())
        process_file_step({'history_run_id':run.id,'step':run.step-1})
        self.assertEqual(RawMessage.objects.count(),100)
        control_run(run.id,'cancel',self.user)
        second=self.upload(body);self.import_all(second)
        self.assertEqual(RawMessage.objects.count(),101);self.assertEqual(second.messages.count(),101)
        second.refresh_from_db();self.assertEqual((second.imported_count,second.existing_count),(1,100))

    def test_csrf_staff_and_upload_scope_are_enforced(self):
        url=reverse('admin:api_whatsapphistoryjob_import_txt',args=[self.job.id])
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.user)
        self.assertEqual(csrf.post(url,{}).status_code,403)
        staff=User.objects.create_user('staff',is_staff=True);self.client.force_login(staff)
        self.assertEqual(self.client.post(url,{}).status_code,403)
        self.client.force_login(self.user);run=self.upload();self.import_all(run)
        other=WhatsAppHistoryJob.objects.create(config=WhatsAppConfig.objects.create(team=self.team,group_jid='other@g.us'))
        self.assertEqual(self.client.get(reverse('admin:api_whatsapphistoryjob_export_download',args=[other.id,run.export_upload_id])).status_code,404)

    def test_oversized_and_changed_original_never_queue_ai(self):
        file=SimpleUploadedFile('huge.txt',b'x');file.size=20*1024*1024+1
        with self.assertRaises(ValidationError):start_file(self.job.id,file,'UTC','DMY',self.user,str(uuid.uuid4()))
        run=self.upload()
        with run.export_upload.file.open('wb') as stream:stream.write(b'changed')
        self.import_all(run);run.refresh_from_db()
        self.assertEqual(run.error_code,'export_checksum_changed');self.assertFalse(RawMessage.objects.exists())


class ReplayTests(ExportFixtures):
    def candidate(self,raw,status='pending',kind='payment'):
        trace=MessageProcessingTrace.objects.create(raw_message=raw,operation_key=f'old:{raw.id}',attempt_no=(raw.traces.aggregate(value=Max('attempt_no'))['value'] or 0)+1)
        return FactCandidate.objects.create(trace=trace,team=self.team,status=status,fact_type=kind,source_key=f'candidate:{raw.id}',proposed_changes={})

    def execute(self,event,result):
        with patch.object(AIService,'_credential',return_value='fixture-key'),patch('api.message_context.context_runtime',return_value=(gemma_counter(),endpoint())),patch.object(AIService,'analyze_payload',return_value=(result,{},{})):
            run_outbox(event.id)

    def result(self,raw,facts=None):
        return {'threads':[{'key':'report','topic':'Отчёт','state':'ready','completion_reason':'Получена полная реплика.','messages':[{'raw_message_id':raw.id,'thought_state':'final','relation':'discusses'}]}],'facts':facts or []}

    def test_empty_facts_replaces_old_only_after_validated_success(self):
        previous=self.upload();self.import_all(previous);raw=previous.messages.get();old=self.candidate(raw)
        control_run(previous.id,'cancel',self.user);run=self.upload();self.import_all(run)
        item=run.analysis_items.get();self.execute(item.outbox_event,self.result(raw))
        old.refresh_from_db();item.refresh_from_db();item.trace.refresh_from_db()
        self.assertEqual(old.status,'superseded',str({'event':OutboxEvent.objects.get(pk=item.outbox_event_id).state,'trace_status':item.trace.status,'metadata_replace':item.trace.context_metadata.get('replace_unsent'),'error':item.trace.error_code}));self.assertEqual(item.disposition,'no_facts')
        self.assertTrue(item.trace.context_metadata['replace_unsent'])

    def test_provider_failure_and_publication_failure_keep_old_results(self):
        for stage in ('provider','publication'):
            run=self.upload();self.import_all(run);raw=run.messages.get()
            old=FactCandidate.objects.filter(trace__raw_message=raw,status='pending').first() or self.candidate(raw)
            control_run(run.id,'cancel',self.user);run=self.upload();self.import_all(run)
            event=run.analysis_items.get().outbox_event
            with patch.object(AIService,'_credential',return_value='fixture-key'),patch('api.message_context.context_runtime',return_value=(gemma_counter(),endpoint())),patch.object(AIService,'analyze_payload',side_effect=ProviderUnavailable('provider_connection_error') if stage=='provider' else None,return_value=(self.result(raw),{},{})),patch('api.dialogue_threads.persist_themes',side_effect=ProviderUnavailable('thread_revision_conflict')):
                run_outbox(event.id)
            old.refresh_from_db();self.assertEqual(old.status,'pending')
            self.assertFalse(AuditEvent.objects.filter(action='replay_superseded').exists())
            control_run(run.id,'cancel',self.user)

    def test_delivered_evidence_skips_ai_and_bitrix_project_id_alone_does_not(self):
        run=self.upload();self.import_all(run);raw=run.messages.get()
        candidate=self.candidate(raw,'approved')
        project=Project.objects.create(name='CRM project',team=self.team,bitrix_id='123')
        candidate.project=project;candidate.save()
        self.assertIsNone(guard(raw))
        decision=FactDecision.objects.create(candidate=candidate,outcome='accepted',policy_version='test',input_fingerprint='test',reason_code='test',explanation='test')
        event=FactEvent.objects.create(decision=decision,team=self.team,event_key='test',event_type='payment')
        link=ExternalObjectLink.objects.create(team=self.team,integration_key='test',object_type='deal',local_type='Project',local_id=1,origin_key='test',external_id='123')
        outbox=OutboxEvent.objects.create(event_type='autonomous_crm',deduplication_key='test',state='done')
        CrmDelivery.objects.create(outbox_event=outbox,external_object_link=link,fact_event=event,target_version=1,state='delivered')
        self.assertEqual(guard(raw),'skipped_crm_delivered')
        control_run(run.id,'cancel',self.user);second=self.upload();self.import_all(second)
        self.assertEqual(counts(second)['skipped_crm_delivered'],1);self.assertEqual(counts(second)['pending'],0)
        self.assertFalse(OutboxEvent.objects.filter(event_type='extract_message',payload__history_run_id=second.id).exists())

    def test_replacing_unsent_payment_compensates_money_and_keeps_audit(self):
        run=self.upload();self.import_all(run);raw=run.messages.get()
        candidate=self.candidate(raw,'approved')
        project=Project.objects.create(name='Альфа',team=self.team,contract_amount=1000,paid_amount=100,is_verified=True)
        candidate.project=project;candidate.save()
        original=FinancialRecord.objects.create(project=project,candidate=candidate,amount=100,currency='KZT',payment_date=raw.timestamp.date(),is_verified=True)
        with transaction.atomic():replace_unsent([raw.id],run.analysis_items.get().trace)
        project.refresh_from_db();candidate.refresh_from_db()
        self.assertEqual(project.paid_amount,0);self.assertEqual(candidate.status,'superseded')
        self.assertTrue(FinancialRecord.objects.filter(pk=original.pk).exists());self.assertEqual(FinancialRecord.objects.aggregate(total=Sum('amount'))['total'],0)
        with transaction.atomic():replace_unsent([raw.id],run.analysis_items.get().trace)
        self.assertEqual(FinancialRecord.objects.count(),2)
        self.assertTrue(AuditEvent.objects.filter(action='replay_superseded').exists())

    def test_manual_reversal_prevents_replacement(self):
        run=self.upload();self.import_all(run);raw=run.messages.get();candidate=self.candidate(raw,'approved')
        project=Project.objects.create(name='Альфа',team=self.team,paid_amount=70)
        candidate.project=project;candidate.save()
        payment=FinancialRecord.objects.create(project=project,candidate=candidate,amount=100,payment_date=raw.timestamp.date(),is_verified=True)
        FinancialRecord.objects.create(project=project,reverses=payment,amount=-30,payment_date=raw.timestamp.date(),is_verified=True)
        self.assertEqual(guard(raw),'projection_changed')
        with self.assertRaises(ProviderUnavailable),transaction.atomic():replace_unsent([raw.id],run.analysis_items.get().trace)
        self.assertEqual(FinancialRecord.objects.count(),2)

    def test_accepted_project_and_commitment_snapshots_restore_without_losing_manual_data(self):
        from .replay_replacement import row_snapshot
        run=self.upload();self.import_all(run);raw=run.messages.get();candidate=self.candidate(raw,'approved','project')
        project=Project.objects.create(name='Альфа',team=self.team,contract_amount=100,contract_known=True,is_verified=True)
        after=row_snapshot(project);before={**after,'contract_amount':'0.00','contract_known':False,'is_verified':False}
        candidate.project=project;candidate.materialization_snapshot={'project_before':before,'project_after':after};candidate.save()
        project.next_action='Ручная заметка';project.save()
        with transaction.atomic():replace_unsent([raw.id],run.analysis_items.get().trace)
        project.refresh_from_db();self.assertEqual(project.contract_amount,0);self.assertEqual(project.next_action,'Ручная заметка')
        task_candidate=FactCandidate.objects.create(trace=candidate.trace,team=self.team,status='approved',fact_type='commitment',source_key='task',project=project)
        task=Commitment.objects.create(candidate=task_candidate,team=self.team,project=project,source_message=raw,commitment_text='Сделать отчёт',is_verified=True)
        task_candidate.materialization_snapshot={'commitment_before':{},'commitment_after':row_snapshot(task)};task_candidate.save()
        with transaction.atomic():replace_unsent([raw.id],run.analysis_items.get().trace)
        task.refresh_from_db();self.assertEqual(task.status,'cancelled');self.assertFalse(task.is_verified)

    def test_repeated_full_analysis_and_corrected_amount_have_one_active_payment(self):
        from .autonomous import decide
        self.cfg.autonomous_enabled=True;self.cfg.save()
        project=Project.objects.create(name='Альфа',normalized_name='альфа',team=self.team,identity_confirmed=True)
        run=self.upload('01.09.2026, 12:00 - Оператор: Альфа: поступило 100000 тенге, итог после уточнения 90000 тенге\n');self.import_all(run);raw=run.messages.get()
        for amount in ('100000','100000','90000'):
            fact={'thread_key':'report','evidence_message_id':raw.id,'fact_type':'payment','object_name':'Альфа','evidence':raw.content,'amount':amount,'currency':'KZT','payment_date':'2026-09-01','payment_kind':'increment','confidence':0.95}
            self.execute(run.analysis_items.get().outbox_event,self.result(raw,[fact]))
            candidate=FactCandidate.objects.get(trace=run.analysis_items.get().trace)
            decision=decide(candidate.id);self.assertEqual(decision.outcome,'accepted',decision.reason_code)
            project.refresh_from_db();self.assertEqual(project.paid_amount,Decimal(amount))
            self.assertEqual(FactCandidate.objects.filter(status='approved',fact_type='payment').count(),1)
            self.assertEqual(FinancialRecord.objects.aggregate(value=Sum('amount'))['value'],Decimal(amount))
            control_run(run.id,'cancel',self.user);run=self.upload('01.09.2026, 12:00 - Оператор: Альфа: поступило 100000 тенге, итог после уточнения 90000 тенге\n');self.import_all(run)

    def test_pending_and_rejected_replaced_without_touching_neighbor(self):
        run=self.upload();self.import_all(run);raw=run.messages.get()
        old=self.candidate(raw)
        neighbor=RawMessage.objects.create(config=self.config,team=self.team,chat_id=self.config.group_jid,message_id='neighbor',timestamp=raw.timestamp,content='Соседний факт')
        other=self.candidate(neighbor)
        with transaction.atomic():replace_unsent([raw.id],run.analysis_items.get().trace)
        old.refresh_from_db();other.refresh_from_db()
        self.assertEqual(old.status,'superseded');self.assertEqual(other.status,'pending')


@skipUnlessDBFeature('has_select_for_update')
class ExportConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.settings=override_settings(MEDIA_ROOT=self.temp.name);self.settings.enable();self.addCleanup(self.settings.disable)
        self.user=User.objects.create_superuser('concurrent','concurrent@example.test','password')
        self.team=Team.objects.create(name='Concurrent exports')
        self.config=WhatsAppConfig.objects.create(team=self.team,group_jid='concurrent@g.us',snapshot={'timezone':'UTC'})
        self.job=WhatsAppHistoryJob.objects.create(config=self.config)
        gemma_config(is_active=True).save()

    def test_double_submission_and_duplicate_worker_claim_have_one_original(self):
        def submit(_):
            close_old_connections()
            try:return start_file(self.job.id,SimpleUploadedFile('chat.txt',b'01.09.2026, 12:00 - Test: text'),'UTC','DMY',self.user,str(uuid.uuid4())).id
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:ids=list(pool.map(submit,range(2)))
        self.assertEqual(ids[0],ids[1]);run=WhatsAppHistoryRun.objects.get(pk=ids[0])
        payload={'history_run_id':run.id,'step':run.step}
        def process(_):
            close_old_connections()
            try:process_file_step(payload)
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(process,range(2)))
        self.assertEqual(RawMessage.objects.count(),1);self.assertEqual(WhatsAppExportEntry.objects.count(),1)
        self.assertEqual(HistoryAnalysisItem.objects.count(),1)
        self.assertEqual(OutboxEvent.objects.filter(event_type='extract_message').count(),1)

    def test_delivery_finishing_during_model_response_preserves_accepted_results(self):
        run=start_file(self.job.id,SimpleUploadedFile('chat.txt',b'01.09.2026, 12:00 - Test: text'),'UTC','DMY',self.user,str(uuid.uuid4()))
        process_file_step({'history_run_id':run.id,'step':run.step})
        item=run.analysis_items.get();raw=item.raw_message
        old=MessageProcessingTrace.objects.create(raw_message=raw,attempt_no=2,status='warning')
        candidate=FactCandidate.objects.create(trace=old,team=self.team,status='approved',fact_type='payment',source_key='race')
        decision=FactDecision.objects.create(candidate=candidate,outcome='accepted',policy_version='test',input_fingerprint='test',reason_code='test',explanation='test')
        event=FactEvent.objects.create(decision=decision,team=self.team,event_key='race',event_type='payment')
        link=ExternalObjectLink.objects.create(team=self.team,integration_key='test',object_type='deal',local_type='Project',local_id=1,origin_key='race',external_id='123')
        outbox=OutboxEvent.objects.create(event_type='autonomous_crm',deduplication_key='race',state='pending')
        delivery=CrmDelivery.objects.create(outbox_event=outbox,external_object_link=link,fact_event=event,target_version=1,state='pending')
        def response(*args,**kwargs):
            def finish():
                close_old_connections()
                try:
                    with transaction.atomic():
                        owned=OutboxEvent.objects.select_for_update().get(pk=outbox.id)
                        owned.state='done';owned.save()
                        CrmDelivery.objects.filter(pk=delivery.id).update(state='delivered')
                finally:close_old_connections()
            with ThreadPoolExecutor(max_workers=1) as pool:pool.submit(finish).result(timeout=10)
            return ({'threads':[{'key':'report','topic':'Report','state':'ready','completion_reason':'Complete','messages':[{'raw_message_id':raw.id,'thought_state':'final'}]}],'facts':[]},{},{})
        with patch.object(AIService,'_credential',return_value='fixture-key'),patch('api.message_context.context_runtime',return_value=(gemma_counter(),endpoint())),patch.object(AIService,'analyze_payload',side_effect=response):run_outbox(item.outbox_event_id)
        candidate.refresh_from_db();event.refresh_from_db();item.refresh_from_db()
        self.assertEqual(candidate.status,'approved');self.assertFalse(event.is_superseded)
        self.assertEqual(item.disposition,'skipped_crm_delivered')
