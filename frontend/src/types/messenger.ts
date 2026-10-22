// DTO новых read-эндпоинтов Messenger (§7). Локальные id — number,
// внешние — string; неизвестные значения — null, не 0.

export interface StatusAxes {
  analysis: string
  clarification: string
  delivery_whatsapp: string
  crm: string
  attention: boolean
  attention_reasons: string[]
}

export interface DialogSummary {
  match_record_id:number|null
  key: string
  session_name: string
  chat_id: string
  category: 'group' | 'private' | 'system'
  team_id: number | null
  name: string
  last_at: string | null
  last_preview: string
  last_direction: string | null
  attention_count: number
}

export interface RecordBrief {
  sender_name:string
  id: number
  dialog: { session_name: string; chat_id: string; category: string; team_id: number | null }
  category: string
  direction: string
  purpose: string
  body: string
  occurred_at: string | null
  time_is_estimated: boolean
  statuses: StatusAxes
  attention: boolean
}

export interface RecordDetail extends RecordBrief {
  origin: {
    kind: string
    raw_message_id: number | null
    sender_name: string | null
    sent_at_known: boolean
    source_time: string | null
    event_type: string | null
  }
  capabilities: { can_trace: boolean; can_reanalyze: boolean }
  counts: { events: number }
}

export interface ChainNode {
  kind: string
  id: number
  label: string
  state: string
  allowed: boolean
  href: string | null
  meta: Record<string, unknown>
}

export interface ChainTimelineItem {
  id: number
  kind: string
  state: string
  occurred_at: string | null
  time_is_estimated: boolean
  parent_event_id: number | null
}

export interface RecordChain { nodes:ChainNode[]; timeline:ChainTimelineItem[]; next_cursor:string|null }
export interface ChangeRow extends RecordBrief { type:string; record_id?:number; kind?:string; state?:string }

export interface TraceRow {
  id: number
  created_at: string | null
  attempt_no: number
  status: string
  pipeline_action: string
  model_version: string
  prompt_version: string
  error_code: string
  result_summary: string
  ai_confidence: number
  earlier_messages_count: number
  bitrix_matched_deal_id: string
  bitrix_deal_title: string
  bitrix_deal_stage: string
  ai_extracted_facts: Record<string, unknown>
  context_metadata: Record<string, unknown>
  has_raw_payload: boolean
  has_context: boolean
}

export interface Paged<T> {
  items: T[]
  next_cursor: string | null
  as_of: string
  coverage: string
}
export interface TeamOption {id:number;name:string}
export interface OperationPage<T> extends Paged<T> {teams:TeamOption[];state_labels?:Record<string,string>}

export interface DialogsPage extends Paged<DialogSummary> {}
export interface RecordsPage extends Paged<RecordBrief> {}

export interface ErrorRecoveryPreview { found: number; can_start: boolean; model: string; blocker: string }
export interface ErrorRecoveryProgress {
  operation_id: number; status: string; error_code: string; model: string
  found: number; scheduled: number; already_processing: number; changed: number; unavailable: number
  pending: number; succeeded: number; needs_review: number; failed: number
}

export interface ChangeItem {
  type: 'record' | 'event'
  id: number
  [key: string]: unknown
}

export interface AnalyticsOperations {
  as_of: string
  timezone: string
  coverage: string
  messages: { inbound_total: number; by_state: Record<string, number>; attention: number; outbound_by_purpose: Record<string, number> }
  crm: { operations_by_state: Record<string, number>; logical_operations: number; attempts_total: number }
  errors: { current: Record<string, number>; failed_attempts: Record<string, number> }
  dynamics: { day: string | null; messages: number }[]
  catalog: { last_success_at: string | null; imported_count: number; state: string; coverage: string } | null
}

export interface QueueJob {
  source: 'outbox' | 'source_work'
  can_retry: boolean
  requires_delivery_confirmation?: boolean
  record_href?: string | null
  queue: string
  id: number
  type: string
  state: string
  raw_message_id?: number | null
  processing_version?: string
  attempt_count?: number
  error_code: string | null
  next_attempt_at?: string | null
  lease_until?: string | null
  created_at: string | null
  updated_at: string | null
}

export interface CrmChangeRow {
  team_id:number|null
  actor_kind:string|null
  actor_label:string
  methods?:string[]
  source: 'delivery' | 'journal'
  method?: string | null
  record_href?: string | null
  reconstructed?: boolean
  id: number
  object_type: string | null
  external_id: string | null
  external_url: string | null
  local_id: number | null
  action: string | null
  state: string
  error_code: string | null
  before: unknown
  after: unknown
  fact_event_id: number | null
  outbox_event_id: number | null
  created_at: string | null
  updated_at: string | null
}

export interface ThreadRow {
  id: number
  topic: string
  state: string
  version: number
  project_id: number | null
  config_id: number | null
  last_activity_at?: string | null
  message_count?: number
  [key: string]: unknown
}
export interface TraceContextItem {raw_message_id:number;body:string;sender:string;time:string|null;partial:boolean;href:string|null}
