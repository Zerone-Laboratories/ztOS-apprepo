# Putting your app in the ztOS store

The store lists apps made with the ztOS SDK (`zrn`, libzrnui), built **by the
store from your source** at the exact commit you name, reviewed by moderators
on the ztOS Mobile Discord, and signed with the store's key. You never upload
a binary, and nobody merges a pull request to add your app.

## Before you submit

1. Your app builds and packs with the SDK: `zrn build && zrn pack && zrn verify dist/*.zexec`.
2. It looks right in both of ztCoolStyle's styles, Metro and DotMatrix:
   `zrn render --style metro out.png` and `zrn render --style dotmatrix out.png`.
3. `zrnapp.toml` is complete:
   - `id`: reverse-DNS you control, at least three parts, lower case
     (`io.github.alice.notes`). It never changes. `zrn-` ids are reserved for
     Zerone Laboratories' own repositories.
   - `version`: semantic (`1.2.0`), higher than any version already listed.
   - `name`, `summary`, `author`, `licence` (an SPDX id), `icon`.
   - `permissions`: everything the app needs, and nothing else
     (`network`, `audio`, `camera`, `location`, `notifications`, `bluetooth`,
     `phone`, `contacts`, `messages`, `background`, `files.music`,
     `files.pictures`, `files.documents`, `files.downloads`). Moderators ask
     about each one.
4. The code is in a **public GitHub repository** (other hosts later) and the
   commit you submit is pushed. Everything needed to build must be in that
   commit: no downloads during the build other than your language's normal
   package registry (Rust crates).
5. The licence lets the store redistribute your app. libzrnui and its bindings
   are LGPL-2.1-or-later, so an app under any licence can link them.

## Submitting

From your app's folder, with the commit pushed:

```
zrn submit                 # shows the entry for HEAD: id, version, repo, commit, folder
zrn submit --send          # sends it
```

or on Discord: `/submit-app repo:https://github.com/you/notes commit:<40-digit SHA> [subdir:app]`.

The ztOS Apps bot opens a post in **#ztos-app-submissions**. Moderators read
the source at that commit and press **Accept** or **Reject** (with a reason in
the post). Accepted, the store's CI builds that commit for aarch64 (Python
apps once, for any CPU), signs the package and lists it, usually within
minutes.

## Updates

An update is a new submission: raise `version`, commit, push, `zrn submit
--send`. Moderators see a compare link from your last approved commit. The
store never follows a branch; changing or force-pushing your repository does
nothing to what is already listed.

## What gets you rejected

- Doing something the permissions do not say, or asking for permissions the
  app does not use.
- Code that is obfuscated, downloads and runs code, or is not the app the
  name and summary describe.
- A build that does not work from a clean checkout of the commit.
- Breaking the ztCoolStyle rules badly (unreadable text, fake system UI).

## Revocation

A moderator can revoke a release (a security problem, abuse, a licence
problem). It leaves the index at once and phones that have it disable it and
show the reason. Fix it and submit a new version.

## What is in this repository

| | |
|---|---|
| `ledger/<id>.toml` | every app and release, with who approved it and the exact commit (written by the bot) |
| `tools/store.py` | validate, plan, build, publish, index, check-index |
| `.github/workflows/` | `validate.yml` (every change), `publish.yml` (build, sign, list) |
| `services/app-submit/` | the Discord relay (a Cloudflare Worker) |
| `keys/` | the store's public key, once made |
| `sdk.lock` | the zrn-sdk commit CI builds with |

The listed packages are release assets (`<id>-<version>`); the signed index
is `index.json` + `index.json.minisig` on the `store-index` branch.
