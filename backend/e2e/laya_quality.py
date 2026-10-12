"""Synthetic pre-existing topics for the independent routing E2E benchmark.

Topic generation itself is tested with a real LLM in laya-routing-local.spec.ts;
these fixtures isolate the held-out membership labels from LLM phrasing changes.
"""
import json
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from api.models import WhatsAppConfig,RawMessage,DialogueThread,ThreadMessage
from api.dialogue_threads import source_key
from api.thread_routing import update_descriptor,digest


@transaction.atomic
def seed():
    rows = json.loads((settings.E2E_DIR/'laya-quality-inputs.json').read_text())
    if len(rows)!=400:
        raise RuntimeError('Expected independent 400-dialogue set')
    team = WhatsAppConfig.objects.get(group_jid='laya-synthetic-routing@g.us').team
    outputs = []
    for index,row in enumerate(rows):
        group = f'laya-quality-{index}@g.us'
        config,_ = WhatsAppConfig.objects.get_or_create(team=team,session_name='default',group_jid=group,
            defaults={'name':f'Independent quality {index}','thread_routing_enabled':True,'snapshot':{'timezone':'Asia/Almaty'}})
        topics = []
        for q,question in row['questions'].items():
            description = question['criteria']['A']
            topic,_,summary = description.partition('. ')
            if not summary:
                raise RuntimeError('A prepared fixture needs a meaningful summary')
            raw,_ = RawMessage.objects.get_or_create(message_id=f'laya-quality-context-{index}-{q}',source='waha',
                defaults={'team':team,'config':config,'chat_id':group,'session_name':'default','timestamp':timezone.now()-timedelta(days=1),
                    'content':description,'sender_name':'Синтетический контекст','processed':True,'processing_state':'analyzed'})
            thread,_ = DialogueThread.objects.get_or_create(identity=digest(['quality-fixture',index,q]),defaults={
                'team':team,'config':config,'source_key':source_key(raw),'topic':topic,'summary':summary,'state':'ready'})
            ThreadMessage.objects.get_or_create(thread=thread,raw_message=raw,defaults={'thought_state':'final','relation':'discusses','rationale':'Синтетическое исходное состояние независимого контрольного диалога.'})
            update_descriptor(thread,{'topic':topic,'summary':summary,'state':'ready','messages':[{'raw_message_id':raw.id}]})
            topics.append(thread.id)
        outputs.append({**row,'index':index,'group':group,'thread_ids':topics})
    (settings.E2E_DIR/'laya-quality-ready.json').write_text(json.dumps(outputs,ensure_ascii=False))
