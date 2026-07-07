#!/usr/bin/env python3
"""
Publish CC ES Analyzer under an nginx reverse proxy's existing PATH space, at
/cc_es_analyzer/ on the same IP/hostname the proxy already answers on.

This is the more invasive sibling of setup_nginx.py: a `location` block only
takes effect inside the server{} block that actually matches the incoming
request, so reaching /cc_es_analyzer/ on the proxy's default server requires
INSERTING a location into that existing block — an isolated conf.d file (like
setup_nginx.py's name-based vhost) cannot do this.

It auto-detects everything host-specific, so it works on any host:

  0. Detect the nginx container: running containers whose name/image mention
     "nginx" (or that carry an `nginx` binary); if several, prefer the one
     holding the default server. Override with --proxy-container.
  1. Pick a working UPSTREAM the proxy container can actually reach: its docker
     gateway → the host's published app port (or 127.0.0.1 when the proxy uses
     host networking). A literal 127.0.0.1 in the proxy would point at the nginx
     container itself, not the app — the usual cause of a 502. Override with
     --upstream.
  2. If a source template is given/known and exists, insert/refresh our block in
     it (durable across rebuilds). Skipped with a note when absent.
  3. Find the LIVE rendered config inside the container (the file holding the
     anchor). If that file is bind-mounted, edit the HOST copy in place (avoids
     `docker cp`'s "device or resource busy" on a mounted file, and is durable);
     otherwise `docker cp` a patched copy back in.
  4. Validate with `nginx -t` BEFORE anything goes live, roll back on failure,
     and only then reload gracefully.

Re-running is safe and idempotent: an existing managed block is REPLACED in
place (so a changed --upstream is picked up), never duplicated. nginx matches
the most specific prefix location regardless of order, so /cc_es_analyzer/
cannot shadow the proxy's own `location /`, `/api/`, etc.

Usage:
    # over SSH from your workstation:
    python deploy/setup_nginx_path.py --host <host> --user root
    # ON the host itself (no SSH), e.g. from deploy/install.sh:
    python3 deploy/setup_nginx_path.py --local --skip-if-no-proxy
    # pin things explicitly if auto-detection needs help:
    python deploy/setup_nginx_path.py --host <host> --proxy-container rdwrsim-nginx \
        --upstream 172.17.0.1:8801 --upstream-scheme https --anchor 'server_name _;'
"""
from __future__ import annotations
import argparse, getpass, os, re, sys, datetime, subprocess

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_SNIPPET = os.path.join(PROJECT_ROOT, "deploy", "nginx", "cc-analyzer-path.conf")

# Optional durable-edit target (docs-platform reference env). Skipped if absent.
TEMPLATE_PATH    = "/opt/kvision_tools/docs-platform/reverse-proxy/nginx.conf.template"
HOST_STAGE       = "/opt/cc_es_analyzer/nginx"
DEFAULT_APP_PORT = "8801"          # host-published port of the analyzer container
UPSTREAM_TOKEN   = "__CC_UPSTREAM__"
SCHEME_TOKEN     = "__CC_SCHEME__"     # http, or https when the app serves TLS
# Line our location block is inserted after — the default server owning the
# host's root path. Override with --anchor if the proxy's default server uses a
# concrete server_name instead of the catch-all `_`.
DEFAULT_ANCHOR   = "server_name _;"

MARK_BEGIN = "# >>> cc_es_analyzer path (managed) >>>"
MARK_END   = "# <<< cc_es_analyzer path (managed) <<<"


def _apply_block(content: str, block: str, anchor: str) -> tuple[str, str]:
    """Insert `block` after `anchor`, or REPLACE an existing managed block.

    Returns (new_content, action) where action is one of:
      "replaced"  — managed block(s) already present, refreshed in place
      "nochange"  — managed block(s) already present and identical
      "inserted"  — no managed block yet; inserted after every anchor line
      "anchor-missing" — no managed block and anchor not found (caller decides)
    """
    if MARK_BEGIN in content and MARK_END in content:
        pat = re.compile(re.escape(MARK_BEGIN) + ".*?" + re.escape(MARK_END), re.DOTALL)
        new = pat.sub(lambda _m: block, content)
        return new, ("nochange" if new == content else "replaced")
    if anchor not in content:
        return content, "anchor-missing"
    return content.replace(anchor, anchor + "\n" + block), "inserted"


def _print_running(run) -> None:
    _rc, out, _ = run("docker ps --format '  {{.Names}}  ({{.Image}})'")
    print("[nginx-path] Running containers:\n" + (out.rstrip() or "  (none)"))


def _dump_server_names(run, proxy: str) -> None:
    _rc, out, _ = run(f"docker exec {proxy} sh -c \"grep -rns -- 'server_name' /etc/nginx 2>/dev/null\"")
    if out.strip():
        print("[nginx-path] server_name lines found (pick the default server and re-run with\n"
              "             --anchor '<that exact line>' and/or --live-conf-path <file>):")
        print(out.rstrip())


def _host_path_for(run, proxy: str, container_path: str):
    """If `container_path` inside `proxy` is backed by a bind/volume mount,
    return the matching HOST path to the same file (most specific mount wins);
    otherwise None."""
    fmt = "{{range .Mounts}}{{.Source}}\t{{.Destination}}\n{{end}}"
    _rc, out, _ = run("docker inspect --format '" + fmt + "' " + proxy)
    best = None  # (len(dest), host_path)
    for line in out.splitlines():
        if "\t" not in line:
            continue
        source, dest = (p.strip() for p in line.split("\t", 1))
        if not source.startswith("/") or not dest.startswith("/"):
            continue
        d = dest.rstrip("/")
        if container_path == dest or container_path == d:
            cand = source
        elif container_path.startswith(d + "/"):
            cand = source.rstrip("/") + container_path[len(d):]
        else:
            continue
        if best is None or len(d) > best[0]:
            best = (len(d), cand)
    return best[1] if best else None


def _pick_upstream(run, proxy: str, app_port: str) -> tuple[str, str]:
    """Choose an upstream address the PROXY CONTAINER can reach for the app.

    - host networking → the host's own loopback works (127.0.0.1:app_port).
    - bridge / user-defined network → reach the host's published port via the
      network gateway IP (works for user-defined bridges, which compose uses).
    Returns (host:port, human_reason).
    """
    _rc, mode, _ = run("docker inspect --format '{{.HostConfig.NetworkMode}}' " + proxy)
    if mode.strip() == "host":
        return f"127.0.0.1:{app_port}", "proxy uses host networking"
    _rc, out, _ = run("docker inspect --format '{{range .NetworkSettings.Networks}}{{.Gateway}}\n{{end}}' " + proxy)
    gw = next((l.strip() for l in out.splitlines()
               if l.strip() and l.strip() != "<no value>"), "")
    if gw:
        return f"{gw}:{app_port}", f"reach host-published :{app_port} via docker gateway {gw}"
    return f"127.0.0.1:{app_port}", "fallback: no gateway found, using loopback"


def _pick_scheme(run, app_port: str) -> tuple[str, str]:
    """Detect whether the app speaks TLS on its published port.

    Probes the host's own loopback: if https answers with a real HTTP status the
    app runs with SERVICE_SSL=true (so nginx must proxy over https); otherwise
    plain http. Returns (scheme, human_reason).
    """
    _rc, out, _ = run(f"curl -sk -m 6 -o /dev/null -w '%{{http_code}}' "
                      f"https://127.0.0.1:{app_port}/api/health")
    code = out.strip()
    if code and code not in ("000",):
        return "https", f"app answered https on :{app_port} (HTTP {code}) — SERVICE_SSL is on"
    return "http", f"app not serving TLS on :{app_port} — using plain http"


def _detect_proxy_container(run, anchor: str):
    """Auto-detect the reverse-proxy nginx container. Returns
    (chosen|None, candidates, note)."""
    _rc, out, _ = run("docker ps --format '{{.Names}}\t{{.Image}}'")
    rows = [ln.split("\t") for ln in out.splitlines() if ln.strip()]
    names = [r[0] for r in rows]
    candidates = [r[0] for r in rows
                  if "nginx" in r[0].lower()
                  or (len(r) > 1 and "nginx" in r[1].lower())]
    if not candidates:
        for n in names:
            rc2, _, _ = run(f"docker exec {n} sh -c 'command -v nginx >/dev/null 2>&1'")
            if rc2 == 0:
                candidates.append(n)
    if not candidates:
        return None, [], "no running container appears to run nginx"
    if len(candidates) == 1:
        return candidates[0], candidates, "only one nginx container running"
    holding = []
    for n in candidates:
        rc3, _, _ = run(f"docker exec {n} sh -c \"grep -rqs -- '{anchor}' /etc/nginx\"")
        if rc3 == 0:
            holding.append(n)
    if len(holding) == 1:
        return holding[0], candidates, "the only nginx container holding the default server"
    return None, candidates, "multiple nginx containers"


def main() -> int:
    ap = argparse.ArgumentParser(description="Publish CC ES Analyzer at /cc_es_analyzer/ on an nginx proxy's own path space.")
    ap.add_argument("--local", action="store_true",
                    help="Run against THIS machine directly (subprocess + local files) instead of "
                         "over SSH. Use when running on the host itself; --host/--user are ignored.")
    ap.add_argument("--skip-if-no-proxy", action="store_true",
                    help="Exit 0 with a note (instead of erroring) when no nginx reverse-proxy "
                         "container is found — for hosts where the app is reached directly on its port.")
    ap.add_argument("--upstream-scheme", choices=("auto", "http", "https"), default="auto",
                    help="Scheme nginx uses to reach the app. 'auto' (default) detects whether the "
                         "app serves TLS (SERVICE_SSL=true) and picks https, else http.")
    ap.add_argument("--host", default=os.getenv("CC_DEPLOY_HOST"),
                    help="Target host running the reverse proxy (or set CC_DEPLOY_HOST). "
                         "Required unless --local.")
    ap.add_argument("--user", default=os.getenv("CC_DEPLOY_USER", "root"))
    ap.add_argument("--password", default=os.getenv("CC_DEPLOY_PASS"))
    ap.add_argument("--ssh-port", type=int, default=22)
    ap.add_argument("--proxy-container", default=os.getenv("CC_PROXY_CONTAINER"),
                    help="Reverse-proxy nginx container name. AUTO-DETECTED if omitted.")
    ap.add_argument("--app-port", default=os.getenv("CC_APP_PORT", DEFAULT_APP_PORT),
                    help=f"Host-published port of the analyzer container (default {DEFAULT_APP_PORT}).")
    ap.add_argument("--upstream", default=os.getenv("CC_UPSTREAM"),
                    help="Explicit upstream host:port for proxy_pass (e.g. 172.17.0.1:8801 or "
                         "cc_es_analyzer:8000). AUTO-DETECTED if omitted.")
    ap.add_argument("--template-path", default=os.getenv("CC_PROXY_TEMPLATE", TEMPLATE_PATH),
                    help="Host path to the proxy's nginx source template, for a durable edit. "
                         "Skipped (with a note) if it doesn't exist.")
    ap.add_argument("--live-conf-path", default=os.getenv("CC_PROXY_LIVE_CONF"),
                    help="Path INSIDE the container to the rendered default-server config. "
                         "Auto-detected via the anchor if omitted.")
    ap.add_argument("--anchor", default=os.getenv("CC_PROXY_ANCHOR", DEFAULT_ANCHOR),
                    help=f"Config line to insert our location block after (default {DEFAULT_ANCHOR!r}).")
    args = ap.parse_args()
    if not args.local and not args.host:
        ap.error("--host is required (or set CC_DEPLOY_HOST), unless --local.")
    template_path = args.template_path
    anchor = args.anchor
    host_label = args.host or "this-host"

    with open(LOCAL_SNIPPET, "r", encoding="utf-8") as fh:
        snippet_raw = fh.read().replace("\r\n", "\n")

    ssh = None
    if args.local:
        # Run against THIS machine: shell commands via subprocess, files written
        # directly. `_write` truncates in place (O_TRUNC) so bind mounts stay valid.
        print("[nginx-path] Running locally on this host (no SSH).")

        def run(cmd: str):
            p = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True)
            return p.returncode, p.stdout, p.stderr

        def _write(path: str, text: str):
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
    else:
        try:
            import paramiko
        except ImportError:
            sys.exit("paramiko is required for remote mode:  pip install paramiko  (or use --local)")
        password = args.password or getpass.getpass(f"SSH password for {args.user}@{args.host}: ")
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        print(f"[nginx-path] Connecting to {args.user}@{args.host} …")
        ssh.connect(args.host, port=args.ssh_port, username=args.user, password=password, timeout=20)

        def run(cmd: str):
            _i, o, e = ssh.exec_command(cmd)
            rc = o.channel.recv_exit_status()
            return rc, o.read().decode(errors="replace"), e.read().decode(errors="replace")

        def _write(path: str, text: str):
            with sftp.file(path, "w") as f:  # O_TRUNC keeps the inode → bind mounts stay valid
                f.write(text.encode("utf-8"))

    try:
        sftp = ssh.open_sftp() if ssh is not None else None
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        # 0) Pick the reverse-proxy nginx container: explicit flag, else detect.
        proxy = args.proxy_container
        if proxy:
            _rc, out, _ = run("docker ps --format '{{.Names}}'")
            if proxy not in out.split():
                print(f"[nginx-path] ERROR: container {proxy!r} not running.")
                _print_running(run)
                return 2
            print(f"[nginx-path] Using nginx container: {proxy} (specified).")
        else:
            proxy, candidates, note = _detect_proxy_container(run, anchor)
            if not proxy and not candidates and args.skip_if_no_proxy:
                print(f"[nginx-path] No nginx reverse proxy found ({note}). Skipping proxy setup — "
                      f"reach the app directly on its published port.")
                return 0
            if not proxy:
                print(f"[nginx-path] ERROR: could not auto-pick an nginx container ({note}).")
                if candidates:
                    print("[nginx-path] nginx candidates: " + ", ".join(candidates))
                    print("[nginx-path] Re-run with  --proxy-container <name>  to choose one.")
                else:
                    _print_running(run)
                return 2
            print(f"[nginx-path] Auto-detected nginx container: {proxy}  ({note}).")

        # 1) Choose the upstream the proxy container can actually reach.
        if args.upstream:
            upstream, why = args.upstream, "specified"
        else:
            upstream, why = _pick_upstream(run, proxy, args.app_port)
        print(f"[nginx-path] Upstream for proxy_pass: {upstream}  ({why}).")

        # 1b) Scheme nginx uses to reach the app (https when SERVICE_SSL=true).
        if args.upstream_scheme != "auto":
            scheme, why_s = args.upstream_scheme, "specified"
        else:
            scheme, why_s = _pick_scheme(run, args.app_port)
        print(f"[nginx-path] Upstream scheme: {scheme}  ({why_s}).")

        snippet = (snippet_raw
                   .replace(UPSTREAM_TOKEN, upstream)
                   .replace(SCHEME_TOKEN, scheme))
        block = f"{MARK_BEGIN}\n{snippet.rstrip()}\n{MARK_END}"

        # 2) Durable edit of the source template — best-effort, skipped if absent.
        _rc, _, _ = run(f"test -f {template_path}")
        if _rc != 0:
            print(f"[nginx-path] NOTE: source template {template_path} not found — skipping the "
                  f"durable edit (the live change below still applies). If this proxy is later "
                  f"recreated from a template/image, re-run this script or pass --template-path.")
        else:
            rc, _, err = run(f"cp -a {template_path} {template_path}.bak.{ts}")
            print(f"[nginx-path] Backed up template -> {template_path}.bak.{ts}" if rc == 0
                  else f"[nginx-path] WARN: template backup failed: {err.strip()}")
            with sftp.file(template_path, "r") as f:
                tmpl = f.read().decode(errors="replace")
            new_tmpl, action = _apply_block(tmpl, block, anchor)
            if action == "anchor-missing":
                print(f"[nginx-path] WARN: anchor {anchor!r} not in template — skipping durable edit.")
            elif action == "nochange":
                print("[nginx-path] Template already up to date.")
            else:
                _write(template_path, new_tmpl)
                print(f"[nginx-path] {action.capitalize()} block in template (durable across rebuilds).")

        # 3) Locate the LIVE rendered config inside the container.
        live_path = args.live_conf_path
        if not live_path:
            _rc, out, _ = run(
                f"docker exec {proxy} sh -c \"grep -rls -- '{anchor}' /etc/nginx 2>/dev/null | head -1\""
            )
            live_path = out.strip()
        if not live_path:
            print(f"[nginx-path] ERROR: no file under /etc/nginx in {proxy} contains the anchor "
                  f"{anchor!r}, so the live default server can't be found.")
            _dump_server_names(run, proxy)
            return 4
        print(f"[nginx-path] Live config: {live_path} (in {proxy}).")

        # 4) Read the live config and apply our block (insert or refresh).
        rc, live_content, err = run(f"docker exec {proxy} cat {live_path}")
        if rc != 0:
            print(f"[nginx-path] ERROR: could not read {live_path}: {err.strip()}")
            return 4
        new_live, action = _apply_block(live_content, block, anchor)
        if action == "anchor-missing":
            print(f"[nginx-path] ERROR: anchor {anchor!r} not found in {live_path} — aborting.")
            _dump_server_names(run, proxy)
            return 4
        if action == "nochange":
            print("[nginx-path] Live config already up to date — nothing to apply.")
        else:
            run(f"mkdir -p {HOST_STAGE}")
            orig_stage = f"{HOST_STAGE}/live.orig.{ts}.conf"
            _write(orig_stage, live_content)

            # Prefer editing the file on the HOST when it's bind-mounted into the
            # container: `docker cp` onto a mounted file fails ("device or
            # resource busy"), and an in-place host edit is durable across
            # restarts too. Fall back to `docker cp` for image-baked files.
            host_path = _host_path_for(run, proxy, live_path)
            if host_path:
                print(f"[nginx-path] {live_path} is bind-mounted from host {host_path} — "
                      f"editing there in place (durable across restarts).")
                run(f"cp -a {host_path} {host_path}.bak.{ts}")
                _write(host_path, new_live)
            else:
                patched_stage = f"{HOST_STAGE}/live.patched.{ts}.conf"
                _write(patched_stage, new_live)
                rc, _, err = run(f"docker cp {patched_stage} {proxy}:{live_path}")
                if rc != 0:
                    print(f"[nginx-path] ERROR: docker cp failed: {err.strip()} — aborting (nothing reloaded).")
                    return 5

            def _rollback():
                if host_path:
                    _write(host_path, live_content)
                else:
                    run(f"docker cp {orig_stage} {proxy}:{live_path}")

            # 5) Validate INSIDE the container. Roll back on failure.
            rc, out, err = run(f"docker exec {proxy} nginx -t")
            print("[nginx-path] nginx -t:\n" + (out + err).strip())
            if rc != 0:
                print("[nginx-path] ERROR: config test FAILED — restoring original config, NOT reloading.")
                _rollback()
                return 6

            rc, out, err = run(f"docker exec {proxy} nginx -s reload")
            if rc != 0:
                print(f"[nginx-path] ERROR: reload failed: {(out+err).strip()} — restoring original config.")
                _rollback()
                return 7
            print(f"[nginx-path] {action.capitalize()} block live and reloaded nginx gracefully.")

        if sftp is not None:
            sftp.close()

        # 6) Diagnostics: is the app itself reachable, and where does the proxy listen?
        _rc, out, _ = run(f"curl -sk -m 8 -o /dev/null -w '%{{http_code}}' {scheme}://127.0.0.1:{args.app_port}/api/health")
        print(f"[nginx-path] App health on host {scheme}://127.0.0.1:{args.app_port} -> HTTP {out.strip()}  "
              f"(200 = app up; 000/refused = app not reachable on that port).")
        _rc, out, _ = run(f"docker exec {proxy} sh -c \"(command -v curl >/dev/null && curl -sk -m 8 -o /dev/null "
                          f"-w '%{{http_code}}' {scheme}://{upstream}/api/health) || echo 'no-curl-in-proxy'\"")
        print(f"[nginx-path] App reachable from inside {proxy} at {scheme}://{upstream} -> {out.strip()}  "
              f"(200 = proxy can reach the app).")

        print(f"\n[nginx-path] DONE. Upstream={scheme}://{upstream}. Open  http://{host_label}/cc_es_analyzer/  "
              f"(or https://{host_label}/cc_es_analyzer/).")
        return 0
    finally:
        if ssh is not None:
            ssh.close()


if __name__ == "__main__":
    raise SystemExit(main())
