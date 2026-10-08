import { test, expect } from '@playwright/test'
import { readFileSync, writeFileSync, renameSync, existsSync, unlinkSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import { join } from 'node:path'
import type { Candidate, Page } from '../src/types/platform'

const directory = process.env.MAZORY_E2E_DIR!
interface Fixture { raw_id: number; trace_id: number; config_id: number; total: number; run_id?: number }
interface Session { access: string; fixtures: Record<string, Fixture> }
interface ProviderCall { target_id: number; source_ids: number[]; phase: string; input_tokens: number; prior_statuses: string[]; pages: number; output_reserve: number }
let session: Session
test.describe.configure({ mode: 'serial' })
test.setTimeout(180000)
function control(value: Record<string, unknown>) {
  const target = join(directory, 'provider-state.json'), temporary = `${target}.${randomUUID()}`
  writeFileSync(temporary, JSON.stringify(value)); renameSync(temporary, target)
}
function calls(id: number): ProviderCall[] {
  const path = join(directory, 'thread-provider.jsonl')
  return existsSync(path) ? readFileSync(path, 'utf8').trim().split('\n').filter(Boolean).map(row => JSON.parse(row) as ProviderCall).filter(row => row.target_id === id) : []
}
test.beforeAll(() => { session = JSON.parse(readFileSync(join(directory, 'thread-session.json'), 'utf8')) })

for (const kind of ['whole', 'chunks', 'cancel', 'restart', 'pages', 'repair', 'output']) {
  test(`full original history: ${kind}`, async ({ page, request }) => {
    const fixture = session.fixtures[kind]!
    const initialCalls = calls(fixture.raw_id).length
    const errorMarker = join(directory, `thread-error-${fixture.raw_id}`)
    if (existsSync(errorMarker)) unlinkSync(errorMarker)
    for (const suffix of ['first', 'later']) if (existsSync(`${errorMarker}-${suffix}`)) unlinkSync(`${errorMarker}-${suffix}`)
    control({ thread_window: ['whole','repair','output'].includes(kind) ? 131072 : 12000,
              ...(kind === 'restart' ? { pause_thread_after_stage: 1 } : {}),
              ...(kind === 'repair' ? { thread_schema_error_once: true } : {}),
              ...(kind === 'chunks' ? { thread_schema_error_per_stage: true } : {}),
              ...(kind === 'output' ? { thread_output_errors: 2 } : {}) })
    await page.goto('/admin/login/?next=/admin/auth/user/')
    await page.getByLabel('Имя пользователя').fill('79990000001')
    await page.getByLabel('Пароль').fill('local-e2e-only')
    await page.getByRole('button', { name: 'Войти' }).click()
    await page.goto(`/admin/api/messageprocessingtrace/${fixture.trace_id}/change/`)
    const form = page.locator('form[action$="/reanalyse/"]')
    const replayForm = { request_key: await form.locator('input[name="request_key"]').inputValue(),
                         csrfmiddlewaretoken: await form.locator('input[name="csrfmiddlewaretoken"]').inputValue() }
    await page.getByRole('button', { name: 'Повторить анализ с полной историей' }).click()
    await expect(page).toHaveURL(/messageprocessingtrace\/\d+\/change\//)
    const submittedTrace = Number(new URL(page.url()).pathname.match(/messageprocessingtrace\/(\d+)\//)![1])
    if (kind === 'restart') {
      await expect.poll(() => existsSync(join(directory, 'thread-paused.json'))).toBe(true)
      const pause: { trace_id: number; state: { stage_trace_ids: number[] } } = JSON.parse(readFileSync(join(directory, 'thread-paused.json'), 'utf8'))
      expect(pause.state.stage_trace_ids).toHaveLength(1)
      writeFileSync(join(directory, 'thread-restart-required'), 'ready')
      await expect.poll(() => existsSync(join(directory, 'thread-restart-done')), { timeout: 60000 }).toBe(true)
      control({ thread_window: 12000 })
    }
    const completed = join(directory, 'thread-completed.json')
    await expect.poll(() => existsSync(completed) ?
      (JSON.parse(readFileSync(completed, 'utf8'))[String(fixture.raw_id)] ?? 0) : 0,
      { timeout: 120000 }).toBeGreaterThanOrEqual(submittedTrace)
    const headers = { Authorization: `Bearer ${session.access}` }
    let fact: Candidate | undefined
    await expect.poll(async () => {
      const response = await request.get('/api/candidates/?status=pending', { headers })
      expect(response.ok()).toBe(true)
      const result: Page<Candidate> = await response.json()
      fact = result.results.find(row => row.proposed_changes.promise_message_id === fixture.raw_id)
      return fact?.proposed_changes.commitment_status
    }, { timeout: 120000 }).toBe(kind === 'cancel' ? 'cancelled' : 'fulfilled')
    expect(fact).toBeDefined()
    expect(fact!.proposed_changes.evidence).toBe('Полный разбор: да, подготовлю смету Альфа завтра')
    expect(fact!.proposed_changes.deadline_message_id).not.toBe(fixture.raw_id)
    const evidence = fact!.proposed_changes.evidence_messages as Array<{ raw_message_id: number; role: string; quote: string }>
    expect(evidence.some(row => row.role === 'request' && row.quote === 'Полный разбор: подготовь смету Альфа')).toBe(true)
    expect(evidence.some(row => row.role === (kind === 'cancel' ? 'cancellation' : 'fulfillment'))).toBe(true)
    expect(evidence.some(row => row.quote === 'Полный разбор: сделаешь завтра?' && row.role === 'promise')).toBe(false)
    const provider = calls(fixture.raw_id).slice(initialCalls)
    if (['whole','repair','output'].includes(kind)) {
      if (kind === 'output') expect(provider.length).toBeGreaterThan(3)
      else expect(provider).toHaveLength(kind === 'whole' ? 1 : 2)
      expect(new Set(provider[0]!.source_ids).size).toBe(fixture.total)
      expect(provider.slice(0, kind === 'output' ? 2 : provider.length).every(row => row.input_tokens > 8192 && new Set(row.source_ids).size === fixture.total)).toBe(true)
      if (kind === 'output') {
        expect(provider[1]!.output_reserve).toBe(8192)
        expect(provider[2]!.source_ids.length).toBeLessThan(fixture.total)
        expect(provider[2]!.input_tokens).toBeLessThan(provider[1]!.input_tokens)
        expect(new Set(provider.flatMap(row => row.source_ids)).size).toBe(fixture.total)
        expect(provider.at(-1)!.phase).toBe('reconcile')
      }
    } else {
      expect(provider.length).toBeGreaterThan(2)
      expect(provider.at(-1)!.phase).toBe('reconcile')
      expect(provider.every(row => row.input_tokens < 12000 - 4096 - 512 + 100)).toBe(true)
      expect(new Set(provider.flatMap(row => row.source_ids)).size).toBe(fixture.total)
      expect(provider.some(row => row.prior_statuses.includes('pending'))).toBe(true)
      if (kind === 'pages') expect(provider.some(row => row.pages > 1)).toBe(true)
      if (kind === 'chunks') for (const suffix of ['first', 'later']) expect(existsSync(`${errorMarker}-${suffix}`)).toBe(true)
    }
    // The final candidate links to the final, genuinely published trace.
    const traceId: number = JSON.parse(readFileSync(completed, 'utf8'))[String(fixture.raw_id)]
    await page.goto(`/admin/api/messageprocessingtrace/${traceId}/context/`)
    await expect(page.getByTestId('history-progress')).toContainText(`Прочитано ${fixture.total} из ${fixture.total}`)
    await expect(page.getByText('Полный разбор завершён: все оригиналы прочитаны и итог сверен.', { exact: true })).toBeVisible()
    if (kind === 'output') await expect(page.getByText(/Очередная часть уменьшена для размера ответа:/)).toBeVisible()
    expect((await request.get('/static/mazory/css/admin_trace.css')).ok()).toBe(true)
    await page.getByTestId('history-progress').scrollIntoViewIfNeeded()
    await page.screenshot({ path: join(directory, `thread-${kind}.png`) })
    if (kind === 'whole') {
      const repeated = await page.request.post(`/admin/api/messageprocessingtrace/${fixture.trace_id}/reanalyse/`,
        { form: replayForm, headers: { Referer: `${process.env.MAZORY_E2E_URL}/admin/` } })
      expect(repeated.ok()).toBe(true)
      expect(calls(fixture.raw_id)).toHaveLength(initialCalls + 1)
      const value: Page<Candidate> = await (await request.get('/api/candidates/?status=pending', { headers })).json()
      expect(value.results.filter(row => row.proposed_changes.promise_message_id === fixture.raw_id)).toHaveLength(1)
    }
  })
}

test('cancelling a run between chunks preserves progress and publishes no incomplete facts', async ({ page, request }) => {
  const fixture = session.fixtures.stop!
  if (existsSync(join(directory, 'thread-paused.json'))) unlinkSync(join(directory, 'thread-paused.json'))
  control({ thread_window: 12000, pause_thread_after_stage: 1 })
  await page.goto('/admin/login/?next=/admin/auth/user/')
  await page.getByLabel('Имя пользователя').fill('79990000001')
  await page.getByLabel('Пароль').fill('local-e2e-only')
  await page.getByRole('button', { name: 'Войти' }).click()
  await page.goto(`/admin/api/whatsapphistoryrun/${fixture.run_id}/change/`)
  await page.getByRole('button', { name: 'Переанализировать сохранённые сообщения' }).click()
  await expect.poll(() => existsSync(join(directory, 'thread-paused.json'))).toBe(true)
  const paused: { history_run_id: number; state: { stage_trace_ids: number[] } } = JSON.parse(readFileSync(join(directory, 'thread-paused.json'), 'utf8'))
  expect(paused.state.stage_trace_ids).toHaveLength(1)
  await page.goto(`/admin/api/whatsapphistoryrun/${paused.history_run_id}/change/`)
  await page.getByRole('button', { name: 'Отменить запуск' }).click()
  await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'cancelled')
  control({ thread_window: 12000 })
  const before = calls(fixture.raw_id).length
  const headers = { Authorization: `Bearer ${session.access}` }
  await expect.poll(async () => {
    const result: Page<Candidate> = await (await request.get('/api/candidates/?status=pending', { headers })).json()
    return result.results.filter(row => row.proposed_changes.promise_message_id === fixture.raw_id).length
  }).toBe(0)
  await page.reload()
  await expect(page.getByTestId('history-progress')).toHaveAttribute('data-state', 'cancelled')
  expect(calls(fixture.raw_id)).toHaveLength(before)
})
