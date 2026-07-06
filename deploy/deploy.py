#!/usr/bin/env python3
"""
CC Elasticsearch Analyzer — one-shot deployer.

Packs the project into a lean build context, uploads it to a remote Linux host
over SSH/SFTP, and runs `deploy/remote_deploy.sh` there to build + start a single
isolated Docker container. It NEVER touches other containers on the host.

Examples
--------
    python deploy/deploy.py --host <host> --user root --password '***'
    python deploy/deploy.py --host <host> --user root         # prompts for password
    CC_DEPLOY_HOST=<host> CC_DEPLOY_PASS=*** python deploy/deploy.py

The target host is required (--host or CC_DEPLOY_HOST); the app can run on any
machine. Secrets are never written to disk or committed — pass them at runtime.
"""
from __future__ import annotations

import argparse
import getpass
import io
import os
import sys
import tarfile
import tempfile

try:
    import paramiko
except ImportError:
    sys.exit("paramiko is required:  pip install paramiko")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REMOTE_BASE = "/opt/cc_es_analyzer"

# Directories/files kept OUT of the uploaded build context.
EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "env", ".idea", ".claude", "__pycache__",
    "logs", "dist", "build", "node_modules",
}
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".log", ".tar", ".tar.gz")
EXCLUDE_NAMES = {".env"}


def _should_skip(rel_path: str) -> bool:
    parts = rel_path.replace("\\", "/").split("/")
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    name = parts[-1]
    if name in EXCLUDE_NAMES:
        return True
    if name.startswith("HANDOFF"):
        return True
    return rel_path.endswith(EXCLUDE_SUFFIXES)


def build_context_tar() -> str:
    """Create a gzip tarball of the project (lean) and return its local path."""
    fd, tar_path = tempfile.mkstemp(suffix=".tar.gz", prefix="cc_es_ctx_")
    os.close(fd)
    added = 0
    with tarfile.open(tar_path, "w:gz") as tar:
        for root, dirs, files in os.walk(PROJECT_ROOT):
            # prune excluded dirs in-place for speed
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for f in files:
                abs_p = os.path.join(root, f)
                rel_p = os.path.relpath(abs_p, PROJECT_ROOT)
                if _should_skip(rel_p):
                    continue
                tar.add(abs_p, arcname=rel_p)
                added += 1
    print(f"[deploy] Packed {added} files -> {tar_path} "
          f"({os.path.getsize(tar_path) / 1024:.0f} KB)")
    return tar_path


def run(host: str, user: str, password: str, port: int, ssh_port: int) -> int:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"[deploy] Connecting to {user}@{host}:{ssh_port} …")
    ssh.connect(host, port=ssh_port, username=user, password=password, timeout=20)

    try:
        # Ensure remote base dir exists.
        _exec(ssh, f"mkdir -p {REMOTE_BASE}")

        sftp = ssh.open_sftp()
        # Upload build context.
        local_tar = build_context_tar()
        remote_tar = f"{REMOTE_BASE}/context.tar.gz"
        print(f"[deploy] Uploading context -> {remote_tar}")
        sftp.put(local_tar, remote_tar)
        os.remove(local_tar)

        # Upload the remote deploy script, normalised to LF line endings.
        local_sh = os.path.join(PROJECT_ROOT, "deploy", "remote_deploy.sh")
        with open(local_sh, "r", encoding="utf-8") as fh:
            script = fh.read().replace("\r\n", "\n")
        remote_sh = f"{REMOTE_BASE}/remote_deploy.sh"
        with sftp.file(remote_sh, "w") as rf:
            rf.write(script)
        sftp.chmod(remote_sh, 0o755)
        sftp.close()

        # Execute the deploy on the host, streaming output live.
        print(f"[deploy] Running remote_deploy.sh (host port {port}) …\n")
        code = _exec_stream(ssh, f"bash {remote_sh} {port}")
        return code
    finally:
        ssh.close()


def _exec(ssh: "paramiko.SSHClient", cmd: str) -> None:
    _in, out, err = ssh.exec_command(cmd)
    rc = out.channel.recv_exit_status()
    if rc != 0:
        raise RuntimeError(f"remote cmd failed ({rc}): {cmd}\n{err.read().decode()}")


def _exec_stream(ssh: "paramiko.SSHClient", cmd: str) -> int:
    _in, out, _err = ssh.exec_command(cmd, get_pty=True)
    chan = out.channel
    while True:
        if chan.recv_ready():
            sys.stdout.write(chan.recv(4096).decode(errors="replace"))
            sys.stdout.flush()
        if chan.exit_status_ready() and not chan.recv_ready():
            break
    # drain any remaining bytes
    while chan.recv_ready():
        sys.stdout.write(chan.recv(4096).decode(errors="replace"))
    sys.stdout.flush()
    return chan.recv_exit_status()


def main() -> int:
    ap = argparse.ArgumentParser(description="Deploy CC ES Analyzer to a remote host via Docker.")
    ap.add_argument("--host", default=os.getenv("CC_DEPLOY_HOST"),
                    help="Target host to deploy to (or set CC_DEPLOY_HOST). Required.")
    ap.add_argument("--user", default=os.getenv("CC_DEPLOY_USER", "root"))
    ap.add_argument("--password", default=os.getenv("CC_DEPLOY_PASS"))
    ap.add_argument("--port", type=int, default=int(os.getenv("CC_DEPLOY_PORT", "8801")),
                    help="Desired host port (auto-advances if busy). Default 8801.")
    ap.add_argument("--ssh-port", type=int, default=22)
    args = ap.parse_args()

    if not args.host:
        ap.error("--host is required (or set CC_DEPLOY_HOST).")

    password = args.password or getpass.getpass(f"SSH password for {args.user}@{args.host}: ")

    try:
        code = run(args.host, args.user, password, args.port, args.ssh_port)
    except Exception as exc:  # noqa: BLE001
        print(f"[deploy] ERROR: {exc}", file=sys.stderr)
        return 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())

