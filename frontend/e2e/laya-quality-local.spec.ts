import { readFileSync } from './helpers/files'
import { test,expect } from '@playwright/test'
import { writeFileSync, existsSync, renameSync } from 'node:fs'
import { createHmac,randomUUID } from 'node:crypto'
import { join } from 'node:path'

test.skip(process.env.MAZORY_E2E_LAYA!=='1'||process.env.MAZORY_E2E_QUALITY!=='1','Independent real-model Docker profile required')
test.setTimeout(3600000)
const dir=process.env.MAZORY_E2E_DIR!
interface Fixture { index:number;group:string;state:{message:string;sender:string};expected_thread:number|null;thread_ids:number[];dialogue_id:string }
interface Proof {
  messages:{id:number;message_id:string;processed:boolean}[]
  routing:{raw_message_id:number;outcome:string;reason_code:string;latency_ms:number;scores:{thread_id:number;probability:number}[]}[]
}

test('400 held-out membership decisions use the genuine API, database, Q2 and LAYA',async ({request})=>{
  const nonce=randomUUID(),path=join(dir,'laya-control.json'),temp=`${path}.${nonce}`
  writeFileSync(temp,JSON.stringify({nonce,mode:'laya',seed_quality:true}));renameSync(temp,path)
  await expect.poll(()=>existsSync(join(dir,'laya-quality-ready.json')),{timeout:120000}).toBe(true)
  const rows:Fixture[]=JSON.parse(readFileSync(join(dir,'laya-quality-ready.json'),'utf8'))
  expect(rows).toHaveLength(400)
  for(const row of rows){
    const data=JSON.stringify({event:'message',session:'default',payload:{id:`laya-quality-input-${row.index}`,body:row.state.message,from:row.group,participant:'969999@lid',notifyName:row.state.sender,timestamp:Math.floor(Date.now()/1000)-5}})
    const response=await request.post('/api/whatsapp/webhook/',{data,headers:{'Content-Type':'application/json','X-Webhook-Hmac':createHmac('sha512','isolated-local-webhook-secret').update(data).digest('hex')}})
    expect([200,202]).toContain(response.status())
    await new Promise(resolve=>setTimeout(resolve,70))
  }
  const proof=():Proof=>JSON.parse(readFileSync(join(dir,'laya-proof.json'),'utf8'))
  await expect.poll(()=>{const p=proof();return rows.every(row=>{const raw=p.messages.find(m=>m.message_id===`laya-quality-input-${row.index}`);return raw&&p.routing.some(r=>r.raw_message_id===raw.id&&r.scores.length===row.thread_ids.length&&['laya','llm_corrected','llm','llm_pending'].includes(r.outcome))})},{timeout:3300000,intervals:[5000,10000]}).toBe(true)
  const p=proof()
  const decisions=rows.map(row=>{
    const raw=p.messages.find(m=>m.message_id===`laya-quality-input-${row.index}`)!,attempt=p.routing.find(r=>r.raw_message_id===raw.id)!
    return {dialogue_id:row.dialogue_id,expected:row.expected_thread===null?null:row.thread_ids[row.expected_thread],selected:['laya','llm_corrected'].includes(attempt.outcome)?attempt.scores[0]?.thread_id:null,...attempt}
  })
  const accepted=decisions.filter(d=>d.selected!==null),correct=accepted.filter(d=>d.selected===d.expected).length
  const precision=correct/accepted.length,coverage=accepted.length/rows.length,n=accepted.length,z=1.96
  const centre=(precision+z*z/(2*n))/(1+z*z/n),radius=z*Math.sqrt(precision*(1-precision)/n+z*z/(4*n*n))/(1+z*z/n)
  const latency=accepted.map(d=>d.latency_ms).sort((a,b)=>a-b),p95=latency[Math.ceil(latency.length*.95)-1]!
  const extraction_completed=rows.filter(row=>p.messages.find(m=>m.message_id===`laya-quality-input-${row.index}`)?.processed).length
  writeFileSync(join(dir,'laya-results/quality-e2e.json'),JSON.stringify({scope:'membership decisions; fact extraction is verified separately',total:rows.length,accepted:n,correct,precision,coverage,precision_wilson95:[centre-radius,centre+radius],p95_ms:p95,extraction_completed,decisions},null,2))
  expect(precision).toBeGreaterThanOrEqual(.98)
  expect(coverage).toBeGreaterThanOrEqual(.40)
  expect(p95).toBeLessThanOrEqual(1000)
})
