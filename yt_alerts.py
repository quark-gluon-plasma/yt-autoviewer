"""YouTube channel background alerter (Windows).

- Polls YouTube RSS feed (no API key needed) for a configurable channel.
- Shows a small toast in the bottom-right corner on new videos.
- Toast has [Open] (mini borderless player, bottom-right, auto-closes
  when the video finishes) and [Dismiss].
- Lives in the system tray (right side of taskbar); right-click -> Exit.

Usage:
    python yt_alerts.py            # run background app
    python yt_alerts.py --play <VIDEO_ID>   # internal: mini-player subprocess
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
import webbrowser
import xml.etree.ElementTree as ET

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SEEN_PATH = os.path.join(BASE_DIR, "seen_videos.json")

DEFAULT_CONFIG = {
    "channel": "@YouTube",
    "channel_comment": "Any channel ID (UC...), @handle, or full channel URL. Examples: '@LinusTechTips', 'https://www.youtube.com/@LinusTechTips', 'https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw'",
    "poll_minutes": 5,
    "toast_timeout_seconds": 15,
    "theme": "system",
    "theme_comment": "Toast theme: 'system' (follow Windows light/dark), 'pastel', 'light', or 'dark'. Also switchable live from the tray icon menu.",
    "player_width": 480,
    "player_height": 300,
    "alert_on_first_run": False,
}

# DEFAULT_CONFIG is the single source of truth for all default values.
# config.json only stores user overrides; every read below goes through
# cfg_val() so fallback literals never get duplicated across the code.
def cfg_val(cfg_dict, key):
    v = cfg_dict.get(key, DEFAULT_CONFIG[key])
    return DEFAULT_CONFIG[key] if v in (None, "") else v

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print("config.json error: %s (using defaults)" % e)
    else:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def load_seen():
    if os.path.exists(SEEN_PATH):
        try:
            with open(SEEN_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data if isinstance(data, list) else [])
        except Exception:
            pass
    return set()


def save_seen(seen):
    try:
        with open(SEEN_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(list(seen))[-200:], f)
    except Exception as e:
        print("could not save seen file: %s" % e)


# --------------------------------------------------------------- youtube ---

def resolve_channel_id(channel_setting):
    """Accept UC... id, @handle, or full URL, return UC... id."""
    s = (channel_setting or "").strip()
    if re.fullmatch(r"UC[\w-]{22}", s):
        return s
    # /channel/UC... inside a URL
    m = re.search(r"/channel/(UC[\w-]{22})", s)
    if m:
        return m.group(1)
    # feeds URL with channel_id param
    m = re.search(r"[?&]channel_id=(UC[\w-]{22})", s)
    if m:
        return m.group(1)
    # Otherwise treat as handle / user / custom URL: fetch page, scrape channelId
    url = s if s.startswith("http") else "https://www.youtube.com/%s" % s.lstrip("/")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", "ignore")
        for pat in (r'"channelId"\s*:\s*"(UC[\w-]{22})"',
                    r'"externalChannelId"\s*:\s*"(UC[\w-]{22})"',
                    r"channel/(UC[\w-]{22})"):
            m = re.search(pat, html)
            if m:
                return m.group(1)
    except Exception as e:
        print("channel resolve failed for %r: %s" % (s, e))
    raise ValueError(
        "Could not resolve channel %r to a channel ID. "
        "Put the raw UC... ID in config.json." % s)


def fetch_latest_videos(channel_id, limit=5):
    """Poll the public RSS feed. Returns [{id, title, link, published}]."""
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=%s" % channel_id
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read()
    ns = {"atom": "http://www.w3.org/2005/Atom",
          "yt": "http://www.youtube.com/xml/schemas/2015"}
    root = ET.fromstring(data)
    videos = []
    for entry in root.findall("atom:entry", ns)[:limit]:
        vid = (entry.findtext("yt:videoId", "", ns) or "").strip()
        title = (entry.findtext("atom:title", "", ns) or "").strip()
        link = ""
        link_el = entry.find("atom:link", ns)
        if link_el is not None:
            link = link_el.get("href", "")
        published = (entry.findtext("atom:published", "", ns) or "").strip()
        if vid:
            videos.append({"id": vid, "title": title or vid,
                           "link": link or ("https://www.youtube.com/watch?v=%s" % vid),
                           "published": published})
    return videos


def channel_title_from_feed(channel_id):
    try:
        url = "https://www.youtube.com/feeds/videos.xml?channel_id=%s" % channel_id
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            root = ET.fromstring(r.read())
        t = root.findtext("{http://www.w3.org/2005/Atom}title", "")
        return (t or "").strip() or channel_id
    except Exception:
        return channel_id


# ------------------------------------------------------------ mini player ---

PLAYER_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="referrer" content="strict-origin-when-cross-origin">
<style>
  html,body {{ margin:0; padding:0; background:#000; height:100%; overflow:hidden; }}
  #bar {{ position:fixed; top:0; left:0; right:0; height:28px; background:rgba(0,0,0,.75);
          color:#fff; font:12px/28px Segoe UI,Arial; display:flex;
          -webkit-app-region:drag; z-index:10; }}
  #title {{ flex:1; padding:0 8px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  #close {{ width:40px; text-align:center; cursor:pointer; -webkit-app-region:no-drag; }}
  #close:hover {{ background:#c00; }}
  #player {{ position:absolute; top:28px; left:0; right:0; bottom:0; border:0; }}
  #embed_err {{ color:#fff; font:14px Segoe UI,Arial; padding:40px 20px; text-align:center; }}
</style></head><body>
<div id="bar"><div id="title">{title}</div><div id="close" onclick="closePlayer()">&#10005;</div></div>
<div id="player"></div>
<script src="https://www.youtube.com/iframe_api"></script>
<script>
var player;
function onYouTubeIframeAPIReady() {{
  player = new YT.Player('player', {{
    videoId: '{video_id}',
    playerVars: {{ autoplay: 1, rel: 0, origin: '{origin}' }},
    events: {{ onStateChange: onState, onError: onPlayerError,
               onReady: function(e) {{ e.target.playVideo(); fitPlayer();
                                        setTimeout(fitPlayer, 2000); }} }}
  }});
}}
function fitPlayer() {{
  // Ground truth sizing: the API stamps a fixed 640x360 inline size on its
  // iframe, which beats stylesheets. Override it from JS using the real
  // window client area so the video exactly fills below the title bar.
  if (!player || !player.getIframe) return;
  var f = player.getIframe();
  f.style.position = 'absolute';
  f.style.top = '28px';
  f.style.left = '0px';
  f.style.width = window.innerWidth + 'px';
  f.style.height = Math.max(0, window.innerHeight - 28) + 'px';
}}
window.addEventListener('resize', fitPlayer);
function onState(e) {{
  if (e.data === YT.PlayerState.ENDED) {{
    setTimeout(closePlayer, 800);
  }}
}}
function onPlayerError(e) {{
  // 101/150 = owner disabled embedding, 153 = player config rejected.
  // Either way this video can't play embedded: hand off to the browser.
  var msg = document.createElement('div');
  msg.id = 'embed_err';
  msg.textContent = 'This video cannot play embedded. Opening in browser...';
  var p = document.getElementById('player');
  p.innerHTML = '';
  p.appendChild(msg);
  setTimeout(function() {{
    try {{ if (window.pywebview) {{ pywebview.api.open_external(); return; }} }} catch (err) {{}}
    closePlayer();
  }}, 1500);
}}
var closed = false;
function closePlayer() {{
  if (closed) return; closed = true;
  try {{ if (window.pywebview) {{ pywebview.api.destroy(); return; }} }} catch (err) {{}}
  window.close();
}}
</script></body></html>"""


def run_mini_player(video_id, video_title, width, height):
    """Entry point for `yt_alerts.py --play <id>`. Borderless bottom-right."""
    import tkinter as tk
    from http.server import BaseHTTPRequestHandler, HTTPServer
    # Compute bottom-right position with tkinter (no visible window).
    tmp = tk.Tk()
    tmp.withdraw()
    sw, sh = tmp.winfo_screenwidth(), tmp.winfo_screenheight()
    tmp.destroy()
    x = max(0, sw - width - 12)
    y = max(0, sh - height - 60)  # 60px clears the taskbar

    watch_url = "https://www.youtube.com/watch?v=%s" % video_id

    # Serve the player page over local HTTP instead of webview's html=.
    # YouTube's embed player (since late 2025) rejects pages with no
    # valid Referer/origin with "Error 153"; a local http:// origin fixes it.
    page_holder = {}

    class PlayerHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = page_holder["html"].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    try:
        import webview

        server = HTTPServer(("127.0.0.1", 0), PlayerHandler)
        port = server.server_address[1]
        origin = "http://127.0.0.1:%d" % port
        page_holder["html"] = PLAYER_HTML.format(
            video_id=video_id, origin=origin,
            title=(video_title or "YouTube").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;").replace("{", "&#123;"))
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def destroy():
            # called from JS (pywebview.api.destroy) when video ends / X clicked
            threading.Timer(0.1, lambda: os._exit(0)).start()

        def open_external():
            # called from JS when the video refuses to play embedded
            # (owner disabled embedding / player config error): browser fallback
            webbrowser.open(watch_url)
            threading.Timer(0.1, lambda: os._exit(0)).start()

        win = webview.create_window(
            "Now playing", url=origin + "/", width=width, height=height,
            x=x, y=y, frameless=True, on_top=True, resizable=False)
        win.expose(destroy, open_external)
        webview.start(gui="edgechromium")
    except Exception as e:
        print("mini player fallback (webview unavailable: %s)" % e)
        webbrowser.open(watch_url)


# ------------------------------------------------------------- toast ------
# Sleek notification window: a transparent WebView2 window styled with real
# CSS (borderless card, gradients, pill buttons). Corners come from
# border-radius, and the window default background is transparent black so
# no white square ever shows behind the card.
#
# Sizing note: the web client area is smaller than the requested outer
# window by a fixed inset eaten from the right/bottom (measured: a 400x188
# outer yields a ~386x151 client). The outer window is therefore oversized
# by that inset at the same origin so the card lands exactly on target.
_TOAST_OVER_W, _TOAST_OVER_H = 14, 37

TOAST_W, TOAST_H = 400, 188

# Toast themes. Keys map 1:1 onto the var(--*) names in TOAST_HTML.
# "pastel" is the signature look; "light"/"dark" follow the OS convention.
# "system" (the default in config.json) resolves to light/dark from Windows.
THEMES = {
    "pastel": {
        "card-text": "#4a4458",
        "card-bg": "linear-gradient(165deg, #ffedf3 0%, #ffe0eb 50%, #dabef6 100%)",
        "card-border": "rgba(170,130,190,.5)",
        "card-inset": "inset 0 1px 0 rgba(255,255,255,.9), inset 0 -1px 0 rgba(150,130,180,.2)",
        "live-text": "#6a3fb0",
        "live-bg": "linear-gradient(180deg, #ddccfa, #c2a3ef)",
        "live-glow": "0 0 12px rgba(150,110,230,.55), inset 0 1px 0 rgba(255,255,255,.8)",
        "chan": "#9c7fb8",
        "x": "#c08aa0",
        "x-hover": "#7a4a63",
        "x-hover-bg": "rgba(120,90,140,.12)",
        "thumb-bg": "linear-gradient(135deg, #ffe3ec, #e3d9fb)",
        "thumb-border": "rgba(150,120,180,.3)",
        "thumb-glyph": "rgba(150,110,160,.5)",
        "title": "#43434e",
        "hint": "#9b8fa3",
        "bar-track": "rgba(130,110,150,.18)",
        "bar-fill": "linear-gradient(90deg, #8fe3c0, #b3a1f2)",
        "go-text": "#1e3f7a",
        "go-bg": "linear-gradient(180deg, #c2d9fd, #a3c2f7)",
        "go-shadow": "0 2px 10px rgba(140,175,240,.5), inset 0 1px 0 rgba(255,255,255,.7)",
        "ghost-text": "#5d5468",
        "ghost-bg": "rgba(130,110,150,.14)",
        "ghost-hover": "rgba(130,110,150,.24)",
    },
    "light": {
        "card-text": "#2b2b30",
        "card-bg": "linear-gradient(165deg, #ffffff 0%, #f4f6fa 60%, #e9eef5 100%)",
        "card-border": "rgba(20,40,80,.16)",
        "card-inset": "inset 0 1px 0 #ffffff",
        "live-text": "#ffffff",
        "live-bg": "linear-gradient(180deg, #ef4444, #c81e1e)",
        "live-glow": "0 0 12px rgba(239,68,68,.45), inset 0 1px 0 rgba(255,255,255,.4)",
        "chan": "#4b6a9b",
        "x": "#8a97a8",
        "x-hover": "#334155",
        "x-hover-bg": "rgba(20,40,80,.08)",
        "thumb-bg": "linear-gradient(135deg, #dbe4f0, #c9d6e8)",
        "thumb-border": "rgba(20,40,80,.2)",
        "thumb-glyph": "rgba(60,90,140,.45)",
        "title": "#1f2937",
        "hint": "#6b7280",
        "bar-track": "rgba(20,40,80,.12)",
        "bar-fill": "linear-gradient(90deg, #38bdf8, #2563eb)",
        "go-text": "#ffffff",
        "go-bg": "linear-gradient(180deg, #3b82f6, #1d4ed8)",
        "go-shadow": "0 2px 10px rgba(37,99,235,.4), inset 0 1px 0 rgba(255,255,255,.4)",
        "ghost-text": "#334155",
        "ghost-bg": "rgba(20,40,80,.08)",
        "ghost-hover": "rgba(20,40,80,.16)",
    },
    "dark": {
        "card-text": "#f1f2f6",
        "card-bg": "linear-gradient(165deg, #26262e 0%, #17171c 55%, #131316 100%)",
        "card-border": "rgba(255,255,255,.14)",
        "card-inset": "inset 0 1px 0 rgba(255,255,255,.12)",
        "live-text": "#ffffff",
        "live-bg": "linear-gradient(180deg, #ff3b30, #c00)",
        "live-glow": "0 0 14px rgba(255,60,50,.55), inset 0 1px 0 rgba(255,255,255,.5)",
        "chan": "#ffb4ac",
        "x": "#ffd9d2",
        "x-hover": "#ffffff",
        "x-hover-bg": "rgba(255,255,255,.14)",
        "thumb-bg": "linear-gradient(135deg, #3a3a44, #222228)",
        "thumb-border": "rgba(255,255,255,.14)",
        "thumb-glyph": "rgba(255,255,255,.35)",
        "title": "#ffffff",
        "hint": "#9a9aa5",
        "bar-track": "rgba(255,255,255,.12)",
        "bar-fill": "linear-gradient(90deg, #22d3ee, #0f9bf0)",
        "go-text": "#ffffff",
        "go-bg": "linear-gradient(180deg, #2aa9f2, #0b7cc4)",
        "go-shadow": "0 2px 10px rgba(15,155,240,.45), inset 0 1px 0 rgba(255,255,255,.4)",
        "ghost-text": "#ffffff",
        "ghost-bg": "rgba(255,255,255,.1)",
        "ghost-hover": "rgba(255,255,255,.2)",
    },
}

THEME_NAMES = ("system", "pastel", "light", "dark")


def system_theme():
    """Follow Windows: light/dark from the AppsUseLightTheme registry value."""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if value else "dark"
    except Exception:
        return "light"


def resolve_theme(cfg):
    """config 'theme' may be pastel/light/dark, or 'system' (default)."""
    if isinstance(cfg, str):
        cfg = {"theme": cfg}
    name = str(cfg.get("theme", "system") or "system").lower()
    if name in THEMES:
        return name
    return system_theme()


def render_toast_html(theme, video_id, title, channel, timeout):
    """Fill the theme variables + content tokens. No token may survive."""
    theme_vars = "".join("--%s:%s;" % (k, v)
                         for k, v in THEMES[resolve_theme(theme)].items())
    thumb = ("https://i.ytimg.com/vi/%s/hqdefault.jpg" % video_id) if video_id else ""
    hint = ("Opens in a mini player  &bull;  hides in %ds" % timeout
            if timeout > 0 else "Opens in a mini player")
    page = TOAST_HTML.replace("@@VARS@@", theme_vars)
    if timeout > 0:
        page = page.replace("@@BAR@@",
                            '<div class="bar"><i style="animation-duration: %ds"></i></div>'
                            % timeout)
        page = page.replace("@@AUTOCLOSE@@",
                            "setTimeout(bye, %d);" % (timeout * 1000))
    else:
        page = page.replace("@@BAR@@", "")
        page = page.replace("@@AUTOCLOSE@@", "")
    import html as htmlmod
    page = page.replace("@@TITLE@@", htmlmod.escape(title or "New video"))
    page = page.replace("@@CHANNEL@@", htmlmod.escape(channel or "YouTube"))
    page = page.replace("@@THUMB@@", thumb)
    page = page.replace("@@HINT@@", hint)
    return page

TOAST_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="referrer" content="strict-origin-when-cross-origin">
<style>
:root { @@VARS@@ }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { background: transparent; overflow: hidden;
               font-family: "Segoe UI", system-ui, sans-serif; }
  ::-webkit-scrollbar { display: none; }
  .card { width: 400px; height: 188px; border-radius: 18px; color: var(--card-text);
          background: var(--card-bg);
          border: 1px solid var(--card-border);
          box-shadow: var(--card-inset);
          padding: 12px 15px 10px; display: flex; flex-direction: column;
          overflow: hidden;
          animation: rise .28s cubic-bezier(.2,.9,.3,1.15); }
  @keyframes rise { from { transform: translateY(26px); opacity: 0; } }
  .top { display: flex; align-items: center; gap: 9px; }
  .live { font-size: 10px; font-weight: 800; letter-spacing: .1em;
          color: var(--live-text); background: var(--live-bg);
          padding: 4px 11px; border-radius: 999px; white-space: nowrap;
          box-shadow: var(--live-glow); }
  .chan { flex: 1; font-size: 12px; color: var(--chan); white-space: nowrap;
          overflow: hidden; text-overflow: ellipsis; }
  .x { color: var(--x); font-size: 13px; font-weight: 700; cursor: pointer;
       padding: 2px 6px; border-radius: 6px; }
  .x:hover { color: var(--x-hover); background: var(--x-hover-bg); }
  .mid { display: flex; gap: 12px; margin-top: 7px; }
  .thumb { position: relative; width: 148px; height: 72px; flex: none;
           border-radius: 10px; overflow: hidden;
           background: var(--thumb-bg);
           border: 1px solid var(--thumb-border); }
  .thumb span { position: absolute; inset: 0; display: flex; align-items: center;
                justify-content: center; color: var(--thumb-glyph);
                font-size: 22px; }
  .thumb img { position: absolute; inset: 0; width: 100%; height: 100%;
               object-fit: cover; }
  .txt { flex: 1; min-width: 0; }
  .title { font-size: 13px; font-weight: 600; line-height: 1.32;
           color: var(--title);
           display: -webkit-box; -webkit-line-clamp: 2;
           -webkit-box-orient: vertical; overflow: hidden; }
  .hint { font-size: 11px; color: var(--hint); margin-top: 5px; }
  .bar { height: 3px; border-radius: 3px; background: var(--bar-track);
         margin-top: 7px; overflow: hidden; }
  .bar i { display: block; height: 100%; border-radius: 3px;
           background: var(--bar-fill);
           transform-origin: left; animation: drain linear forwards; }
  @keyframes drain { to { transform: scaleX(0); } }
  .bot { display: flex; gap: 8px; justify-content: flex-end; margin-top: 7px; }
  .bot button { font: 600 12px "Segoe UI", sans-serif; border: 0; cursor: pointer;
                padding: 5px 18px; border-radius: 999px; }
  .go { color: var(--go-text); background: var(--go-bg);
        box-shadow: var(--go-shadow); }
  .go:hover { filter: brightness(1.05); }
  .ghost { color: var(--ghost-text); background: var(--ghost-bg); }
  .ghost:hover { background: var(--ghost-hover); }
</style></head><body>
<div class="card">
  <div class="top"><span class="live">&#9679; NEW VIDEO</span><span class="chan">@@CHANNEL@@</span><span class="x" onclick="bye()">&#10005;</span></div>
  <div class="mid">
    <div class="thumb"><span>&#9654;</span><img src="@@THUMB@@" onerror="this.remove()"></div>
    <div class="txt"><div class="title">@@TITLE@@</div><div class="hint">@@HINT@@</div></div>
  </div>
  @@BAR@@
  <div class="bot"><button class="ghost" onclick="bye()">Dismiss</button><button class="go" onclick="go()">&#9654; Open</button></div>
</div>
<script>
@@AUTOCLOSE@@
function bye() { try { pywebview.api.dismiss(); } catch (err) {}
                 setTimeout(function () { window.close(); }, 500); }
function go() { try { pywebview.api.open_it(); } catch (err) {} }
</script></body></html>"""


def run_toast_window(video_id, title, channel, timeout, x, y, theme):
    """Entry point for `yt_alerts.py --toast ...`. Blocks till dismissed."""
    import webview

    page = render_toast_html(theme, video_id, title, channel, timeout)

    try:
        def dismiss():
            threading.Timer(0.1, lambda: os._exit(0)).start()

        def open_it():
            open_video_subprocess({
                "id": video_id,
                "link": "https://www.youtube.com/watch?v=%s" % video_id})
            dismiss()

        # Transparent windows ignore hidden=True, so the opaque-white first
        # paint would flash on screen. Instead the window starts far
        # off-screen (where it loads and composes) and is moved into place
        # once settled. No local server needed: nothing is embedded here.
        win = webview.create_window(
            "YTAlerts", html=page,
            width=TOAST_W + _TOAST_OVER_W, height=TOAST_H + _TOAST_OVER_H,
            x=-3000, y=-3000,
            frameless=True, on_top=True, transparent=True, resizable=False)
        win.expose(dismiss, open_it)

        def place():
            try:
                win.move(x, y)
            except Exception:
                pass

        threading.Timer(1.2, place).start()
        if timeout > 0:
            # Hard backstop: even if every JS path fails, the toast process
            # can never outlive its timeout by more than a few seconds, so
            # zombie processes can never pile up next to the background app.
            threading.Timer(timeout + 10, lambda: os._exit(0)).start()
        webview.start(gui="edgechromium")
    except Exception as e:
        print("toast failed (webview unavailable: %s)" % e)


def open_video_subprocess(video):
    """Launch the borderless mini-player without blocking the tray app."""
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__),
             "--play", video["id"], video.get("title", "")],
            cwd=BASE_DIR,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        webbrowser.open(video["link"])


# ------------------------------------------------------------------- app ---

class YTAlertsApp:
    def __init__(self):
        self.cfg = load_config()
        self.channel_id = None
        self.channel_name = "YouTube"
        self.seen = load_seen()
        self.first_poll = True
        self.latest = None  # newest video dict, for tray "open latest"

        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()  # background app: no main window
        self.root.title("YT Alerts")

        self.poll_thread_stop = threading.Event()
        self.tray_icon = None
        self.toast_procs = []  # live --toast subprocess windows

    # -- toast (WebView2 subprocess) --------------------------------
    def _toast_slot(self, w, h, margin_x=16, margin_bottom=60):
        """Bottom-right slot for a toast window, stacking live ones upward."""
        self.toast_procs = [p for p in self.toast_procs if p.poll() is None]
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        offset = len(self.toast_procs) * (h + 12)
        return sw - w - margin_x, sh - h - margin_bottom - offset

    def show_toast(self, video):
        """Fire-and-forget sleek toast in its own borderless window."""
        timeout = int(cfg_val(self.cfg, "toast_timeout_seconds")
                      or DEFAULT_CONFIG["toast_timeout_seconds"])
        x, y = self._toast_slot(TOAST_W, TOAST_H)
        cmd = [sys.executable, os.path.abspath(__file__), "--toast",
               video.get("id", ""), video.get("title", "New video"),
               self.channel_name, str(timeout), str(x), str(y),
               resolve_theme(self.cfg)]
        try:
            proc = subprocess.Popen(
                cmd, cwd=BASE_DIR,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.toast_procs.append(proc)
        except Exception as e:
            print("toast failed: %s" % e)

    # -- polling ----------------------------------------------------
    def poll_once(self):
        try:
            if not self.channel_id:
                self.channel_id = resolve_channel_id(cfg_val(self.cfg, "channel"))
                self.channel_name = channel_title_from_feed(self.channel_id)
                if self.tray_icon is not None:
                    try:
                        self.tray_icon.title = \
                            "YouTube Alerts — %s" % self.channel_name
                    except Exception:
                        pass
            videos = fetch_latest_videos(self.channel_id)
        except Exception as e:
            print("poll failed: %s" % e)
            return
        if not videos:
            return
        self.latest = videos[0]
        fresh = [v for v in videos if v["id"] not in self.seen]
        if self.first_poll and not cfg_val(self.cfg, "alert_on_first_run"):
            # Don't spam history on first run; just remember them.
            fresh = []
        self.first_poll = False
        # Oldest-first so stacking reads chronologically.
        for v in reversed(fresh):
            self.root.after(0, self.show_toast, v)
        for v in videos:
            self.seen.add(v["id"])
        save_seen(self.seen)

    def poll_loop(self):
        while not self.poll_thread_stop.is_set():
            self.poll_once()
            mins = float(cfg_val(self.cfg, "poll_minutes")
                         or DEFAULT_CONFIG["poll_minutes"])
            self.poll_thread_stop.wait(max(60, mins * 60))

    # -- tray -------------------------------------------------------
    def test_notification(self):
        """Fire a display-only test toast (tray menu -> Show test notification).

        Uses the latest known video without touching seen_videos.json, so
        real alerts are unaffected. Safe to call from the pystray thread:
        the toast itself is created on the Tk thread.
        """
        def worker():
            if self.latest is None:
                self.poll_once()
            video = self.latest or {
                "id": "", "title": "Test notification — alerts are working!",
                "link": "https://www.youtube.com/", "published": ""}
            self.root.after(0, self.show_toast, video)

        threading.Thread(target=worker, daemon=True).start()

    def make_tray_image(self):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([4, 10, 60, 54], radius=10, fill=(200, 0, 0, 255))
        d.polygon([(26, 22), (26, 42), (44, 32)], fill=(255, 255, 255, 255))
        return img

    def start_tray(self):
        import pystray
        from pystray import MenuItem as item

        def check_now(icon, _):
            threading.Thread(target=self.poll_once, daemon=True).start()

        def open_latest(icon, _):
            if self.latest:
                open_video_subprocess(self.latest)

        def edit_config(icon, _):
            # Open config.json in the default editor so the channel is configurable.
            try:
                os.startfile(CONFIG_PATH)  # Windows
            except Exception:
                webbrowser.open(CONFIG_PATH)

        def quit_app(icon, _):
            self.shutdown()

        def test_toast(icon, _):
            self.test_notification()

        def set_theme(name):
            def apply(icon, _):
                self.cfg["theme"] = name
                try:
                    save_config(self.cfg)
                except Exception as e:
                    print("could not save theme: %s" % e)
                if self.tray_icon is not None:
                    try:
                        self.tray_icon.update_menu()
                    except Exception:
                        pass
            return apply

        def is_theme(name):
            return lambda item: (self.cfg.get("theme", "system") or "system") == name

        menu = pystray.Menu(
            item("Check for new videos now", check_now),
            item("Show test notification", test_toast),
            item("Open latest video", open_latest),
            item("Theme", pystray.Menu(
                item("System (follow Windows)", set_theme("system"),
                     checked=is_theme("system")),
                item("Pastel", set_theme("pastel"), checked=is_theme("pastel")),
                item("Light", set_theme("light"), checked=is_theme("light")),
                item("Dark", set_theme("dark"), checked=is_theme("dark")),
            )),
            item("Settings (edit channel)", edit_config),
            item("Exit", quit_app),
        )
        self.tray_icon = pystray.Icon(
            "yt-alerts", self.make_tray_image(),
            "YouTube Alerts — %s" % cfg_val(self.cfg, "channel"), menu)
        # pystray blocks; run in daemon thread so Tk mainloop owns the process.
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    # -- lifecycle --------------------------------------------------
    def shutdown(self):
        self.poll_thread_stop.set()
        try:
            if self.tray_icon:
                self.tray_icon.stop()
        except Exception:
            pass
        try:
            self.root.after(0, self.root.destroy)
        except Exception:
            pass
        # Hard exit: kills tray thread + any pending Tk callbacks.
        os._exit(0)

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)
        threading.Thread(target=self.poll_once, daemon=True).start()
        threading.Thread(target=self.poll_loop, daemon=True).start()
        self.start_tray()
        print("Watching channel %s (poll every %s min). Right-click tray icon to Exit."
              % (cfg_val(self.cfg, "channel"), cfg_val(self.cfg, "poll_minutes")))
        self.root.mainloop()


def main():
    # argv: [script, --toast, id, title, channel, timeout, x, y, theme] = 9
    if len(sys.argv) >= 9 and sys.argv[1] == "--toast":
        _, _, vid, title, channel, timeout, x, y, theme = sys.argv[:9]
        run_toast_window(vid, title, channel, int(timeout), int(x), int(y),
                         theme)
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "--play":
        vid = sys.argv[2]
        title = sys.argv[3] if len(sys.argv) > 3 else vid
        cfg = load_config()
        run_mini_player(vid, title,
                        int(cfg_val(cfg, "player_width")),
                        int(cfg_val(cfg, "player_height")))
        return
    YTAlertsApp().run()


if __name__ == "__main__":
    main()
