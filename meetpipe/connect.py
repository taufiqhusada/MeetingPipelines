"""`run.py connect`: a localhost page with one-click Connect buttons for Google and GitHub.

Also hosts the one-time team setup for GitHub (`/setup/github`): creates a read-only GitHub App from a
manifest and stores its credentials in .env, so teammates only ever click "Connect".
"""

from __future__ import annotations

import html
import json
import secrets
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import github_auth, google_auth
from .config import set_env, settings

HOST, DEFAULT_PORT = "127.0.0.1", 8765


class _State:
    google_flows: dict = {}  # oauth state -> Flow (holds the PKCE verifier between start and callback)
    github_states: set = set()
    manifest_state: str | None = None


STYLE = """<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
 :root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--mute:#5d6470;--line:#e3e6eb;--ok:#127a46;--warn:#9a5b00;--accent:#2457d6}
 @media (prefers-color-scheme:dark){:root{--bg:#111318;--card:#1a1d24;--ink:#eceef2;--mute:#9aa2ae;--line:#2a2f39;--ok:#4cc38a;--warn:#e0a54a;--accent:#7aa2ff}}
 body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,system-ui,Segoe UI,sans-serif}
 main{max-width:580px;margin:48px auto;padding:0 16px}
 h1{font-size:22px;margin:0 0 4px} p.sub{color:var(--mute);margin:0 0 24px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:14px}
 .row{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
 h2{font-size:16px;margin:0} .what{color:var(--mute);font-size:13px;margin:4px 0 0}
 .ok{color:var(--ok);font-weight:600} .warn{color:var(--warn)}
 button,a.btn{font:inherit;border:0;border-radius:8px;padding:8px 14px;cursor:pointer;text-decoration:none;
   background:var(--accent);color:#fff;display:inline-block} .ghost{background:transparent!important;color:var(--mute)!important;border:1px solid var(--line)!important}
 .msg{padding:10px 14px;border-radius:8px;background:var(--card);border:1px solid var(--line);margin-bottom:14px}
 input{font:inherit;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink);width:100%;box-sizing:border-box;margin:6px 0 12px}
 code{font-size:13px}
</style>"""


def _page(title: str, body: str) -> str:
    return f"<!doctype html><html><head><title>{title}</title>{STYLE}</head><body><main>{body}</main></body></html>"


def _main_page(msg: str | None) -> str:
    # Google
    if settings.google_token.exists() and (acct := google_auth.whoami()):
        google = (f'<div><span class="ok">✓ {html.escape(acct)}</span> '
                  f'<button class="ghost" onclick="off(\'google\')">Disconnect</button></div>')
    elif google_auth.client_config():
        google = '<a class="btn" href="/google/start">Connect Google</a>'
    else:
        google = ('<span class="warn">Team Google app not set up yet</span>'
                  '<p class="what">App owner: add GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET to .env (README → Google).</p>')
    # GitHub
    login, extra = github_auth.whoami(), ""
    if login and settings.github_token:
        github = f'<div><span class="ok">✓ @{html.escape(login)}</span><p class="what">using GITHUB_TOKEN from .env</p></div>'
    elif login:
        github = (f'<div><span class="ok">✓ @{html.escape(login)}</span> '
                  f'<button class="ghost" onclick="off(\'github\')">Disconnect</button></div>')
        if github_auth.installations() == 0 and github_auth.install_url():
            extra = (f'<p class="warn">Almost done: the app isn\'t installed on any of your repos yet.</p>'
                     f'<a class="btn" target="_blank" href="{github_auth.install_url()}">Choose repos to share</a>')
        elif github_auth.install_url():
            extra = f'<p class="what"><a href="{github_auth.install_url()}" target="_blank">Change which repos are shared</a></p>'
    elif github_auth.configured():
        github = '<a class="btn" href="/github/start">Connect GitHub</a>'
    else:
        github = ('<a class="btn" href="/setup/github">Set up team GitHub App</a>'
                  '<p class="what">One-time, by the app owner.</p>')
    banner = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""
    return _page("Meeting Pipelines · Connect", f"""
<h1>Connect your accounts</h1>
<p class="sub">Read-only access so Meeting Pipelines can build your pre-meeting brief.
Tokens stay on this machine in <code>~/.meetpipe</code>.</p>
{banner}
<div class="card"><div class="row"><div><h2>Google: Gmail &amp; Drive</h2>
<p class="what">Meeting transcripts &amp; notes from email, specs and docs from Drive.</p></div>{google}</div></div>
<div class="card"><div class="row"><div><h2>GitHub</h2>
<p class="what">Pull requests, issues and commits.</p></div>{github}</div>{extra}</div>
<p class="sub">Coding-agent logs (Claude Code, Codex) are read locally, so there's nothing to connect. Close this tab when done.</p>
<script>async function off(w){{await fetch('/'+w+'/disconnect',{{method:'POST'}});location.href='/'}}</script>""")


def _setup_github_page(port: int, default_name: str) -> str:
    _State.manifest_state = secrets.token_urlsafe(12)
    manifest = json.dumps(github_auth.app_manifest(port, default_name))
    return _page("Set up GitHub App", f"""
<h1>Create the team GitHub App</h1>
<p class="sub">Done once. GitHub opens with everything pre-filled (read-only Contents, Issues, Pull requests);
you click <b>Create GitHub App</b> and the credentials are saved to <code>.env</code> automatically.</p>
<div class="card">
<label>App name (must be unique on GitHub)<input id="name" value="{html.escape(default_name)}"></label>
<label>Create under organization (optional, leave empty for your personal account)<input id="org" placeholder="my-org"></label>
<form id="f" method="post"><input type="hidden" name="manifest" id="manifest"></form>
<button onclick="go()">Continue to GitHub</button> <a class="btn ghost" href="/">Cancel</a>
</div>
<script>
const base={manifest};
function go(){{
  const m=Object.assign({{}},base,{{name:document.getElementById('name').value.trim()}});
  const org=document.getElementById('org').value.trim();
  const f=document.getElementById('f');
  f.action=(org?'https://github.com/organizations/'+encodeURIComponent(org)+'/settings/apps/new':
            'https://github.com/settings/apps/new')+'?state={_State.manifest_state}';
  document.getElementById('manifest').value=JSON.stringify(m); f.submit();
}}
</script>""")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    @property
    def base(self) -> str:
        return f"http://{HOST}:{self.server.server_port}"

    def _send(self, code: int, body: str, ctype="text/html; charset=utf-8", headers: dict | None = None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to: str):
        self._send(302, "", headers={"Location": to})

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            return self._route_get(url.path, q)
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            return self._send(200, _main_page(f"Error: {type(e).__name__}: {e}"))

    def _route_get(self, path: str, q: dict):
        # --- Google ---
        if path == "/" and "code" in q and "state" in q:  # Google redirects back to the root URL
            flow = _State.google_flows.pop(q["state"], None)
            if not flow:
                return self._redirect("/?msg=stale")
            missing = google_auth.finish_flow(flow, q["code"])
            if missing:
                names = ", ".join("Gmail" if "gmail" in m else "Drive" for m in missing)
                return self._send(200, _main_page(
                    f"Connected, but {names} access wasn't granted. Click Disconnect, then Connect again and tick every box."))
            return self._redirect("/?msg=google")
        if path == "/" and "error" in q:
            print(f"google callback error: {q}", file=sys.stderr)
            return self._send(200, _main_page(f"Google: {q['error']} (access was not granted)"))
        if path == "/google/start":
            flow, auth_url = google_auth.start_flow(redirect_uri=f"{self.base}/")
            _State.google_flows[parse_qs(urlparse(auth_url).query)["state"][0]] = flow
            return self._redirect(auth_url)
        # --- GitHub user login ---
        if path == "/github/start":
            auth_url, state = github_auth.authorize_url(f"{self.base}/github/callback")
            _State.github_states.add(state)
            return self._redirect(auth_url)
        if path == "/github/callback":
            if q.get("state") not in _State.github_states:
                return self._redirect("/?msg=stale")
            _State.github_states.discard(q["state"])
            if "code" not in q:
                return self._send(200, _main_page(f"GitHub: {q.get('error', 'access was not granted')}"))
            github_auth.finish_login(q["code"], f"{self.base}/github/callback")
            return self._redirect("/?msg=github")
        # --- GitHub one-time app setup ---
        if path == "/setup/github":
            name = f"Meeting Pipelines {secrets.token_hex(2)}"
            if who := github_auth.whoami():
                name = f"Meeting Pipelines ({who})"
            return self._send(200, _setup_github_page(self.server.server_port, name))
        if path == "/github/app-created":
            if not q.get("code") or q.get("state") != _State.manifest_state:
                return self._redirect("/?msg=stale")
            app = github_auth.convert_manifest(q["code"])
            set_env("GITHUB_CLIENT_ID", app["client_id"])
            set_env("GITHUB_CLIENT_SECRET", app["client_secret"])
            set_env("GITHUB_APP_SLUG", app["slug"])
            return self._send(200, _page("GitHub App created", f"""
<h1>✓ GitHub App “{html.escape(app['name'])}” created</h1>
<p class="sub">Saved <code>GITHUB_CLIENT_ID</code>, <code>GITHUB_CLIENT_SECRET</code>, <code>GITHUB_APP_SLUG</code> to <code>.env</code>.
Share those three lines with your team (with the Google ones).</p>
<div class="card"><p>Next: install it on the repos the team works in. An org admin does this once per org.</p>
<a class="btn" target="_blank" href="{github_auth.install_url()}">Install on repos</a>
<a class="btn ghost" href="/">Back to Connect</a></div>"""))
        if path == "/":
            msg = {"google": "Google connected ✓", "github": "GitHub connected ✓",
                   "stale": "That login link expired. Please click Connect again."}.get(q.get("msg"))
            return self._send(200, _main_page(msg))
        if path == "/api/status":
            return self._send(200, json.dumps({
                "google": google_auth.whoami() if settings.google_token.exists() else None,
                "google_configured": google_auth.client_config() is not None,
                "github": github_auth.whoami(), "github_configured": github_auth.configured(),
            }), "application/json")
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/google/disconnect":
            google_auth.disconnect()
        elif path == "/github/disconnect":
            github_auth.disconnect()
        else:
            return self._send(404, "not found", "text/plain")
        self._send(200, '{"ok": true}', "application/json")


def serve(port: int = DEFAULT_PORT, open_browser: bool = True, start_path: str = "/") -> None:
    server = ThreadingHTTPServer((HOST, port), Handler)
    url = f"http://{HOST}:{server.server_port}{start_path}"
    print(f"Connect page: {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
