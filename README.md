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
   --> ztstore on the phone (`zrn`, client/): verifies index.json and each .zexec with the store key
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
     "zrnui": "0.1", "min_ztos": "0.1", "permissions": ["network"], "zexec": 1, "services": [],
     "commit": "40-hex", "approved_by": "discord:...", "approved_at": "..."
   }]
 }],
 "revoked": [{"id": "...", "version": "...", "arch": "...", "sha256": "...", "reason": "...", "revoked_at": "..."}]
}
```

`index.json.minisig` signs it (trusted comment
`zrn-index format=1 generated=... sha256=...`).

## Packages: `.zexec` format 1 and 2

One container for both SDKs: a `.zexec` (shebang line + zip, `zexec.json`
first), signed detached with the store key, trusted comment
`zexec id= version= arch= sha256=`. The index stays `format: 1`; each release
adds `zexec` (1 or 2) and `services` (names), which older phones ignore.

| | format 1 (SDK v1) | format 2 (SDK v2) |
|---|---|---|
| built by | SDK v1 `zrn pack` (`sdk.lock` `commit`) | `store.py build`: CMake against `sdk.lock` `commit_v2`, zrnui + zrnapp linked statically |
| program | `bin/<id>` or `app/` (Python), libzrnui from the system or `lib/` | `bin/<id>`, one static program (`zrnui: "2.x"`) |
| fonts, theme | system or bundled | `share/zrnui/fonts/`, `share/zrnui/theme/theme.ini`; zrn-exec sets `ZRNUI_FONT_DIR`, `ZRNUI_THEME` |
| login services | none | `services` in the manifest, written as systemd user units by the client |

**Why the same container.** zrn-exec, the signature, the index, revocation and
the install path stay one code path; a format 1 app installs, updates and runs
exactly as before. An old client meets format 2 with a clear "reads format 1"
refusal and changes nothing, so updating the client first is safe in any order.

**Why fonts and theme go in every package, not a shared runtime package.**
They are 236 KB. A shared package would need its own versioning, signing,
dependency resolution in the client and an upgrade order, for that. Bundled,
each app gets exactly the theme keys its statically linked zrnui was built
with, and an app never breaks because another updated the runtime.

**Stable launch path.** The package always lives at
`~/.local/share/zrn/apps/<id>/<id>.zexec` and is executable (its shebang runs
zrn-exec). An update renames the new file over it, so the app list's
`Exec=zrn-exec <that path>`, the shell's cached copy of it and the units keep
working. zrn-exec gives the app `ZRN_APP_PACKAGE` (that path) to start itself
again.

**Login services.** A package declares what to run, never unit text:

```toml
[[service]]                       # zrnapp.toml; the store copies it into zexec.json
name = "io.github.you.app-sync"   # the unit name: the app id or id-/id. prefix (zrn- apps: any zrn- name)
description = "Sync"
args = ["--sync"]                 # after the package path
after = ["io.github.you.app"]     # only this app's other services
env = { APP_UI = "{exec}" }       # {exec}: the package path
restart = "on-failure"            # no | on-failure | always
restart_sec = 5
restart_prevent_exit_status = [3]
umask = "0077"
nice = 5
memory_high = "24M"
```

Services need the `background` permission. The client writes
`~/.config/systemd/user/<name>.service` (`ExecStart=zrn-exec <package> <args>`,
`ConditionPathExists=<package>`, `NoNewPrivileges=yes`, a `# zrn-app: <id>`
marker), links it into `default.target.wants`, and (re)starts it. A unit file of
the same name that zrn did not write (an app installed by hand) is taken over.

**Install, update, uninstall** (`client/`, installed on the phone as `zrn` and
`zrn-exec`):

- *Install / update* (`zrn install`, `zrn store install ID`): verify; for
  format 2, stop the old services and processes; rename the package over the
  old one; drop zrn-exec's unpacked copies; rewrite the app-list entry and icon;
  write the units, delete units of services this version no longer declares,
  restart the rest. **It never deletes an app's data**, in any format.
- *Uninstall* (`zrn uninstall ID`): asks first (the store app asks on screen and
  calls it with `--json`; in a terminal it asks, `--yes` skips, nobody to answer
  means no). Then, as before, it removes the package, entry, icon and everything
  the app stored (`<id>` and `data_dirs` under data, state, config and cache),
  and now also its units. `--keep-data` keeps the data.
- *Revocation*: also stops and removes the app's units at once.

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
python3 tools/store.py validate
python3 tools/test_store.py
make -C client check
(cd services/app-submit && npm test)
# building: ZRN_SDK (v1 checkout) and/or ZRN_SDK2 (v2 checkout), then
python3 tools/store.py build --id ID --version V --out out
```

## The phone's client

`client/` is `zrn` and `zrn-exec` for the phone (SDK v2 dropped them). Install
it where SDK v1 put them, so `/usr/bin/zrn`, `/usr/bin/zrn-exec`, app-list
entries and the store app keep working: `sudo make -C client install PREFIX=/usr`.
The ztOS image should install it from here at a pinned commit instead of SDK
v1's `make install` (ztOSMobile `installer/ztos_installer/build.py`).

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
- Commit and push the SDK v2 work in zrn-sdk and put that commit in `sdk.lock`
  `commit_v2` (now all zeros: SDK v2 builds fail until then).
- Push zrn-sdk to Zerone-Laboratories/zrn-sdk and point `sdk.lock` at it; the
  sample ledger entry is a staging approval (`discord:0`) that CI never
  publishes: submit it on Discord or delete it.
