import { test, expect } from '@playwright/test'
import { readFileSync } from './helpers/files'
import { writeFileSync } from 'node:fs'
import { join } from 'node:path'
test.skip(process.env.MAZORY_MESSENGER_PERFORMANCE !== '1', 'Separate real 100k history acceptance after the business suite')
test('100000 real messages: first-page p95 and useful browser screen meet the agreed budget', async ({page,context,request}) => {
  test.setTimeout(120000)
  const directory=process.env.MAZORY_E2E_DIR!
  const fixture=JSON.parse(readFileSync(join(directory,'messenger-performance.json'),'utf8')) as {access:string;refresh:string;cookie_name:string;count:number}
  expect(fixture.count).toBe(100000)
  const timings:Record<string,number[]>={}
  for(const path of ['/api/messenger/dialogs/','/api/messenger/records/?session_name=messenger-performance&chat_id=performance-0%40g.us&category=group']){
    timings[path]=[]
    for(let i=0;i<21;i++) { const start=performance.now(); const response=await request.get(path,{headers:{Authorization:`Bearer ${fixture.access}`}}); expect(response.ok()).toBe(true); const data=await response.json() as {items:unknown[]}; expect(data.items.length).toBeGreaterThan(0); if(i) timings[path]!.push(performance.now()-start) }
  }
  let authenticated=0
  page.on('response',response=>{if(response.url().endsWith('/api/auth/refresh/')&&response.ok())authenticated=performance.now()})
  await context.addCookies([{name:fixture.cookie_name,value:fixture.refresh,url:`${process.env.MAZORY_E2E_URL}/api/auth/`,httpOnly:true,sameSite:'Lax'}])
  await page.goto('/messenger')
  await expect(page.getByLabel('Диалоги',{exact:true}).getByRole('button').filter({hasText:'История для проверки производительности'}).first()).toBeVisible()
  const useful=performance.now()-authenticated
  expect(authenticated).toBeGreaterThan(0)
  const p95=Object.fromEntries(Object.entries(timings).map(([key,values])=>[key,[...values].sort((a,b)=>a-b)[18]!]))
  writeFileSync(join(directory,'messenger-performance-results.json'),JSON.stringify({count:fixture.count,samples:20,p95_ms:p95,useful_screen_ms:useful},null,2))
  for(const value of Object.values(p95)) expect(value).toBeLessThan(1000)
  expect(useful).toBeLessThan(2000)
  await page.getByRole('button',{name:'Ещё диалоги',exact:true}).click()
  await expect(page.getByLabel('Диалоги',{exact:true}).getByRole('button').filter({hasText:'История для проверки производительности'})).toHaveCount(100)
  const seen=new Set<string>()
  let cursor:string|null=null
  do {
    const result=await (await request.get('/api/messenger/dialogs/?limit=17'+(cursor?'&cursor='+encodeURIComponent(cursor):''),{headers:{Authorization:`Bearer ${fixture.access}`}})).json() as {items:{key:string}[];next_cursor:string|null}
    for(const row of result.items){expect(seen.has(row.key)).toBe(false);seen.add(row.key)}
    cursor=result.next_cursor
  } while(cursor)
  expect(seen.size).toBe(100)
  await page.screenshot({path:join(directory,'playwright/messenger-100k.png')})
  const original='История для проверки производительности: часть 0, сообщение 0'
  const found=await (await request.get('/api/messenger/records/?q='+encodeURIComponent(original),{headers:{Authorization:`Bearer ${fixture.access}`}})).json() as {items:{id:number;body:string}[]}
  expect(found.items).toHaveLength(1)
  await page.getByLabel('Поиск по чатам',{exact:true}).fill(original)
  const match=page.getByLabel('Диалоги',{exact:true}).getByRole('button').filter({hasText:original})
  await expect(match).toHaveCount(1)
  await match.click()
  await expect.poll(()=>new URL(page.url()).searchParams.get('record')).toBe(String(found.items[0]!.id))
  await expect(page.getByRole('region',{name:'Лента сообщений'})).toContainText(original)
  await page.reload()
  await expect(page.getByRole('region',{name:'Лента сообщений'})).toContainText(original)
})
