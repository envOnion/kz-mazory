"""Frozen SQL for migration 0050. Do not import the live analytical catalog."""

TIMESTAMPS = """
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE OR REPLACE FUNCTION public.mazory_record_times() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    IF current_setting('mazory.temporal_backfill', true) = 'on' THEN RETURN NEW; END IF;
    IF TG_OP = 'INSERT' THEN
        NEW.created_at := COALESCE(NEW.created_at, clock_timestamp());
        NEW.updated_at := COALESCE(NEW.updated_at, NEW.created_at);
    ELSE
        NEW.created_at := OLD.created_at;
        IF (to_jsonb(NEW) - 'created_at' - 'updated_at') IS DISTINCT FROM
           (to_jsonb(OLD) - 'created_at' - 'updated_at') THEN
            NEW.updated_at := clock_timestamp();
        ELSE NEW.updated_at := OLD.updated_at; END IF;
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.mazory_record_times() FROM PUBLIC;
"""

# Only business attributes are captured, never configuration, tokens or raw text.
HISTORY_FIELDS = {
    'project': ['team_id', 'manager_id', 'company_id', 'status', 'archived', 'identity_confirmed', 'is_verified', 'contract_known', 'contract_amount', 'cost_amount', 'cost_confirmed', 'currency', 'source_created_at', 'source_updated_at', 'source_time_precision', 'whatsapp_fields'],
    'crmprojectsnapshot': ['project_id', 'external_stage_id', 'external_stage_name', 'external_manager_id', 'external_manager_name', 'opportunity', 'currency', 'source_created_at', 'source_updated_at', 'source_stage_changed_at', 'source_begin_date', 'source_close_date', 'source_closed'],
    'commitment': ['team_id', 'project_id', 'manager_id', 'source_message_id', 'status', 'is_verified', 'promised_at', 'deadline', 'deadline_at', 'original_deadline_at', 'fulfilled_at', 'deadline_precision'],
    'financialrecord': ['project_id', 'credited_profile_id', 'amount', 'currency', 'payment_date', 'status', 'direction', 'is_verified', 'amount_precision', 'reverses_id'],
    'paymentscheduleitem': ['project_id', 'due_date', 'amount', 'currency', 'state', 'is_verified'],
    'paymentallocation': ['financial_record_id', 'schedule_item_id', 'amount'],
    'salestarget': ['team_id', 'profile_id', 'month', 'amount', 'currency', 'is_active', 'approved_at', 'version'],
    'company': ['name', 'bitrix_company_id', 'client_type'],
}

HISTORY = """
CREATE OR REPLACE FUNCTION public.mazory_business_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE
    row_data jsonb; previous jsonb := '{}'::jsonb; snapshot jsonb; previous_snapshot jsonb;
    changes jsonb; project_key bigint; team_key bigint; entity_key bigint;
    sequence_no integer; happened timestamptz; precision_name text := 'unknown';
    kind text; field_name text; fields text[] := string_to_array(TG_ARGV[1], ',');
BEGIN
    IF current_setting('mazory.temporal_backfill', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;
    row_data := CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
    IF TG_OP <> 'INSERT' THEN previous := to_jsonb(OLD); END IF;
    SELECT COALESCE(jsonb_object_agg(key, value), '{}'::jsonb) INTO snapshot
        FROM jsonb_each(row_data) WHERE key = ANY(fields);
    SELECT COALESCE(jsonb_object_agg(key, value), '{}'::jsonb) INTO previous_snapshot
        FROM jsonb_each(previous) WHERE key = ANY(fields);
    IF TG_OP = 'UPDATE' AND snapshot = previous_snapshot THEN RETURN NEW; END IF;
    entity_key := (row_data->>'id')::bigint;
    PERFORM pg_advisory_xact_lock(hashtextextended(TG_ARGV[0] || ':' || entity_key, 0));
    SELECT COALESCE(MAX(revision), 0) + 1 INTO sequence_no FROM public.api_temporalentityrevision
        WHERE entity_type = TG_ARGV[0] AND entity_id = entity_key;
    project_key := CASE WHEN TG_ARGV[0] = 'project' THEN entity_key ELSE (row_data->>'project_id')::bigint END;
    IF TG_ARGV[0] = 'paymentallocation' THEN
        SELECT project_id INTO project_key FROM public.api_financialrecord WHERE id = (row_data->>'financial_record_id')::bigint;
    END IF;
    team_key := (row_data->>'team_id')::bigint;
    IF team_key IS NULL AND project_key IS NOT NULL THEN SELECT team_id INTO team_key FROM public.api_project WHERE id = project_key; END IF;
    kind := CASE TG_OP WHEN 'INSERT' THEN 'created' WHEN 'DELETE' THEN 'deleted' ELSE 'changed' END;
    IF TG_OP = 'INSERT' AND (TG_ARGV[0] = 'crmprojectsnapshot' OR (TG_ARGV[0] = 'project' AND row_data->>'source' = 'bitrix_crm')) THEN kind := 'baseline'; END IF;
    happened := NULLIF(row_data->>CASE WHEN TG_OP = 'INSERT' THEN 'source_created_at' ELSE 'source_updated_at' END, '')::timestamptz;
    IF kind = 'baseline' THEN happened := NULL; precision_name := 'observed'; END IF;
    IF TG_OP = 'UPDATE' AND previous->>'source_updated_at' IS NOT DISTINCT FROM row_data->>'source_updated_at' THEN happened := NULL; END IF;
    IF TG_ARGV[0] = 'commitment' THEN
        happened := CASE WHEN TG_OP = 'INSERT' THEN (row_data->>'promised_at')::timestamptz
            WHEN previous->>'status' IS DISTINCT FROM row_data->>'status' AND row_data->>'status' = 'fulfilled' THEN (row_data->>'fulfilled_at')::timestamptz ELSE NULL END;
        IF TG_OP = 'UPDATE' AND previous->>'status' IS DISTINCT FROM row_data->>'status' THEN kind := row_data->>'status';
        ELSIF TG_OP = 'UPDATE' AND previous->>'deadline_at' IS DISTINCT FROM row_data->>'deadline_at' THEN kind := 'postponed'; END IF;
    ELSIF TG_ARGV[0] = 'financialrecord' AND TG_OP = 'INSERT' THEN
        happened := (row_data->>'payment_date')::date::timestamp AT TIME ZONE 'Asia/Almaty';
        IF happened IS NOT NULL THEN precision_name := 'date'; END IF;
        IF row_data->>'reverses_id' IS NOT NULL THEN kind := 'correction'; END IF;
    ELSIF TG_ARGV[0] = 'crmprojectsnapshot' AND TG_OP = 'UPDATE' AND
        previous->>'external_stage_id' IS DISTINCT FROM row_data->>'external_stage_id' THEN
        kind := 'stage_changed'; happened := CASE WHEN previous->>'source_stage_changed_at' IS DISTINCT FROM row_data->>'source_stage_changed_at' THEN (row_data->>'source_stage_changed_at')::timestamptz END;
    ELSIF TG_ARGV[0] = 'project' AND TG_OP = 'UPDATE' AND previous->>'archived' IS DISTINCT FROM row_data->>'archived' THEN
        kind := CASE WHEN (row_data->>'archived')::boolean THEN 'archived' ELSE 'restored' END; happened := NULL;
    ELSIF TG_ARGV[0] = 'project' AND TG_OP = 'UPDATE' AND previous->>'status' IS DISTINCT FROM row_data->>'status' THEN
        kind := 'stage_changed'; happened := NULL;
    END IF;
    IF happened IS NULL AND TG_ARGV[0] <> 'crmprojectsnapshot' AND
       kind NOT IN ('baseline','archived','restored','deleted') THEN
        SELECT occurred_at INTO happened FROM public.api_factevent
            WHERE id=NULLIF(current_setting('mazory.fact_event_id',true),'')::bigint;
        IF happened IS NULL THEN
            SELECT timestamp INTO happened FROM public.api_rawmessage
                WHERE id=NULLIF(current_setting('mazory.raw_message_id',true),'')::bigint AND sent_at_known;
        END IF;
    END IF;
    IF TG_OP='UPDATE' AND TG_ARGV[0] IN ('project','crmprojectsnapshot') AND
       (snapshot - ARRAY['source_created_at','source_updated_at','source_time_precision',
                         'source_stage_changed_at','source_begin_date','source_close_date']) =
       (previous_snapshot - ARRAY['source_created_at','source_updated_at','source_time_precision',
                                  'source_stage_changed_at','source_begin_date','source_close_date']) THEN
        -- Recovering missing source metadata is an observation, not a business change.
        kind := 'baseline'; happened := NULL; precision_name := 'observed';
    END IF;
    IF happened IS NOT NULL AND precision_name = 'unknown' THEN precision_name := 'exact'; END IF;
    changes := '{}'::jsonb;
    FOREACH field_name IN ARRAY fields LOOP
        IF snapshot->field_name IS DISTINCT FROM previous_snapshot->field_name THEN
            changes := changes || jsonb_build_object(field_name, jsonb_build_object('before', previous_snapshot->field_name, 'after', snapshot->field_name));
        END IF;
    END LOOP;
    INSERT INTO public.api_temporalentityrevision
        (entity_type, entity_id, team_id, project_id, actor_id, raw_message_id, fact_event_id,
         revision, effective_at, recorded_at, event_kind, time_precision, source_key, changes, snapshot, created_at, updated_at)
    VALUES (TG_ARGV[0], entity_key, team_key, project_key,
        NULLIF(current_setting('mazory.actor_id', true), '')::bigint,
        COALESCE(NULLIF(current_setting('mazory.raw_message_id', true), '')::bigint, (row_data->>'source_message_id')::bigint),
        COALESCE(NULLIF(current_setting('mazory.fact_event_id', true), '')::bigint,(row_data->>'fact_event_id')::bigint),
        sequence_no, happened, clock_timestamp(), kind, precision_name,
        TG_ARGV[0] || ':' || entity_key || ':' || sequence_no, changes, snapshot, clock_timestamp(), clock_timestamp());
    IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;
REVOKE ALL ON FUNCTION public.mazory_business_revision() FROM PUBLIC;
"""

PROFILE = "CASE WHEN u.full_name <> '' THEN u.full_name || ' (#' || u.id || ')' END"
STAGES = {'lead': 'Лид / Первичный контакт', 'qualification': 'Квалификация / Сбор ТЗ', 'design': 'Проектирование / Экспертиза', 'proposal_sent': 'КП отправлено', 'contract_signing': 'Согласование / Договор', 'in_execution': 'В исполнении / Производство / Монтаж', 'completed': 'Завершен / Сдан', 'stalled': 'Завис / Требует внимания', 'lost': 'Проигран / Архив'}
STAGE_LABEL = 'CASE p.status ' + ' '.join("WHEN '%s' THEN '%s'" % pair for pair in STAGES.items()) + ' ELSE p.status END'

VIEWS = {
    'projects': f"""SELECT p.id, p.id AS project_id, p.team_id, p.manager_id AS profile_id, p.company_id,
        p.name, t.name AS team, {PROFILE} AS project_manager,
        CASE WHEN p.whatsapp_fields @> '["stage"]'::jsonb THEN {STAGE_LABEL}
             WHEN s.external_stage_id <> '' THEN s.external_stage_name ELSE 'Стадия не определена' END AS status,
        p.status AS local_status, p.project_type, p.currency, p.archived, p.is_verified, p.identity_confirmed,
        p.created_at, p.updated_at, p.source_created_at, p.source_updated_at, p.source_time_precision,
        CASE WHEN p.contract_known OR p.contract_amount > 0 THEN p.contract_amount END AS contract_amount,
        CASE WHEN p.cost_confirmed THEN p.cost_amount END AS confirmed_cost,
        CASE WHEN p.cost_confirmed AND p.contract_amount > 0 THEN p.contract_amount END AS margin_contract,
        CASE WHEN p.cost_confirmed AND p.contract_amount > 0 THEN p.cost_amount END AS margin_cost
        FROM public.api_project p LEFT JOIN public.api_team t ON t.id=p.team_id
        LEFT JOIN public.api_userprofile u ON u.id=p.manager_id LEFT JOIN public.api_crmprojectsnapshot s ON s.project_id=p.id""",
    'crm_deals': """SELECT s.id, s.project_id, p.team_id, p.manager_id AS profile_id, p.company_id,
        p.name, t.name AS team, s.external_stage_name AS status, s.external_stage_id,
        s.external_manager_name AS crm_manager, s.external_manager_id, s.opportunity AS crm_amount, s.currency,
        s.created_at, s.updated_at, s.source_created_at, s.source_updated_at, s.source_stage_changed_at,
        s.source_begin_date, s.source_close_date, s.source_closed, s.synced_at
        FROM public.api_crmprojectsnapshot s JOIN public.api_project p ON p.id=s.project_id
        LEFT JOIN public.api_team t ON t.id=p.team_id""",
    'companies': "SELECT c.id,c.name,c.client_type,c.created_at,c.updated_at FROM public.api_company c",
    'commitments': f"""SELECT c.id,c.project_id,c.team_id,c.manager_id AS profile_id,p.company_id,
        {PROFILE} AS responsible_manager,t.name AS team,c.status,c.severity,c.is_verified,
        p.status AS project_status,c.created_at,c.updated_at,c.promised_at,c.fulfilled_at,c.original_deadline_at,
        COALESCE(c.deadline_at,c.deadline::timestamp AT TIME ZONE 'Asia/Almaty') AS effective_deadline,
        c.source_message_id,c.deadline_precision,c.version
        FROM public.api_commitment c LEFT JOIN public.api_project p ON p.id=c.project_id
        LEFT JOIN public.api_team t ON t.id=c.team_id LEFT JOIN public.api_userprofile u ON u.id=c.manager_id""",
    'payments': f"""SELECT f.id,f.project_id,p.team_id,f.credited_profile_id AS profile_id,p.company_id,
        {PROFILE} AS credited_manager,t.name AS team,p.status,p.project_type,
        f.payment_date,f.amount AS received_amount,f.currency,f.created_at,f.updated_at,
        f.is_verified,f.status AS payment_status,f.direction,f.amount_precision,f.reverses_id
        FROM public.api_financialrecord f JOIN public.api_project p ON p.id=f.project_id
        LEFT JOIN public.api_team t ON t.id=p.team_id LEFT JOIN public.api_userprofile u ON u.id=f.credited_profile_id""",
    'payment_schedule': """SELECT s.id,s.project_id,p.team_id,p.manager_id AS profile_id,s.currency,s.state AS status,
        s.due_date,s.amount AS scheduled_amount,s.amount-COALESCE(a.paid,0) AS outstanding_amount,
        s.created_at,s.updated_at,s.is_verified FROM public.api_paymentscheduleitem s
        JOIN public.api_project p ON p.id=s.project_id LEFT JOIN
        (SELECT a.schedule_item_id,SUM(a.amount) AS paid FROM public.api_paymentallocation a JOIN public.api_financialrecord f ON f.id=a.financial_record_id WHERE f.is_verified AND f.status='received' AND f.direction='income' AND f.amount_precision='exact' GROUP BY a.schedule_item_id) a ON a.schedule_item_id=s.id""",
    'sales_targets': f"""SELECT s.id,s.team_id,s.profile_id,{PROFILE} AS manager,t.name AS team,s.month,
        s.amount AS target_amount,s.currency,s.version,s.is_active,s.approved_at,s.created_at,s.updated_at
        FROM public.api_salestarget s LEFT JOIN public.api_userprofile u ON u.id=s.profile_id LEFT JOIN public.api_team t ON t.id=s.team_id""",
    'messages': """SELECT m.id,m.project_id,m.team_id,t.name AS team,m.chat_id AS chat,m.timestamp,m.received_at,
        m.created_at,m.updated_at,m.sent_at_known,m.source FROM public.api_rawmessage m
        LEFT JOIN public.api_team t ON t.id=m.team_id
        WHERE m.source IN ('waha','whatsapp_export') AND m.processing_state NOT IN ('deleted','superseded','export_staged','deduplication_ambiguous')
        AND NOT EXISTS (SELECT 1 FROM public.api_rawmessage n WHERE n.config_id IS NOT DISTINCT FROM m.config_id
        AND n.source=m.source AND n.session_name=m.session_name AND n.message_id=m.message_id AND n.id>m.id)""",
    'fact_review': "SELECT f.id,f.project_id,f.team_id,f.manager_id AS profile_id,f.fact_type,f.status,f.created_at,f.updated_at,f.reviewed_at,f.confidence FROM public.api_factcandidate f",
    'business_events': """SELECT e.id,e.project_id,p.team_id,e.manager_id AS profile_id,e.event_type,e.severity,
        e.timestamp,e.created_at,e.updated_at FROM public.api_businessevent e LEFT JOIN public.api_project p ON p.id=e.project_id""",
    'entity_revisions': "SELECT r.id,r.entity_type,r.entity_id,r.team_id,r.project_id,r.revision,r.effective_at,r.recorded_at,r.event_kind,r.time_precision,r.created_at,r.updated_at FROM public.api_temporalentityrevision r",
    'entity_events': "SELECT r.*,r.effective_at AS event_at FROM analytics.entity_revisions r WHERE r.event_kind<>'baseline'",
    'project_stage_events': """SELECT r.id,r.team_id,r.project_id,r.entity_type,r.effective_at AS event_at,r.recorded_at,
        r.time_precision,r.changes->COALESCE(CASE WHEN r.entity_type='project' THEN 'status' END,'external_stage_id')->>'before' AS from_stage,
        r.changes->COALESCE(CASE WHEN r.entity_type='project' THEN 'status' END,'external_stage_id')->>'after' AS status,
        r.created_at,r.updated_at FROM public.api_temporalentityrevision r
        WHERE r.event_kind='stage_changed' AND r.entity_type IN ('project','crmprojectsnapshot')""",
    'commitment_events': "SELECT r.*,r.effective_at AS event_at FROM analytics.entity_revisions r WHERE r.entity_type='commitment' AND r.event_kind<>'baseline'",
    'financial_events': "SELECT r.*,r.effective_at AS event_at FROM analytics.entity_revisions r WHERE r.entity_type IN ('financialrecord','paymentscheduleitem','paymentallocation') AND r.event_kind<>'baseline'",
    'project_activity': "SELECT r.*,r.effective_at AS event_at FROM analytics.entity_revisions r WHERE r.project_id IS NOT NULL AND r.event_kind<>'baseline'",
    'cashflow': """SELECT MIN(id) AS id,project_id,team_id,profile_id,currency,payment_date,
        SUM(received_amount) AS received_amount,COUNT(*) AS payment_count FROM analytics.payments
        WHERE is_verified AND payment_status='received' AND direction='income' AND amount_precision='exact'
        GROUP BY project_id,team_id,profile_id,currency,payment_date""",
    'commitment_performance': """SELECT c.*,EXTRACT(EPOCH FROM (fulfilled_at-promised_at))/86400.0 AS duration_days,
        CASE WHEN fulfilled_at IS NOT NULL AND effective_deadline IS NOT NULL THEN (fulfilled_at<=effective_deadline)::int END AS on_time,
        CASE WHEN status IN ('pending','overdue') AND effective_deadline IS NOT NULL THEN GREATEST(EXTRACT(EPOCH FROM (now()-effective_deadline))/86400.0,0) END AS overdue_days
        FROM analytics.commitments c WHERE is_verified""",
    'plan_fact_monthly': """SELECT NULL::bigint AS project_id,team_id,profile_id,month,currency,
        SUM(target_amount) AS target_amount,NULL::numeric AS received_amount
        FROM analytics.sales_targets WHERE is_active GROUP BY team_id,profile_id,month,currency
        UNION ALL SELECT project_id,team_id,profile_id,date_trunc('month',payment_date)::date,currency,
        NULL::numeric,SUM(received_amount) FROM analytics.cashflow
        GROUP BY project_id,team_id,profile_id,date_trunc('month',payment_date)::date,currency""",
    'source_coverage': "SELECT c.id,c.team_id,c.source_scope,c.complete_through,c.updated_at,c.created_at,jsonb_array_length(c.gaps) AS gap_count FROM public.api_sourcecheckpoint c",
}


def install(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        raise RuntimeError('Temporal analytics requires PostgreSQL in Docker')
    schema_editor.execute(TIMESTAMPS)
    # Recover the previous auto_now_add insertion before repairing business time.
    schema_editor.execute("UPDATE public.api_commitment SET created_at=promised_at WHERE created_at IS NULL")
    schema_editor.execute("""UPDATE public.api_commitment c SET promised_at=CASE WHEN m.sent_at_known THEN m.timestamp END
        FROM public.api_rawmessage m WHERE m.id=c.source_message_id""")
    schema_editor.execute("UPDATE public.api_commitment SET promised_at=NULL WHERE source_message_id IS NULL")
    # Existing rows enqueue deferred FK checks even when only dates change.
    # Flush them before trigger/index DDL in this atomic migration.
    schema_editor.execute('SET CONSTRAINTS ALL IMMEDIATE')
    schema_editor.execute(HISTORY)
    for model in apps.get_app_config('api').get_models():
        table = schema_editor.quote_name(model._meta.db_table)
        schema_editor.execute(f'CREATE TRIGGER mazory_record_times BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION public.mazory_record_times()')
        fields = HISTORY_FIELDS.get(model._meta.model_name)
        if fields:
            kind = model._meta.model_name
            schema_editor.execute(f"CREATE TRIGGER mazory_business_revision AFTER INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION public.mazory_business_revision('{kind}','{','.join(fields)}')")
    schema_editor.execute("""CREATE FUNCTION public.mazory_history_immutable() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$ BEGIN RAISE EXCEPTION 'Temporal history is append-only'; END $$; REVOKE ALL ON FUNCTION public.mazory_history_immutable() FROM PUBLIC; CREATE TRIGGER mazory_history_immutable BEFORE UPDATE OR DELETE ON public.api_temporalentityrevision FOR EACH ROW EXECUTE FUNCTION public.mazory_history_immutable();""")
    for name, query in VIEWS.items():
        schema_editor.execute(f'CREATE VIEW analytics.{name} WITH (security_invoker=true) AS {query}')
    for name in HISTORY_FIELDS:
        model = apps.get_model('api',name)
        table = schema_editor.quote_name(model._meta.db_table)
        for field in ('created_at','updated_at'):
            schema_editor.execute(f'CREATE INDEX {model._meta.db_table}_{field}_temporal ON {table} ({field})')
    schema_editor.execute('REVOKE ALL ON SCHEMA analytics FROM PUBLIC')


def uninstall(apps, schema_editor):
    for name in reversed(VIEWS):
        schema_editor.execute(f'DROP VIEW IF EXISTS analytics.{name}')
    for name in HISTORY_FIELDS:
        for field in ('created_at','updated_at'):
            schema_editor.execute(f'DROP INDEX IF EXISTS api_{name}_{field}_temporal')
    schema_editor.execute('DROP TRIGGER IF EXISTS mazory_history_immutable ON public.api_temporalentityrevision')
    schema_editor.execute('DROP FUNCTION IF EXISTS public.mazory_history_immutable()')
    for model in apps.get_app_config('api').get_models():
        table = schema_editor.quote_name(model._meta.db_table)
        schema_editor.execute(f'DROP TRIGGER IF EXISTS mazory_business_revision ON {table}')
        schema_editor.execute(f'DROP TRIGGER IF EXISTS mazory_record_times ON {table}')
    schema_editor.execute('DROP FUNCTION IF EXISTS public.mazory_business_revision()')
    schema_editor.execute('DROP FUNCTION IF EXISTS public.mazory_record_times()')
