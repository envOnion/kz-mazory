import { test, expect } from '@playwright/test'
import { readFileSync, writeFileSync, existsSync, renameSync, unlinkSync } from 'node:fs'
import { createHmac, randomUUID } from 'node:crypto'
import { join } from 'node:path'
import type { DialogueThread } from '../src/types/factReview'
import type { Page, Candidate } from '../src/types/platform'

test.skip(process.env.MAZORY_E2E_LAYA !== '1', 'Explicit private read-only Docker profile required')
test.setTimeout(1800000)
const dir = process.env.MAZORY_E2E_DIR!
interface Proof {
  messages: { id: number; chat_id: string; message_id: string; content: string; processed: boolean; processing_state: string }[]
  routing: { raw_message_id: number; outcome: string; reason_code: string; scores: { thread_id: number; probability: number }[]; model_revision: string }[]
  events: { id: number; event_type: string; state: string; payload: { raw_id?: number; thread_id?: number } }[]
  usage: { outbox_event_id: number | null; operation: string; input_tokens: number | null }[]
}
function proof(): Proof { return JSON.parse(readFileSync(join(dir,'laya-proof.json'),'utf8')) }
function control(data: Record<string,unknown>) {
  const nonce = randomUUID(), path = join(dir,'laya-control.json'), tmp = `${path}.${nonce}`
  writeFileSync(tmp,JSON.stringify({nonce,...data})); renameSync(tmp,path)
  return nonce
}

test('LLM topics, real LAYA assignment, durable extraction, shadow, outage and stale descriptor', async ({ request, page, context }) => {
  const session: { access: string; refresh: string; cookie_name: string } = JSON.parse(readFileSync(join(dir,'laya-session.json'),'utf8'))
  const headers = { Authorization: `Bearer ${session.access}` }
  await context.addCookies([{name:session.cookie_name,value:session.refresh,url:`${process.env.MAZORY_E2E_URL}/api/auth/`,httpOnly:true,sameSite:'Lax'}])
  const group = 'laya-synthetic-routing@g.us'
  let ordinal = 0
  async function message(body: string) {
    const id = `laya-routing-${++ordinal}`
    const data = JSON.stringify({event:'message',session:'default',payload:{id,body,from:group,participant:'989999@lid',notifyName:'Синтетический менеджер',timestamp:1791792000+ordinal}})
    const response = await request.post('/api/whatsapp/webhook/',{data,headers:{'Content-Type':'application/json','X-Webhook-Hmac':createHmac('sha512','isolated-local-webhook-secret').update(data).digest('hex')}})
    expect([200,202]).toContain(response.status())
    return id
  }
  async function processed(id: string) { await expect.poll(()=>proof().messages.find(m=>m.message_id===id)?.processed,{timeout:180000}).toBe(true) }
  async function applied(value: Record<string,unknown>) {
    const nonce = control(value)
    await expect.poll(()=>existsSync(join(dir,'laya-control-applied.json'))?JSON.parse(readFileSync(join(dir,'laya-control-applied.json'),'utf8')).nonce:null).toBe(nonce)
    return nonce
  }
  async function threads() {
    const result: Page<DialogueThread> = await (await request.get('/api/threads/?page_size=100',{headers})).json()
    return result.results.filter(t=>t.chat_name==='LAYA synthetic routing')
  }
  async function route(id: string) {
    await expect.poll(()=>{ const p=proof(), raw=p.messages.find(m=>m.message_id===id); return p.routing.find(r=>r.raw_message_id===raw?.id)?.outcome },{timeout:180000}).toMatch(/^(laya|llm)$/)
    const p=proof(),raw=p.messages.find(m=>m.message_id===id)!
    return p.routing.find(r=>r.raw_message_id===raw.id)!
  }
  await applied({mode:'laya'})
  const initial = await message('По объекту Независимый объект 101 поставку насосов согласовали на следующую неделю. Обсуждение с заказчиком по этому объекту продолжается.')
  await processed(initial)
  expect((await route(initial)).reason_code).toBe('no_prepared_topic')
  const prepared = (await threads()).filter(t=>t.routing_state==='prepared')
  expect(prepared.length).toBeGreaterThan(0)
  const topic = prepared.find(t=>/101/.test(t.topic))!
  expect(topic).toBeDefined()
  const update = await message('По объекту Независимый объект 101 поставку насосов перенесли на пятницу. Обсуждение с заказчиком продолжается.')
  const assigned = await route(update)
  expect(assigned.outcome).toBe('laya')
  expect(assigned.model_revision).toBe('f5252e332072ff175e5fc7e31b8e3c3ca29a0c6a46cb22b8489b902e9a2ead49')
  expect(assigned.scores[0]?.thread_id).toBe(topic.id)
  expect(proof().messages.find(m=>m.message_id===update)?.processed).toBe(false)
  writeFileSync(join(dir,'laya-restart-required'),'pending extraction is durable')
  await expect.poll(()=>existsSync(join(dir,'laya-restart-done')),{timeout:60000}).toBe(true)
  const flush = await request.post(`/api/threads/${topic.id}/flush/`,{headers})
  expect([200,202]).toContain(flush.status())
  await processed(update)
  const detail: DialogueThread = await (await request.get(`/api/threads/${topic.id}/`,{headers})).json()
  expect(detail.last_extracted_routing_generation).toBe(detail.routing_generation)
  expect(detail.extraction_pending).toBe(false)
  expect(detail.messages.some(m=>m.assignment_method==='laya')).toBe(true)
  await applied({mode:'shadow'})
  const shadow = await message('По объекту Независимый объект 101 поставку насосов перенесли на пятницу. Обсуждение с заказчиком продолжается.')
  expect((await route(shadow)).reason_code).toBe('shadow')
  await processed(shadow)
  await applied({mode:'laya',laya_outage:true})
  const outage = await message('По объекту Независимый объект 101 поставку насосов перенесли на пятницу. Обсуждение с заказчиком продолжается.')
  expect((await route(outage)).reason_code).toBe('laya_unavailable')
  await processed(outage)
  const nonce = await applied({mode:'laya',laya_delay:true})
  const stale = await message('По объекту Независимый объект 101 поставку насосов перенесли на пятницу. Обсуждение с заказчиком продолжается.')
  await expect.poll(()=>existsSync(join(dir,'laya-provider-started.json'))?JSON.parse(readFileSync(join(dir,'laya-provider-started.json'),'utf8')).nonce:null).toBe(nonce)
  await applied({mode:'laya',bump_thread:topic.id})
  expect((await route(stale)).reason_code).toBe('stale_snapshot')
  await processed(stale)
  await applied({mode:'laya'})
  await page.goto('/')
  await page.getByRole('button',{name:'Рабочий кабинет',exact:true}).click()
  await page.getByRole('button',{name:'Темы переписки',exact:true}).click()
  await page.getByPlaceholder('Поиск по теме, компании, сделке...').fill('101')
  await page.getByRole('button',{name:'Найти',exact:true}).click()
  await page.getByRole('button',{name:'Развернуть всё',exact:true}).click()
  await page.locator('strong').filter({hasText:`#${topic.id}:`}).click()
  await expect(page.getByTestId('thread-routing-status')).toBeVisible()
  await page.screenshot({path:join(dir,'laya-results/routing.png'),fullPage:true})
  writeFileSync(join(dir,'laya-results/routing.json'),JSON.stringify(proof(),null,2))
})

test('same 100 messages and Gemma retain object facts while reducing measured LLM input tokens',async ({request})=>{
  test.setTimeout(3600000)
  const session:{access:string}=JSON.parse(readFileSync(join(dir,'laya-session.json'),'utf8'))
  const headers={Authorization:`Bearer ${session.access}`}
  const cases=['baseline','economy'] as const
  const reports:{kind:string;tokens:number;messages:number;facts:Candidate[];laya:number}[]=[]
  const objects=[101,102,103]
  for(const kind of cases){
    let seq=0
    async function send(body:string){
      const id=`laya-${kind}-${++seq}`
      const data=JSON.stringify({event:'message',session:'default',payload:{id,body,from:`laya-synthetic-${kind}@g.us`,participant:'979999@lid',notifyName:'Синтетический менеджер',timestamp:Math.floor(Date.now()/1000)-120+seq}})
      const response=await request.post('/api/whatsapp/webhook/',{data,headers:{'Content-Type':'application/json','X-Webhook-Hmac':createHmac('sha512','isolated-local-webhook-secret').update(data).digest('hex')}})
      expect([200,202]).toContain(response.status())
      return id
    }
    const inputs=[...objects.map(id=>`По объекту Независимый объект ${id} поставку насосов согласовали на следующую неделю. Обсуждение с заказчиком по этому объекту продолжается.`),...Array.from({length:100},(_,i)=>`По объекту Независимый объект ${objects[i%3]} поставку насосов перенесли на пятницу. Обсуждение с заказчиком продолжается.`)]
    const existing=proof().messages.filter(m=>m.chat_id===`laya-synthetic-${kind}@g.us`)
    if(existing.length){
      // Resume only the exact saved input, retaining every recorded model call.
      expect(existing).toHaveLength(inputs.length)
      for(let i=0;i<inputs.length;i++) expect(existing.find(m=>m.message_id===`laya-${kind}-${i+1}`)?.content).toBe(inputs[i])
    }else{
      for(const body of inputs.slice(0,3)){
        const raw=await send(body)
        await expect.poll(()=>proof().messages.find(m=>m.message_id===raw)?.processed,{timeout:180000}).toBe(true)
      }
      writeFileSync(join(dir,'laya-pause'),'identical arrival window for both modes')
      try{for(const body of inputs.slice(3)) await send(body)}finally{unlinkSync(join(dir,'laya-pause'))}
    }
    await expect.poll(()=>{const messages=proof().messages.filter(m=>m.chat_id===`laya-synthetic-${kind}@g.us`);return messages.length===103&&messages.every(m=>m.processed)},{timeout:1800000,intervals:[2000,5000]}).toBe(true)
    const p=proof()
    const updateKeys=new Set(Array.from({length:100},(_,i)=>`laya-${kind}-${i+4}`))
    const ids=new Set(p.messages.filter(m=>m.chat_id===`laya-synthetic-${kind}@g.us`&&updateKeys.has(m.message_id)).map(m=>m.id))
    expect(ids.size).toBe(100)
    const eventIds=new Set(p.events.filter(e=>ids.has(e.payload.raw_id??0)).map(e=>e.id))
    const usage=p.usage.filter(u=>u.operation==='chat'&&u.outbox_event_id!==null&&eventIds.has(u.outbox_event_id))
    expect(usage.length).toBeGreaterThan(0)
    expect(usage.every(u=>u.input_tokens!==null)).toBe(true)
    const facts:Candidate[]=[]
    for(const status of ['pending','approved']){
      let next:string|null=`/api/candidates/?status=${status}`
      while(next){const result:Page<Candidate>=await(await request.get(next,{headers})).json();facts.push(...result.results.filter(c=>c.chat_name===`LAYA synthetic ${kind}`));next=result.next}
    }
    for(const id of objects){
      expect(facts.some(c=>c.fact_type==='project'&&String(c.proposed_changes.object_name).includes(String(id))&&/пятниц/i.test(String(c.proposed_changes.current_action)+' '+String(c.proposed_changes.next_action)))).toBe(true)
    }
    expect(facts.some(c=>c.fact_type==='payment')).toBe(false)
    reports.push({kind,tokens:usage.reduce((sum,u)=>sum+(u.input_tokens??0),0),messages:100,facts,laya:p.routing.filter(r=>ids.has(r.raw_message_id)&&r.outcome==='laya').length})
  }
  const reduction=1-reports[1]!.tokens/reports[0]!.tokens
  writeFileSync(join(dir,'laya-results/economy.json'),JSON.stringify({model:'gemma4:e4b',context_window_tokens:65536,output_tokens:8192,all_retry_usage_included:true,reports,reduction},null,2))
  expect(reports[1]!.laya).toBeGreaterThanOrEqual(40)
  expect(reduction).toBeGreaterThanOrEqual(.30)
})
