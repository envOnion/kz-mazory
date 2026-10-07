"""Transport aliases preserve immutable originals across WhatsApp ingress paths."""
import hashlib
from datetime import timedelta
from django.db.models import Q
from .models import RawMessage,WhatsAppMessageAlias

WHATSAPP_SOURCES=('waha','whatsapp_export')


def whatsapp_scope(config,*,include_staged=False):
    query=RawMessage.objects.filter(config=config,team_id=config.team_id,session_name=config.session_name,chat_id=config.group_jid,source__in=WHATSAPP_SOURCES).exclude(processing_state__in=['deleted','superseded'])
    return query if include_staged else query.exclude(processing_state='export_staged')


def temporal_originals(config,stamp,precision,sender,phone):
    query=whatsapp_scope(config,include_staged=True)
    if precision=='minute':
        start=stamp.replace(second=0,microsecond=0)
        query=query.filter(timestamp__gte=start,timestamp__lt=start+timedelta(minutes=1))
    else:query=query.filter(timestamp=stamp)
    if phone:
        query=query.filter(Q(sender_phone__in=[phone,phone.lstrip('+')])|Q(sender_phone='',sender_name=sender))
    elif sender:query=query.filter(sender_name=sender)
    else:return query.none()
    return query.order_by('timestamp','id')


def matching_originals(config,stamp,precision,sender,phone,content):
    return temporal_originals(config,stamp,precision,sender,phone).filter(content=content)


def alias(config,namespace,external_id,revision,raw=None):
    key=dict(config=config,session_name=config.session_name,chat_id=config.group_jid,namespace=namespace,external_id=external_id,source_revision=revision)
    if raw is None:return WhatsAppMessageAlias.objects.filter(**key).select_related('raw_message').first()
    saved,_=WhatsAppMessageAlias.objects.get_or_create(**key,defaults={'raw_message':raw})
    if saved.raw_message_id!=raw.id:raise ValueError('whatsapp_alias_conflict')
    return saved


def ingest_waha_batch(config, items):
    """Keep history pages bounded in SQL when no TXT originals need resolving.

    The caller holds the source lock. Once a file original exists, individual
    resolution remains necessary to preserve cross-transport identities.
    """
    if RawMessage.objects.filter(config=config,source='whatsapp_export').exists():
        known={(row.external_id,row.source_revision):row.raw_message for row in
            WhatsAppMessageAlias.objects.filter(config=config,session_name=config.session_name,chat_id=config.group_jid,namespace='waha',external_id__in=[item['message_id'] for item in items]).select_related('raw_message')}
        return [(known[key],False) if (key:=(item['message_id'],hashlib.sha256(item['content'].encode()).hexdigest())) in known else ingest_waha(config,**item) for item in items]
    ids=[item['message_id'] for item in items]
    query=RawMessage.objects.filter(source='waha',session_name=config.session_name,message_id__in=ids)
    before={(row.message_id,row.source_revision):row for row in query}
    rows=[]
    identities=[]
    for item in items:
        content=item['content'];revision=hashlib.sha256(content.encode()).hexdigest()
        identities.append((item['message_id'],revision))
        rows.append(RawMessage(source='waha',session_name=config.session_name,
            message_id=item['message_id'],source_revision=revision,config=config,
            team_id=config.team_id,chat_id=config.group_jid,timestamp=item['timestamp'],
            sent_at_known=True,sender_phone=item.get('sender_phone',''),sender_name=item.get('sender_name',''),
            content=content,raw_payload=item.get('raw_payload',{}),processed=not bool(content.strip()),
            processing_state='received' if content.strip() else 'no_text'))
    RawMessage.objects.bulk_create(rows,ignore_conflicts=True,batch_size=250)
    stored={(row.message_id,row.source_revision):row for row in query.iterator()}
    originals=[stored[identity] for identity in identities]
    if any(raw.config_id!=config.id or raw.chat_id!=config.group_jid for raw in originals):
        raise ValueError('whatsapp_alias_conflict')
    WhatsAppMessageAlias.objects.bulk_create([
        WhatsAppMessageAlias(config=config,raw_message=raw,session_name=config.session_name,
            chat_id=config.group_jid,namespace='waha',external_id=raw.message_id,source_revision=raw.source_revision)
        for raw in originals],ignore_conflicts=True,batch_size=250)
    query.exclude(pk__in=[raw.pk for raw in originals]).update(processing_state='superseded',processed=True)
    seen=set(before)
    result=[]
    for identity,raw in zip(identities,originals,strict=True):
        result.append((raw,identity not in seen));seen.add(identity)
    return result


def ingest_waha(config,*,message_id,content,timestamp,sender_phone='',sender_name='',sent_at_known=True,raw_payload=None):
    """Caller holds config lock. Never merge uncertain repeated occurrences."""
    revision=hashlib.sha256(content.encode()).hexdigest()
    previous=alias(config,'waha',message_id,revision)
    if previous:return previous.raw_message,False
    exact=RawMessage.objects.filter(source='waha',session_name=config.session_name,message_id=message_id,source_revision=revision).first()
    if exact:
        if exact.config_id!=config.id:raise ValueError('whatsapp_alias_conflict')
        alias(config,'waha',message_id,revision,exact)
        return exact,False
    possible=[]
    if sent_at_known and not WhatsAppMessageAlias.objects.filter(config=config,namespace='waha',external_id=message_id).exists():
        start=timestamp.replace(second=0,microsecond=0)
        qs=whatsapp_scope(config,include_staged=True).filter(source='whatsapp_export',content=content,timestamp__gte=start,timestamp__lt=start+timedelta(minutes=1))
        if sender_phone:
            qs=qs.filter(Q(sender_phone__in=[sender_phone,sender_phone.lstrip('+')])|Q(sender_phone='',sender_name=sender_name))
        elif sender_name:qs=qs.filter(sender_name=sender_name)
        else:qs=qs.none()
        possible=list(qs[:3])
        if not possible:
            possible=list(temporal_originals(config,timestamp,'minute',sender_name,sender_phone).filter(source='whatsapp_export')[:3])
            if possible:
                # A changed export or another message in the same minute cannot
                # be proven equivalent from TXT alone. Hold it for resolution.
                qs=qs.none()
        if len(possible)==1 and possible[0].content==content and not possible[0].transport_aliases.filter(namespace='waha').exists():
            raw=possible[0];alias(config,'waha',message_id,revision,raw)
            activated=raw.processing_state=='export_staged'
            if activated:
                raw.processing_state='received';raw.save(update_fields=['processing_state'])
            return raw,activated
    raw,created=RawMessage.objects.get_or_create(source='waha',session_name=config.session_name,message_id=message_id,source_revision=revision,defaults={
        'config':config,'team_id':config.team_id,'chat_id':config.group_jid,'timestamp':timestamp,'sent_at_known':sent_at_known,
        'sender_phone':sender_phone,'sender_name':sender_name,'content':content,'raw_payload':{**(raw_payload or {}),**({'deduplication_ambiguous':True} if possible else {})},
        'processed':bool(possible) or not bool(content.strip()),'processing_state':'deduplication_ambiguous' if possible else 'received' if content.strip() else 'no_text'})
    alias(config,'waha',message_id,revision,raw)
    older=RawMessage.objects.filter(transport_aliases__config=config,transport_aliases__namespace='waha',transport_aliases__external_id=message_id).exclude(pk=raw.pk)
    previous_ids=list(older.values_list('id',flat=True).distinct())
    if previous_ids:
        raw.raw_payload={**raw.raw_payload,'previous_original_ids':previous_ids};raw.save(update_fields=['raw_payload'])
        older.update(processing_state='superseded',processed=True)
    return raw,created
