import { execFileSync } from 'node:child_process'
import type { Page } from '@playwright/test'
const composeFile = new URL('../../compose.e2e.yml', import.meta.url).pathname
export interface TestSession { access: string; refresh: string; user_id: number; session_id: number }
export function isolatedCompose(args: string[]): string {
  const remote = process.env.E2E_SSH_HOST
  const compose = remote ? process.env.E2E_REMOTE_COMPOSE_FILE : composeFile
  if (!compose) throw new Error('E2E_REMOTE_COMPOSE_FILE is required with E2E_SSH_HOST')
  const command = ['docker', 'compose', '-f', compose, '-p', process.env.E2E_COMPOSE_PROJECT || 'mazory-platform-e2e', ...args]
  const options = { encoding: 'utf8' as const, stdio: ['pipe', 'pipe', 'pipe'] as ['pipe', 'pipe', 'pipe'], maxBuffer: 16 * 1024 * 1024, timeout: 120000 }
  if (remote) {
    const names = ['E2E_IMAGE_TAG', 'E2E_GATEWAY_PORT', 'E2E_PROVIDER_PORT', 'E2E_BIND_ADDRESS']
    const env = names.filter(name => process.env[name]).map(name => `${name}=${process.env[name]}`)
    const controlPath = process.env.E2E_SSH_CONTROL_PATH
    const sshOptions = ['-o', 'BatchMode=yes', ...(controlPath ? ['-S', controlPath] : [])]
    // Transport argv as data: synthetic fixture code never passes through a shell.
    return execFileSync('ssh', [...sshOptions, remote, "python3 -c 'import json,subprocess,sys; subprocess.run(json.load(sys.stdin), check=True)'"], { ...options, input: JSON.stringify(['env', ...env, ...command]) }).trim()
  }
  return execFileSync(command[0]!, command.slice(1), options).trim()
}
export function isolatedCommand(command: string, args: string[] = []): string {
  return isolatedCompose(['exec', '-T', 'backend', 'python', 'manage.py', command, ...args])
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
  // The login URL itself ends with ?next=/admin/; a glob can match it before
  // the POST completes, so the next navigation cancels login in WebKit.
  await page.waitForURL(url => url.pathname === '/admin/', { timeout: 15_000 })
}
