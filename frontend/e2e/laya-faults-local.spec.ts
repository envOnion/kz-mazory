import { readFileSync } from './helpers/files'
import { test, expect } from '@playwright/test'
import { writeFileSync, renameSync } from 'node:fs'
import { createHmac, randomUUID } from 'node:crypto'
import { join } from 'node:path'

test.skip(process.env.MAZORY_E2E_LAYA !== '1', 'Explicit real LAYA Docker profile required')
test.setTimeout(300000)
const dir = process.env.MAZORY_E2E_DIR!
interface Proof {
  messages: { id: number; message_id: string; processed: boolean }[]
  routing: { raw_message_id: number; outcome: string; reason_code: string }[]
}
const proof = (): Proof => JSON.parse(readFileSync(join(dir, 'laya-proof.json'), 'utf8'))

test('invalid probabilities, changed checkpoint, truncated input and timeout return to the real LLM', async ({ request }) => {
  // The preceding routing scenario created this topic with the real LLM.
  expect(proof().messages.find(m => m.message_id === 'laya-routing-1')?.processed).toBe(true)
  const cases = [
    { fault: 'probabilities', reason: 'laya_invalid_response' },
    { fault: 'revision', reason: 'laya_model_changed' },
    { fault: 'truncation', reason: 'laya_input_truncated' },
    { fault: 'timeout', reason: 'laya_unavailable' },
  ]
  for (const row of cases) {
    const nonce = randomUUID(), path = join(dir, 'laya-control.json'), temporary = `${path}.${nonce}`
    writeFileSync(temporary, JSON.stringify({ nonce, mode: 'laya', laya_fault: row.fault }))
    renameSync(temporary, path)
    await expect.poll(() => JSON.parse(readFileSync(join(dir, 'laya-control-applied.json'), 'utf8')).nonce).toBe(nonce)
    const id = `laya-fault-${row.fault}-${nonce}`
    const data = JSON.stringify({ event: 'message', session: 'default', payload: {
      id, body: 'По объекту Независимый объект 101 поставку насосов перенесли на пятницу. Обсуждение с заказчиком продолжается.',
      from: 'laya-synthetic-routing@g.us', participant: '989999@lid', notifyName: 'Синтетический менеджер', timestamp: Math.floor(Date.now() / 1000),
    } })
    const response = await request.post('/api/whatsapp/webhook/', { data, headers: {
      'Content-Type': 'application/json', 'X-Webhook-Hmac': createHmac('sha512', 'isolated-local-webhook-secret').update(data).digest('hex'),
    } })
    expect([200, 202]).toContain(response.status())
    await expect.poll(() => proof().messages.find(m => m.message_id === id)?.processed, { timeout: 90000 }).toBe(true)
    const p = proof(), raw = p.messages.find(m => m.message_id === id)!
    expect(p.routing.find(r => r.raw_message_id === raw.id)).toMatchObject({ outcome: 'llm', reason_code: row.reason })
  }
  const path = join(dir, 'laya-control.json'), temporary = `${path}.restore`
  writeFileSync(temporary, JSON.stringify({ nonce: randomUUID(), mode: 'laya' }))
  renameSync(temporary, path)
  writeFileSync(join(dir, 'laya-results/faults.json'), JSON.stringify(proof(), null, 2))
})
