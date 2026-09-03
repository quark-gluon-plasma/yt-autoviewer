# YouTube Autoviewer (Windows)

A background desktop app that watches any YouTube channel and auto-plays new
uploads for you: every new video pops a small themed alert in the
**bottom-right corner** with **Open** / **Dismiss**. Open plays the video in
a small **borderless** window, also bottom-right, which **closes itself when
the video finishes**. A tiny icon lives in the Windows tray (right side of
the taskbar) — right-click → **Exit**.

No YouTube API key needed (uses the public RSS feed).

## Setup

```powershell
pip install -r requirements.txt
python yt_alerts.py
```

On first run a `config.json` is created next to the script — open it (or
right-click the tray icon → `Settings`) and set `"channel"` to any of:

- Channel ID: `UCp5lYB-oMvUwFN3Pf21VLtw`
- Handle: `@BimmyJimmyCat`
- Full URL: `https://www.youtube.com/@BimmyJimmyCat`

## Settings (`config.json`)

| Key | Default | What it does |
|---|---|---|
| `channel` | `@BimmyJimmyCat` | Channel to watch (ID, `@handle`, or URL) |
| `poll_minutes` | `5` | How often to check for new videos (min 1) |
| `toast_timeout_seconds` | `15` | Alert auto-dismiss time (`0` = stay forever) |
| `theme` | `"system"` | Toast theme: `system` (follow Windows light/dark), `pastel`, `light`, or `dark`. Switchable live from the tray icon → `Theme` menu |
| `player_width` / `player_height` | `480` / `300` | Mini-player window size |
| `alert_on_first_run` | `false` | `true` also alerts for videos already posted |

Optional: put a shortcut in the Startup folder (`Win+R` → `shell:startup`)
so it runs on login.

## Tray menu

Right-click the tray icon for: **Check for new videos now**, **Show test
notification** (preview the alert without affecting anything), **Open latest
video**, **Theme** (`System`/`Pastel`/`Light`/`Dark`, applies instantly),
**Settings** (opens `config.json`), **Exit** (fully terminates the app).

## Build a single .exe (optional)

```powershell
pip install pyinstaller
pyinstaller --noconsole --onefile --name YTAutoviewer yt_alerts.py
```

## How it works

- `fetch_latest_videos()` polls
  `https://www.youtube.com/feeds/videos.xml?channel_id=...`
- Seen video IDs persist in `seen_videos.json` (git-ignored) so you only
  get alerted once per video.
- Toast = transparent pywebview window: a real CSS rounded card with
  thumbnail, title, countdown bar, and Open/Dismiss, always-on-top,
  bottom-right. Spawned per alert as `python yt_alerts.py --toast ...`;
  exits on Open / Dismiss / timeout. Starts off-screen so it never flashes
  white, then glides into place. Theme variables come from `THEMES` and
  resolve per alert, so switching themes needs no restart.
- Open = spawns `python yt_alerts.py --play <id>` → pywebview
  (Edge WebView2) frameless window with the YouTube IFrame API; JS calls back
  into Python on `ENDED` and the window exits. Videos that refuse embedding
  fall back to the browser automatically.
- Tray icon = pystray with a right-click menu; Exit stops polling, removes
  the icon, and terminates the process.
