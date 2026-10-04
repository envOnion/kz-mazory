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
export interface DialogueThread {
  id: number; topic: string; summary: string; state: 'open' | 'ready' | 'unknown' | 'superseded';
  version: number; parent_id: number | null; project_id: number | null;
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
