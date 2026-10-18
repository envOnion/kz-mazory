import { defineConfig } from '@playwright/test'
export default defineConfig({
  testDir: './e2e', testMatch: '*-local.spec.ts', workers: 1,
  timeout: 60000, expect: { timeout: 30000 },
  use: { baseURL: process.env.MAZORY_E2E_URL || 'http://127.0.0.1:18080', browserName: 'chromium', actionTimeout: 30000, trace: 'retain-on-failure',
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } : {} },
  reporter: [['list'], ['json', {outputFile:`${process.env.MAZORY_E2E_OUTPUT || '/tmp/mazory-local-playwright'}/results.json`}]],
  outputDir: process.env.MAZORY_E2E_OUTPUT || '/tmp/mazory-local-playwright',
})
