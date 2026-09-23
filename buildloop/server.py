"""`buildloop serve` — the dashboards, plus a box to ask Claude about them.

A static `file://` page cannot run the `claude` CLI, so asking questions needs
something on this machine to do it. This is that, and deliberately no more:
it binds to 127.0.0.1, runs in the foreground, and stops on Ctrl-C. The static
dashboards written by `refresh` do not depend on it.

Being on localhost does not make it private. Any web page open in the browser
can send requests to 127.0.0.1, and every question costs a model call, so
`POST /api/ask` has to prove it came from a page this server rendered:

* **Host header** must be this server's own address. Blocks DNS rebinding,
  where a hostile domain re-resolves to 127.0.0.1 to become "same origin".
* **Content-Type: application/json.** A cross-origin page cannot send that
  without a CORS preflight, and this server answers no preflights.
* **A per-run token**, embedded only in pages this server rendered and
  required in a header. A page elsewhere never sees it.
* **Origin**, when the browser sends one, must match.

The server sends no CORS headers at all, so nothing cross-origin can read a
response either.
"""

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from . import ask, claude_cli, dashboard, db, insights
from .charts import esc
from .config import Config

BIND = "127.0.0.1"
SERVER_NAME = "buildloop"
MAX_BODY_BYTES = 32_000

Answerer = Callable[[sqlite3.Connection, str, object, object], str]


class BuildLoopServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config: Config, port: int, *, answerer: Answerer = ask.answer,
                 token: str | None = None, quiet: bool = False):
        self.config = config
        self.quiet = quiet
        self.token = token or secrets.token_urlsafe(24)
        self.answerer = answerer
        super().__init__((BIND, port), _Handler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"


class _Handler(BaseHTTPRequestHandler):
    server: BuildLoopServer
    server_version = SERVER_NAME

    # --- routing ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 — http.server's naming
        if not self._host_ok():
            return self._text(HTTPStatus.FORBIDDEN, "wrong host")
        path = self.path.split("?", 1)[0]
        names = [p.name for p in self.server.config.projects]

        if path == "/":
            if len(names) == 1:
                return self._redirect(f"/p/{names[0]}")
            return self._html(HTTPStatus.OK, _index(names))
        if path.startswith("/p/"):
            name = unquote(path[3:])
            if name not in names:
                return self._text(HTTPStatus.NOT_FOUND, "unknown project")
            with db.open_db(self.server.config.db_path) as conn:
                page = dashboard.render(
                    conn, name, insights.load_cached(conn, name),
                    ask={"project": name, "token": self.server.token,
                         "available": claude_cli.available()},
                    repo=self.server.config.project(name).github_repo,
                )
            return self._html(HTTPStatus.OK, page)
        return self._text(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/ask":
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        rejection = self._reject_untrusted()
        if rejection:
            status, message = rejection
            return self._json(status, {"error": message})

        try:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        except (ValueError, TypeError):
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "body is not JSON"})
        if not isinstance(payload, dict):
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "body must be an object"})

        project = payload.get("project")
        if project not in {p.name for p in self.server.config.projects}:
            return self._json(HTTPStatus.NOT_FOUND, {"error": "unknown project"})

        try:
            with db.open_db(self.server.config.db_path) as conn:
                text = self.server.answerer(
                    conn, project, payload.get("question"), payload.get("history"),
                )
        except ask.BadQuestion as exc:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except claude_cli.ClaudeUnavailable as exc:
            return self._json(HTTPStatus.SERVICE_UNAVAILABLE,
                              {"error": f"Claude couldn't answer: {exc}"})
        return self._json(HTTPStatus.OK, {"answer": text})

    # --- request trust ------------------------------------------------------

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "") in self.server.allowed_hosts

    def _reject_untrusted(self) -> tuple[HTTPStatus, str] | None:
        if not self._host_ok():
            return HTTPStatus.FORBIDDEN, "wrong host"
        origin = self.headers.get("Origin")
        if origin is not None and origin.removeprefix("http://") not in self.server.allowed_hosts:
            return HTTPStatus.FORBIDDEN, "cross-origin request"
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "expected application/json"
        token = self.headers.get("X-Buildloop-Token") or ""
        if not hmac.compare_digest(token.encode(), self.server.token.encode()):
            return HTTPStatus.FORBIDDEN, "missing or stale token — reload the page"
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            return HTTPStatus.LENGTH_REQUIRED, "Content-Length required"
        if length < 0 or length > MAX_BODY_BYTES:
            return HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request too large"
        return None

    # --- responses ----------------------------------------------------------

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, status: HTTPStatus, text: str) -> None:
        self._send(status, text.encode("utf-8"), "text/html; charset=utf-8")

    def _json(self, status: HTTPStatus, obj: dict) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json")

    def _text(self, status: HTTPStatus, text: str) -> None:
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        # One line per request; never the request body, which is the user's
        # question.
        if self.server.quiet:
            return
        print(f"  {self.command} {self.path.split('?', 1)[0]} -> {args[1] if len(args) > 1 else ''}", flush=True)


def already_serving(port: int, timeout: float = 1.0) -> bool:
    """Is a buildloop server already listening on this port?

    "Address already in use" has two very different causes — your own server
    from earlier, or something unrelated — and they want opposite responses.
    Identified by the Server header, which only this server sends.
    """
    import http.client

    conn = http.client.HTTPConnection(BIND, port, timeout=timeout)
    try:
        conn.request("HEAD", "/", headers={"Host": f"{BIND}:{port}"})
        return (conn.getresponse().getheader("Server") or "").startswith(SERVER_NAME)
    except OSError:
        return False
    finally:
        conn.close()


def _index(names: list[str]) -> str:
    links = "".join(f'<li><a href="/p/{esc(n)}">{esc(n)}</a></li>' for n in names)
    return (
        "<!doctype html><meta charset=utf-8><title>buildloop</title>"
        "<style>body{font:15px/1.6 -apple-system,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem}"
        "</style><h1>buildloop</h1><ul>" + links + "</ul>"
    )
