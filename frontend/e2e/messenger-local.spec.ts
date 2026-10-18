import { readFileSync } from './helpers/files'
import { test, expect, type BrowserContext } from '@playwright/test'
import { writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { createHash, createHmac, randomUUID } from 'node:crypto'
import type { Paged, RecordBrief, RecordDetail, QueueJob, AnalyticsOperations } from '../src/types/messenger'
import type { SettingsPage } from '../src/types/settings'
import type { Directory } from '../src/types/platform'
const directory = process.env.MAZORY_E2E_DIR
if (!directory) throw new Error('Use the isolated Docker E2E stack')
interface Session { access: string; refresh: string; cookie_name: string; user_id: number }
interface Fixtures { otp:{phone:string;user_id:number};team_lead: Session; manager: Session; finance: Session; client: Session; integrator: Session; mfa: Session; admin: Session; records: {allowed:number; foreign:number; private:number}; config_id:number;private_raw_id:number;private_reply_record_id:number }
const fixture: Fixtures = JSON.parse(readFileSync(join(directory, 'messenger-session.json'), 'utf8'))
test.describe.configure({mode:'serial'})
test.setTimeout(120000)
const headers = (session:Session) => ({Authorization:`Bearer ${session.access}`})
async function authenticate(context:BrowserContext, session:Session) {
  const fresh:Fixtures = JSON.parse(readFileSync(join(directory!, 'messenger-session.json'),'utf8'))
  for (const role of ['admin','client'] as const) if (fresh[role].user_id === session.user_id) session.refresh = fresh[role].refresh
  context.on('response', async response => {
    if (response.url().endsWith('/api/auth/refresh/') && response.request().method() === 'POST' && response.ok()) {
      await response.finished()
      const cookies = await context.cookies(); const cookie = cookies.find(c => c.name === session.cookie_name)
      if (cookie) {
        session.refresh = cookie.value
        const path = join(directory!, 'messenger-session.json')
        const data:Fixtures = JSON.parse(readFileSync(path,'utf8'))
        for (const role of ['admin','client'] as const) if (data[role].user_id === session.user_id) data[role].refresh = cookie.value
        writeFileSync(path,JSON.stringify(data))
      }
    }
  })
  await context.addCookies([{name:session.cookie_name,value:session.refresh,url:`${process.env.MAZORY_E2E_URL}/api/auth/`,httpOnly:true,sameSite:'Lax'}])
}

test('messenger shell, direct links, six sections, responsive panels and settings persist through real API', async ({page,context,request}) => {
  await authenticate(context,fixture.admin)
  const browserErrors:string[] = []; page.on('pageerror', error => browserErrors.push(error.message))
  await page.goto('/')
  await expect(page).toHaveURL(/\/messenger/)
  const rail = page.getByRole('navigation',{name:'Разделы',exact:true})
  for (const label of ['Чаты','Аналитика','Треды','Очередь','Изменения CRM','Настройки']) await expect(rail.getByRole('link',{name:label,exact:true})).toBeVisible()
  await page.getByRole('button',{name:'Найти раздел',exact:true}).click()
  await page.getByRole('textbox',{name:'Название раздела'}).fill('Треды')
  await expect(page.getByRole('navigation',{name:'Результаты поиска разделов'}).getByRole('link')).toHaveText('Треды')
  await page.keyboard.press('Escape')
  await page.goto(`/messenger?record=${fixture.records.allowed}`)
  await expect(page.getByRole('region',{name:'Лента сообщений'})).toContainText('Messenger E2E allowed')
  await page.getByRole('button',{name:'История',exact:true}).click()
  await expect(page).toHaveURL(/tab=history/)
  await page.reload(); await expect(page.getByLabel('Инспектор',{exact:true})).toContainText('Получено')
  for (const width of [320,390,768,1280,1680]) {
    await page.setViewportSize({width,height:980})
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    if (width < 1130) {
      await page.getByRole('button',{name:'Открыть инспектор'}).click()
      await expect(page.getByLabel('Инспектор',{exact:true})).toBeVisible()
      await page.keyboard.press('Escape')
      await expect(page.getByLabel('Инспектор',{exact:true})).toBeHidden()
    }
    await page.screenshot({path:join(directory,`playwright/messenger-${width}.png`)})
  }
  for (const path of ['/analytics','/threads','/queue','/crm-changes','/settings','/profile']) {
    await page.goto(path); await expect(page.locator('[data-main-region]')).toBeVisible(); await expect(page.getByRole('alert')).toHaveCount(0); await page.reload()
    await expect(page.locator('[data-main-region]')).toBeVisible()
  }
  await page.goto('/settings?section=whatsapp')
  const chatSettings=page.locator('article').filter({has:page.getByRole('heading',{name:'Messenger E2E: чат A',exact:true})})
  await chatSettings.getByRole('button',{name:'История и импорт',exact:true}).click()
  await expect(page).toHaveURL(new RegExp(`config=${fixture.config_id}`))
  await page.reload()
  await expect(page.getByRole('heading',{name:'История WhatsApp',exact:true})).toBeVisible()
  await page.goto('/settings?section=access')
  const teams=await (await request.get('/api/settings/access/grants/',{headers:headers(fixture.admin)})).json() as {teams:{id:number;name:string;low_margin_percent:string}[]}
  const team=teams.teams.find(row=>row.name==='Messenger E2E: команда A')!
  const teamForm=page.locator('details').filter({has:page.locator('summary',{hasText:`Параметры команды · ${team.name}`})})
  await teamForm.locator('summary').click()
  await teamForm.getByLabel('Порог низкой маржи, %',{exact:true}).fill('16.25')
  const changed=page.waitForResponse(r=>r.url().includes('/api/settings/access/grants/?team=') && r.request().method()==='PATCH')
  await teamForm.getByRole('button',{name:'Сохранить настройки',exact:true}).click()
  expect((await changed).status()).toBe(200)
  const teamEndpoint=`/api/settings/access/grants/?team=${team.id}`
  expect((await request.patch(teamEndpoint,{headers:headers(fixture.admin),data:{low_margin_percent:'101'}})).status()).toBe(400)
  const persisted=await (await request.get('/api/settings/access/grants/',{headers:headers(fixture.admin)})).json() as typeof teams
  expect(persisted.teams.find(row=>row.id===team.id)?.low_margin_percent).toBe('16.25')
  expect((await request.patch(teamEndpoint,{headers:headers(fixture.admin),data:{low_margin_percent:team.low_margin_percent}})).ok()).toBe(true)
  await page.goto('/settings?section=ai')
  const before:SettingsPage = await (await request.get('/api/settings/ai/',{headers:headers(fixture.admin)})).json()
  const config = before.items[0]!
  expect(before.fields.map(field=>field.name)).toEqual(expect.arrayContaining(['routing_policy_version','autonomous_policy_version']))
  expect(typeof config.routing_policy_version).toBe('string')
  expect(typeof config.autonomous_policy_version).toBe('string')
  await expect(page.getByLabel('Версия политики распределения по темам',{exact:true}).first()).toHaveValue(String(config.routing_policy_version))
  await expect(page.getByLabel('Версия политики автономной обработки',{exact:true}).first()).toHaveValue(String(config.autonomous_policy_version))
  await page.getByLabel('Температура',{exact:true}).first().fill('0.35')
  await page.getByRole('button',{name:'Сохранить настройки',exact:true}).first().click()
  await expect.poll(async () => {
    const data:SettingsPage = await (await request.get('/api/settings/ai/',{headers:headers(fixture.admin)})).json()
    return data.items.find(row => row.id === config.id)?.chat_temperature
  }).toBe(0.35)
  const endpoint = `/api/settings/ai/${config.id}/`
  for (const invalid of [{chat_temperature:2.5},{daily_request_limit:1.5},{autonomous_enabled:'false'},
    {daily_budget_usd:'NaN'},{laya_match_threshold:'NaN'},{thread_extract_batch_size:31},
    {chat_api_format:'unknown'},{chat_provider_url:'http://localhost'}, {context_window_tokens:1}]) {
    expect((await request.patch(endpoint,{headers:headers(fixture.admin),data:invalid})).status()).toBe(400)
  }
  expect((await request.patch(endpoint,{headers:headers(fixture.admin),data:{chat_temperature:config.chat_temperature}})).ok()).toBe(true)
  expect(browserErrors).toEqual([])
})

test('transferred finance forms persist schedules and allocations, and CSV is produced by real Q2', async ({page,context,request}) => {
  await authenticate(context,fixture.admin)
  const h=headers(fixture.admin)
  const catalog:Directory=await (await request.get('/api/directory/',{headers:h})).json()
  const before=await (await request.get('/api/finance/',{headers:h})).json() as {payments:{id:number;project_id:number;amount:string}[]}
  const payment=before.payments.find(p=>p.amount==='80.00')!
  const project=catalog.projects.find(p=>p.id===payment.project_id)!
  await page.goto('/analytics?view=finance')
  const form=page.locator('form').filter({has:page.getByRole('heading',{name:'Добавить срок оплаты',exact:true})})
  await form.getByLabel('Проект',{exact:true}).selectOption(String(project.id))
  await form.getByLabel('Срок',{exact:true}).fill('2026-10-25')
  await form.getByLabel('Сумма',{exact:true}).fill('1.25')
  const saved=page.waitForResponse(r=>r.url().endsWith('/api/finance/schedules/') && r.request().method()==='POST')
  await form.getByRole('button',{name:'Добавить срок',exact:true}).click()
  const response=await saved; expect(response.status()).toBe(201)
  const schedule=await response.json() as {id:number}
  const summary=await (await request.get('/api/finance/',{headers:h})).json() as {payments:{id:number;project_id:number}[];schedules:{id:number;amount:string}[]}
  expect(summary.schedules.find(s=>s.id===schedule.id)?.amount).toBe('1.25')
  const allocation=page.locator('form').filter({has:page.getByRole('heading',{name:'Распределить оплату',exact:true})})
  await allocation.getByLabel('Оплата',{exact:true}).selectOption(String(payment.id))
  await allocation.getByLabel('Срок графика',{exact:true}).selectOption(String(schedule.id))
  await allocation.getByLabel('Сумма',{exact:true}).fill('1.25')
  const allocated=page.waitForResponse(r=>r.url().endsWith('/api/finance/allocations/') && r.request().method()==='POST')
  await allocation.getByRole('button',{name:'Распределить',exact:true}).click()
  expect((await allocated).status()).toBe(201)
  const download=page.waitForEvent('download')
  await page.getByRole('button',{name:'Экспорт оплат в CSV',exact:true}).click()
  expect((await download).suggestedFilename()).toBe('payments.csv')
  await expect(page.getByRole('status')).toContainText('Экспорт сформирован')
  expect((await request.post('/api/finance/schedules/',{headers:headers(fixture.client),data:{project_id:project.id,due_date:'2026-10-25',amount:'1'}})).status()).toBe(404)
})

test('attachments cross the real upload, shared storage, Q2 and media provider; publication preserves client scope', async ({page,context,request}) => {
  await authenticate(context,fixture.admin)
  const h=headers(fixture.admin)
  const catalog:Directory=await (await request.get('/api/directory/',{headers:h})).json()
  const project=catalog.projects.find(row=>row.name==='Messenger E2E: клиентский объект')!
  const bytes=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aB9sAAAAASUVORK5CYII=','base64')
  await page.goto('/analytics?view=documents')
  await page.getByLabel('Проект вложения',{exact:true}).selectOption(String(project.id))
  await page.locator('input[type=file]').setInputFiles({name:'e2e.png',mimeType:'image/png',buffer:bytes})
  const uploaded=page.waitForResponse(r=>r.url().endsWith('/api/attachments/') && r.request().method()==='POST')
  await page.getByRole('button',{name:'Загрузить вложение',exact:true}).click()
  const response=await uploaded; expect(response.status()).toBe(202)
  const item=await response.json() as {id:number}
  const endpoint=`/api/attachments/${item.id}/`
  await expect.poll(async()=> (await (await request.get(endpoint,{headers:h})).json() as {state:string}).state).toBe('ready')
  const provider=readFileSync(join(directory!,'media-provider-requests.jsonl'),'utf8').trim().split('\n').map(line=>JSON.parse(line) as {sha256:string;bytes:number})
  expect(provider).toContainEqual({sha256:createHash('sha256').update(bytes).digest('hex'),bytes:bytes.length})
  expect((await request.get(endpoint,{headers:headers(fixture.client)})).status()).toBe(404)
  expect((await request.get(endpoint,{headers:headers(fixture.manager)})).status()).toBe(404)
  const row=page.locator(`[data-attachment-id="${item.id}"]`)
  await row.getByRole('button',{name:'Извлечённый текст',exact:true}).click()
  await expect(page.getByText('Документ E2E: переданы проверенные оригиналы вложения.',{exact:true})).toBeVisible()
  const downloaded=page.waitForEvent('download')
  await row.getByRole('button',{name:'Скачать',exact:true}).click()
  expect((await downloaded).suggestedFilename()).toBe(`document-${item.id}`)
  expect((await (await request.get(endpoint+'?download=1',{headers:h})).body()).equals(bytes)).toBe(true)
  await row.getByRole('button',{name:'Опубликовать клиенту',exact:true}).click()
  await expect.poll(async()=> (await request.get(endpoint,{headers:headers(fixture.client)})).status()).toBe(200)
  await row.getByRole('button',{name:'Снять публикацию для клиента',exact:true}).click()
  await expect.poll(async()=> (await request.get(endpoint,{headers:headers(fixture.client)})).status()).toBe(404)
})

test('ChatAccess, manager/client isolation, private recipient, trace and integration model permissions hold at the real API', async ({request,page,context}) => {
  const record = (id:number) => `/api/messenger/records/${id}/`
  for (const session of [fixture.manager,fixture.client,fixture.integrator]) {
    expect((await request.get(record(fixture.records.allowed),{headers:headers(session)})).status()).toBe(404)
  }
  for (const session of [fixture.team_lead,fixture.finance]) {
    expect((await request.get(record(fixture.records.allowed),{headers:headers(session)})).ok()).toBe(true)
    expect((await request.get(record(fixture.records.foreign),{headers:headers(session)})).status()).toBe(404)
    expect((await request.get(record(fixture.records.private),{headers:headers(session)})).status()).toBe(404)
    const page:Paged<RecordBrief> = await (await request.get('/api/messenger/records/',{headers:headers(session)})).json()
    expect(page.items.map(row => row.id)).not.toContain(fixture.records.foreign)
    expect(page.items.map(row => row.id)).not.toContain(fixture.records.private)
    const jobs:Paged<QueueJob> = await (await request.get('/api/processing/jobs/',{headers:headers(session)})).json()
    expect(jobs.items.every(row => row.queue === 'ai' || row.can_retry === false)).toBe(true)
  }
  expect((await request.get(record(fixture.records.private),{headers:headers(fixture.manager)})).ok()).toBe(true)
  for(const session of [fixture.team_lead,fixture.finance,fixture.integrator,fixture.client]) {
    expect((await request.get(`/api/messages/${fixture.private_raw_id}/`,{headers:headers(session)})).status()).toBe(404)
    expect((await request.get(record(fixture.private_reply_record_id),{headers:headers(session)})).status()).toBe(404)
  }
  expect((await request.get(`/api/messages/${fixture.private_raw_id}/`,{headers:headers(fixture.manager)})).status()).toBe(200)
  expect((await request.get(record(fixture.private_reply_record_id),{headers:headers(fixture.manager)})).status()).toBe(200)
  expect((await request.get('/api/settings/ai/',{headers:headers(fixture.integrator)})).status()).toBe(403)
  expect((await request.get('/api/settings/bitrix/',{headers:headers(fixture.integrator)})).status()).toBe(403)
  expect((await request.get('/api/processing/jobs/')).status()).toBe(401)
  const metrics:AnalyticsOperations = await (await request.get('/api/analytics/operations/',{headers:headers(fixture.manager)})).json()
  expect(metrics.messages.inbound_total).toBe(0)
  const leadMetrics:AnalyticsOperations = await (await request.get('/api/analytics/operations/',{headers:headers(fixture.team_lead)})).json()
  for(const [state,count] of Object.entries(leadMetrics.messages.by_state)) {
    const cohort:Paged<RecordBrief> = await (await request.get(`/api/messenger/records/?cohort=source&analysis=${state}`,{headers:headers(fixture.team_lead)})).json()
    expect(cohort.items.length).toBe(count)
    expect(cohort.items.every(row=>row.statuses.analysis===state && row.direction==='inbound' && row.purpose==='source')).toBe(true)
  }
  await authenticate(context,fixture.client); await page.goto('/messenger')
  await expect(page).toHaveURL(/\/client/)
  await expect(page.getByRole('navigation',{name:'Разделы',exact:true})).toHaveCount(0)
})

function totp() {
  const buffer = Buffer.alloc(8); buffer.writeBigUInt64BE(BigInt(Math.floor(Date.now()/30000)))
  const digest = createHmac('sha1', Buffer.from('48656c6c6f21deadbeef','hex')).update(buffer).digest()
  const offset = digest[digest.length-1]! & 15
  return String((digest.readUInt32BE(offset)&0x7fffffff)%1000000).padStart(6,'0')
}
test('JWT settings and WAHA commands preserve MFA and reject replay in a different browser session', async ({request,browser}) => {
  const h = headers(fixture.mfa)
  const code = totp()
  expect((await request.get('/api/settings/ai/',{headers:h})).status()).toBe(403)
  expect((await request.post('/api/whatsapp/restart/',{headers:h,data:{config_id:fixture.config_id}})).status()).toBe(403)
  expect((await request.post('/api/settings/access/',{headers:h,data:{code}})).ok()).toBe(true)
  expect((await request.get('/api/settings/ai/',{headers:h})).ok()).toBe(true)
  const isolated = await browser.newContext()
  expect((await isolated.request.get('/api/settings/ai/',{headers:h})).status()).toBe(403)
  expect((await isolated.request.post('/api/settings/access/',{headers:h,data:{code}})).status()).toBe(400)
  await isolated.close()
})

test('real webhook is deduplicated, Q2 completes analysis, cursor sees later events and browser shows persisted history', async ({request,page,context}) => {
  await authenticate(context,fixture.admin)
  const h = headers(fixture.admin)
  const baseline = await (await request.get('/api/messenger/changes/',{headers:h})).json() as {next_cursor:string}
  const id = `messenger-${randomUUID()}`
  const body = JSON.stringify({event:'message',session:'default',payload:{id,body:'Привет, спасибо!',from:'fixture@g.us',participant:'79990000002@c.us',notifyName:'Боб',timestamp:Math.floor(Date.now()/1000)}})
  const hook = { 'Content-Type':'application/json','X-Webhook-Hmac':createHmac('sha512','isolated-local-webhook-secret').update(body).digest('hex') }
  expect((await request.post('/api/whatsapp/webhook/',{headers:hook,data:body})).status()).toBe(202)
  expect((await request.post('/api/whatsapp/webhook/',{headers:hook,data:body})).status()).toBe(200)
  let target:RecordBrief | undefined
  await expect.poll(async () => {
    const records:Paged<RecordBrief> = await (await request.get('/api/messenger/records/?q='+encodeURIComponent('Привет, спасибо!'),{headers:h})).json()
    target = records.items[0]
    return target?.statuses.analysis
  },{timeout:90000}).toMatch(/analyzed|no_crm_actions/)
  const detail:RecordDetail = await (await request.get(`/api/messenger/records/${target!.id}/`,{headers:h})).json()
  expect(detail.origin.raw_message_id).toBeTruthy()
  const changes = await (await request.get(`/api/messenger/changes/?cursor=${encodeURIComponent(baseline.next_cursor)}`,{headers:h})).json() as {items:{type:string;record_id?:number;id:number}[]}
  expect(changes.items.some(row => row.type === 'event' && row.record_id === target!.id)).toBe(true)
  await page.goto(`/messenger?record=${target!.id}&tab=history`)
  await expect(page.getByRole('region',{name:'Лента сообщений'})).toContainText('Привет, спасибо!')
  await expect(page.getByLabel('Инспектор',{exact:true})).toContainText(/Проанализировано|Без CRM-действий/)
})

test('phone OTP passes through Redis, real outbox and Q2/provider; journal never contains the code', async ({page,request}) => {
  const phone = fixture.otp.phone
  type ProviderMessages={waha_messages?:{id:string;chatId:string;text:string}[]}
  const previous=JSON.parse(readFileSync(join(directory!,'provider-state.json'),'utf8')) as ProviderMessages
  const previousIds=new Set(previous.waha_messages?.map(row=>row.id)??[])
  await page.goto('/')
  await page.getByTestId('open-auth').click()
  await page.getByPlaceholder('+7 (999) 000-00-00').fill(phone)
  const issued = page.waitForResponse(r => r.url().endsWith('/api/auth/send-code/') && r.request().method() === 'POST')
  await page.getByRole('button',{name:'Получить код',exact:true}).click()
  expect((await issued).status()).toBe(200)
  expect((await request.post('/api/auth/send-code/',{data:{phone}})).status()).toBe(429)
  let code = ''
  await expect.poll(() => {
    const provider = JSON.parse(readFileSync(join(directory!,'provider-state.json'),'utf8')) as ProviderMessages
    const text = provider.waha_messages?.findLast(row => !previousIds.has(row.id) && row.chatId === `${phone}@c.us` && row.text.startsWith('Код входа Mazory:'))?.text
    code = text?.match(/: ([0-9]{4})\./)?.[1] ?? ''
    return code.length
  },{timeout:30000}).toBe(4)
  const wrong = code === '1111' ? '2222' : '1111'
  expect((await request.post('/api/auth/verify-code/',{data:{phone,code:wrong}})).status()).toBe(401)
  await page.getByPlaceholder('• • • •').fill(code)
  const verified=page.waitForResponse(r=>r.url().endsWith('/api/auth/verify-code/')&&r.request().method()==='POST')
  await page.getByRole('button',{name:'Войти в систему',exact:true}).click()
  const login=await verified
  expect(login.status()).toBe(200)
  const issuedSession=await login.json() as {access:string}
  await expect(page).toHaveURL(/\/messenger/)
  expect((await request.post('/api/auth/verify-code/',{data:{phone,code}})).status()).toBe(401)
  const history = await request.get('/api/messenger/records/?category=system',{headers:{Authorization:`Bearer ${issuedSession.access}`}})
  expect(history.status()).toBe(200)
  const payload = await history.text()
  expect(payload).toContain('Отправлен код входа')
  const records = JSON.parse(payload) as Paged<RecordBrief>
  const own=records.items.filter(row=>row.purpose==='auth')
  expect(own.length).toBeGreaterThan(0)
  const other=await (await request.get('/api/messenger/records/?category=system',{headers:headers(fixture.team_lead)})).json() as Paged<RecordBrief>
  expect(other.items.some(row=>own.some(record=>record.id===row.id))).toBe(false)
  expect(records.items.filter(row => row.purpose === 'auth').every(row => row.body === 'Отправлен код входа')).toBe(true)
  expect(payload).not.toContain('\"code\":')
})
