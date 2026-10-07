import copy
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone
from .models import AISettings, HistoryAnalysisItem, OutboxEvent, ProviderUsage, RawMessage, Team, WhatsAppConfig, WhatsAppHistoryJob, WhatsAppHistoryRun
from .history_analysis import POLICY, schedule, split, counts
from .history_jobs import control_run
from .tests_gemma_context import config as gemma_config, endpoint
from .context_tokens import gemma_counter
from .providers import ProviderUnavailable
from .tasks import dispatch_outbox, run_outbox
from .ai_retries import handle_failure
from .history_packets import prepare_segments, merge_segments
from .ai_service import AIService


class HistoryPacketsTests(TestCase):
    def setUp(self):
        self.cfg = gemma_config(is_active=True)
        self.cfg.save()
        credential = patch.object(AIService,"_credential",return_value="test-only-key")
        credential.start();self.addCleanup(credential.stop)
        self.source = WhatsAppConfig.objects.create(team=Team.objects.create(name="History packets"), group_jid="packets@g.us")
        self.job = WhatsAppHistoryJob.objects.create(config=self.source)
        self.run = WhatsAppHistoryRun.objects.create(job=self.job, state="analyzing", settings_snapshot={"analysis_policy":POLICY,"analysis_mode":"reprocess_all","only_new":False,"analyze_after_import":True})

    def message(self, content="Привет", source=None):
        source = source or self.source
        return RawMessage.objects.create(config=source, team=source.team, chat_id=source.group_jid, message_id=f"m:{RawMessage.objects.count()}", timestamp=timezone.now(), content=content)

    def packet(self, number=2):
        self.run.messages.add(*[self.message(f"Здравствуйте {i}") for i in range(number)])
        schedule(self.run)
        return OutboxEvent.objects.filter(payload__history_run_id=self.run.pk).order_by("id").first()

    def model_result(self, event):
        return {"threads":[{"key":"info","topic":"Информационные сообщения","state":"ready","completion_reason":"Нет коммерческого действия или суммы.","messages":[{"raw_message_id":pk,"thought_state":"final","relation":"discusses"} for pk in event.payload["batch_ids"]]}],"facts":[]}

    def test_packet_coverage_is_unique_and_no_text_is_separate(self):
        self.packet(10)
        self.run.messages.add(self.message(""))
        schedule(self.run)
        self.assertEqual(self.run.analysis_items.count(),11)
        self.assertEqual(OutboxEvent.objects.filter(payload__history_run_id=self.run.pk).count(),2)
        value=counts(self.run)
        self.assertEqual((value['no_text'],value['processed'],value['pending']),(1,0,10))

    def test_terminal_summary_accounts_for_non_text_and_errors(self):
        from .history_analysis import summary
        event=self.packet(2)
        self.run.messages.add(self.message(""));schedule(self.run)
        self.run.analysis_items.filter(outbox_event=event).update(state="failed",reason_code="thread_sources_unavailable")
        value=counts(self.run)
        self.assertEqual(value['processed']+value['errors']+value['no_text']+value['pending']+value['cancelled'],value['total'])
        self.assertIn('Учтено: 3 / 3',summary(value))
        self.assertIn('Без текста: 1',summary(value))

    def test_wire_schema_binds_only_visible_complete_originals(self):
        from .extraction_schema import extraction_schema
        shape=extraction_schema({'target_message_id':42,'context':[{'raw_message_id':11},{'raw_message_id':12,'partial':True}],
                                 'target_messages':[{'raw_message_id':43}],
                                 'known_threads':[{'id':7,'message_ids':[999], 'commitments':[{'id':19}]}]})
        theme=shape['properties']['threads']['items']['properties']
        self.assertEqual(theme['messages']['items']['properties']['raw_message_id']['enum'],[11,42,43])
        self.assertEqual(theme['thread_id']['anyOf'][0]['enum'],[7])
        commitment=next(v for v in shape['properties']['facts']['items']['oneOf'] if v['properties']['fact_type']['enum']==['commitment'])
        self.assertEqual(commitment['properties']['promise_message_id']['enum'],[11,42,43])
        self.assertEqual(commitment['properties']['commitment_id']['enum'],[19])
        empty=extraction_schema({'target_message_id':42})
        self.assertEqual(empty['properties']['threads']['items']['properties']['thread_id'],{'type':'null'})
        commitment=empty['properties']['facts']['items']['oneOf'][-1]
        self.assertNotIn('commitment_id',commitment['properties'])

    def test_missing_anchor_has_distinct_numeric_diagnostics(self):
        from .dialogue_threads import prepare_themes
        event=self.packet(2)
        item=self.run.analysis_items.get(raw_message_id=event.payload['raw_id'])
        other=next(pk for pk in event.payload['batch_ids'] if pk!=item.raw_message_id)
        item.trace.earlier_messages_context=[{'raw_message_id':other}]
        item.trace.context_metadata['snapshot_max_id']=other
        result=self.model_result(event);result['threads'][0]['messages']=[{'raw_message_id':other,'thought_state':'final'}]
        with self.assertRaises(ProviderUnavailable) as exc:
            prepare_themes(result,item.raw_message,item.trace)
        self.assertEqual(str(exc.exception),'batch_classification_missing')
        self.assertEqual(exc.exception.diagnostics['missing_target_count'],1)
        self.assertEqual(exc.exception.diagnostics['unoffered_reference_count'],0)

    def test_thread_conflict_keeps_fresh_attempt_owned_by_history_run(self):
        event=self.packet(1)
        with patch('api.message_context.context_runtime',return_value=(gemma_counter(),endpoint())),patch.object(AIService,'analyze_payload',return_value=(self.model_result(event),{},{})),patch('api.dialogue_threads.persist_themes',side_effect=ProviderUnavailable('thread_revision_conflict')):
            run_outbox(event.id)
        event.refresh_from_db()
        self.assertEqual(event.state,'pending')
        self.assertTrue(event.payload['schema_repaired'])
        self.assertEqual(event.payload['history_run_id'],self.run.id)
        self.assertEqual(self.run.analysis_items.get().trace_id,event.payload['trace_id'])
        self.assertFalse(OutboxEvent.objects.filter(deduplication_key__startswith='thread-conflict:').exists())

    def test_dispatch_does_not_enqueue_blocked_source_or_starve_other_source(self):
        self.packet(10)
        other_source=WhatsAppConfig.objects.create(team=self.source.team,group_jid="other@g.us")
        other=self.message("Оплата",other_source)
        OutboxEvent.objects.create(event_type="extract_message",deduplication_key="other",payload={"raw_id":other.id})
        with patch("api.tasks.async_task") as send:
            self.assertEqual(dispatch_outbox(),2)
            self.assertEqual(dispatch_outbox(),0)
        self.assertEqual(send.call_count,2)
        self.assertEqual(OutboxEvent.objects.filter(payload__history_run_id=self.run.pk,state="pending").count(),1)

    def test_waiting_retry_does_not_block_ready_packet(self):
        first=self.packet(10)
        first.next_attempt_at=timezone.now()+timedelta(minutes=8);first.attempt_count=1;first.save()
        with patch("api.tasks.async_task") as send:
            self.assertEqual(dispatch_outbox(),1)
        self.assertNotEqual(send.call_args.args[1],first.id)

    def test_split_children_finish_earlier_position_before_later_packets(self):
        first=self.packet(10)
        self.assertTrue(split(first))
        child=OutboxEvent.objects.filter(deduplication_key=first.deduplication_key+':split:0').get()
        with patch('api.tasks.async_task') as send:
            self.assertEqual(dispatch_outbox(),1)
        self.assertEqual(send.call_args.args[1],child.id)

    def test_dispatch_order_follows_source_dates_when_import_ids_are_reversed(self):
        self.run.settings_snapshot['analysis_target_message_limit']=2;self.run.save()
        raws=[self.message(f'Сообщение {i}') for i in range(3)]
        for i,raw in enumerate(raws):
            raw.timestamp=timezone.now()-timedelta(days=i);raw.save()
        self.run.messages.add(*raws);schedule(self.run)
        with patch('api.tasks.async_task') as send:
            self.assertEqual(dispatch_outbox(),1)
        event=OutboxEvent.objects.get(pk=send.call_args.args[1])
        self.assertEqual(event.payload['batch_ids'],[raws[2].id,raws[1].id])

    def test_packet_marks_each_target_only_after_validated_classification(self):
        event=self.packet()
        with patch("api.message_context.context_runtime",return_value=(gemma_counter(),endpoint())),patch.object(AIService,"analyze_payload",return_value=(self.model_result(event),{},{})):
            run_outbox(event.id)
        event.refresh_from_db()
        self.assertEqual(event.state,"done")
        self.assertEqual(counts(self.run)['processed'],2)
        self.assertEqual(counts(self.run)['no_facts'],2)
        for item in self.run.analysis_items.all():
            self.assertEqual(item.trace.raw_message_id,item.raw_message_id)
            self.assertEqual(item.state,"succeeded")
            self.assertTrue(item.reason_description)
            self.assertLessEqual(item.trace.context_metadata['input_tokens_preflight'],16384)

    def test_missing_target_is_not_a_success_and_both_halves_are_preserved(self):
        event=self.packet(4)
        result=self.model_result(event);result['threads'][0]['messages']=result['threads'][0]['messages'][:1]
        with patch("api.message_context.context_runtime",return_value=(gemma_counter(),endpoint())),patch.object(AIService,"analyze_payload",return_value=(result,{},{})):
            run_outbox(event.id)
        event.refresh_from_db()
        self.assertEqual(event.state,"failed")
        self.assertEqual(counts(self.run)['processed'],0)
        children=list(OutboxEvent.objects.exclude(pk=event.pk).filter(event_type="extract_message"))
        self.assertEqual(len(children),2)
        self.assertEqual(set(sum([child.payload['batch_ids'] for child in children],[])),set(event.payload['batch_ids']))
        self.assertEqual(self.run.analysis_items.exclude(state="queued").count(),0)

    def test_unproven_ready_packet_is_split_without_accepting_results(self):
        event=self.packet(4)
        result=self.model_result(event)
        result['threads'][0]['completion_reason']=''
        with patch("api.message_context.context_runtime",return_value=(gemma_counter(),endpoint())),patch.object(AIService,"analyze_payload",return_value=(result,{},{})):
            run_outbox(event.id)
        event.refresh_from_db()
        self.assertEqual(event.error_code,'thread_completion_unproven')
        self.assertEqual(counts(self.run)['processed'],0)
        self.assertEqual(self.run.analysis_items.filter(state='queued').count(),4)
        self.assertEqual(OutboxEvent.objects.filter(event_type='extract_message',state='pending').count(),2)

    def test_terminal_error_is_visible_without_a_waiting_retry(self):
        event=self.packet(1)
        event.state='failed';event.error_code='thread_completion_unproven';event.save()
        from .history_analysis import synchronize
        synchronize(event)
        value=counts(self.run)
        self.assertEqual(value['errors'],1)
        self.assertEqual(value['last_error'],'thread_completion_unproven')
        self.assertIsNone(value['next_attempt_at'])

    def test_new_wire_schema_and_prompt_require_classification_explanations(self):
        from .context_tokens import extraction_payload
        from .ollama_chat import native_request
        cfg=gemma_config();cfg.analysis_policy=POLICY
        value=extraction_payload(cfg,endpoint(),'Привет','Автор',[],[],None,'UTC',target_message_id=3,batch_message_ids=[3,4])
        import json
        body=json.loads(value['messages'][-1]['content'])
        self.assertIn('КАЖДЫЙ',body['analysis_instructions'])
        self.assertGreater(value['messages'][-1]['content'].index('"batch_message_ids"'),value['messages'][-1]['content'].index('"context"'))
        theme=native_request(value,cfg)['format']['properties']['threads']['items']
        self.assertIn('completion_reason',theme['required'])
        self.assertEqual(theme['properties']['messages']['minItems'],1)

    def test_payment_wire_schema_cannot_omit_known_amount(self):
        from .extraction_schema import extraction_schema
        variants=extraction_schema()['properties']['facts']['items']['oneOf']
        payment=[v for v in variants if v['properties']['fact_type']['enum']==['payment']]
        self.assertEqual(len(payment),2)
        known=next(v for v in payment if 'exact' in v['properties']['amount_precision']['enum'])
        unknown=next(v for v in payment if v['properties']['amount_precision']['enum']==['unknown'])
        self.assertIn('amount',known['required'])
        self.assertEqual(known['properties']['amount']['type'],'number')
        self.assertNotIn('amount',unknown['required'])
        promise=next(v for v in variants if v['properties']['fact_type']['enum']==['commitment'])
        self.assertTrue({'commitment_text','promise_message_id','evidence_messages'}.issubset(promise['required']))

    def test_large_output_splits_a_single_report_before_repeating_whole_text(self):
        event=self.packet(1)
        raw=RawMessage.objects.get(pk=event.payload['raw_id'])
        raw.content='Объект Альфа: поступило 100000 тенге.\n'*300;raw.save()
        event.state='processing';event.attempt_count=1;event.save()
        handle_failure(event,ProviderUnavailable('provider_output_truncated'))
        event.refresh_from_db()
        ranges=event.payload['segment_ranges']
        self.assertGreater(len(ranges),1)
        self.assertEqual(ranges[0][0],0)
        self.assertEqual(ranges[-1][1],len(raw.content))
        self.assertEqual(self.run.analysis_items.get().state,'queued')

    def test_oversized_optional_report_does_not_displace_complete_sources(self):
        from .message_context import build_context
        target=self.message('Привет')
        report=self.message('Длинный отчёт по всем объектам. '*10000)
        reply=self.message('Добрый день')
        cfg=gemma_config();cfg.analysis_policy=POLICY
        with patch('api.message_context.context_runtime',return_value=(gemma_counter(),endpoint())):
            context,metadata,value=build_context(target,cfg,[],reply.id,include_following=True,batch_ids=[target.id])
        self.assertNotIn(report.id,[item['raw_message_id'] for item in context])
        self.assertIn(reply.id,[item['raw_message_id'] for item in context])
        self.assertFalse(any(item['partial'] for item in context))
        self.assertLessEqual(metadata['input_tokens_preflight'],cfg.analysis_input_token_limit)

    def test_targets_are_explicit_and_saved_request_restores_without_duplicates(self):
        from .pipeline import _restore_request
        event=self.packet(3)
        with patch('api.message_context.context_runtime',return_value=(gemma_counter(),endpoint())),patch.object(AIService,'analyze_payload',return_value=(self.model_result(event),{},{})):
            run_outbox(event.id)
        trace=self.run.analysis_items.order_by('raw_message_id').first().trace
        value=_restore_request(trace)
        import json
        body=json.loads(value['messages'][-1]['content'])
        ids={row['raw_message_id'] for row in body['target_messages']}
        self.assertEqual(ids,set(event.payload['batch_ids'])-{body['target_message_id']})
        self.assertFalse(ids & {row['raw_message_id'] for row in body['context']})
        self.assertEqual(gemma_counter().count_payload(value),trace.context_metadata['input_tokens_preflight'])

    def test_deployment_probe_rejects_semantically_invalid_schema_response(self):
        from .tasks import verify_history_packets
        def response(value, **kwargs):
            result={'threads':[{'key':'info','topic':'Проверка','state':'ready','messages':[{'raw_message_id':1,'thought_state':'intermediate'}]}],'facts':[]}
            return result,{'prompt_tokens':gemma_counter().count_payload(value)},{}
        with patch('api.context_tokens.context_runtime',return_value=(gemma_counter(),endpoint())),patch.object(AIService,'analyze_payload',side_effect=response):
            with self.assertRaisesMessage(ProviderUnavailable,'analysis_probe_classification_invalid'):
                verify_history_packets()

    def test_payment_is_attributed_to_its_own_target_trace(self):
        first=self.message('Привет')
        paid=self.message('Объект Сапфир: поступило 100000 тенге.')
        self.run.messages.add(first,paid);schedule(self.run)
        event=OutboxEvent.objects.filter(payload__history_run_id=self.run.pk).get()
        result=self.model_result(event)
        result['facts']=[{'fact_type':'payment','thread_key':'info','evidence_message_id':paid.id,
                         'object_name':'Сапфир','amount':'100000','currency':'KZT','payment_kind':'increment',
                         'evidence':'поступило 100000 тенге','confidence':0.99}]
        with patch('api.message_context.context_runtime',return_value=(gemma_counter(),endpoint())),patch.object(AIService,'analyze_payload',return_value=(result,{},{})),patch('api.tasks.enqueue_crm_match'),patch('api.notifications.notify_on_new_candidate'):
            run_outbox(event.id)
        from .models import FactCandidate
        candidate=FactCandidate.objects.get()
        self.assertEqual(candidate.trace.raw_message_id,paid.id)
        self.assertEqual(self.run.analysis_items.get(raw_message=first).disposition,'no_facts')
        self.assertEqual(self.run.analysis_items.get(raw_message=paid).disposition,'facts')

    def test_single_schema_error_gets_one_new_corrected_snapshot_then_stops(self):
        event=self.packet(1)
        old=event.payload['trace_id']
        handle_failure(event,ProviderUnavailable("extraction_facts_missing"))
        event.refresh_from_db()
        self.assertNotEqual(event.payload['trace_id'],old)
        self.assertTrue(event.payload['schema_repaired'])
        handle_failure(event,ProviderUnavailable("extraction_facts_missing"))
        event.refresh_from_db()
        self.assertEqual(event.state,"failed")

    def test_cancel_does_not_cancel_another_source_or_drop_original(self):
        event=self.packet()
        other=OutboxEvent.objects.create(event_type="index_message",deduplication_key="other",payload={})
        control_run(self.run.id,"cancel",None)
        self.assertEqual(self.run.analysis_items.filter(state="cancelled").count(),2)
        other.refresh_from_db();self.assertEqual(other.state,"pending")
        self.assertEqual(self.run.messages.count(),2)

    def test_report_segmentation_covers_final_line_with_overlap_without_new_raws(self):
        raw=self.message(("Объект Сапфир. Поступило 100000 тенге.\n"*1500)+"Последняя строка: поступило 200000 тенге.")
        self.run.messages.add(raw);schedule(self.run)
        event=OutboxEvent.objects.filter(event_type="extract_message").get()
        self.assertTrue(prepare_segments(event))
        event.refresh_from_db();ranges=event.payload['segment_ranges']
        self.assertEqual(ranges[0][0],0);self.assertEqual(ranges[-1][1],len(raw.content))
        self.assertTrue(all(left[1]>=right[0] for left,right in zip(ranges,ranges[1:])))
        self.assertEqual(RawMessage.objects.count(),1)
        self.assertEqual(self.run.analysis_items.get().state,"queued")

    def test_metadata_timeout_has_short_retry_and_three_attempt_limit(self):
        event=self.packet(1);event.attempt_count=1;event.save()
        handle_failure(event,ProviderUnavailable('provider_timeout'))
        event.refresh_from_db()
        self.assertLess((event.next_attempt_at-timezone.now()).total_seconds(),21)
        event.attempt_count=3;event.save()
        handle_failure(event,ProviderUnavailable('provider_timeout'))
        event.refresh_from_db();self.assertEqual(event.state,'failed')

    def test_stale_claim_cannot_change_new_attempt(self):
        event=self.packet(1)
        event.payload['claim_generation']='old';event.save()
        OutboxEvent.objects.filter(pk=event.pk).update(payload={**event.payload,'claim_generation':'new'})
        handle_failure(event,ProviderUnavailable('provider_timeout'))
        event.refresh_from_db()
        self.assertEqual(event.payload['claim_generation'],'new')
        self.assertEqual(event.state,'pending')

    def test_saved_reanalysis_does_not_fetch_waha_or_inherit_old_success(self):
        self.packet()
        self.run.source_snapshot={"config_id":self.source.id,"team_id":self.source.team_id,"session_name":"default","chat_id":self.source.group_jid}
        self.run.save()
        from .history_jobs import reanalyze_saved
        new=reanalyze_saved(self.run.id,None)
        self.assertEqual(new.state,'analyzing')
        self.assertEqual(new.messages.count(),2)
        self.assertEqual(counts(new)['processed'],0)
        self.run.refresh_from_db();self.assertEqual(self.run.state,'cancelled')

    def test_stale_response_cannot_publish_or_mark_new_claim_done(self):
        event=self.packet()
        def answer(*args,**kwargs):
            current=OutboxEvent.objects.get(pk=event.pk)
            current.payload={**current.payload,'claim_generation':'replacement'}
            current.save(update_fields=['payload'])
            return self.model_result(event),{},{}
        with patch("api.message_context.context_runtime",return_value=(gemma_counter(),endpoint())),patch.object(AIService,"analyze_payload",side_effect=answer):
            run_outbox(event.id)
        self.assertEqual(counts(self.run)['processed'],0)
        event.refresh_from_db();self.assertEqual(event.payload['claim_generation'],'replacement')


class PostgresClaimTests(TransactionTestCase):
    @skipUnlessDBFeature('has_select_for_update')
    def test_two_dispatchers_keep_global_and_source_limits(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections
        cfg=gemma_config(is_active=True,autonomous_max_in_flight=8);cfg.save()
        team=Team.objects.create(name='Concurrency')
        for index in range(3):
            source=WhatsAppConfig.objects.create(team=team,group_jid=f'{index}@g.us')
            for position in range(2):
                raw=RawMessage.objects.create(team=team,config=source,chat_id=source.group_jid,message_id=f'{index}:{position}',timestamp=timezone.now(),content='Привет')
                from .dialogue_threads import source_key
                OutboxEvent.objects.create(event_type='extract_message',deduplication_key=f'{index}:{position}',payload={'raw_id':raw.id},analysis_source_key=source_key(raw),analysis_position=raw.id)
        gate=Barrier(2)
        def dispatch():
            close_old_connections();gate.wait()
            try:
                return dispatch_outbox()
            finally:
                close_old_connections()
        with patch('api.tasks.async_task') as send,ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:dispatch(),range(2)))
        self.assertEqual(sum(results),2)
        self.assertEqual(send.call_count,2)
        enqueued=OutboxEvent.objects.filter(state='enqueued')
        self.assertEqual(enqueued.values('analysis_source_key').distinct().count(),2)
