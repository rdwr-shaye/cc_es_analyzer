#!/usr/bin/env python3
"""
Find the machine's nginx reverse proxy WITHOUT knowing its name.

Container names and service names differ on every host (`docs-reverse-proxy`,
`rdwrsim-nginx`, `proxy`, a bare `nginx.service`, …), so nothing here matches on
a name. Detection works from the outside in:

  1. WHO OWNS THE WEB PORT — `ss`/`netstat` gives the PID listening on 443/80.
     That PID is then resolved to what actually serves it:
       * a process inside a container      → /proc/<pid>/cgroup carries the
                                             container id → `docker inspect`
       * docker-proxy (the userland shim)  → the container publishing that port
       * a plain host process              → nginx as a systemd service or
                                             a hand-started binary
     This is the strongest signal: it identifies the thing that actually answers
     on the port, whatever it is called.
  2. CONTAINER SCAN — any running container whose name/image/entrypoint mentions
     nginx or openresty, else any container that carries an nginx binary.
  3. HOST BINARY — an nginx/openresty binary on the host with a running master.

The result is a `NginxTarget` that hides the docker/host difference: callers ask
it to run nginx commands, dump the live config (`nginx -T`), map a config path
back to the host filesystem, test and reload — and it does the right thing for
both kinds.

Run it standalone to just report what is on a machine:

    python3 deploy/nginx_detect.py              # this machine
    python3 deploy/nginx_detect.py --host X --user root   # over SSH
"""
from __future__ import annotations
import re
import subprocess

# Ports a reverse proxy plausibly fronts, most significant first.
WEB_PORTS = (443, 80, 8443, 8080)
NGINX_BINARIES = ("nginx", "openresty")


def sh_quote(s: str) -> str:
    """Single-quote a string for /bin/sh."""
    return "'" + s.replace("'", "'\\''") + "'"


# ── The detected proxy ────────────────────────────────────────────────────────
class NginxTarget:
    """An nginx the caller can drive, whether it runs in a container or not.

    `run(cmd)` is supplied by the caller — subprocess locally, or SSH exec —
    and must return (rc, stdout, stderr).
    """

    def __init__(self, run, kind: str, *, container: str | None = None,
                 binary: str = "nginx", how: str = "", pid: str | None = None,
                 service: str | None = None):
        self._run = run
        self.kind = kind                  # "docker" | "host"
        self.container = container
        self.binary = binary              # nginx | openresty | absolute path
        self.how = how                    # human-readable detection reason
        self.pid = pid
        self.service = service            # systemd unit, when known

    # -- identity ------------------------------------------------------------
    @property
    def label(self) -> str:
        if self.kind == "docker":
            return f"container {self.container}"
        if self.service:
            return f"host service {self.service}"
        return "host process"

    def __str__(self) -> str:
        return f"{self.label} [{self.binary}] ({self.how})"

    # -- running commands ----------------------------------------------------
    def exec(self, cmd: str):
        """Run a shell command INSIDE the proxy (container namespace or host)."""
        if self.kind == "docker":
            return self._run(f"docker exec {self.container} sh -c {sh_quote(cmd)}")
        return self._run(cmd)

    def nginx(self, args: str):
        """Run the nginx binary with `args` (e.g. '-t', '-T', '-s reload')."""
        return self.exec(f"{self.binary} {args}")

    def test(self):
        return self.nginx("-t")

    def reload(self):
        """Graceful reload. Falls back to the service manager on the host, where
        a packaged nginx may refuse `-s reload` if the pid file moved."""
        rc, out, err = self.nginx("-s reload")
        if rc == 0 or self.kind == "docker":
            return rc, out, err
        for cmd in (f"systemctl reload {self.service or 'nginx'}",
                    f"service {self.service or 'nginx'} reload"):
            rc2, out2, err2 = self._run(cmd)
            if rc2 == 0:
                return rc2, out2, err2
        return rc, out, err

    # -- config files --------------------------------------------------------
    def dump(self) -> list[tuple[str, str]]:
        """`nginx -T` split into (config_path, contents).

        This is how the LIVE configuration is discovered: it lists every file
        nginx actually loaded, including ones outside /etc/nginx, so nothing has
        to be guessed from directory layout.
        """
        rc, out, err = self.nginx("-T")
        text = out + ("\n" + err if rc != 0 else "")
        sections: list[tuple[str, str]] = []
        current, buf = None, []
        for line in text.splitlines():
            m = re.match(r"^# configuration file (.+):\s*$", line)
            if m:
                if current:
                    sections.append((current, "\n".join(buf)))
                current, buf = m.group(1).strip(), []
            elif current is not None:
                buf.append(line)
        if current:
            sections.append((current, "\n".join(buf)))
        return sections

    def host_path_for(self, path: str) -> str | None:
        """The HOST path holding `path`, or None when it only exists inside the
        container image. On a host nginx the path is already a host path."""
        if self.kind != "docker":
            return path
        fmt = "{{range .Mounts}}{{.Source}}\t{{.Destination}}\n{{end}}"
        _rc, out, _ = self._run("docker inspect --format '" + fmt + "' " + self.container)
        best = None  # (len(dest), host_path) — most specific mount wins
        for line in out.splitlines():
            if "\t" not in line:
                continue
            source, dest = (p.strip() for p in line.split("\t", 1))
            if not source.startswith("/") or not dest.startswith("/"):
                continue
            d = dest.rstrip("/")
            if path == dest or path == d:
                cand = source
            elif path.startswith(d + "/"):
                cand = source.rstrip("/") + path[len(d):]
            else:
                continue
            if best is None or len(d) > best[0]:
                best = (len(d), cand)
        return best[1] if best else None

    def read_file(self, path: str) -> tuple[int, str]:
        rc, out, err = self.exec(f"cat {sh_quote(path)}")
        return rc, out

    # -- network -------------------------------------------------------------
    def network_mode(self) -> str:
        if self.kind != "docker":
            return "host"
        _rc, out, _ = self._run(
            "docker inspect --format '{{.HostConfig.NetworkMode}}' " + self.container)
        return out.strip()

    def gateway(self) -> str:
        """Docker gateway IP the container can use to reach the host."""
        if self.kind != "docker":
            return ""
        _rc, out, _ = self._run(
            "docker inspect --format '{{range .NetworkSettings.Networks}}{{.Gateway}}\n{{end}}' "
            + self.container)
        return next((l.strip() for l in out.splitlines()
                     if l.strip() and l.strip() != "<no value>"), "")


# ── Detection steps ───────────────────────────────────────────────────────────
def _have_docker(run) -> bool:
    rc, _, _ = run("command -v docker >/dev/null 2>&1 && docker ps -q >/dev/null 2>&1")
    return rc == 0


def _listeners(run) -> list[tuple[int, str, str]]:
    """[(port, pid, process_name)] for the web ports, best effort.

    Needs root to see other users' PIDs — which is how these scripts run. With
    no PID visible the entry is still returned (pid=""), so the caller knows the
    port is taken even if it cannot attribute it.
    """
    found: list[tuple[int, str, str]] = []
    rc, out, _ = run("ss -lntpH 2>/dev/null")
    if rc != 0 or not out.strip():
        rc, out, _ = run("netstat -lntp 2>/dev/null")
    for line in out.splitlines():
        m_port = re.search(r"[:.](\d+)\s", line)
        if not m_port:
            continue
        port = int(m_port.group(1))
        if port not in WEB_PORTS:
            continue
        m = (re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)      # ss
             or re.search(r"\s(\d+)/(\S+)", line))                   # netstat
        if m and m.lastindex == 2 and m.group(1).isdigit():          # netstat order
            pid, name = m.group(1), m.group(2)
        elif m:                                                       # ss order
            name, pid = m.group(1), m.group(2)
        else:
            pid, name = "", ""
        found.append((port, pid, name.split(":")[0]))
    # Most significant port first, and de-duplicate.
    order = {p: i for i, p in enumerate(WEB_PORTS)}
    seen, uniq = set(), []
    for item in sorted(found, key=lambda t: order.get(t[0], 99)):
        if item in seen:
            continue
        seen.add(item)
        uniq.append(item)
    return uniq


def _container_of_pid(run, pid: str) -> str | None:
    """Container NAME owning `pid`, via its cgroup — works for docker, podman
    and containerd-shimmed processes."""
    rc, out, _ = run(f"cat /proc/{pid}/cgroup 2>/dev/null")
    if rc != 0:
        return None
    m = re.search(r"(?:docker[-/]|libpod-|cri-containerd[-:]|/)([0-9a-f]{12,64})(?:\.scope)?\b", out)
    if not m:
        return None
    cid = m.group(1)
    rc, name, _ = run("docker inspect --format '{{.Name}}' " + cid + " 2>/dev/null")
    name = name.strip().lstrip("/")
    return name or None


def _container_publishing(run, port: int) -> str | None:
    """Container that publishes `port` on the host (for a docker-proxy PID)."""
    _rc, out, _ = run("docker ps --format '{{.Names}}\t{{.Ports}}'")
    for line in out.splitlines():
        if "\t" not in line:
            continue
        name, ports = line.split("\t", 1)
        if re.search(rf"(?::|^){port}->", ports):
            return name.strip()
    return None


def binary_in(run, container: str | None = None) -> str | None:
    """First nginx-family binary available in a container (or on the host)."""
    for b in NGINX_BINARIES:
        cmd = f"command -v {b} >/dev/null 2>&1"
        rc, _, _ = (run(f"docker exec {container} sh -c {sh_quote(cmd)}") if container
                    else run(cmd))
        if rc == 0:
            return b
    return None


def _from_port_owner(run) -> NginxTarget | None:
    """Step 1: resolve whoever holds :443/:80 into a target."""
    docker = _have_docker(run)
    for port, pid, name in _listeners(run):
        if not pid:
            continue
        # docker's userland port forwarder — the real server is the container
        # it points at.
        if name.startswith("docker-proxy") and docker:
            container = _container_publishing(run, port)
            if container:
                binary = binary_in(run, container)
                if binary:
                    return NginxTarget(run, "docker", container=container, binary=binary,
                                       how=f"publishes :{port} on this host (via docker-proxy)")
            continue
        container = _container_of_pid(run, pid) if docker else None
        if container:
            binary = binary_in(run, container) or "nginx"
            rc, _, _ = run(f"docker exec {container} sh -c {sh_quote(binary + ' -v')}")
            if rc == 0:
                return NginxTarget(run, "docker", container=container, binary=binary, pid=pid,
                                   how=f"holds :{port} on this host")
            continue
        # A plain host process. Confirm it really is nginx AND that the binary
        # runs on the host — a containerised process whose runtime we could not
        # identify would otherwise be mistaken for a host install.
        _rc, exe, _ = run(f"readlink -f /proc/{pid}/exe 2>/dev/null")
        exe = exe.strip()
        base = (exe.rsplit("/", 1)[-1] or name).lower()
        if not any(b in base for b in NGINX_BINARIES) and not any(b in name.lower() for b in NGINX_BINARIES):
            continue
        rc, _, _ = run(f"{exe or 'nginx'} -v 2>/dev/null")
        if rc != 0:
            continue
        _rc, unit, _ = run(f"systemctl status {pid} --no-pager -n0 2>/dev/null | head -1")
        m = re.search(r"([\w@.\-]+\.service)", unit)
        return NginxTarget(run, "host", binary=exe or "nginx", pid=pid,
                           service=(m.group(1) if m else None),
                           how=f"holds :{port} on this host")
    return None


def _from_container_scan(run) -> tuple[NginxTarget | None, list[str], str]:
    """Step 2: any running container that looks like / carries nginx."""
    if not _have_docker(run):
        return None, [], "docker not available"
    _rc, out, _ = run("docker ps --format '{{.Names}}\t{{.Image}}\t{{.Command}}'")
    rows = [ln.split("\t") for ln in out.splitlines() if ln.strip()]
    names = [r[0] for r in rows]
    candidates = [r[0] for r in rows
                  if any(any(b in cell.lower() for b in NGINX_BINARIES) for cell in r)]
    if not candidates:
        # Name/image say nothing — ask each container whether it HAS nginx.
        candidates = [n for n in names if binary_in(run, n)]
    if not candidates:
        return None, [], "no running container appears to run nginx"
    if len(candidates) == 1:
        binary = binary_in(run, candidates[0]) or "nginx"
        return (NginxTarget(run, "docker", container=candidates[0], binary=binary,
                            how="only nginx container running"),
                candidates, "only one nginx container running")
    # Several: prefer one that publishes a web port, then one holding a
    # default_server.
    for port in WEB_PORTS:
        pub = _container_publishing(run, port)
        if pub in candidates:
            binary = binary_in(run, pub) or "nginx"
            return (NginxTarget(run, "docker", container=pub, binary=binary,
                                how=f"the nginx container publishing :{port}"),
                    candidates, f"publishes :{port}")
    holding = []
    for n in candidates:
        rc, _, _ = run(f"docker exec {n} sh -c "
                       + sh_quote("grep -rqs -- 'default_server' /etc/nginx || grep -rqs -- 'server_name _;' /etc/nginx"))
        if rc == 0:
            holding.append(n)
    if len(holding) == 1:
        binary = binary_in(run, holding[0]) or "nginx"
        return (NginxTarget(run, "docker", container=holding[0], binary=binary,
                            how="the only nginx container holding a default server"),
                candidates, "holds the default server")
    return None, candidates, "multiple nginx containers"


def _from_host_binary(run) -> NginxTarget | None:
    """Step 3: nginx installed on the host (systemd service or bare process)."""
    binary = binary_in(run, None)
    if not binary:
        return None
    rc, _, _ = run(f"pgrep -x {binary} >/dev/null 2>&1 || pgrep -f 'nginx: master' >/dev/null 2>&1")
    running = rc == 0
    service = None
    for unit in ("nginx", "openresty"):
        rc2, out2, _ = run(f"systemctl is-active {unit} 2>/dev/null")
        if out2.strip() == "active":
            service, running = unit, True
            break
    if not running:
        return None
    return NginxTarget(run, "host", binary=binary, service=service,
                       how=f"nginx running on the host{f' as {service}.service' if service else ''}")


def detect(run) -> tuple[NginxTarget | None, list[str], str]:
    """Find the reverse proxy. Returns (target|None, docker_candidates, note)."""
    t = _from_port_owner(run)
    if t:
        return t, [], t.how
    t, candidates, note = _from_container_scan(run)
    if t:
        return t, candidates, note
    t2 = _from_host_binary(run)
    if t2:
        return t2, candidates, t2.how
    return None, candidates, note or "no nginx found"


# ── Choosing where to insert a location block ─────────────────────────────────
_LISTEN_RE = re.compile(r"^\s*listen\s+([^;]+);", re.M)


def pick_default_server(target: NginxTarget) -> dict | None:
    """The server{} block that answers this host's web port, from `nginx -T`.

    Returns {"path", "line", "listen", "server_name", "score"} where `line` is
    the 1-based line number of the `server {` line in that file — inserting
    right after it puts a location inside the block without relying on a text
    anchor that may repeat.
    """
    best = None
    for path, text in target.dump():
        lines = text.split("\n")
        depth = 0
        start = None
        block: list[str] = []
        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            if start is None:
                if re.match(r"^server\s*\{", stripped):
                    start, depth, block = i, 1, []
                continue
            block.append(line)
            depth += line.count("{") - line.count("}")
            if depth > 0:
                continue
            body = "\n".join(block)
            listens = _LISTEN_RE.findall(body)
            names = re.search(r"^\s*server_name\s+([^;]+);", body, re.M)
            score = 0
            for l in listens:
                if "default_server" in l:
                    score += 100
                if re.search(r"\b443\b", l) or "ssl" in l:
                    score += 40
                if re.search(r"\b80\b", l):
                    score += 20
            if names and names.group(1).strip() == "_":
                score += 30
            if listens and (best is None or score > best["score"]):
                best = {"path": path, "line": start, "score": score,
                        "listen": listens[0].strip(),
                        "server_name": names.group(1).strip() if names else ""}
            start = None
    return best


# ── Standalone report ─────────────────────────────────────────────────────────
def report(run) -> int:
    target, candidates, note = detect(run)
    print("[detect] Listening web ports:")
    for port, pid, name in _listeners(run) or []:
        print(f"           :{port}  pid={pid or '?'}  {name or '?'}")
    if not target:
        print(f"[detect] No nginx reverse proxy found ({note}).")
        if candidates:
            print("[detect] nginx-ish containers: " + ", ".join(candidates))
        return 1
    print(f"[detect] nginx: {target}")
    if target.kind == "docker":
        print(f"[detect]   network mode : {target.network_mode()}")
        print(f"[detect]   gateway      : {target.gateway() or '(none)'}")
    rc, out, err = target.test()
    print("[detect]   nginx -t     : " + (out + err).strip().replace("\n", "\n                  "))
    files = [p for p, _ in target.dump()]
    print(f"[detect]   config files : {len(files)}")
    for p in files[:10]:
        print(f"                  {p}")
    srv = pick_default_server(target)
    if srv:
        print(f"[detect]   default srv  : {srv['path']}:{srv['line']}  "
              f"listen {srv['listen']}  server_name {srv['server_name'] or '(none)'}")
        host = target.host_path_for(srv["path"])
        print(f"[detect]   on host as   : {host or '(inside the image only)'}")
    return 0


def main() -> int:
    import argparse, getpass
    ap = argparse.ArgumentParser(description="Report the nginx reverse proxy on a machine.")
    ap.add_argument("--host", help="Report on a remote host over SSH instead of this machine.")
    ap.add_argument("--user", default="root")
    ap.add_argument("--password")
    ap.add_argument("--ssh-port", type=int, default=22)
    args = ap.parse_args()

    if not args.host:
        def run(cmd: str):
            p = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True)
            return p.returncode, p.stdout, p.stderr
        return report(run)

    import paramiko
    password = args.password or getpass.getpass(f"SSH password for {args.user}@{args.host}: ")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(args.host, port=args.ssh_port, username=args.user, password=password, timeout=20)
    try:
        def run(cmd: str):
            _i, o, e = ssh.exec_command(cmd)
            rc = o.channel.recv_exit_status()
            return rc, o.read().decode(errors="replace"), e.read().decode(errors="replace")
        return report(run)
    finally:
        ssh.close()


if __name__ == "__main__":
    raise SystemExit(main())
