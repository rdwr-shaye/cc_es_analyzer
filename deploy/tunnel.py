#!/usr/bin/env python3
"""
Local SSH port-forward (tunnel) to reach the CC ES Analyzer when a perimeter
firewall only allows ports 22/80/443 to the host.

It forwards  localhost:<local_port>  ->  (through SSH)  ->  <host>:<remote_port>
so you can open  http://localhost:<local_port>/  in your browser.

Usage:
    python deploy/tunnel.py --host <host> --user root
    python deploy/tunnel.py --host <host> --user root --local 8801 --remote 8801

Password: --password / env CC_DEPLOY_PASS / interactive prompt.
Leave it running; press Ctrl+C to close the tunnel.
"""
from __future__ import annotations
import argparse, getpass, os, select, socketserver, sys, threading

try:
    import paramiko
except ImportError:
    sys.exit("paramiko is required:  pip install paramiko")


def make_handler(transport, remote_host, remote_port):
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                chan = transport.open_channel(
                    "direct-tcpip",
                    (remote_host, remote_port),
                    self.request.getpeername(),
                )
            except Exception as e:  # noqa: BLE001
                print(f"[tunnel] channel open failed: {e}")
                return
            if chan is None:
                return
            while True:
                r, _, _ = select.select([self.request, chan], [], [])
                if self.request in r:
                    data = self.request.recv(1024)
                    if not data:
                        break
                    chan.sendall(data)
                if chan in r:
                    data = chan.recv(1024)
                    if not data:
                        break
                    self.request.sendall(data)
            chan.close()
            self.request.close()
    return Handler


class ForwardServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    ap = argparse.ArgumentParser(description="SSH tunnel to reach CC ES Analyzer through a firewall.")
    ap.add_argument("--host", default=os.getenv("CC_DEPLOY_HOST"),
                    help="SSH host to tunnel through (or set CC_DEPLOY_HOST). Required.")
    ap.add_argument("--user", default=os.getenv("CC_DEPLOY_USER", "root"))
    ap.add_argument("--password", default=os.getenv("CC_DEPLOY_PASS"))
    ap.add_argument("--ssh-port", type=int, default=22)
    ap.add_argument("--local", type=int, default=8801, help="local port to listen on")
    ap.add_argument("--remote", type=int, default=8801, help="remote (host) port to reach")
    ap.add_argument("--remote-host", default="127.0.0.1", help="target as seen from the SSH host")
    args = ap.parse_args()

    if not args.host:
        ap.error("--host is required (or set CC_DEPLOY_HOST).")

    password = args.password or getpass.getpass(f"SSH password for {args.user}@{args.host}: ")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(args.host, port=args.ssh_port, username=args.user,
                   password=password, timeout=20)
    transport = client.get_transport()

    server = ForwardServer(("127.0.0.1", args.local),
                           make_handler(transport, args.remote_host, args.remote))
    print(f"[tunnel] Forwarding  http://localhost:{args.local}/  ->  "
          f"{args.host}:{args.remote}  (via SSH)")
    print("[tunnel] Open the URL in your browser. Press Ctrl+C to stop.")
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        while True:
            t.join(1)
    except KeyboardInterrupt:
        print("\n[tunnel] Closing.")
        server.shutdown()
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

