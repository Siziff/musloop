"""Phone remote: a web page on the phone controls Live mode on the PC.

Audio plays in the musloop tab on the PC. The phone only sends intents
("cue part 3", "scene Ambush", "Next") and shows the PC's state, relayed
through this server over WebSockets:

    phone (/remote) --ws--> server --ws--> PC tab (the "host", plays audio)
                    <-------  state  <-------

Security (the server binds 0.0.0.0, so the LAN can reach it):
  * requests from this computer (loopback) are trusted, as before;
  * requests from the network need the remote key (from the QR code shown in
    Live mode) and may only reach the remote page, its WebSocket and a few
    read-only endpoints. Everything else (folders, uploads, deletes, exports,
    analysis...) is refused for network clients, with or without a key.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import secrets
import socket
import time

from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from starlette.responses import JSONResponse, PlainTextResponse

log = logging.getLogger("musloop")

# Rotates on every server start; "Reset" in the UI rotates it on demand.
_KEY = {"value": secrets.token_urlsafe(9)}

# Files a phone may load without the key (the page has to load before it can
# read the key from its own URL). Exact paths, static and harmless.
REMOTE_PUBLIC = {"/remote", "/remote.js", "/vendor/react.min.js", "/vendor/react-dom.min.js",
                 "/keepawake.mp4", "/keepawake.webm", "/favicon.ico"}
# API a phone may call with the key (read-only). Commands go over /ws/remote.
REMOTE_ALLOWED = {"/api/remote/ping"}
# The Android app's WebView runs on its own origin (Capacitor), not the PC's.
APP_ORIGINS = {"https://localhost", "http://localhost", "capacitor://localhost"}


def remote_key() -> str:
    return _KEY["value"]


def rotate_key() -> str:
    _KEY["value"] = secrets.token_urlsafe(9)
    return _KEY["value"]


def is_local(host: str | None) -> bool:
    """Client address is this computer (loopback) or a network listed in
    MUSLOOP_TRUST_NETWORK (comma-separated CIDRs, e.g. to use the full app
    from another computer at home)."""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host in ("localhost", "testclient")
    if ip.is_loopback:
        return True
    trust = os.environ.get("MUSLOOP_TRUST_NETWORK", "").strip()
    for net in filter(None, (n.strip() for n in trust.split(","))):
        try:
            if ip in ipaddress.ip_network(net, strict=False):
                return True
        except ValueError:
            continue
    return False


def _remote_port() -> int | None:
    v = os.environ.get("MUSLOOP_REMOTE_PORT", "").strip()
    return int(v) if v.isdigit() else None


def trusted(scope) -> bool:
    """Full access for this request?

    * a request on the dedicated remote port (MUSLOOP_REMOTE_PORT, Docker)
      is never trusted, whatever its source address — it can only reach the
      phone remote;
    * MUSLOOP_MAIN_TRUSTED=1 (Docker): the main port is published on the
      host's 127.0.0.1 only, so anything arriving there is this computer —
      the source address inside a container is a gateway, not loopback;
    * otherwise (native run): by client address."""
    server = scope.get("server") or (None, None)
    rport = _remote_port()
    if rport and server[1] == rport:
        return False
    if os.environ.get("MUSLOOP_MAIN_TRUSTED") == "1":
        return True
    return is_local((scope.get("client") or (None, None))[0])


def _key_ok(given: str | None) -> bool:
    return bool(given) and secrets.compare_digest(given, remote_key())


def lan_addresses() -> list[str]:
    """IPv4 addresses of this machine that a phone on the same Wi-Fi can use
    (private ranges, no loopback / link-local / docker bridges first)."""
    found: list[str] = []
    try:  # the address used for outgoing traffic = the main LAN address
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        found.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    out = []
    for a in found:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or not ip.is_private or a in out:
            continue
        out.append(a)
    # docker / virtual bridges (172.17-31.x) last
    out.sort(key=lambda a: a.startswith("172."))
    return out


def remote_urls(port: int) -> list[str]:
    """URLs for the QR code. MUSLOOP_PUBLIC_URL wins (Docker: the container
    cannot see the host's LAN address)."""
    key = remote_key()
    public = os.environ.get("MUSLOOP_PUBLIC_URL", "").strip().rstrip("/")
    if public:
        return [f"{public}/remote#k={key}"]
    if in_docker():
        return []  # the container's own address is useless: the UI asks for the PC's IP
    return [f"http://{a}:{port}/remote#k={key}" for a in lan_addresses()]


def in_docker() -> bool:
    return os.environ.get("MUSLOOP_IN_DOCKER") == "1" or os.path.exists("/.dockerenv")


class SecurityMiddleware:
    """Pure ASGI middleware (does not break WebSockets / streaming)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        client = scope.get("client") or (None, None)
        if trusted(scope):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if scope["type"] == "websocket":
            # the remote socket authenticates itself (key in the first message)
            if path == "/ws/remote":
                return await self.app(scope, receive, send)
            return await _ws_reject(scope, receive, send)
        method = scope.get("method", "GET")
        if method in ("GET", "HEAD") and path in REMOTE_PUBLIC:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        origin = headers.get(b"origin", b"").decode()
        # CORS preflight from the app for the key-checked ping
        if method == "OPTIONS" and path in REMOTE_ALLOWED and origin in APP_ORIGINS:
            return await PlainTextResponse("", 204, headers=_cors(origin))(scope, receive, send)
        key = headers.get(b"x-musloop-key", b"").decode() or None
        if method in ("GET", "HEAD") and path in REMOTE_ALLOWED and _key_ok(key):
            if origin in APP_ORIGINS:
                return await self.app(scope, receive, _with_headers(send, _cors(origin)))
            return await self.app(scope, receive, send)
        log.info("remote: refused %s %s from %s", method, path, client[0])
        resp = (PlainTextResponse("musloop: this page is only available on the computer running musloop. "
                                  "To control it from a phone, scan the QR code in Live mode.", 403)
                if method == "GET" and not path.startswith("/api/")
                else JSONResponse({"detail": "Not allowed from the network"}, 403))
        return await resp(scope, receive, send)


def _cors(origin: str) -> dict:
    return {"Access-Control-Allow-Origin": origin, "Vary": "Origin",
            "Access-Control-Allow-Headers": "X-Musloop-Key",
            "Access-Control-Allow-Methods": "GET, OPTIONS", "Access-Control-Max-Age": "600"}


def _with_headers(send, extra: dict):
    async def wrapped(message):
        if message["type"] == "http.response.start":
            message = dict(message)
            message["headers"] = list(message.get("headers") or []) + \
                [(k.lower().encode(), v.encode()) for k, v in extra.items()]
        await send(message)
    return wrapped


async def _ws_reject(scope, receive, send):
    await receive()  # websocket.connect
    await send({"type": "websocket.close", "code": 4403})


# --------------------------------------------------------------------------
# Relay: one host (the PC tab that plays), any number of remotes (phones).
# --------------------------------------------------------------------------
class Relay:
    def __init__(self):
        self.host: WebSocket | None = None
        self.remotes: dict[WebSocket, dict] = {}
        self.state: dict | None = None      # last state from the host
        self.lock = asyncio.Lock()

    def info(self) -> dict:
        return {"host": self.host is not None, "remotes": len(self.remotes)}

    async def _send(self, ws: WebSocket, msg: dict) -> bool:
        try:
            await ws.send_text(json.dumps(msg))
            return True
        except Exception:  # noqa: BLE001 - peer went away
            return False

    async def broadcast_remotes(self, msg: dict):
        dead = [ws for ws in list(self.remotes) if not await self._send(ws, msg)]
        for ws in dead:
            self.remotes.pop(ws, None)

    async def presence(self):
        """Tell everybody who is connected (host sees the phone count, phones
        see whether the PC is online)."""
        info = self.info()
        if self.host:
            await self._send(self.host, {"type": "presence", **info})
        await self.broadcast_remotes({"type": "presence", **info})


RELAY = Relay()


# --------------------------------------------------------------------------
# Discovery: the app broadcasts "MUSLOOP?" on UDP 8803; we answer with the
# remote port and the computer's name (no key — pairing still needs the QR).
# --------------------------------------------------------------------------
DISCOVERY_PORT = 8803


class _Discovery(asyncio.DatagramProtocol):
    def __init__(self, port: int):
        self.port = port

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if data.strip() != b"MUSLOOP?":
            return
        reply = json.dumps({"app": "musloop", "port": self.port, "name": socket.gethostname()})
        try:
            self.transport.sendto(reply.encode(), addr)
        except OSError:
            pass


async def start_discovery(port: int):
    if os.environ.get("MUSLOOP_DISCOVERY", "1") == "0":
        return None
    try:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Discovery(port), local_addr=("0.0.0.0", DISCOVERY_PORT), allow_broadcast=True)
        log.info("remote: discovery on udp/%d", DISCOVERY_PORT)
        return transport
    except OSError as e:   # port busy (second musloop) - discovery is optional
        log.info("remote: discovery off (%s)", e)
        return None


def register(app):
    """Attach the remote endpoints + middleware to the FastAPI app."""
    frontend = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")

    @app.get("/remote")
    def remote_page():
        return FileResponse(os.path.join(frontend, "remote.html"), media_type="text/html",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/api/remote/info")
    def remote_info(request: Request):
        """For the PC: key, QR URLs, who is connected. Loopback only
        (the middleware refuses it from the network)."""
        port = request.url.port or int(os.environ.get("PORT", 8801))
        rport = _remote_port() or port
        return {"key": remote_key(), "urls": remote_urls(rport), "remote_port": rport, **RELAY.info(),
                "docker_hint": in_docker() and not os.environ.get("MUSLOOP_PUBLIC_URL")}

    @app.get("/api/remote/qr.svg")
    def remote_qr(url: str):
        """QR code (SVG) for the remote URL. Loopback only (middleware), and
        it only encodes a musloop remote URL with the current key."""
        import io
        from urllib.parse import urlparse
        from fastapi import HTTPException
        from fastapi.responses import Response
        import segno
        u = urlparse(url)
        if u.scheme not in ("http", "https") or u.path != "/remote" or u.fragment != f"k={remote_key()}":
            raise HTTPException(400, "Not a musloop remote URL")
        buf = io.BytesIO()
        segno.make(url, error="m").save(buf, kind="svg", scale=6, border=2,
                                        dark="#111419", light="#ffffff")
        return Response(buf.getvalue(), media_type="image/svg+xml",
                        headers={"Cache-Control": "no-store"})

    @app.post("/api/remote/reset")
    async def remote_reset():
        """New key: phones that had the old QR code are disconnected."""
        rotate_key()
        for ws in list(RELAY.remotes):
            try:
                await ws.close(code=4401)
            except Exception:  # noqa: BLE001
                pass
        RELAY.remotes.clear()
        await RELAY.presence()
        return {"key": remote_key()}

    @app.get("/api/remote/ping")
    def remote_ping(request: Request):
        """Pairing check for the phone app. A presented key is always checked,
        even from this computer (the Android emulator reaches the host as
        127.0.0.1), so "wrong key" behaves the same everywhere."""
        given = request.headers.get("x-musloop-key")
        if given is not None and not _key_ok(given):
            return JSONResponse({"detail": "bad key"}, 403)
        return {"ok": True, "app": "musloop", "api": 1, "name": socket.gethostname(), **RELAY.info()}

    @app.websocket("/ws/host")
    async def ws_host(ws: WebSocket):
        """The PC tab. Loopback only (middleware). The newest tab wins."""
        await ws.accept()
        old = RELAY.host
        RELAY.host = ws
        if old is not None:
            try:
                await old.send_text(json.dumps({"type": "replaced"}))
                await old.close(code=4409)
            except Exception:  # noqa: BLE001
                pass
        await RELAY.presence()
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                if msg.get("type") == "state":
                    RELAY.state = msg.get("state")
                    await RELAY.broadcast_remotes({"type": "state", "state": RELAY.state})
                elif msg.get("type") == "ack":
                    target = next((r for r, meta in RELAY.remotes.items()
                                   if meta.get("id") == msg.get("to")), None)
                    if target:
                        await RELAY._send(target, msg)
        except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
            pass
        finally:
            if RELAY.host is ws:
                RELAY.host = None
                await RELAY.presence()

    @app.websocket("/ws/remote")
    async def ws_remote(ws: WebSocket):
        """A phone. First message must be {"type":"hello","key":...}."""
        await ws.accept()
        local = trusted(ws.scope)
        try:
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=10))
        except Exception:  # noqa: BLE001
            await ws.close(code=4400)
            return
        # a presented key is always checked (the PC's own browser may omit it)
        given = hello.get("key")
        if not ((local and not given) or _key_ok(given)):
            await RELAY._send(ws, {"type": "error", "error": "bad_key"})
            await ws.close(code=4401)
            return
        rid = secrets.token_hex(4)
        RELAY.remotes[ws] = {"id": rid, "since": time.time(),
                             "ua": (hello.get("ua") or "")[:60]}
        await RELAY._send(ws, {"type": "welcome", "id": rid, **RELAY.info()})
        if RELAY.state is not None:
            await RELAY._send(ws, {"type": "state", "state": RELAY.state})
        await RELAY.presence()
        try:
            last = 0.0
            while True:
                msg = json.loads(await ws.receive_text())
                if msg.get("type") != "cmd":
                    continue
                # rate limit: a stuck button must not flood the PC
                now = time.monotonic()
                if now - last < 0.05:
                    continue
                last = now
                if RELAY.host is None:
                    await RELAY._send(ws, {"type": "ack", "to": rid, "seq": msg.get("seq"),
                                           "ok": False, "error": "host_offline"})
                    continue
                await RELAY._send(RELAY.host, {"type": "cmd", "from": rid, "seq": msg.get("seq"),
                                               "cmd": msg.get("cmd"), "args": msg.get("args") or {}})
        except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
            pass
        finally:
            RELAY.remotes.pop(ws, None)
            await RELAY.presence()

    @app.on_event("startup")
    async def _discovery():
        port = _remote_port() or int(os.environ.get("PORT", "0") or 0) or 8801
        app.state.discovery = await start_discovery(port)

    app.add_middleware(SecurityMiddleware)
