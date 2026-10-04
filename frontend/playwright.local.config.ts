import { defineConfig } from '@playwright/test'
export default defineConfig({
  testDir: './e2e', testMatch: 'dialogue-local.spec.ts', workers: 1,
  timeout: 60000, expect: { timeout: 30000 },
  use: { baseURL: process.env.MAZORY_E2E_URL || 'http://127.0.0.1:18080', browserName: 'chromium', trace: 'retain-on-failure' },
  reporter: 'list', outputDir: process.env.MAZORY_E2E_OUTPUT || '/tmp/mazory-local-playwright',
})
