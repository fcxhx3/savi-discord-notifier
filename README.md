# savi-discord-notifier

Pings a Discord channel when Savi finishes building something on [spawn.co](https://www.spawn.co), so you can close the app and go do something else.

**This is unofficial.** I don't work for Spawn and it isn't affiliated with them. It reads an endpoint that nobody documented, which means it can stop working any time they push an update. There's a [troubleshooting table](#when-it-breaks) for when that happens.

**Before you start:** setting this up puts a working login to your Spawn account into `config.json` on your machine. Please read [your config is a password file](#your-config-is-a-password-file) before you go anywhere near a `git commit`.

## Why

Savi's builds run on Spawn's servers. You can close the desktop app and she carries on, which is the whole point of it. The catch is there's no way to find out she's finished without opening the app again and looking.

So your options are leaving a full Electron app plus a WebGPU viewport running just to catch a notification, or checking manually every ten minutes.

Spawn already writes the notification you want. It just only shows up inside the app:

> savi finished building in MONSTER O'CLOCK - come see

This forwards those to Discord. It's about 30 MB of RAM, no GPU, no window, nothing in the tray.

## What you need

- Python 3.9 or newer (`python --version`)
- A Discord channel you can make a webhook in
- A spawn.co account

No `pip install`. It's stdlib only, so clone it and run it.

## Setup

### 1. Discord webhook

Server Settings > Integrations > Webhooks > New Webhook. Pick a channel, hit Copy Webhook URL.

Treat that URL like a password. Anyone who has it can post in your channel.

### 2. Your credentials

Spawn's backend is a Supabase deployment at `kiln.spawn.co`, so there are three things to grab. All of them come out of your browser while you're signed in.

**user_id and apikey**, from a network request:

1. Open spawn.co, signed in, and press F12
2. Network tab, then type `notifications` in the filter box
3. Open your notifications so a request actually fires
4. Click the `notifications?...` row that shows up:
   - The Request URL has `user_id=eq.<uuid>` in it. That uuid is your `user_id`.
   - Under Request Headers there's an `apikey` line. That's your `apikey`. It's a public anon key, so it's not a secret.

**The session**, from cookies. Not local storage. I looked there first and it isn't there, because Spawn uses `@supabase/ssr` which keeps the session in a cookie instead.

5. Application tab, then Cookies, then `https://www.spawn.co`
6. Look for `sb-spawn-auth-token.0` and `sb-spawn-auth-token.1`

The session is too big to fit in one cookie so it gets split into numbered chunks. You might have more than two.

### 3. Config

```bash
cp config.example.json config.json
```

Fill in `discord_webhook_url`, `user_id` and `apikey`.

For the cookie, don't edit it in by hand. It's several KB of base64 and one stray newline makes the whole file invalid JSON. Run this instead:

```bash
python setup_session.py
```

Paste chunk `.0`, press enter, paste `.1`, press enter, then enter again on the blank line. It glues them together, checks they decode, and writes them in. It only prints masked values and doesn't send anything anywhere.

You've now got a live login to your Spawn account sitting in `config.json`. It ships gitignored and you should leave it that way. See [below](#your-config-is-a-password-file) for what that actually means.

### 4. Check it works

```bash
python savi_notify.py --test-discord
```

If a message lands in your channel, the Discord half is fine. Now the Spawn half:

```bash
python savi_notify.py --dump
```

That prints your latest notifications as raw JSON. If you can see them, you're connected.

### 5. Pick what gets forwarded

Your feed has more in it than Savi updates. Mine also had people playing my games, likes, and world_alive pings. Have a look:

```bash
python savi_notify.py --types
```

On my account that gave:

```
  21  savi_finished
  18  player_played
   7  world_alive
   2  like
   2  pulse_milestone
```

Then pick, in `config.json`:

```jsonc
"only_types": ["savi_finished"],  // allowlist, just these
"ignore_types": ["player_played"] // or blocklist, everything except these
```

The default is `savi_finished` only, which is the flames and nothing else.

One thing worth knowing: filter on `kind`, not `type`. Every row has both. `type` is the UI action and says `redirect` on nearly everything, so it's useless for this. `kind` is what actually happened. The defaults already point at the right one, this only bites you if you go changing `fields.type` yourself.

Also, `savi finished building in X` and `savi built most of X - a couple pieces want your eye` share the same kind, so you can't separate those two. Both of them mean she's stopped and wants you, which is normally what you're after anyway.

### 6. How the message looks

Two styles, set with `style`:

```jsonc
"style": "plain"   // default, one line
"style": "embed"   // boxed card with a coloured bar and a footer
```

Plain comes out as a single line with the link at the front, the way you'd type it yourself. Change the link text with `link_label`, and set `mention` (something like `"<@your-user-id>"`) if you want it to actually ping you rather than sit there quietly.

To see a real one without waiting for Savi:

```bash
python savi_notify.py --preview
```

That sends your most recent notification and leaves `state.json` alone, so it won't affect anything later.

### 7. Run it in the background

```powershell
powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
```

Runs through `pythonw.exe` so there's no console window, and starts straight away rather than waiting for you to log out.

It tries two things in order:

1. A Scheduled Task, which has the advantage of restarting the script if it ever dies. Registering one usually needs admin.
2. A shortcut in your Startup folder, if the task gets refused. No admin needed.

`Register-ScheduledTask : Access is denied` is normal on a standard account. The installer catches it and falls back on its own. Force one with `-Method task` or `-Method startup`.

```powershell
powershell -ExecutionPolicy Bypass -File .\install-windows.ps1 -Uninstall
```

To check it's actually alive:

```powershell
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object { $_.CommandLine -like '*savi_notify*' }
```

On macOS or Linux you'd want a launchd plist or a systemd user unit. I haven't written those, PRs welcome.

## Your config is a password file

Worth being blunt about this, because it's the one way using this tool can actually hurt you.

Once you've run `setup_session.py`, your `config.json` contains a working login to your Spawn account. Not a settings file with an API key in it. A login. Anyone who gets hold of it can sign in as you, see your projects, and act as you, without needing your password and without touching your email.

`state.json` is the same. The token gets renewed as it runs and the fresh one is written there, so it is every bit as sensitive as the config.

So:

- **Don't commit either of them.** Both are in `.gitignore` already. If you fork this, restructure it, or copy the files somewhere else, check they're still ignored. `savi_notify.py` shouts at you on startup if it spots either one tracked in git, but don't lean on that.
- **Don't paste them anywhere.** Not in an issue, not in Discord asking for help, not in a screenshot. If you need help, the error message is almost always enough on its own.
- **Careful with `--dump` output too.** It doesn't have your tokens in it, but it does carry your `user_id` and the names of your projects in every row.

One thing that is genuinely fine: the `apikey`. That's Supabase's public anon key. It's shipped in the website's own JavaScript, everyone using Spawn has the same one, and on its own it does nothing. It's the session cookie and the tokens that matter.

**If one does get out:** sign out of Spawn, which invalidates the refresh token and makes the leaked copy useless. Then run `setup_session.py` again with a fresh cookie. Do that first and worry about tidying up the git history afterwards, since the token is the part that's actually dangerous while it's still valid.

Your Discord webhook URL lives in there as well. That one's less serious, but anyone holding it can post into your channel, so if it leaks just delete the webhook in Discord and make a new one.

## How it works

Every `poll_seconds` (60 by default) it runs the same query the site itself runs:

```
GET https://kiln.spawn.co/rest/v1/notifications
    ?select=*&user_id=eq.<you>&status=neq.archived
    &order=created_at.desc,id.desc&limit=50
```

Anything with an id it hasn't seen goes to Discord, oldest first. `state.json` keeps the ids it's already sent so a restart doesn't spam you with things you've already been told about.

Access tokens die after about an hour, so on a 401 it refreshes and retries. Supabase rotates the refresh token every time you use it, so the new one gets saved back to `state.json`. That file is a credential too, treat it accordingly. The upshot is you paste the cookie once, not every hour.

It backs off exponentially when things fail and jitters the interval so it isn't hammering Spawn on a perfect metronome. Please don't drop `poll_seconds` to something rude, a minute is already faster than you'll notice.

If the refresh ever fails it posts one message saying so and exits, rather than dying quietly and leaving you wondering why the pings stopped.

The first run is deliberately silent. It records what's already in your feed without forwarding any of it, otherwise installing this dumps your entire notification history into the channel at once.

## When it breaks

| What you see | Probably | Fix |
|---|---|---|
| `token could not be refreshed` | Refresh token expired or revoked | Redo step 2, run `setup_session.py` again |
| `returned 404` | Table or route renamed | Check `spawn.table` and `base_url` |
| `Expected a list of rows` | Response shape changed | Run `--dump` and open an issue |
| Runs fine, nothing arrives | Everything's being filtered | Compare `only_types` against `--types` |
| Messages are raw JSON | Text column renamed | Set `fields.text` from what `--dump` shows |

Most of these are a config edit rather than a code change. That's on purpose, so you're not stuck waiting for me to push a fix.

## Limitations

- It polls, so there's up to `poll_seconds` of delay. Supabase Realtime would make it instant, I just haven't written it.
- Only runs while your PC is on. A cheap VPS works too, but then your Spawn login is sat on someone else's box, which is your call to make.
- It forwards notifications, not build state. If Spawn doesn't write a notification for something, this can't tell you about it.

## Contributing

```bash
python -m unittest -v
```

42 tests, stdlib only, no network calls.

Things that would genuinely help: launchd and systemd units, Supabase Realtime support, and reports of what `--types` prints on your account so the defaults can get smarter.

If you open an issue with `--dump` output in it, strip your `user_id` first. It's in every row, and the messages have your project names in them.

## If you work at Spawn

Hi! I built this for myself. I kept asking Savi for something, closing the app so I could get my machine back, and then having no idea when she'd finished. Maybe I'm the only person who works that way, but if anyone else has the same problem then hopefully this is useful to them too.

Honestly, I'd love for this to stop being necessary. If notifications outside the app ever turned into a real feature, whether that's a webhook, an email, or something I haven't thought of, I'd be genuinely happy about it. I really do want it :)

And if you'd rather this didn't exist, just ask and I'll take it down, no argument from me. For what it's worth it only reads my own notifications, and it checks less often than the app does.

## License

MIT, see [LICENSE](LICENSE).
