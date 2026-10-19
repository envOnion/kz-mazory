import type { ChatModelProfile, JsonValue } from '../../types/settings'

const fields = ['version', 'quantization', 'tokenizer', 'chat_template', 'context_window_tokens', 'tokenizer_id', 'tokenizer_revision']
function object(value: unknown): value is Record<string, unknown> { return value !== null && typeof value === 'object' && !Array.isArray(value) }
export function safeJson(value: unknown): JsonValue {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (Array.isArray(value)) return value.map(safeJson)
  if (object(value)) return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, safeJson(item)]))
  return null
}
export function parseProfile(value: unknown): ChatModelProfile {
  if (!object(value) || value.version !== 1) throw new Error('Нужен JSON-профиль с version: 1. Для tokenizer.json поместите содержимое в поле tokenizer.')
  if (Object.keys(value).some(key => !fields.includes(key))) throw new Error('Допустимые поля профиля: version, quantization, tokenizer, chat_template, context_window_tokens, tokenizer_id, tokenizer_revision.')
  for (const key of ['quantization', 'tokenizer']) if (key in value && !object(value[key])) throw new Error(`Поле ${key} должно содержать JSON-объект.`)
  if (object(value.quantization) && Object.entries(value.quantization).some(([key, item]) => !['method', 'bits', 'group_size', 'format', 'description'].includes(key) || !['string', 'number', 'boolean'].includes(typeof item))) throw new Error('Квантование: допустимы только method, bits, group_size, format, description со скалярными значениями.')
  for (const key of ['chat_template', 'tokenizer_id', 'tokenizer_revision']) if (key in value && typeof value[key] !== 'string') throw new Error(`Поле ${key} должно содержать строку.`)
  if (('tokenizer' in value) !== ('chat_template' in value)) throw new Error('Содержимое tokenizer и строку chat_template нужно передать вместе.')
  if ('context_window_tokens' in value && (typeof value.context_window_tokens !== 'number' || !Number.isInteger(value.context_window_tokens) || value.context_window_tokens < 1)) throw new Error('context_window_tokens должно быть положительным целым числом.')
  return safeJson(value) as unknown as ChatModelProfile
}
export function safeProfile(value: unknown): ChatModelProfile | null {
  try { return parseProfile(value) } catch { return null }
}
