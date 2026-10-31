// node --test services/app-submit/test: the parts of the Worker with no network
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import worker from '../src/worker.js';
import { _test } from '../src/worker.js';

const { normalise, parseAppToml, parseLedger, renderLedger } = _test;
const here = dirname(fileURLToPath(import.meta.url));

test('entries are checked and clipped', () => {
	const good = normalise({ id: 'io.github.alice.notes', version: '1.0.0', repo: 'https://github.com/alice/notes.git', commit: 'A'.repeat(40).toLowerCase(), subdir: '/app/' });
	assert.deepEqual(good.problems, []);
	assert.equal(good.s.repo, 'https://github.com/alice/notes');
	assert.equal(good.s.subdir, 'app');
	const bad = normalise({ id: 'zrn-sneaky', version: '1', repo: 'http://evil', commit: 'main', subdir: '../x', firstParty: true });
	assert.equal(bad.problems.length, 5);
	assert.ok(bad.problems.some((p) => p.includes('zrn- ids')));
});

test('zrnapp.toml is read', () => {
	const app = parseAppToml('[app]\nid = "io.github.a.b"\nversion = "1.2.3"\npermissions = ["network", "audio"]\n\n[build]\nlang = "c"  # here\n');
	assert.equal(app.id, 'io.github.a.b');
	assert.equal(app.version, '1.2.3');
	assert.deepEqual(app.permissions, ['network', 'audio']);
	assert.equal(app.lang, 'c');
});

test('the ledger the Worker writes is the ledger store.py accepts', () => {
	const ledger = { id: 'io.github.alice.notes', source: 'https://github.com/alice/notes', subdir: '', name: 'Notes "quoted"', author: 'Alice', contact: 'a\nb', release: [
		{ version: '1.0.0', commit: '1'.repeat(40), submitted_at: '2026-10-30T01:02:03Z', thread: '42', status: 'approved', approved_by: 'discord:123456789012345678', approved_by_name: 'mod', approved_at: '2026-10-30T01:02:03Z' },
	] };
	const text = renderLedger(ledger);
	assert.deepEqual(parseLedger(text).release, ledger.release);
	assert.equal(parseLedger(text).name, 'Notes "quoted"');
	const dir = mkdtempSync(join(tmpdir(), 'ledger-'));
	writeFileSync(join(dir, 'io.github.alice.notes.toml'), text);
	const out = execFileSync('python3', [join(here, '../../../tools/store.py'), 'validate'], { env: { ...process.env, STORE_LEDGER: dir } }).toString();
	assert.match(out, /0 with errors/);
});

test('interactions without a valid Discord signature are refused', async () => {
	const env = { DISCORD_PUBLIC_KEY: '00'.repeat(32) };
	const r = await worker.fetch(new Request('https://x/interactions', { method: 'POST', body: '{}', headers: { 'X-Signature-Ed25519': '00'.repeat(64), 'X-Signature-Timestamp': '1' } }), env, {});
	assert.equal(r.status, 401);
});
