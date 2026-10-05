export type Role = 'manager' | 'team_lead' | 'finance' | 'admin' | 'client'
export type Period = 'this_month' | 'last_month' | 'quarter' | 'year'
export type Money = string
export interface AuthUser { id: number; phone: string; name: string; username: string; roles: Role[] }
export interface AuthResponse { access: string; user: AuthUser; session_id: number; csrf_token: string }
export interface ApiError { error?: string; code?: string; fields?: Record<string, unknown> }
export interface Page<T> { count: number; next: string | null; previous?: string | null; results: T[]; stats?: { total: number; open: number; ready: number; commitments?: number } }
export interface Coverage { status: 'complete' | 'partial'; message: string }
export interface Profile {
  id: number; full_name: string; role: string; department: string; email: string; phone: string; avatar_url: string;
  timezone: string; notification_preferences: NotificationPreferences;
  monthly_target: Money | null; monthly_target_formatted: string; current_sales: Money; current_sales_formatted: string;
  deals_count: number; rank_in_team: number | null; conversion_rate: string | null; kpi_percent: number | null;
  whatsapp_daily_digest: boolean; whatsapp_stalled_deals: boolean; whatsapp_critical_kpi: boolean;
  ai_response_mode: 'detailed' | 'concise' | 'finance'; ai_auto_suggest_next_actions: boolean; updated_at: string; coverage: Coverage
}
export interface NotificationPreferences { quiet_start?: string; quiet_end?: string; digest_time?: string; whatsapp?: boolean; reminder?: boolean; daily_digest?: boolean }
export interface NotificationItem { id: number; title: string; message: string; type: string; created_at: string; is_read: boolean; acknowledged: boolean; project_id: number | null; commitment_id: number | null; deliveries: Delivery[] }
export interface Delivery { state: string; channel: string; updated_at: string; error_code: string }
export interface DispatchNotificationPayload { recipient_id: number; project_id?: number; title: string; message: string; type?: string; send_whatsapp?: boolean }
export interface Session { id: number; device: string; created_at: string; last_used_at: string; expires_at: string }
export type CandidateStatus = 'pending' | 'approved' | 'rejected' | 'superseded'
export type CandidateFactType = 'project' | 'payment' | 'commitment'
export type CrmMatchState = 'not_requested' | 'queued' | 'matched' | 'ambiguous' | 'not_found' | 'disabled' | 'error'
export type CrmMatchSelectionState = 'suggested' | 'selected' | 'dismissed'
export interface CrmMatchOption {
  id: number
  project_id: number | null
  project_version: number | null
  bitrix_deal_id: string
  bitrix_company_id: string
  deal_title: string
  company_name: string
  object_label: string
  stage_id: string
  stage_label?: string
  opportunity: Money | null
  currency: string
  score: number
  match_reasons: string[]
  selection_state: CrmMatchSelectionState
  captured_at: string
}
export interface CrmResolution {
  state: CrmMatchState
  revision: number
  checked_at: string | null
  error_code: string
  options: CrmMatchOption[]
}
export interface Candidate {
  thread?: { id: number; topic: string; version: number } | null
  id: number
  project_id: number | null
  project_name: string
  team_id: number
  manager_id: number | null
  fact_type: CandidateFactType
  proposed_changes: Record<string, unknown>
  source_metadata?: { sent_at: string | null; received_at: string; time_basis: string; timezone: string } | null
  source_available?: boolean
  current_values?: Record<string, unknown>
  chat_name?: string
  conversation_key?: string
  status: CandidateStatus
  base_version: number
  current_version: number
  confidence: number
  uncertainties: string[]
  evidence: Evidence[]
  review_reason: string
  created_at: string
  crm_resolution: CrmResolution
}
export interface Evidence { role?: string; id: number; quote: string; source_id: number; source_url: string }
export interface Source { id: number; content: string; sender_name: string; sent_at: string | null; received_at: string; processing_state: string; revision: string }
export interface CrmCatalogStatus { team_id: number; state: string; imported_count: number; last_success_at: string | null; error_code: string }
export interface CompanyItem {
  id: number
  bitrix_company_id: string
  name: string
  client_type: string
  phone: string
}

export interface DirectoryProject {
  id: number
  name: string
  version: number
  currency: string
  team_id: number
  company_id?: number | null
  company_name?: string | null
  bitrix_id?: string | null
}

export interface Directory {
  chats?: { id: number; name: string; team_id: number }[]
  crm_catalog?: CrmCatalogStatus[]
  projects_count?: number
  projects_next_page?: number | null
  teams: { id: number; name: string; history_complete_from: string | null }[]
  profiles: { id: number; user_id: number; full_name: string }[]
  projects: DirectoryProject[]
  companies?: CompanyItem[]
}
export interface Payment { id: number; project_id: number; amount: Money; currency: string; payment_date: string; candidate_id: number | null; reverses_id: number | null; credited_profile_id: number | null }
export interface ScheduleRow { id: number; project_id: number; amount: Money; remaining: Money; due_date: string; bucket: string }
export interface Receivables { buckets: Record<string, Money>; overdue: Money; rows: ScheduleRow[]; unknown_schedule_projects: number; currency: string }
export interface Target { id: number; team_id: number; profile_id: number; month: string; amount: Money; currency: string; version: number }
export interface Finance { payments: Payment[]; targets: Target[]; receivables: Receivables }
export interface Attachment { id: number; project_id: number; content_type: string; state: string; published_to_client: boolean; created_at: string }
export interface ExportResult { filename: string; content: string; coverage: Coverage }
export interface Operation<T> { id: number; status: 'queued' | 'running' | 'succeeded' | 'failed' | 'expired' | 'cancelled'; result: T | null; error_code: string; expires_at: string }
export interface OperationReceipt { operation_id: number; status: string }
export interface Health { ai_last_24h: { requests: number; cost_usd: Money | null; unknown_cost_requests: number; failed_requests: number; mean_duration_ms: number | null }; outbox: { event_type: string; state: string; count: number }[]; oldest_pending_seconds: number; errors: { id: number; event_type: string; state: string; error_code: string; attempt_count: number }[] }
export interface KpiFilters { team_id?: number; manager_id?: number; project_id?: number; currency?: string }

export interface LegacyProject { id: number; name: string; team_id: number | null; contract_amount: Money; legacy_paid_amount: Money; currency: string }
