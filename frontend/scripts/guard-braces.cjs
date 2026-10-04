// CVE-2026-93687: bound parser input before the recursive AST walkers run.
// Temporary mitigation recommended in https://github.com/micromatch/braces/issues/70.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const parser = path.join(root, 'node_modules/braces/lib/parse.js');
const originalHash = 'e572166565f15fa6ad9865ae49d678218e32aabfd1b3720f6d0d43d39800d310';
const original = "const max = typeof opts.maxLength === 'number' ? Math.min(MAX_LENGTH, opts.maxLength) : MAX_LENGTH;";
const replacement = "const max = Math.min(1000, typeof opts.maxLength === 'number' ? Math.min(MAX_LENGTH, opts.maxLength) : MAX_LENGTH);";
const hash = value => crypto.createHash('sha256').update(value).digest('hex');

function verifyGuard() {
  const source = fs.readFileSync(parser, 'utf8');
  assert.ok(source.includes(replacement), 'braces length guard is missing');
  assert.equal(hash(source.replace(replacement, original)), originalHash, 'Unexpected braces source; review mitigation');
  const braces = require(path.join(root, 'node_modules/braces'));
  assert.deepEqual(braces('src/{a,b}.vue', { expand: true }), ['src/a.vue', 'src/b.vue']);
  const nested = '{'.repeat(4500) + 'x' + '}'.repeat(4500);
  for (const expand of [false, true]) {
    assert.throws(() => braces(nested, { expand, maxLength: 10000 }),
      error => error instanceof SyntaxError && error.message.includes('max characters (1000)'));
  }
}

if (require.main === module) {
  const source = fs.readFileSync(parser, 'utf8');
  if (!source.includes(replacement)) {
    assert.equal(hash(source), originalHash, 'Unexpected braces source; review mitigation');
    fs.writeFileSync(parser, source.replace(original, replacement));
  }
  verifyGuard();
  console.log('braces parser guard installed and tested.');
}
module.exports = { verifyGuard };
