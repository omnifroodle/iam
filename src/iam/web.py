"""The user's page: proposals waiting for review at the top, the vault's history below,
drawn like `git log --graph` with global memories on main and one branch per project.

Standard library only, one thread, bound to 127.0.0.1. Reads are the user's own view and
are not logged; every accept and reject goes through iam.core and is audited via "web".
A random token minted at start-up guards writes: the page carries it, another site in the
same browser cannot read it, and a Host check stops DNS rebinding from borrowing the port.
"""

from __future__ import annotations

import json
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from typing import Any

from .core import Iam, IamError
from .models import USER, AuditEntry, Memory

READS = {"recall", "list", "show", "export"}


def _first(text: str | None, n: int = 100) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= n else line[: n - 1] + "…"


def commit(e: AuditEntry, m: Memory | None) -> dict[str, Any]:
    """One audit row as a commit: a title, the reason as its message, the actor as author."""
    r, res = e.request, e.result
    ids = res.get("memory_ids") or []
    text = r.get("text") or (m.text if m else "")
    a = e.action
    if a == "remember":
        if r.get("accepted"):
            title = f"accept: {_first(text)}"
        elif r.get("supersedes"):
            title = f"supersede: {_first(text)}"
        elif res.get("status") == "proposed":
            title = f"propose: {_first(text)}"
        else:
            title = f"remember: {_first(text)}"
    elif a == "forget":
        title = f"forget: {_first(m.text) if m else r.get('id')}"
    elif a == "recall":
        title = f"recall “{_first(r.get('query'), 60)}” → {len(ids)}"
    elif a == "list":
        what = ",".join(r.get("types") or ["all"])
        title = f"list {what} {','.join(r.get('statuses') or [])} → {len(ids)}"
    elif a in ("grant", "revoke"):
        mode = f" {r['mode']}" if r.get("mode") else ""
        title = f"{a} {r.get('client')}{mode} on {r.get('vault')}"
    elif a == "capture":
        title = f"capture {r.get('kind')}: {_first(r.get('origin'), 80)}"
    elif a == "extract":
        title = f"extract: {res.get('count', 0)} proposal(s) by {r.get('model')}"
    elif a == "migrate":
        title = f"migrate: {r.get('change')} ({res.get('count', 0)})"
    elif a == "evict":
        title = f"evict {r.get('agent')}: {len(res.get('removed') or [])} file(s)"
    else:
        title = a
    project = r.get("project") if "project" in r else (m.project if m else None)
    return {
        "id": e.id,
        "short": e.id[-7:].lower(),
        "at": e.at.isoformat(),
        "actor": e.actor,
        "via": e.via,
        "session": e.session,
        "action": a,
        "read": a in READS,
        "project": project,
        "vault": r.get("vault") or res.get("vault") or (m.vault if m else None),
        "title": title,
        "reason": e.reason,
        "text": text if a in ("remember", "forget") and "\n" in (text or "") else None,
        "memory": {"id": m.id, "status": m.status} if m else None,
        "count": len(ids),
    }


def proposal(iam: Iam, m: Memory) -> dict[str, Any]:
    src = iam.store.get_source(m.provenance.source_ids[0]) if m.provenance.source_ids else None
    return {
        "id": m.id,
        "type": m.type,
        "vault": m.vault,
        "project": m.project,
        "categories": m.categories,
        "text": m.text,
        "created_at": m.created_at.isoformat(),
        "actor": m.provenance.actor,
        "via": m.provenance.via,
        "model": m.provenance.model,
        "reason": m.provenance.reason,
        "evidence": m.meta.get("evidence"),
        "source": src.origin if src else None,
    }


def state(iam: Iam, *, limit: int = 300) -> dict[str, Any]:
    history = [commit(e, m) for e, m in iam.history(USER, limit=limit)]
    return {
        "head": history[0]["id"] if history else None,
        "pending": [proposal(iam, m) for m in iam.review_queue(USER)],
        "history": history,
    }


def head(iam: Iam) -> dict[str, Any]:
    newest = iam.store.query_audit(limit=1)
    return {"head": newest[0].id if newest else None}


def make_handler(iam: Iam, token: str, port: int):
    page = files("iam").joinpath("web.html").read_text().replace("{{TOKEN}}", token)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet; the audit log is the record
            pass

        def _send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj: Any) -> None:
            self._send(status, json.dumps(obj).encode(), "application/json")

        def _host_ok(self) -> bool:
            if self.headers.get("Host") in hosts:
                return True
            self._json(HTTPStatus.FORBIDDEN, {"error": "wrong host"})
            return False

        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return
            path, _, query = self.path.partition("?")
            if path == "/":
                self._send(HTTPStatus.OK, page.encode(), "text/html; charset=utf-8")
            elif path == "/api/head":
                self._json(HTTPStatus.OK, head(iam))
            elif path == "/api/state":
                limit = 300
                for part in query.split("&"):
                    k, _, v = part.partition("=")
                    if k == "limit" and v.isdigit():
                        limit = min(int(v), 5000)
                self._json(HTTPStatus.OK, state(iam, limit=limit))
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if not self._host_ok():
                return
            origin = self.headers.get("Origin")
            if self.headers.get("X-Iam-Token") != token or (
                origin and origin.split("//", 1)[-1] not in hosts
            ):
                self._json(HTTPStatus.FORBIDDEN, {"error": "bad token"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "expected JSON"})
                return
            reason = (body.get("reason") or "").strip()
            try:
                if self.path == "/api/accept":
                    m = iam.accept(
                        USER,
                        body["id"],
                        reason=reason or "accepted in review",
                        text=body.get("text"),
                        everywhere=bool(body.get("everywhere")),
                    )
                elif self.path == "/api/reject":
                    m = iam.forget(USER, body["id"], reason=reason or "rejected in review")
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
            except (IamError, KeyError) as e:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
                return
            self._json(HTTPStatus.OK, {"id": m.id, "status": m.status, "project": m.project})

    return Handler


def serve(iam: Iam, port: int = 8377) -> tuple[HTTPServer, str]:
    """Bind 127.0.0.1 (never anything wider) and return the server and its URL."""
    token = secrets.token_urlsafe(24)
    server = HTTPServer(("127.0.0.1", port), None)  # type: ignore[arg-type]
    server.RequestHandlerClass = make_handler(iam, token, server.server_address[1])
    return server, f"http://127.0.0.1:{server.server_address[1]}/"
