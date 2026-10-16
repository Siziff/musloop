"""Run musloop: the main app plus an optional remote-only listener.

    python -m backend.serve

Docker uses this (see Dockerfile). The main port (PORT, 8801) is published on
the host's 127.0.0.1 only and treated as "this computer"
(MUSLOOP_MAIN_TRUSTED=1). The remote port (MUSLOOP_REMOTE_PORT, 8802) can be
published to the LAN: whatever arrives there only reaches the phone remote
(backend/remote.py), never the rest of the app.

A native run (run.sh / run.bat) keeps using plain uvicorn on one port, with
access decided by the client address.
"""
from __future__ import annotations

import os
import socket

import uvicorn


def _listen(port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(2048)
    s.set_inheritable(True)
    return s


def main() -> None:
    port = int(os.environ.get("PORT", "8801"))
    rport = os.environ.get("MUSLOOP_REMOTE_PORT", "").strip()
    sockets = [_listen(port)]
    if rport.isdigit() and int(rport) != port:
        sockets.append(_listen(int(rport)))
    config = uvicorn.Config("backend.main:app", host="0.0.0.0", port=port,
                            log_level=os.environ.get("LOG_LEVEL", "info"))
    print(f"musloop: app on :{port}" + (f", phone remote on :{rport}" if len(sockets) > 1 else ""),
          flush=True)
    uvicorn.Server(config).run(sockets=sockets)


if __name__ == "__main__":
    main()
