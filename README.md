# savi-discord-notifier

Get a Discord message when **Savi** finishes a task on [spawn.co](https://www.spawn.co) — so you can close the app and go do something else.

> **Unofficial.** Not affiliated with, endorsed by, or supported by Spawn. It reads a private, undocumented endpoint, which means **it can break whenever Spawn ships an update**. If it stops working, see [When it breaks](#when-it-breaks).

---

## The problem

Savi's tasks run on Spawn's servers, not on your machine. That's great — you can close the desktop app and the work keeps going. But there's no way to find out it's *done* except opening the app and looking.

So either you leave a full Electron app plus a WebGPU viewport running just to watch for a notification, or you keep checking manually.

This is a ~200-line Python script that watches for you and posts to Discord instead. It uses about 30 MB of RAM and no GPU.

## Requirements

- Python 3.9 or newer (`python --version`)
- A Discord channel you can create a webhook in
- A spawn.co account

**No `pip install`.** Standard library only — clone it and run it.

---

## Setup

### 1. Make a Discord webhook

In Discord: **Server Settings → Integrations → Webhooks → New Webhook**. Pick your channel, then **Copy Webhook URL**.

> Treat that URL like a password. Anyone who has it can post to your channel.

### 2. Find the endpoint

This is the fiddly part, and it's why the endpoint lives in config instead of in the code — Spawn doesn't publish an API, so we have to look at what the web app itself does.

1. Open [spawn.co](https://www.spawn.co) in your browser and sign in
2. Press **F12** → **Network** tab
3. Filter to **Fetch/XHR**
4. Ask Savi to make something, so a task is actually running
5. Look for a request that **repeats every few seconds** — that's the app polling its own task list
6. Click it:
   - **Headers** tab → copy the full **Request URL** → that's your `spawn.tasks_url`
   - **Headers** → *Request Headers* → find **`Cookie`** → right-click → **Copy value** → that's your `spawn.cookie`
   - **Response** tab → look at the JSON to work out the field names (step 4 below)

<details>
<summary>What if there's no repeating request?</summary>

Check the **WS** filter instead. If Spawn pushes updates over a websocket, there'll be a single long-lived connection with messages flowing through it. Polling still works fine alongside it — just find any regular HTTP endpoint that lists your tasks (loading the dashboard usually fires one).
</details>

### 3. Write your config

```bash
cp config.example.json config.json
```

Fill in `discord_webhook_url`, `spawn.tasks_url`, and `spawn.cookie`.

> `config.json` is gitignored. **Never commit it** — it contains a live session token for your account.

### 4. Point it at the right fields

Every API names things differently, so tell the script what to look for. Once your URL and cookie are in, run:

```bash
python savi_notify.py --dump
```

That prints the first few tasks exactly as Spawn returns them. Map what you see into `spawn.fields`:

```jsonc
"tasks_path": "data.tasks",   // where the array lives; "" if the response IS the array
"fields": {
  "id":     "id",             // something stable and unique per task
  "status": "state",          // the field that changes to done/failed
  "title":  "prompt",         // shown in the Discord message
  "url":    "shareUrl"        // optional - makes the message clickable
}
```

Dotted paths work: `"status": "meta.state"`, `"id": "task.id"`.

A status counts as finished if it's one of `done`, `complete`, `completed`, `succeeded`, `success`, `finished`, `ready` (green message) or `failed`, `error`, `errored`, `cancelled`, `canceled`, `aborted`, `timeout` (red message). If Spawn uses different words, add them to `DONE_OK` / `DONE_BAD` at the top of `savi_notify.py`.

### 5. Test it

```bash
python savi_notify.py --test-discord
```

A message should land in your channel. Then:

```bash
python savi_notify.py --once -v
```

The first run records what's already there **without** notifying, so you don't get spammed about tasks that finished last week. Run it once more and it'll start watching for real.

### 6. Run it in the background

```powershell
powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
```

Registers a Scheduled Task that starts at logon and runs silently via `pythonw.exe` — no console window, no tray icon, nothing to look at.

```powershell
Stop-ScheduledTask -TaskName SaviDiscordNotifier          # pause it
powershell -ExecutionPolicy Bypass -File .\install-windows.ps1 -Uninstall   # remove it
```

On macOS or Linux, use a `launchd` plist or a systemd user unit — PRs welcome.

---

## How it works

Every `poll_seconds` (default 60) it fetches your task list, compares each task's status against `state.json`, and posts to Discord when one crosses into a finished state. `state.json` is what stops it notifying you twice for the same task, including across restarts.

It backs off exponentially on errors, adds a little random jitter so it isn't a perfectly predictable load on Spawn's servers, and sends itself an identifiable `User-Agent`. Please don't lower `poll_seconds` to something rude — a minute is already faster than you'd notice.

If your session expires, it posts one message telling you so and exits, rather than dying quietly and leaving you wondering why the pings stopped.

## When it breaks

Spawn is moving fast and this reads an endpoint they never promised to keep stable.

| Symptom | Likely cause | Fix |
|---|---|---|
| `session token has expired` | Cookie went stale | Redo step 2, paste the new `Cookie` value |
| `spawn.co returned 404` | Endpoint moved | Redo step 2 to find the new URL |
| `Expected a list of tasks at path...` | Response shape changed | Re-run `--dump`, fix `tasks_path` |
| Runs fine, never notifies | Status words changed | `--dump` while a task is finishing, check `DONE_OK` |

Most breakages are a config edit, not a code change. That's deliberate.

## Limitations

- **Polling, not push.** Up to `poll_seconds` of delay.
- **Only while your PC is on.** It's a local script. Running it on a cheap VPS or a free tier works too, but then your session token lives on someone else's server — your call.
- **Session tokens expire.** Expect to re-paste the cookie periodically. There's no API key to use instead, because there's no public API.

## Contributing

Issues and PRs welcome — especially macOS/Linux service files, and reports of what the response shape looks like on your account so the defaults can get smarter.

```bash
python -m unittest -v
```

Tests are stdlib-only and hit no network.

## A note on Spawn

If you work at Spawn: this exists because people want to know when Savi is done without leaving the app open. A real webhook would make this repo unnecessary, and I'd retire it happily.

## License

MIT — see [LICENSE](LICENSE).
