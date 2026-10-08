import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import type { Conversation, Artifact } from '../src/types/dialogue'
test.setTimeout(180000)
const dir = process.env.MAZORY_E2E_DIR!
const session: { access: string; refresh: string; cookie_name: string; user_id: number } = JSON.parse(readFileSync(join(dir, 'temporal-session.json'), 'utf8'))
const headers = { Authorization: `Bearer ${session.access}` }
test('projects over the full explicit year reject payments and allow date-axis recalculation', async ({ page, context, request }) => {
  const readback: Record<string, boolean | number> = JSON.parse(readFileSync(join(dir, 'temporal-readback.json'), 'utf8'))
  expect(readback).toMatchObject({ all_models_timestamped: true, views: 20, created_immutable: true, real_update: true, noop_unchanged: true, rollback_atomic: true, bulk_timestamps: true })
  expect(readback.triggers).toBe(67)
  await expect.poll(() => { try { const result: Record<string, number> = JSON.parse(readFileSync(join(dir, 'temporal-benchmark.json'), 'utf8')); return Object.keys(result).length } catch { return 0 } }, { timeout: 60000 }).toBe(20)
  const benchmark: Record<string, number> = JSON.parse(readFileSync(join(dir, 'temporal-benchmark.json'), 'utf8'))
  for (const ms of Object.values(benchmark)) expect(ms).toBeLessThan(2000)
  await expect.poll(() => JSON.parse(readFileSync(join(dir, 'temporal-semantic.json'), 'utf8')).backfill_succeeded, { timeout:60000 }).toBe(true)
  const semantics: Record<string, boolean | number> = JSON.parse(readFileSync(join(dir, 'temporal-semantic.json'), 'utf8'))
  expect(semantics).toMatchObject({crm_repeat_no_event:true,crm_stage_source_time:true,commitment_postponed:true,commitment_fulfilled:true,
    financial_correction:true,archived_history:true,append_only:true,foreign_scope_hidden:true,source_timezone_boundary:true,
    backfill_replayed:true,legacy_import_count:1})
  await context.addCookies([{ name: session.cookie_name, value: session.refresh, url: `${process.env.MAZORY_E2E_URL}/api/auth/`, httpOnly: true, sameSite: 'Lax' }])
  await page.goto('/')
  await page.getByTestId('nav-kpi-dashboard').waitFor()
  const input = page.getByPlaceholder('Спросите Mazory...')
  await input.fill('шкала X месяцы в году 2026, шкала Y количество проектов, нарисуй график зависимости, что бы видеть сколько проектов больше или меньше пришло за какой месяц')
  await input.press('Enter')
  let conversationId: number
  await expect.poll(async () => {
    conversationId = await page.evaluate((id: number) => JSON.parse(localStorage.getItem(`mazory-conversation-${id}`) || 'null') as number, session.user_id)
    if (!conversationId) return 'missing'
    const c: Conversation = await (await request.get(`/api/chat/conversations/${conversationId}/`, { headers })).json()
    return c.turns.at(-1)?.state
  }, { timeout: 120000 }).toBe('succeeded')
  const c: Conversation = await (await request.get(`/api/chat/conversations/${conversationId!}/`, { headers })).json()
  const turn = c.turns[0]!
  const artifact: Artifact = await (await request.get(`/api/chat/artifacts/${turn.artifacts[0]!.id}/`, { headers })).json()
  const dataset = Object.values(artifact.presentation!.datasets)[0]!
  expect(dataset.normalized_query).toMatchObject({ dataset: 'projects', measures: ['project_count'], date_axis: 'source_created_at', date_range: { start: '2026-01-01', end_exclusive: '2027-01-01' } })
  expect(dataset.rows).toHaveLength(12)
  expect(dataset.rows.slice(0, 3).map(r => r.project_count)).toEqual([1, 1, 1])
  expect(dataset.rows[11]!.project_count).toBeNull()
  expect(artifact.presentation!.title).toContain('проектов')
  await expect(page.getByTestId('presentation')).toBeVisible()
  await expect(page.getByLabel('Ось времени')).toBeVisible()
  await expect(page.getByLabel('Ось времени')).toHaveValue('source_created_at')
  await page.screenshot({ path: join(dir, 'playwright/temporal-created-projects.png'), fullPage: true })
  await page.getByLabel('Ось времени').selectOption('updated_at')
  await expect.poll(async () => {
    const latest: Conversation = await (await request.get(`/api/chat/conversations/${conversationId!}/`, { headers })).json()
    return latest.turns[1]?.state
  }, { timeout: 120000 }).toBe('succeeded')
  const latest: Conversation = await (await request.get(`/api/chat/conversations/${conversationId!}/`, { headers })).json()
  const updated: Artifact = await (await request.get(`/api/chat/artifacts/${latest.turns[1]!.artifacts[0]!.id}/`, { headers })).json()
  expect(Object.values(updated.presentation!.datasets)[0]!.normalized_query).toMatchObject({ dataset: 'projects', date_axis: 'updated_at', date_range: { start: '2026-01-01', end_exclusive: '2027-01-01' } })
  await expect(page.getByLabel('Ось времени')).toHaveValue('updated_at')
  await page.screenshot({ path: join(dir, 'playwright/temporal-projects.png'), fullPage: true })
})
