import { z } from 'zod'
import type { AnalyticsDataset } from './analytics'
export type ChartKind = 'bar' | 'line' | 'area' | 'scatter' | 'donut' | 'funnel' | 'waterfall' | 'table' | 'kpi'
export interface PresentationBlock {
  id: string; kind: ChartKind; dataset_id: string; title?: string; size: 'wide' | 'half' | 'third'
  encoding: Partial<Record<'category' | 'series' | 'value' | 'x' | 'y' | 'label', string>>
  columns?: string[]; steps?: { label: string; value: string; kind: 'base' | 'delta' | 'total' }[]
}
export interface PresentationDocument { version: '1.0'; title: string; summary: string; blocks: PresentationBlock[]; datasets: Record<string, AnalyticsDataset> }
const text = z.string().max(2000)
const decimal = /^-?\d+(?:\.\d+)?$/
const value = z.union([z.string().max(2000), z.number().finite(), z.null()])
const dataset = z.object({
  dataset_id: z.string().max(64),
  columns: z.array(z.object({ name: z.string().max(64), type: z.enum(['id', 'text', 'date', 'money', 'count', 'percent']), unit: z.string().max(10).nullable() }).strict()).max(10),
  rows: z.array(z.record(z.string(), value)).max(1000), normalized_query: z.record(z.string(), z.unknown()), timezone: z.string().max(64), effective_end_exclusive: z.string().max(10).nullable().optional(),
  coverage: z.object({ status: z.enum(['complete', 'partial']), message: text }).strict(),
  returned_count: z.number().int().nonnegative(), total_groups: z.number().int().nonnegative(), truncated: z.boolean(), definition: text,
  evidence: z.array(z.object({ id: z.number().int().positive(), sender_name: text }).strict()).max(20),
}).strict()
const block = z.object({ id: z.string().regex(/^[a-zA-Z0-9_-]{1,64}$/), kind: z.enum(['bar', 'line', 'area', 'scatter', 'donut', 'funnel', 'waterfall', 'table', 'kpi']), dataset_id: z.string().max(64), title: z.string().max(200).optional(), size: z.enum(['wide', 'half', 'third']),
  encoding: z.object({ category: z.string().max(64).optional(), series: z.string().max(64).optional(), value: z.string().max(64).optional(), x: z.string().max(64).optional(), y: z.string().max(64).optional(), label: z.string().max(64).optional() }).strict(),
  columns: z.array(z.string().max(64)).max(10).optional(), steps: z.array(z.object({ label: text, value: z.string().regex(decimal), kind: z.enum(['base', 'delta', 'total']) }).strict()).max(12).optional(),
}).strict()
const schema = z.object({ version: z.literal('1.0'), title: z.string().min(1).max(200), summary: text, blocks: z.array(block).min(1).max(12), datasets: z.record(z.string(), dataset) }).strict()
export function validatePresentation(input: unknown): PresentationDocument {
  const doc = schema.parse(input)
  if (JSON.stringify(input).length > 1048576 || Object.keys(doc.datasets).length > 8) throw new Error('Превышен размер представления')
  const ids = new Set<string>()
  for (const [id, ds] of Object.entries(doc.datasets)) {
    const names = new Set(ds.columns.map(c => c.name))
    if (id !== ds.dataset_id || names.size !== ds.columns.length || ds.returned_count !== ds.rows.length || ds.total_groups < ds.returned_count || ds.truncated !== (ds.total_groups > ds.returned_count)) throw new Error('Неверный набор данных')
    for (const row of ds.rows) {
      if (Object.keys(row).length !== names.size || Object.keys(row).some(key => !names.has(key))) throw new Error('Неверные поля набора данных')
      for (const col of ds.columns) {
        const cell = row[col.name]
        if (cell === null) continue
        if (['text', 'date'].includes(col.type) && typeof cell !== 'string') throw new Error('Неверная подпись')
        if (col.type === 'money' && (typeof cell !== 'string' || !decimal.test(cell))) throw new Error('Неверная сумма')
        if (['id', 'count', 'percent'].includes(col.type) && typeof cell !== 'number') throw new Error('Неверное число')
        if (['id', 'count'].includes(col.type) && (!Number.isSafeInteger(cell) || (col.type === 'id' && (typeof cell !== 'number' || cell <= 0)))) throw new Error('Неверный целочисленный ключ')
        if (['money', 'count', 'percent', 'id'].includes(col.type) && (!Number.isFinite(Number(cell)) || Math.abs(Number(cell)) > Number.MAX_SAFE_INTEGER)) throw new Error('Число вне диапазона графика')
      }
    }
  }
  for (const b of doc.blocks) {
    const ds = doc.datasets[b.dataset_id]
    if (!ds || ids.has(b.id)) throw new Error('Неверная ссылка блока')
    ids.add(b.id)
    const cols = new Map(ds.columns.map(c => [c.name, c]))
    const required: Record<ChartKind, string[]> = { bar: ['category', 'value'], line: ['category', 'value'], area: ['category', 'value'], scatter: ['x', 'y'], donut: ['category', 'value'], funnel: ['category', 'value'], waterfall: [], table: [], kpi: ['value'] }
    const channels = Object.keys(b.encoding)
    const allowed = [...required[b.kind], ...(['bar', 'line', 'area'].includes(b.kind) ? ['series'] : b.kind === 'scatter' ? ['label'] : [])]
    if (required[b.kind].some(k => !channels.includes(k)) || channels.some(k => !allowed.includes(k)) || Object.values(b.encoding).some(c => !cols.has(c))) throw new Error('Неверные оси')
    for (const key of ['value', 'x', 'y'] as const) { const name = b.encoding[key]; if (name && !['money', 'count', 'percent'].includes(cols.get(name)?.type || '')) throw new Error('Ось должна быть числовой') }
    if (b.kind === 'table' && (!b.columns?.length || b.columns.some(c => !cols.has(c)))) throw new Error('Неверная таблица')
    if (b.kind !== 'table' && b.columns) throw new Error('Неверные параметры блока')
    if (b.kind === 'kpi' && ds.rows.length !== 1) throw new Error('Неверный KPI')
    if (b.kind === 'waterfall' && (!b.steps?.length || b.steps.some(step => !Number.isFinite(Number(step.value)) || Math.abs(Number(step.value)) > Number.MAX_SAFE_INTEGER))) throw new Error('Неверный водопад')
    if (b.kind !== 'waterfall' && b.steps) throw new Error('Неверные шаги')
  }
  return doc
}
