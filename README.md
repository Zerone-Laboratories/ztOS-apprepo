# ztOS-apprepo

The ztOS store: a **ledger** of approved apps (each pinned to a commit of
its public source), the **CI** that builds them from that source, signs them
and lists them, and the **Discord relay** where moderators approve them.
App authors: read [CONTRIBUTING.md](CONTRIBUTING.md). The design and threat
model are in the SDK's DESIGN.md (zrn-sdk, "The store").

```
author --zrn submit / form / /submit-app--> ztos-app-submit (Worker)
   --> post in #ztos-app-submissions --Accept (moderator)--> ledger/<id>.toml (+ repository_dispatch)
   --> publish.yml: plan -> build at the pinned SHA (no secrets) -> sign + release + index.json
   --> ztstore on the phone: verifies index.json and each .zexec with the store key
```

## index.json (on the store-index branch)

```json
{
 "format": 1,
 "generated": "2026-10-30T01:02:03Z",
 "store_key": "KEY ID",
 "apps": [{
   "id": "io.github.alice.notes", "name": "Notes", "summary": "...", "author": "...",
   "licence": "MIT", "categories": ["Utility"],
   "source": "https://github.com/alice/notes", "subdir": "",
   "releases": [{
     "version": "1.0.0", "arch": "aarch64", "sha256": "...", "size": 12345,
     "url": ".../releases/download/io.github.alice.notes-1.0.0/io.github.alice.notes-1.0.0-aarch64.zexec",
     "sig_url": "....zexec.minisig",
     "zrnui": "0.1", "min_ztos": "0.1", "permissions": ["network"],
     "commit": "40-hex", "approved_by": "discord:...", "approved_at": "..."
   }]
 }],
 "revoked": [{"id": "...", "version": "...", "arch": "...", "sha256": "...", "reason": "...", "revoked_at": "..."}]
}
```

`index.json.minisig` signs it (trusted comment
`zrn-index format=1 generated=... sha256=...`).

## What a client must do

`tools/store.py check-index --index URL --pubkey keys/store.pub` is the
reference:

1. Verify `index.json` against the store key compiled into the client. Any
   other key, or no signature: show nothing.
2. For a package: download it and its `.minisig`; its sha256 must equal the
   index's; its signature must verify with the store key; its trusted comment
   must name the same id, version and arch; `zrn verify`'s structure checks
   must pass. Otherwise refuse and say which check failed.
3. An installed package whose sha256 is under `revoked`: disable it and show
   the reason.

## Running it locally

```
export ZRN_SDK=../zrn-sdk
python3 tools/store.py validate
python3 tools/test_store.py
(cd services/app-submit && npm test)
```

`evidence/` holds a full local run (plan, build at the pinned commit,
publish with a test key, index, client check) and pictures of the sample app.

## Before going live (the owner)

- Make the store key (`keys/README.md`), commit `keys/store.pub`, set the
  `store-signing` environment's secret `ZRN_STORE_KEY`.
- Make this repository public (clients fetch the index and releases), protect
  `main` (only the bot's token and admins push), and pin the workflow actions
  to commit SHAs.
- Deploy `services/app-submit`, create #ztos-app-submissions, register the
  slash commands.
- Push zrn-sdk to Zerone-Laboratories/zrn-sdk and point `sdk.lock` at it; the
  sample ledger entry is a staging approval (`discord:0`) that CI never
  publishes: submit it on Discord or delete it.
