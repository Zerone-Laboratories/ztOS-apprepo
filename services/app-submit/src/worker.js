// ztOS app submissions: the relay between authors (`zrn submit --send`, the
// store page's form, or /submit-app on Discord), the moderators on the ztOS
// Mobile Discord, and the store's ledger (Zerone-Laboratories/ztOS-apprepo).
// The same pattern as ztOSMobile/services/device-submit.
//
//   POST /submit        an entry {id, version, repo, commit, subdir, ...}: checked
//                       against GitHub (the commit exists, zrnapp.toml at THAT
//                       commit says the same id and version), then posted in
//                       #ztos-app-submissions with Accept / Reject buttons
//   POST /interactions  Discord: the buttons, the reason forms, /submit-app and
//                       /revoke-app. Accept appends an approved [[release]] to
//                       ledger/<id>.toml (who, when, the exact commit) and starts
//                       CI (repository_dispatch app-approved). Revoke marks a
//                       release revoked and starts CI (app-revoked).
//
// Only members who can manage threads (moderators) can accept, reject or revoke.
// Secrets (wrangler secret put): DISCORD_TOKEN, DISCORD_PUBLIC_KEY, GITHUB_TOKEN.
// Settings (wrangler.toml [vars]): FORUM_CHANNEL, STORE_REPO, STORE_BRANCH,
// BOT_NAME, BOT_EMAIL, ALLOWED_ORIGINS.

const DISCORD = 'https://discord.com/api/v10';
const MANAGE_THREADS = 1n << 34n;
const ADMINISTRATOR = 1n << 3n;
const MAX_BODY = 64 * 1024;
const ID_RE = /^(zrn-[a-z0-9]+(-[a-z0-9]+)*|[a-z][a-z0-9]*(\.[a-z0-9][a-z0-9-]*){2,})$/;
const VERSION_RE = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?$/;
const REPO_RE = /^https:\/\/github\.com\/([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)$/;
const SHA_RE = /^[0-9a-f]{40}$/;

export default {
	async fetch(request, env, ctx) {
		const url = new URL(request.url);
		try {
			if (url.pathname === '/submit') return await submitHttp(request, env);
			if (url.pathname === '/interactions') return await interactions(request, env, ctx);
			return new Response('ztOS app submissions\n', { status: 404 });
		} catch (e) {
			console.error(e.stack || e);
			return json({ error: 'The review service failed. Try again in a minute.' }, 500, cors(request, env));
		}
	},
};

// ---- entries ------------------------------------------------------------------

function json(body, status = 200, headers = {}) {
	return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } });
}

function cors(request, env) {
	const origin = request.headers.get('Origin') || '';
	const allowed = (env.ALLOWED_ORIGINS || '').split(',').map((s) => s.trim());
	if (!allowed.includes(origin)) return {};
	return { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Methods': 'POST, OPTIONS', 'Access-Control-Allow-Headers': 'Content-Type', Vary: 'Origin' };
}

function clip(value, max) {
	return String(value == null ? '' : value).replace(/\r/g, '').trim().slice(0, max);
}

class Refused extends Error {}

// the entry as the ledger will hold it, whatever was sent
function normalise(raw) {
	const s = {
		id: clip(raw.id, 96),
		version: clip(raw.version, 40),
		repo: clip(raw.repo, 200).replace(/\.git$/, ''),
		commit: clip(raw.commit, 40).toLowerCase(),
		subdir: clip(raw.subdir, 200).replace(/^\/+|\/+$/g, ''),
		name: clip(raw.name, 40),
		author: clip(raw.author, 80),
		contact: clip(raw.contact, 200),
		notes: clip(raw.notes, 800),
	};
	const problems = [];
	if (!ID_RE.test(s.id)) problems.push('id (reverse-DNS like io.github.you.app)');
	if (s.id.startsWith('zrn-') && !s.repo.startsWith('https://github.com/Zerone-Laboratories/')) problems.push('id (zrn- ids are for Zerone Laboratories\' own repositories)');
	if (!VERSION_RE.test(s.version)) problems.push('version (like 1.2.0)');
	if (!REPO_RE.test(s.repo)) problems.push('repo (https://github.com/OWNER/REPO)');
	if (!SHA_RE.test(s.commit)) problems.push('commit (all 40 hex digits)');
	if (s.subdir.split('/').includes('..')) problems.push('subdir');
	return { s, problems };
}

async function submitHttp(request, env) {
	const headers = cors(request, env);
	if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers });
	if (request.method !== 'POST') return json({ error: 'POST only' }, 405, headers);
	// a browser must come from the store's pages; `zrn submit` sends no Origin
	if (request.headers.get('Origin') && !headers['Access-Control-Allow-Origin']) return json({ error: 'Send it from the ztOS store page or with `zrn submit`.' }, 403);
	if (env.SUBMIT_LIMIT) {
		const { success } = await env.SUBMIT_LIMIT.limit({ key: request.headers.get('CF-Connecting-IP') || 'unknown' });
		if (!success) return json({ error: 'Too many entries from here. Try again in an hour.' }, 429, headers);
	}
	const text = await request.text();
	if (text.length > MAX_BODY) return json({ error: 'The entry is too big.' }, 413, headers);
	let raw;
	try {
		raw = JSON.parse(text);
	} catch {
		return json({ error: 'The entry was not JSON.' }, 400, headers);
	}
	try {
		const posted = await submit(env, raw);
		return json({ ok: true, id: posted.id, version: posted.version, review: 'posted for moderators' }, 200, headers);
	} catch (e) {
		if (e instanceof Refused) return json({ error: e.message }, 400, headers);
		throw e;
	}
}

// checked, then posted for review; throws Refused with what to fix
async function submit(env, raw) {
	const { s, problems } = normalise(raw);
	if (problems.length) throw new Refused(`Fix: ${problems.join(', ')}.`);
	const app = await checkSource(env, s);
	const existing = await readLedger(env, s.id);
	if (existing && existing.release.some((r) => r.version === s.version)) throw new Refused(`${s.id} ${s.version} is already in the store's ledger; raise the version in zrnapp.toml and submit the new commit.`);
	await post(env, s, app, existing);
	return s;
}

// the commit exists in a public repository, and zrnapp.toml at that commit agrees
async function checkSource(env, s) {
	const [, owner, name] = REPO_RE.exec(s.repo);
	const repo = await github(env, `https://api.github.com/repos/${owner}/${name}`).catch(() => null);
	if (!repo || repo.private) throw new Refused(`${s.repo} is not a public repository.`);
	const commit = await github(env, `https://api.github.com/repos/${owner}/${name}/commits/${s.commit}`).catch(() => null);
	if (!commit || commit.sha !== s.commit) throw new Refused(`Commit ${s.commit} is not in ${s.repo}. Push it first.`);
	const path = [s.subdir, 'zrnapp.toml'].filter(Boolean).join('/');
	const r = await fetch(`https://raw.githubusercontent.com/${owner}/${name}/${s.commit}/${path}`);
	if (!r.ok) throw new Refused(`There is no ${path} at that commit.`);
	const app = parseAppToml(await r.text());
	if (app.id !== s.id || app.version !== s.version) throw new Refused(`${path} at that commit says ${app.id} ${app.version}, the entry says ${s.id} ${s.version}.`);
	return { ...app, tree: `${s.repo}/tree/${s.commit}${s.subdir ? '/' + s.subdir : ''}`, date: commit.commit && commit.commit.committer && commit.commit.committer.date };
}

// enough of zrnapp.toml: [app] id, version, name, summary, licence, permissions; [build] lang
function parseAppToml(text) {
	const out = { permissions: [] };
	let section = '';
	for (const line of text.split('\n')) {
		const t = line.trim();
		const sec = /^\[([A-Za-z0-9_.-]+)\]$/.exec(t);
		if (sec) {
			section = sec[1];
			continue;
		}
		const kv = /^([A-Za-z0-9_-]+)\s*=\s*(.+)$/.exec(t);
		if (!kv) continue;
		let value;
		try {
			value = JSON.parse(kv[2].replace(/\s+#.*$/, ''));
		} catch {
			continue;
		}
		if (section === 'app' && ['id', 'version', 'name', 'summary', 'licence', 'permissions'].includes(kv[1])) out[kv[1]] = value;
		if (section === 'build' && kv[1] === 'lang') out.lang = value;
	}
	return out;
}

// ---- the ledger: a TOML subset this Worker and tools/store.py both read ----------

const TOP = ['id', 'source', 'subdir', 'name', 'author', 'contact'];
const REL = ['version', 'commit', 'submitted_at', 'thread', 'status', 'approved_by', 'approved_by_name', 'approved_at', 'revoked_by', 'revoked_by_name', 'revoked_at', 'revoke_reason'];

function parseLedger(text) {
	const ledger = { release: [] };
	let target = ledger;
	for (const line of text.split('\n')) {
		const t = line.trim();
		if (!t || t.startsWith('#')) continue;
		if (t === '[[release]]') {
			target = {};
			ledger.release.push(target);
			continue;
		}
		const kv = /^([a-z_]+)\s*=\s*(".*")\s*$/.exec(t);
		if (kv) target[kv[1]] = JSON.parse(kv[2]);
	}
	return ledger;
}

function tomlString(v) {
	return JSON.stringify(String(v)).replace(/\u2028|\u2029/g, ' ');
}

function renderLedger(l) {
	const lines = ['# ledger/<id>.toml: one app in the ztOS store, written by the ztOS Apps bot', '# when a moderator accepts or revokes on Discord. Do not edit by hand.', ''];
	for (const k of TOP) if (l[k] != null) lines.push(`${k} = ${tomlString(l[k])}`);
	for (const r of l.release) {
		lines.push('', '[[release]]');
		for (const k of REL) if (r[k] != null && r[k] !== '') lines.push(`${k} = ${tomlString(r[k])}`);
	}
	return lines.join('\n') + '\n';
}

async function readLedger(env, id, ref) {
	const r = await github(env, `/contents/ledger/${id}.toml?ref=${ref || env.STORE_BRANCH || 'main'}`).catch((e) => {
		if (e.status === 404) return null;
		throw e;
	});
	if (!r) return null;
	return parseLedger(new TextDecoder().decode(Uint8Array.from(atob(r.content.replace(/\n/g, '')), (c) => c.charCodeAt(0))));
}

// one commit changing ledger/<id>.toml, authored by the bot (CI checks that)
async function commitLedger(env, id, change, message, tries = 3) {
	const branch = env.STORE_BRANCH || 'main';
	const ref = await github(env, `/git/ref/heads/${branch}`);
	const head = await github(env, `/git/commits/${ref.object.sha}`);
	const current = (await readLedger(env, id, ref.object.sha)) || null;
	const next = change(current);
	const blob = await github(env, '/git/blobs', { method: 'POST', body: JSON.stringify({ content: renderLedger(next), encoding: 'utf-8' }) });
	const tree = await github(env, '/git/trees', { method: 'POST', body: JSON.stringify({ base_tree: head.tree.sha, tree: [{ path: `ledger/${id}.toml`, mode: '100644', type: 'blob', sha: blob.sha }] }) });
	const author = { name: env.BOT_NAME || 'ztOS Apps', email: env.BOT_EMAIL || 'apps-bot@ztos.online', date: new Date().toISOString() };
	const made = await github(env, '/git/commits', { method: 'POST', body: JSON.stringify({ message, tree: tree.sha, parents: [ref.object.sha], author, committer: author }) });
	try {
		await github(env, `/git/refs/heads/${branch}`, { method: 'PATCH', body: JSON.stringify({ sha: made.sha }) });
	} catch (e) {
		if (e.status === 422 && tries > 1) return commitLedger(env, id, change, message, tries - 1); // the branch moved
		throw e;
	}
	return made.sha;
}

async function dispatch(env, type, payload) {
	await github(env, '/dispatches', { method: 'POST', body: JSON.stringify({ event_type: type, client_payload: payload }) });
}

async function github(env, path, init = {}) {
	const url = path.startsWith('https://') ? path : `https://api.github.com/repos/${env.STORE_REPO}${path}`;
	const r = await fetch(url, {
		...init,
		headers: {
			Authorization: `Bearer ${env.GITHUB_TOKEN}`,
			Accept: 'application/vnd.github+json',
			'User-Agent': 'ztos-app-submit',
			'X-GitHub-Api-Version': '2022-11-28',
			...(init.body ? { 'Content-Type': 'application/json' } : {}),
		},
	});
	if (!r.ok) {
		const error = new Error(`GitHub ${init.method || 'GET'} ${url}: ${r.status} ${(await r.text()).slice(0, 200)}`);
		error.status = r.status;
		throw error;
	}
	return r.status === 204 ? null : r.json();
}

// ---- Discord ------------------------------------------------------------------

async function discord(env, path, init = {}) {
	const r = await fetch(`${DISCORD}${path}`, { ...init, headers: { Authorization: `Bot ${env.DISCORD_TOKEN}`, ...(init.headers || {}) } });
	if (!r.ok) throw new Error(`Discord ${init.method || 'GET'} ${path}: ${r.status} ${await r.text()}`);
	return r.status === 204 ? null : r.json();
}

function plain(text) {
	return String(text).replace(/([*_~|>`])/g, '\\$1');
}

function reviewButtons(disabled = false) {
	return [{ type: 1, components: [
		{ type: 2, style: 3, label: 'Accept', custom_id: 'accept', disabled },
		{ type: 2, style: 4, label: 'Reject', custom_id: 'reject', disabled },
	] }];
}

function revokeButton(id, version) {
	return [{ type: 1, components: [{ type: 2, style: 4, label: 'Revoke', custom_id: `revoke:${id}:${version}` }] }];
}

async function post(env, s, app, existing) {
	const last = existing && [...existing.release].reverse().find((r) => r.status === 'approved');
	const sourceChanged = existing && existing.source !== s.repo;
	const fields = [
		{ name: 'Source at this commit', value: `[${plain(s.repo.replace('https://github.com/', ''))} @ ${s.commit.slice(0, 12)}](${app.tree})` },
		{ name: 'Language', value: plain(app.lang || '?'), inline: true },
		{ name: 'Licence', value: plain(app.licence || '?'), inline: true },
		{ name: 'Permissions', value: app.permissions.length ? app.permissions.map((p) => `\`${plain(p)}\``).join(' ') : 'none', inline: true },
		{ name: 'Author', value: plain(s.author || '?'), inline: true },
		{ name: 'Contact', value: plain(s.contact || '?'), inline: true },
	];
	if (last && !sourceChanged) fields.push({ name: 'Changes since the approved release', value: `[${last.version} → ${s.version}](${s.repo}/compare/${last.commit}...${s.commit})` });
	if (sourceChanged) fields.push({ name: '⚠️ Different source', value: `This id was approved from ${plain(existing.source)}. Check the author owns both.` });
	if (s.notes) fields.push({ name: 'Notes', value: plain(s.notes).slice(0, 1000) });
	const embed = {
		title: `${s.name || app.name || s.id} ${s.version}`,
		description: plain(app.summary || ''),
		color: 0x0050ef,
		fields,
		footer: { text: `${s.id} · CI builds exactly this commit; the entry is in submission.json` },
	};
	const form = new FormData();
	form.append('payload_json', JSON.stringify({
		name: `${existing ? 'Update: ' : ''}${s.name || s.id} ${s.version}`.slice(0, 100),
		message: {
			content: 'A new app for the ztOS store. Moderators: read the source at this commit, then **Accept** (CI builds, signs and lists it) or **Reject** (asks for a reason).',
			embeds: [embed],
			components: reviewButtons(),
			allowed_mentions: { parse: [] },
			attachments: [{ id: 0, filename: 'submission.json' }],
		},
	}));
	form.append('files[0]', new Blob([JSON.stringify({ ...s, submitted_at: new Date().toISOString().replace(/\.\d+Z$/, 'Z') }, null, 2) + '\n'], { type: 'application/json' }), 'submission.json');
	await discord(env, `/channels/${env.FORUM_CHANNEL}/threads`, { method: 'POST', body: form });
}

// ---- interactions ---------------------------------------------------------------

function hex(s) {
	return Uint8Array.from(s.match(/../g).map((b) => parseInt(b, 16)));
}

async function verified(request, env, body) {
	const signature = request.headers.get('X-Signature-Ed25519');
	const timestamp = request.headers.get('X-Signature-Timestamp');
	if (!signature || !timestamp) return false;
	const key = await crypto.subtle.importKey('raw', hex(env.DISCORD_PUBLIC_KEY), { name: 'Ed25519' }, false, ['verify']);
	return crypto.subtle.verify('Ed25519', key, hex(signature), new TextEncoder().encode(timestamp + body));
}

function isModerator(interaction) {
	const permissions = BigInt((interaction.member && interaction.member.permissions) || '0');
	return (permissions & MANAGE_THREADS) !== 0n || (permissions & ADMINISTRATOR) !== 0n;
}

function ephemeral(content) {
	return json({ type: 4, data: { content, flags: 64, allowed_mentions: { parse: [] } } });
}

function reasonForm(customId, title) {
	return json({ type: 9, data: { custom_id: customId, title, components: [{ type: 1, components: [{ type: 4, custom_id: 'reason', label: 'Why? (posted for the author)', style: 2, min_length: 3, max_length: 800, required: true }] }] } });
}

async function editOriginal(env, interaction, data) {
	await fetch(`${DISCORD}/webhooks/${interaction.application_id}/${interaction.token}/messages/@original`, {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ allowed_mentions: { parse: [] }, ...data }),
	});
}

function option(interaction, name) {
	const o = (interaction.data.options || []).find((x) => x.name === name);
	return o ? o.value : '';
}

async function interactions(request, env, ctx) {
	const body = await request.text();
	if (!(await verified(request, env, body))) return new Response('bad signature', { status: 401 });
	const interaction = JSON.parse(body);
	if (interaction.type === 1) return json({ type: 1 }); // PING

	const who = (interaction.member && interaction.member.user) || interaction.user;

	// /submit-app repo commit [subdir]: anyone; id and version come from zrnapp.toml
	if (interaction.type === 2 && interaction.data.name === 'submit-app') {
		ctx.waitUntil((async () => {
			let content;
			try {
				const repo = String(option(interaction, 'repo')).replace(/\.git$/, '');
				const commit = String(option(interaction, 'commit')).toLowerCase();
				const subdir = String(option(interaction, 'subdir') || '');
				const m = REPO_RE.exec(repo);
				if (!m || !SHA_RE.test(commit)) throw new Refused('Give the repository as https://github.com/OWNER/REPO and the full 40-digit commit.');
				const path = [subdir.replace(/^\/+|\/+$/g, ''), 'zrnapp.toml'].filter(Boolean).join('/');
				const r = await fetch(`https://raw.githubusercontent.com/${m[1]}/${m[2]}/${commit}/${path}`);
				if (!r.ok) throw new Refused(`There is no ${path} at that commit.`);
				const app = parseAppToml(await r.text());
				const s = await submit(env, { id: app.id, version: app.version, repo, commit, subdir, name: app.name, author: who.global_name || who.username, contact: `<@${who.id}>` });
				content = `Posted ${s.id} ${s.version} for review.`;
			} catch (e) {
				if (!(e instanceof Refused)) console.error(e.stack || e);
				content = e instanceof Refused ? `Not posted: ${e.message}` : 'The review service failed. Try again in a minute.';
			}
			await editOriginal(env, interaction, { content });
		})());
		return json({ type: 5, data: { flags: 64 } });
	}

	if (!isModerator(interaction)) return ephemeral('Only moderators can accept, reject or revoke apps.');

	// /revoke-app id version reason
	if (interaction.type === 2 && interaction.data.name === 'revoke-app') {
		const id = String(option(interaction, 'id'));
		const version = String(option(interaction, 'version'));
		const reason = String(option(interaction, 'reason'));
		ctx.waitUntil(revoke(env, interaction, who, id, version, reason));
		return json({ type: 5, data: { flags: 64 } });
	}

	if (interaction.type === 3 && interaction.data.custom_id === 'reject') return reasonForm('reject-reason', 'Reject this app');
	if (interaction.type === 3 && interaction.data.custom_id.startsWith('revoke:')) {
		return reasonForm(`revoke-reason:${interaction.data.custom_id.slice(7)}`, 'Revoke this release');
	}

	if (interaction.type === 5 && interaction.data.custom_id === 'reject-reason') {
		const reason = interaction.data.components[0].components[0].value;
		ctx.waitUntil((async () => {
			await discord(env, `/channels/${interaction.channel_id}/messages`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content: `❌ Rejected by <@${who.id}>: ${reason}\nFix it, commit, and submit the new commit.`, allowed_mentions: { parse: [] } }) });
			await discord(env, `/channels/${interaction.channel_id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ locked: true, archived: true }) });
		})().catch((e) => console.error(e.stack || e)));
		return json({ type: 7, data: { components: [], content: `❌ Rejected by <@${who.id}>.`, allowed_mentions: { parse: [] } } });
	}

	if (interaction.type === 5 && interaction.data.custom_id.startsWith('revoke-reason:')) {
		const [id, version] = interaction.data.custom_id.slice('revoke-reason:'.length).split(':');
		const reason = interaction.data.components[0].components[0].value;
		ctx.waitUntil(revoke(env, interaction, who, id, version, reason));
		return json({ type: 6 }); // acknowledged; revoke() posts the outcome
	}

	// Accept: answer at once (Discord waits 3 s), then check again, record, build
	if (interaction.type === 3 && interaction.data.custom_id === 'accept') {
		ctx.waitUntil(accept(env, interaction, who));
		return json({ type: 7, data: { components: reviewButtons(true), content: `⏳ Accepting (<@${who.id}>)…`, allowed_mentions: { parse: [] } } });
	}
	return ephemeral('That button is no longer in use.');
}

async function accept(env, interaction, who) {
	try {
		const file = (interaction.message.attachments || []).find((a) => a.filename === 'submission.json');
		if (!file) throw new Error('the post has no submission.json');
		const raw = await (await fetch(file.url)).json();
		const { s, problems } = normalise(raw);
		if (problems.length) throw new Error(`the entry is incomplete: ${problems.join(', ')}`);
		await checkSource(env, s); // the commit must still be there and still say the same
		const now = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
		const sha = await commitLedger(env, s.id, (current) => {
			const l = current || { id: s.id, source: s.repo, subdir: s.subdir, name: s.name, author: s.author, contact: s.contact, release: [] };
			if (l.release.some((r) => r.version === s.version)) throw new Error(`${s.id} ${s.version} is already in the ledger`);
			l.release.push({ version: s.version, commit: s.commit, submitted_at: raw.submitted_at || now, thread: interaction.channel_id, status: 'approved', approved_by: `discord:${who.id}`, approved_by_name: who.global_name || who.username, approved_at: now });
			return l;
		}, `ledger: approve ${s.id} ${s.version}\n\nCommit ${s.commit} of ${s.repo}, accepted by ${who.global_name || who.username} (discord:${who.id}) in #ztos-app-submissions.`);
		await dispatch(env, 'app-approved', { id: s.id, version: s.version, commit: s.commit, ledger_commit: sha });
		await editOriginal(env, interaction, { content: `✅ Accepted by <@${who.id}>. CI builds ${s.commit.slice(0, 12)}, signs it and lists it within minutes.`, components: revokeButton(s.id, s.version) });
		await discord(env, `/channels/${interaction.channel_id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: `✅ ${(interaction.channel && interaction.channel.name) || s.id}`.slice(0, 100) }) }).catch(() => {});
	} catch (e) {
		console.error(e.stack || e);
		await editOriginal(env, interaction, { content: `⚠️ Accepting failed: ${String(e.message).slice(0, 300)}. Press Accept to try again.`, components: reviewButtons() });
	}
}

async function revoke(env, interaction, who, id, version, reason) {
	let content;
	try {
		if (!ID_RE.test(id) || !VERSION_RE.test(version)) throw new Refused('Give the app id and version exactly as listed.');
		const now = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
		await commitLedger(env, id, (l) => {
			const r = l && l.release.find((x) => x.version === version);
			if (!r) throw new Refused(`${id} ${version} is not in the ledger.`);
			if (r.status === 'revoked') throw new Refused(`${id} ${version} is already revoked.`);
			Object.assign(r, { status: 'revoked', revoked_by: `discord:${who.id}`, revoked_by_name: who.global_name || who.username, revoked_at: now, revoke_reason: clip(reason, 800) });
			return l;
		}, `ledger: revoke ${id} ${version}\n\nRevoked by ${who.global_name || who.username} (discord:${who.id}): ${clip(reason, 300)}`);
		await dispatch(env, 'app-revoked', { id, version });
		content = `⛔ ${id} ${version} revoked by <@${who.id}>: ${reason}\nIt leaves the store index within minutes; phones that have it are told.`;
	} catch (e) {
		if (!(e instanceof Refused)) console.error(e.stack || e);
		content = e instanceof Refused ? `Not revoked: ${e.message}` : `⚠️ Revoking failed: ${String(e.message).slice(0, 300)}`;
	}
	if (interaction.channel_id) {
		await discord(env, `/channels/${interaction.channel_id}/messages`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content, allowed_mentions: { parse: [] } }) }).catch((e) => console.error(e.stack || e));
	}
	if (interaction.type === 2) await editOriginal(env, interaction, { content });
}

// for tests (node --test): the parts with no network
export const _test = { normalise, parseAppToml, parseLedger, renderLedger };
