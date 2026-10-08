export type AnalyticsCell = string | number | null
export interface AnalyticsColumn { name: string; label?: string; source?: string; semantic_role?: string; type: 'id' | 'text' | 'date' | 'money' | 'count' | 'percent' | 'number'; unit: string | null }
export interface AnalyticsDataset {
  dataset_id: string; columns: AnalyticsColumn[]; rows: Record<string, AnalyticsCell>[]
  normalized_query: Record<string, unknown>; timezone: string; effective_end_exclusive?: string | null
  coverage: { status: 'complete' | 'partial'; message: string; date_axes?: Record<string, string>; date_axis?: string | null; unknown_dates?: number; future_values?: string }
  returned_count: number; total_groups: number; truncated: boolean; definition: string
  evidence: { id: number; sender_name: string }[]
}
