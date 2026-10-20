export type SettingValue = string | number | boolean | null
export interface SettingField {
  name: string; label: string; type: 'boolean' | 'integer' | 'number' | 'decimal' | 'text' | 'string' | 'json'
  help: string; choices: { value: string; label: string }[]; max_length: number | null
}
export interface SettingsRow {
  id: number
  name?: string
  is_active?: boolean
  is_primary?: boolean
  chat_model_name?: string
  chat_provider_url?: string
  runtime?: {effective_autonomous_write_enabled:boolean}
  keys?: { chat: boolean; embedding: boolean; decision?: boolean }
  [key: string]: unknown
}
export type AiModelRole = 'chat' | 'embedding' | 'decision'
export type AiSettingsRole = AiModelRole | 'shared'
export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue }
export interface ChatModelProfile {
  version: 1
  quantization?: Record<string, JsonValue>
  tokenizer?: Record<string, JsonValue>
  chat_template?: string
  context_window_tokens?: number
  tokenizer_id?: string
  tokenizer_revision?: string
}
export interface AiConnectionCheck {
  id: number
  status: 'queued' | 'running' | 'completed' | 'failed'
  result: Record<string, JsonValue> | null
  error_code: string
}
export interface AiSettingsGroup { title: string; names: string[] }
export interface SettingsPage { items: SettingsRow[]; defaults?: SettingsRow; can_change?: boolean; fields: SettingField[] }
export interface SettingsAccess { can_integrate: boolean; can_ai: boolean; can_create_ai: boolean; can_create_whatsapp: boolean; can_create_bitrix: boolean; can_bitrix: boolean; can_administer_access: boolean; mfa_required: boolean; mfa_enabled: boolean; teams: {id: number; name: string}[] }
export interface HistoryPage { job: SettingsRow | null; fields: SettingField[]; runs: {id: number; state: string; progress: {total: number; processed: number; errors: number; pending: number}}[] }
export interface SettingsAudit { id: number; actor_id: number | null; action: string; target_type: string; target_id: number; created_at: string }
export interface DiagnosticsPage {
  resource:string;title:string;description:string;resources:{id:string;label:string}[]
  columns:{key:string;label:string}[];items:Record<string,unknown>[];next_cursor:string|null;as_of:string;coverage:string
}
export interface AccessGrantsData {
  teams:(SettingsRow & {id:number;name:string;is_active:boolean})[]
  team_fields:SettingField[]
  users:{id:number;username:string;first_name:string;last_name:string;is_active:boolean}[]
  configs:{id:number;name:string}[]; projects:{id:number;name:string}[]
  chats:{id:number;user_id:number;config_id:number;is_active:boolean}[]
  clients:{id:number;user_id:number;project_id:number;status:string}[]
}
