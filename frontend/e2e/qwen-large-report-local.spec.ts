import { test, expect } from '@playwright/test'
import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { createHmac } from 'node:crypto'
import type { Candidate, Page } from '../src/types/platform'

test.skip(process.env.MAZORY_E2E_MODEL !== 'qwen3.8-27b' || process.env.QWEN_COMPACT_EVIDENCE !== '1', 'Explicit real compact-evidence provider verification')
test.setTimeout(1800000)
test('large original report completes with compact references and retains full literal evidence', async ({ request }) => {
  const dir = process.env.MAZORY_E2E_DIR!
  const input: { content: string; timestamp: number; sender: string } = JSON.parse(readFileSync(join(dir,'large-report-input.json'),'utf8'))
  const session: { access: string } = JSON.parse(readFileSync(join(dir,'laya-session.json'),'utf8'))
  const headers = {Authorization:`Bearer ${session.access}`}
  writeFileSync(join(dir,'laya-control.json'),JSON.stringify({nonce:'compact-large-source',mode:'llm',live_crm:false}))
  const body = JSON.stringify({event:'message',session:'default',payload:{id:'laya-compact-large-report',body:input.content,from:'laya-synthetic-baseline@g.us',participant:'979999@lid',notifyName:input.sender,timestamp:input.timestamp}})
  expect((await request.post('/api/whatsapp/webhook/',{data:body,headers:{'Content-Type':'application/json','X-Webhook-Hmac':createHmac('sha512','isolated-local-webhook-secret').update(body).digest('hex')}})).ok()).toBe(true)
  let sourceId: number | undefined
  await expect.poll(() => {
    const p: { messages: {id:number;message_id:string;processed:boolean}[] } = JSON.parse(readFileSync(join(dir,'laya-proof.json'),'utf8'))
    const source = p.messages.find(row=>row.message_id==='laya-compact-large-report'); sourceId=source?.id
    return source?.processed
  },{timeout:1200000,intervals:[2000,5000]}).toBe(true)
  const facts: Candidate[]=[]
  for(const status of ['pending','approved','rejected']) {
    let next: string | null = `/api/candidates/?status=${status}`
    while(next) {
      const response: Page<Candidate> = await (await request.get(next,{headers})).json()
      facts.push(...response.results.filter(row=>row.proposed_changes.evidence_message_id===sourceId))
      next=response.next
    }
  }
  expect(facts.filter(row=>row.fact_type==='project').length).toBeGreaterThanOrEqual(8)
  expect(facts.every(row=>row.proposed_changes.contract_amount===undefined)).toBe(true)
  for(const fact of facts) {
    const primary=fact.evidence.find(row=>row.source_id===sourceId)
    expect(primary?.quote).toBe(input.content)
  }
  const p: {traces:{raw_message_id:number;status:string;error_code:string;context_metadata__response_diagnostics:{response_characters?:number}}[]} = JSON.parse(readFileSync(join(dir,'laya-proof.json'),'utf8'))
  const trace=p.traces.find(row=>row.raw_message_id===sourceId && row.status==='success')!
  expect(trace).toBeTruthy()
  expect(trace.error_code).toBe('')
  // Structured fields for a large report may exceed twice the source length.
  // The regression is repeating the entire source for each extracted fact;
  // references must at least halve that unavoidable full-copy lower bound.
  expect(trace.context_metadata__response_diagnostics.response_characters).toBeLessThan(input.content.length*facts.length/2)
  writeFileSync(join(dir,'laya-results/compact-large-report.json'),JSON.stringify({input,sourceId,facts,trace},null,2))
})
