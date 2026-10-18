import { readFileSync } from './files'
import { writeFileSync } from 'node:fs'
import { join } from 'node:path'
import type { BrowserContext } from '@playwright/test'
/** A real refresh cookie, rotated by the application; no route or API substitution. */
export async function authenticateShell(context: BrowserContext) {
  const path = join(process.env.MAZORY_E2E_DIR!, 'messenger-session.json')
  const data = JSON.parse(readFileSync(path,'utf8')) as {admin:{refresh:string;cookie_name:string}}
  const session = data.admin
  context.on('response', async response => {
    if (response.url().endsWith('/api/auth/refresh/') && response.request().method() === 'POST' && response.ok()) {
      await response.finished()
      const cookie = (await context.cookies()).find(row => row.name === session.cookie_name)
      if (cookie) {
        const current = JSON.parse(readFileSync(path,'utf8')) as {admin:{refresh:string}}
        current.admin.refresh = cookie.value; writeFileSync(path,JSON.stringify(current))
      }
    }
  })
  await context.addCookies([{name:session.cookie_name,value:session.refresh,url:`${process.env.MAZORY_E2E_URL}/api/auth/`,httpOnly:true,sameSite:'Lax'}])
}
