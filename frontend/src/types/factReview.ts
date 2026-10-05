import type { CandidateFactType } from "./platform";
export interface ConversationMessage {
  source_metadata?: {
    time_basis: string;
    sent_at: string | null;
    received_at: string;
  };
  id: number;
  sender_name: string;
  sent_at: string | null;
  received_at: string;
  content: string;
  is_source: boolean;
  used_by_ai: boolean;
  partial?: boolean;
  is_source_revision?: boolean;
}
export interface DialogueThreadChild {
  id: number;
  topic: string;
  state: string;
  version: number;
  updated_at: string;
}

export interface DialogueThreadFact {
  id: number;
  fact_type: string;
  status: string;
  proposed_changes: any;
  in_progress?: boolean;
}

export interface DialogueThread {
  id: number;
  topic: string;
  summary: string;
  state: 'open' | 'ready' | 'unknown' | 'superseded';
  version: number;
  parent_id: number | null;
  project_id: number | null;
  project_name?: string | null;
  company_id?: number | null;
  company_name?: string | null;
  chat_name?: string | null;
  counterparty?: string | null;
  is_subscribed?: boolean;
  children_count?: number;
  messages_count?: number;
  commitments_count?: number;
  total_amount?: number | string | null;
  updated_at?: string;
  children?: { id: number; topic: string; state: string; version: number; updated_at: string }[];
  facts?: { id: number; fact_type: string; status: string; proposed_changes: any; in_progress?: boolean }[];
  messages: (ConversationMessage & { thought_state: 'intermediate' | 'final' | 'unknown'; relation: string; rationale: string })[];
}
export interface CandidateContext {
  thread?: DialogueThread | null;
  source: ConversationMessage | null;
  messages: ConversationMessage[];
  before: number | null;
  after: number | null;
  ai_messages: ConversationMessage[];
  ai_next_page: number | null;
  coverage: string;
  chat_name: string;
  history_available: boolean;
}
export interface ReviewField {
  key: string;
  label: string;
  type:
    | "text"
    | "money"
    | "date"
    | "datetime-local"
    | "textarea"
    | "select"
    | "reference";
  options?: { value: string; label: string }[];
}
export type FactDraft = Record<string, string>;
export type FactType = CandidateFactType;
