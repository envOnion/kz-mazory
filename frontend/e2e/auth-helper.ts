import { execFileSync } from 'node:child_process'
import type { Page } from '@playwright/test'
const composeFile = new URL('../../compose.e2e.yml', import.meta.url).pathname
const composeProject = process.env.E2E_COMPOSE_PROJECT || 'mazory-platform-e2e'
export interface TestSession { access: string; refresh: string; user_id: number; session_id: number }
export function isolatedCommand(command: string, args: string[] = []): string {
  return execFileSync('docker', ['compose', '-f', composeFile, '-p', composeProject, 'exec', '-T', 'backend', 'python', 'manage.py', command, ...args], { encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] }).trim()
}
export async function login(page: Page, phone = '77000000001'): Promise<TestSession> {
  const session: TestSession = JSON.parse(isolatedCommand('test_session', [phone]))
  await page.context().addCookies([{ name: 'mazory_refresh', value: session.refresh, domain: 'localhost', path: '/api/auth/', httpOnly: true, secure: false, sameSite: 'Lax' }])
  await page.goto('/')
  await page.getByRole('button', { name: 'Рабочий кабинет', exact: true }).waitFor()
  return session
}
export async function adminLogin(page: Page) {
  await page.goto('/admin/login/?next=/admin/')
  await page.locator('[name=username]').fill('77000000001')
  await page.locator('[name=password]').fill('test-only-admin-password')
  await page.locator('button[type=submit], input[type=submit]').click()
  await page.waitForURL('**/admin/')
}
