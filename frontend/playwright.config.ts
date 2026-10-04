import { defineConfig } from '@playwright/test'
export default defineConfig({ testDir: './e2e', testIgnore: '**/dialogue-local.spec.ts', use: { baseURL: 'http://127.0.0.1:4175', browserName: 'chromium' }, webServer: { command: 'npm run preview -- --host 127.0.0.1 --port 4175', url: 'http://127.0.0.1:4175', reuseExistingServer: true }, reporter: 'list' })
