// Keep the full npm audit. Accept only the verified, mitigated build-only advisory.
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const { verifyGuard } = require('./guard-braces.cjs');
verifyGuard();
const root = path.resolve(__dirname, '..');
const lock = JSON.parse(fs.readFileSync(path.join(root, 'package-lock.json'), 'utf8'));
const result = spawnSync('npm', ['audit', '--json'], { cwd: root, encoding: 'utf8', maxBuffer: 10 * 1024 * 1024 });
if (result.error) throw result.error;
const audit = JSON.parse(result.stdout);
if (audit.error || !audit.vulnerabilities || ![0, 1].includes(result.status)) {
  throw new Error('npm audit did not complete successfully');
}
function mitigated(name, seen = new Set()) {
  const item = audit.vulnerabilities[name];
  if (!item || seen.has(name) || !item.via.length || !item.nodes.length) return false;
  if (!item.nodes.every(node => lock.packages[node]?.dev === true)) return false;
  const next = new Set([...seen, name]);
  return item.via.every(via => typeof via === 'string' ? mitigated(via, next)
    : name === 'braces' && item.nodes.every(node => node === 'node_modules/braces')
      && via.url === 'https://github.com/advisories/GHSA-vfj7-8cjw-p6xm');
}
let failed = false;
for (const [name, item] of Object.entries(audit.vulnerabilities)) {
  if (['high', 'critical'].includes(item.severity)) {
    const covered = mitigated(name);
    console.log(`${name}: ${covered ? 'verified build-only braces mitigation' : 'UNMITIGATED ' + item.severity}`);
    if (!covered) failed = true;
  }
}
if (failed) process.exit(1);
console.log('Frontend dependency audit passed with verified braces mitigation.');
