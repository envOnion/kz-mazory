"""Legacy operator URLs resolve into the single messenger shell."""
from django.contrib.admin.views.decorators import staff_member_required
from django.http import Http404
from django.shortcuts import redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from . import access
from .messenger_api import records_scope, record_href

# Explicit migration map; registering a new Django model never adds a screen.
SCREENS = {
    'project': '/analytics?view=projects', 'company': '/analytics?view=projects',
    'commitment': '/analytics?view=tasks', 'factcandidate': '/analytics?view=review',
    'financialrecord': '/analytics?view=finance', 'paymentscheduleitem': '/analytics?view=finance',
    'paymentallocation': '/analytics?view=finance', 'salestarget': '/analytics?view=finance',
    'notification': '/analytics?view=notifications', 'notificationdelivery': '/queue?queue=delivery',
    'dialoguethread': '/threads', 'threadrevision': '/threads',
    'whatsappconfig': '/settings?section=whatsapp', 'whatsapphistoryjob': '/settings?section=whatsapp',
    'whatsapphistoryrun': '/settings?section=whatsapp', 'whatsappexportupload': '/settings?section=whatsapp',
    'aisettings': '/settings?section=ai', 'bitrixsettings': '/settings?section=bitrix',
    'team': '/settings?section=access', 'teammembership': '/settings?section=access',
    'chataccess': '/settings?section=access', 'clientprojectaccess': '/settings?section=access',
    'participant': '/settings?section=people', 'participantidentity': '/settings?section=people',
    'auditevent': '/settings?section=system', 'providerreservation': '/settings?section=system&resource=reservations',
    'providerusage': '/settings?section=system&resource=usage', 'temporalentityrevision': '/settings?section=system&resource=versions',
    'asyncoperation': '/queue', 'outboxevent': '/queue', 'sourceworkitem': '/queue',
    'crmdelivery': '/crm-changes', 'bitrixdealchangelog': '/crm-changes',
    'privateattachment': '/analytics?view=documents', 'userprofile': '/profile', 'mcptoken': '/profile',
}


@never_cache
@staff_member_required
@require_GET
def operator_entry(request, path=''):
    if not path:
        return redirect('/messenger')
    if path.rstrip('/') == 'waha-dashboard':
        if not access.integration_allowed(request.user):
            raise Http404
        return redirect('/settings?section=whatsapp')
    parts = path.strip('/').split('/')
    if len(parts) < 2 or parts[0] != 'api':
        raise Http404
    model = parts[1]
    if model in ('rawmessage', 'messageprocessingtrace', 'communicationrecord'):
        qs = records_scope(request.user)
        if len(parts) >= 3 and parts[2].isdecimal():
            ident = int(parts[2])
            if model == 'messageprocessingtrace':
                from .models import MessageProcessingTrace
                raw_id = MessageProcessingTrace.objects.filter(pk=ident).values_list('raw_message_id',flat=True).first()
                qs = qs.filter(raw_message_id=raw_id) if raw_id else qs.none()
            else:
                qs = qs.filter(**{'raw_message_id' if model == 'rawmessage' else 'id':ident})
            record = qs.first()
            if not record:
                raise Http404
            return redirect(record_href(record, **({'tab':'trace'} if model == 'messageprocessingtrace' else {})))
        return redirect('/messenger')
    target = SCREENS.get(model)
    if not target:
        raise Http404
    if model == 'bitrixdealchangelog' and len(parts) >= 3 and parts[2].isdecimal():
        target += '?receipt=' + parts[2]
    if model == 'dialoguethread' and len(parts) >= 3 and parts[2].isdecimal():
        target += '?thread=' + parts[2]
    return redirect(target)
