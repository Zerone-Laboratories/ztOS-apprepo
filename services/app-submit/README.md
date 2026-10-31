# ztos-app-submit

The review relay for the ztOS store, a Cloudflare Worker (not deployed yet).
It copies the device list's pattern (`ztOSMobile/services/device-submit`, the
"ztOS Ports" bot and #ztos-device-ports): a bot opens a forum post per
submission with Accept / Reject buttons that only moderators can press, and
Accept commits to the repository with a fine-grained token.

1. **An entry arrives**: `POST /submit` from `zrn submit --send` or the store
   page, or `/submit-app repo commit [subdir]` on Discord. The Worker checks
   that the repository is public, that the commit exists, and that
   `zrnapp.toml` *at that commit* has the same id and version; it refuses a
   version already in the ledger and `zrn-` ids from outside
   Zerone-Laboratories. Then the **ztOS Apps** bot opens a post in
   **#ztos-app-submissions**: the source link at that exact commit, language,
   licence, permissions, author, notes, a compare link from the last approved
   release, a warning if the id was approved from another repository, and
   `submission.json`.
2. **A moderator** (Manage Threads) presses:
   - **Accept**: the Worker checks the commit again, appends an approved
     `[[release]]` to `ledger/<id>.toml` (version, commit, the moderator's
     Discord id and name, time, thread) in one commit authored by the bot, and
     sends `repository_dispatch` `app-approved`. CI builds, signs and lists it.
     The post then shows **Revoke**.
   - **Reject**: asks for a reason, posts it for the author, locks the post.
   - **Revoke** (or `/revoke-app id version reason`): marks the release revoked
     in the ledger (who, when, why) and sends `app-revoked`; CI drops it from
     the index and lists it as revoked so phones disable it.

## Settings

| Where | Name | What |
|---|---|---|
| `wrangler.toml` | `FORUM_CHANNEL` | #ztos-app-submissions' channel id (a forum) |
| `wrangler.toml` | `STORE_REPO`, `STORE_BRANCH` | where the ledger is |
| `wrangler.toml` | `BOT_NAME`, `BOT_EMAIL` | the ledger commits' author; `publish.yml` checks `BOT_EMAIL` |
| `wrangler.toml` | `ALLOWED_ORIGINS` | pages allowed to send entries from a browser |
| secret | `DISCORD_TOKEN` | the ztOS Apps bot's token |
| secret | `DISCORD_PUBLIC_KEY` | the application's public key (Discord's request signatures) |
| secret | `GITHUB_TOKEN` | fine-grained: ztOS-apprepo only; Contents read and write (commits and repository_dispatch); nothing else |

The bot needs View Channels, Send Messages (and in threads), Create Public
Threads, Manage Threads, Embed Links, Attach Files and Read Message History in
the forum. The application's Interactions Endpoint URL is `.../interactions`.

## Deploying (not done; for the owner)

```
npm install
npx wrangler deploy
npx wrangler secret put DISCORD_TOKEN          # likewise DISCORD_PUBLIC_KEY, GITHUB_TOKEN
# the two slash commands, once (APP_ID: the application's id)
curl -X PUT -H "Authorization: Bot $DISCORD_TOKEN" -H "Content-Type: application/json" \
  --data @commands.json "https://discord.com/api/v10/applications/$APP_ID/commands"
npx wrangler tail                              # its logs
```

`npm test` (node --test) checks entry validation, zrnapp.toml reading, that
the ledger the Worker writes is the ledger `tools/store.py` accepts, and that
unsigned interactions are refused.
