# savi-discord-notifier

Get a Discord message when **Savi** finishes building something on [spawn.co](https://www.spawn.co) — so you can close the app and go do something else.

> **Unofficial.** Not affiliated with, endorsed by, or supported by Spawn. It reads a private backend that nobody promised would stay stable, so **it can break whenever Spawn ships an update**. See [When it breaks](#when-it-breaks).

---

## The problem

Savi's work happens on Spawn's servers, so you can close the desktop app and it keeps building. But there's no way to learn it's *finished* except opening the app and looking.

So either you leave a whole Electron app plus a WebGPU viewport running just to catch a notification, or you keep checking manually.

Spawn already writes the notification you want — *"savi finished building in MONSTER O'CLOCK — come see"* — it just only shows it inside the app. This forwards those into Discord. About 30 MB of RAM, no GPU, no window.

## Requirements

- Python 3.9+ (`python --version`)
- A Discord channel you can add a webhook to
- A spawn.co account

**No `pip install`.** Standard library only.

---

## Setup

### 1. Make a Discord webhook

Discord → **Server Settings → Integrations → Webhooks → New Webhook** → pick a channel → **Copy Webhook URL**.

> Treat that URL like a password. Anyone with it can post to your channel.

### 2. Get your credentials

Spawn's backend is a Supabase deployment at `kiln.spawn.co`, so you need four values. All of them come from your browser while you're logged in.

**Your `user_id` and `apikey`** — from a network request:

1. Open [spawn.co](https://www.spawn.co), signed in
2. **F12** → **Network** tab
3. Type `notifications` in the filter box
4. Click the bell / open your notifications so a request fires
5. Click the `notifications?...` row that appears:
   - The **Request URL** contains `user_id=eq.<uuid>` → that uuid is your **`user_id`**
   - Under *Request Headers*, the **`apikey`** header → that's your **`apikey`**

**Your session** — from cookies, *not* local storage:

Spawn uses `@supabase/ssr`, which keeps the session in a cookie. Don't go looking in Local Storage; it isn't there.

6. Switch to the **Application** tab → **Cookies** → `https://www.spawn.co`
7. Find the cookies named **`sb-spawn-auth-token.0`** and **`sb-spawn-auth-token.1`**

   The session is too big for one cookie, so it's split into numbered chunks. You may have more than two.
8. Copy the **Value** of `.0`, then the value of `.1` right after it, into a single string — **in order** — and put that in `session_cookie`

The value starts with `base64-`. Leave that prefix on; the script strips it, decodes the rest, and pulls out both tokens for you.

### 3. Fill in the config

```bash
cp config.example.json config.json
```

Paste in the webhook URL, your `user_id`, and the `apikey`.

For the session cookie, don't hand-edit it — it's several KB of base64 and one stray newline makes the file invalid JSON. Run this instead and paste the chunks when prompted:

```bash
python setup_session.py
```

It joins the chunks, checks they decode, and writes them in for you. It prints only masked values and sends nothing anywhere.

> **`config.json` is gitignored — never commit it.** The `apikey` is a public anon key and is harmless on its own, but the session cookie contains a live login to your account. Anyone who gets it can act as you until you sign out.

If you'd rather not paste the cookie, you can set `access_token` and `refresh_token` directly instead — the cookie is just a friendlier way to supply the same two values.

### 4. Test it

```bash
python savi_notify.py --test-discord
```

A message should land in your channel. Then check the Spawn side:

```bash
python savi_notify.py --dump
```

That prints your latest notifications as raw JSON. If you see them, you're connected.

### 5. Tune what gets forwarded

Your feed probably has more than Savi updates in it — follows, likes, comments. See what's in there:

```bash
python savi_notify.py --types
```

Then keep only what you want, in `config.json`:

```jsonc
"only_types": ["savi_done"],     // allowlist - only these
"ignore_types": ["new_follower"] // or blocklist - everything but these
```

If the Discord messages come out looking wrong, check `--dump` for which column holds the sentence and point `fields.text` at it (dotted paths work, e.g. `"data.headline"`). Left empty, the script tries `title`, `body`, `message`, `text`, `description`, `content` in that order.

### 6. Run it in the background

```powershell
powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
```

Registers a Scheduled Task that starts at logon and runs silently via `pythonw.exe` — no console window, no tray icon.

```powershell
Stop-ScheduledTask -TaskName SaviDiscordNotifier                            # pause
powershell -ExecutionPolicy Bypass -File .\install-windows.ps1 -Uninstall   # remove
```

macOS/Linux: a `launchd` plist or systemd user unit does the same job. PRs welcome.

---

## How it works

Every `poll_seconds` (default 60) it runs the same query the web app runs:

```
GET https://kiln.spawn.co/rest/v1/notifications
    ?select=*&user_id=eq.<you>&status=neq.archived
    &order=created_at.desc,id.desc&limit=50
```

Anything with an id it hasn't seen before gets forwarded to Discord, oldest first. `state.json` holds the ids it's already sent, so restarts don't re-notify you.

Access tokens are short-lived, so on a `401` it refreshes using your refresh token and retries. Supabase rotates refresh tokens on use, so the new one is written back to `state.json` — **that file is a credential too.** In practice this means you paste tokens once, not every hour.

It backs off exponentially on errors, jitters its interval so it isn't a metronome against their servers, and sends an identifiable `User-Agent`. Please don't drop `poll_seconds` to something rude — a minute is already faster than you'd notice.

If the refresh ever fails, it posts one message saying so and exits, instead of dying quietly and leaving you wondering why the pings stopped.

## When it breaks

| Symptom | Likely cause | Fix |
|---|---|---|
| `token could not be refreshed` | Refresh token revoked or expired | Redo step 2, paste new tokens |
| `returned 404` | Table or route renamed | Check `spawn.table` and `base_url` |
| `Expected a list of rows` | Response shape changed | Run `--dump`, open an issue |
| Runs fine, nothing arrives | Everything filtered out | Check `only_types` against `--types` |
| Messages look like raw JSON | Text column renamed | Set `fields.text` from `--dump` |

Most breakage is a config edit, not a code change. That's deliberate.

## Limitations

- **Polling, not push.** Up to `poll_seconds` of delay. Supabase Realtime could make this instant — unimplemented, PRs welcome.
- **Only while your PC is on.** It's a local script. A cheap VPS works too, but then your Spawn login lives on someone else's box — your call.
- **It mirrors notifications, not task state.** If Spawn doesn't generate a notification for something, this can't tell you about it.

## Contributing

```bash
python -m unittest -v
```

Stdlib only, no network calls. Useful PRs: macOS/Linux service files, Supabase Realtime support, and reports of what `--types` prints on your account so the defaults can get smarter.

> If you file an issue with `--dump` output, **redact it first** — it contains your `user_id` and possibly private project names.

## A note on Spawn

If you work at Spawn: this exists because people want to know when Savi is done without leaving the app open. A real webhook or an email toggle would make this repo unnecessary, and I'd retire it happily.

It only reads your own notifications, at a slower rate than the app itself polls. If you'd rather it didn't exist, open an issue and let's talk.

## License

MIT — see [LICENSE](LICENSE).
