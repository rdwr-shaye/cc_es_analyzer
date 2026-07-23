"""
Thin paramiko wrapper for the snapshot-archive flows.

The snapshot files are written by ES/OpenSearch itself on the ES MACHINE; this
module moves them between that machine and the analyzer's storage (zip, SFTP,
cleanup) over SSH as root. Connections are short-lived — one per job phase set —
and credentials come from services.cred_store or a one-time UI prompt.
"""
import logging
import os

import paramiko

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 15


class SSHError(Exception):
    """A remote command failed; message carries the command's stderr."""


class SSHSession:
    """One authenticated SSH+SFTP session to an ES machine."""

    def __init__(self, host: str, user: str, password: str, port: int = 22):
        self.host = host
        self.client = paramiko.SSHClient()
        # CC appliances are reached by IP and re-imaged freely — pinning host
        # keys would break every re-install, so accept them automatically.
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(host, port=port, username=user, password=password,
                            timeout=_CONNECT_TIMEOUT, allow_agent=False,
                            look_for_keys=False)
        self._sftp = None

    def run(self, cmd: str, timeout: int = 600) -> str:
        """Run a command; return stdout. Raises SSHError on non-zero exit."""
        logger.info("[ssh %s] $ %s", self.host, cmd)
        _, stdout, stderr = self.client.exec_command(cmd, timeout=timeout)
        rc = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace").strip()
        logger.info("[ssh %s] rc=%s%s%s", self.host, rc,
                    f" stdout: {out.strip()[:800]}" if out.strip() else "",
                    f" stderr: {err[:400]}" if err else "")
        if rc != 0:
            raise SSHError(f"`{cmd}` failed (rc={rc}): {err or out.strip()}")
        return out

    def sftp(self):
        if self._sftp is None:
            self._sftp = self.client.open_sftp()
        return self._sftp

    def get(self, remote: str, local: str, progress_cb=None) -> None:
        """Download remote → local. progress_cb(done_bytes, total_bytes)."""
        logger.info("[ssh %s] SFTP GET %s -> %s", self.host, remote, local)
        self.sftp().get(remote, local, callback=progress_cb)
        logger.info("[ssh %s] SFTP GET done (%s bytes)", self.host,
                    os.path.getsize(local))

    def put(self, local: str, remote: str, progress_cb=None) -> None:
        """Upload local → remote. progress_cb(done_bytes, total_bytes)."""
        logger.info("[ssh %s] SFTP PUT %s (%s bytes) -> %s", self.host, local,
                    os.path.getsize(local), remote)
        self.sftp().put(local, remote, callback=progress_cb)
        logger.info("[ssh %s] SFTP PUT done", self.host)

    def close(self) -> None:
        try:
            if self._sftp is not None:
                self._sftp.close()
        finally:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def check_login(host: str, user: str, password: str) -> str | None:
    """Try to connect and run a trivial command. Returns an error message
    (None when the login works) — used to validate credentials up front."""
    try:
        with SSHSession(host, user, password) as ssh:
            ssh.run("true", timeout=20)
        return None
    except Exception as exc:
        return str(exc)
