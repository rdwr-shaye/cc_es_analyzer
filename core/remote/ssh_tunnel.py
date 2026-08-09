"""
Persistent SSH local port-forward so the app can reach an Elasticsearch /
OpenSearch that is only reachable on the remote host's loopback, or that sits
behind a perimeter firewall which permits SSH (22) but blocks the ES port.

It opens an SSH session to the remote host and forwards:

    127.0.0.1:<local_port>   (a socket inside THIS process)
        --- over the SSH connection (port 22) --->
    <remote_host>:<remote_port>   as seen from the remote box (default 127.0.0.1:9200)

so every ES request "rides" inside SSH — the firewall only ever sees port 22.

A single active tunnel is kept as a module-level singleton; starting a new one
tears down the previous. The forwarding loop is the same pattern as
`deploy/tunnel.py`, adapted to run inside the service for the session lifetime.
"""
from __future__ import annotations

import logging
import select
import socketserver
import threading

logger = logging.getLogger(__name__)

_active: "_Tunnel | None" = None
_lock = threading.Lock()


class _Handler(socketserver.BaseRequestHandler):
    """Relays one accepted local connection to the remote over an SSH channel.

    The server carries `ssh_transport`, `remote_host`, `remote_port`.
    """

    def handle(self):
        srv = self.server
        try:
            chan = srv.ssh_transport.open_channel(
                "direct-tcpip",
                (srv.remote_host, srv.remote_port),
                self.request.getpeername(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ssh-tunnel] channel open failed: %s", exc)
            return
        if chan is None:
            return
        try:
            while True:
                r, _, _ = select.select([self.request, chan], [], [], 60)
                if self.request in r:
                    data = self.request.recv(4096)
                    if not data:
                        break
                    chan.sendall(data)
                if chan in r:
                    data = chan.recv(4096)
                    if not data:
                        break
                    self.request.sendall(data)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try: chan.close()
            except Exception: pass
            try: self.request.close()
            except Exception: pass


class _ForwardServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Tunnel:
    def __init__(self, client, server, thread, local_host, local_port, meta):
        self.client = client
        self.server = server
        self.thread = thread
        self.local_host = local_host
        self.local_port = local_port
        self.meta = meta

    def alive(self) -> bool:
        tr = self.client.get_transport() if self.client else None
        return bool(tr and tr.is_active())

    def close(self):
        try: self.server.shutdown(); self.server.server_close()
        except Exception: pass
        try: self.client.close()
        except Exception: pass


def start_tunnel(ssh_host: str, ssh_user: str, ssh_password: str,
                 ssh_port: int = 22, remote_host: str = "127.0.0.1",
                 remote_port: int = 9200, local_host: str = "127.0.0.1",
                 timeout: float = 20.0) -> dict:
    """
    (Re)start the SSH tunnel. Any existing tunnel is torn down first.

    Returns {"ok": True, "local_host", "local_port"} or {"ok": False, "error"}.
    """
    try:
        import paramiko
    except ImportError:
        return {"ok": False, "error": "paramiko is not installed (see requirements.txt)."}

    stop_tunnel()

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(hostname=ssh_host, port=ssh_port, username=ssh_user,
                       password=ssh_password, timeout=timeout,
                       banner_timeout=timeout, auth_timeout=timeout,
                       allow_agent=False, look_for_keys=False)
    except Exception as exc:  # noqa: BLE001 - surface a clear message to the UI
        msg = str(exc)
        if exc.__class__.__name__ == "AuthenticationException":
            msg = "SSH authentication failed — check the SSH username/password."
        logger.error("[ssh-tunnel] SSH connect to %s@%s:%s failed: %s",
                     ssh_user, ssh_host, ssh_port, exc)
        return {"ok": False, "error": f"SSH connection failed: {msg}"}

    transport = client.get_transport()
    try:
        transport.set_keepalive(30)
    except Exception:
        pass

    try:
        server = _ForwardServer((local_host, 0), _Handler)
    except Exception as exc:  # noqa: BLE001
        client.close()
        return {"ok": False, "error": f"could not open local forward socket: {exc}"}

    server.ssh_transport = transport
    server.remote_host = remote_host
    server.remote_port = remote_port
    local_port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, name="ssh-tunnel", daemon=True)
    thread.start()

    global _active
    with _lock:
        _active = _Tunnel(client, server, thread, local_host, local_port, {
            "ssh_host": ssh_host, "ssh_port": ssh_port,
            "remote": f"{remote_host}:{remote_port}",
        })

    logger.info("[ssh-tunnel] up: %s:%s -> (ssh %s@%s:%s) -> %s:%s",
                local_host, local_port, ssh_user, ssh_host, ssh_port,
                remote_host, remote_port)
    return {"ok": True, "local_host": local_host, "local_port": local_port}


def stop_tunnel() -> None:
    global _active
    with _lock:
        if _active is not None:
            _active.close()
            logger.info("[ssh-tunnel] closed")
            _active = None


def active_tunnel() -> "_Tunnel | None":
    return _active
