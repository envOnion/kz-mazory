// A durable child exit code is required: some Docker-compatible engines lose
// the real exit code when a VM/container is terminated. This is not a test retry.
const { spawnSync } = require('node:child_process')
const { mkdirSync, writeFileSync } = require('node:fs')
const { join } = require('node:path')
const directory = process.env.MAZORY_E2E_OUTPUT || '/tmp/mazory-local-playwright'
mkdirSync(directory, { recursive: true })
const result = spawnSync(process.execPath, [
  'node_modules/@playwright/test/cli.js', 'test', '--config=playwright.local.config.ts', ...process.argv.slice(2),
], { stdio: 'inherit', env: process.env })
const code = result.status ?? 1
writeFileSync(join(directory, 'browser-exit.json'), JSON.stringify({
  run_id: process.env.MAZORY_E2E_RUN_ID || 'manual', exit_code: code, signal: result.signal,
}))
process.exit(code)
