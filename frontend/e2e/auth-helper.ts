import { execSync } from 'child_process'
import type { Page } from '@playwright/test'

let cachedToken = ''

export function getOrCreateE2EToken(): string {
  if (cachedToken) return cachedToken
  if (process.env.E2E_JWT_TOKEN) {
    cachedToken = process.env.E2E_JWT_TOKEN.trim()
    return cachedToken
  }
  const commands = [
    `docker compose exec backend python manage.py shell -c "from django.contrib.auth.models import User; from rest_framework_simplejwt.tokens import RefreshToken; u, _ = User.objects.get_or_create(username='77019876543'); print('E2E_JWT:' + str(RefreshToken.for_user(u).access_token))"`,
    `docker --context joodexpert-vm exec mazory-backend python manage.py shell -c "from django.contrib.auth.models import User; from rest_framework_simplejwt.tokens import RefreshToken; u, _ = User.objects.get_or_create(username='77019876543'); print('E2E_JWT:' + str(RefreshToken.for_user(u).access_token))"`
  ]
  for (const cmd of commands) {
    try {
      const out = execSync(cmd).toString()
      const match = out.match(/E2E_JWT:(eyJ[a-zA-Z0-9_\-\.]+)/)
      if (match) {
        const token = match[1].trim()
        cachedToken = token
        return token
      }
    } catch {
      // try next
    }
  }
  console.error('Failed to generate E2E token via docker or remote context')
  return ''
}

export function setTestOtp(phone = '77019876543', code = '1234') {
  const commands = [
    `docker compose exec backend python manage.py shell -c "from django.core.cache import cache; cache.set('otp:${phone}', '${code}', 3600); cache.delete('otp_attempts:${phone}')"`,
    `docker --context joodexpert-vm exec mazory-backend python manage.py shell -c "from django.core.cache import cache; cache.set('otp:${phone}', '${code}', 3600); cache.delete('otp_attempts:${phone}')"`
  ]
  for (const cmd of commands) {
    try {
      execSync(cmd)
      return
    } catch {
      // try next
    }
  }
}

export async function loginWithToken(page: Page, token?: string) {
  const jwt = token || getOrCreateE2EToken()
  await page.addInitScript((authToken) => {
    window.localStorage.setItem('mazory_access_token', authToken)
    window.localStorage.setItem('mazory_user', JSON.stringify({
      id: 2,
      phone: '77019876543',
      name: 'Сотрудник компании',
      username: '77019876543'
    }))
  }, jwt)
}
