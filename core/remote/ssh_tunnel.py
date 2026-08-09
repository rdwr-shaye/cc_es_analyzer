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

# Tunnels are now keyed by NAME, because one CC needs more than one forward:
# Elasticsearch on 9200 and MariaDB on 3306 are different ports on the same
# box, and a single-slot registry meant opening the second silently tore down
# the first. The default name is "es" so every existing caller keeps its
# behaviour without passing anything.
_tunnels: dict[str, "_Tunnel"] = {}
DEFAULT_NAME = "es"


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
                 timeout: float = 20.0, name: str = DEFAULT_NAME) -> dict:
    """
    (Re)start the SSH tunnel called `name`. Only that one is torn down first —
    other names keep running, so an ES tunnel and a MariaDB tunnel to the same
    CC coexist.

    Returns {"ok": True, "local_host", "local_port"} or {"ok": False, "error"}.
    """
    try:
        import paramiko
    except ImportError:
        return {"ok": False, "error": "paramiko is not installed (see requirements.txt)."}

    stop_tunnel(name)

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
    tunnel = _Tunnel(client, server, thread, local_host, local_port, {
        "ssh_host": ssh_host, "ssh_port": ssh_port,
        "remote": f"{remote_host}:{remote_port}", "name": name,
    })
    with _lock:
        _tunnels[name] = tunnel
        if name == DEFAULT_NAME:
            _active = tunnel      # kept for callers that predate named tunnels

    logger.info("[ssh-tunnel] %s up: %s:%s -> (ssh %s@%s:%s) -> %s:%s",
                name, local_host, local_port, ssh_user, ssh_host, ssh_port,
                remote_host, remote_port)
    return {"ok": True, "local_host": local_host, "local_port": local_port,
            "name": name}


def stop_tunnel(name: str = DEFAULT_NAME) -> None:
    global _active
    with _lock:
        tunnel = _tunnels.pop(name, None)
        if tunnel is not None:
            tunnel.close()
            logger.info("[ssh-tunnel] %s closed", name)
        if name == DEFAULT_NAME:
            _active = None


def stop_all_tunnels() -> None:
    for name in list(_tunnels):
        stop_tunnel(name)


def active_tunnel(name: str = DEFAULT_NAME) -> "_Tunnel | None":
    tunnel = _tunnels.get(name)
    # A tunnel whose thread has died is worse than no tunnel: it still answers
    # on the local port and every connection through it hangs.
    if tunnel is not None and not tunnel.alive():
        stop_tunnel(name)
        return None
    return tunnel
