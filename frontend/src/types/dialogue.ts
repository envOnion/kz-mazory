import type { PresentationDocument } from './presentation'
import type { KpiFilters, Period } from './platform'
export interface AnswerFact { dataset_id: string; measure: string; formula: string; value: string | number; display: string; label: string; row_key?: Record<string, string | number | null>; row_count?: number; scope: Record<string, unknown> }
export interface AnswerAction { label: string; prompt: string; patch?: QueryPatch; artifact_id?: number }
export interface AnswerDocument { version: '1.0'; kind: 'text' | 'analysis' | 'clarification' | 'empty' | 'error'; markdown: string; template?: string; facts: Record<string, AnswerFact>; artifact_ids: number[]; suggested_actions: AnswerAction[] }
export interface ArtifactRef { id: number; parent_artifact_id: number | null; title: string; available: boolean; generated_at: string }
export interface AnalyticsTurn { id: number; sequence: number; parent_turn_id: number | null; operation_id: number | null; user_text: string; state: 'queued' | 'running' | 'succeeded' | 'failed' | 'expired' | 'cancelled'; answer_document: AnswerDocument | null; error: string; artifacts: ArtifactRef[]; created_at: string }
export interface ConversationRef { id: number; title: string; updated_at?: string }
export interface Conversation extends ConversationRef { default_scope: KpiFilters & { period: Period }; turns: AnalyticsTurn[] }
export interface Artifact { id: number; available: boolean; presentation?: PresentationDocument; query_plan?: { title: string; blocks: { query: Record<string, unknown>; block: Record<string, unknown> }[] }; parent_artifact_id?: number | null; message?: string; expires_at?: string }
export interface QueryPatch { date_axis?: string; grouping?: 'day' | 'week' | 'month' | 'quarter' | 'year' | 'manager' | 'status' | 'project'; sort?: 'date_asc' | 'date_desc' | 'value_asc' | 'value_desc' }
export interface TurnReceipt { turn_id: number; operation_id: number; status: string }
