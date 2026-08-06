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

  0. Detect the nginx (deploy/nginx_detect.py) with no assumption about names:
     whoever holds :443/:80 is resolved back to a container, a systemd service,
     or a plain host process. Works for nginx in Docker under ANY container
     name and for nginx installed directly on the machine. Override with
     --proxy-container, or just look with --detect-only.
  1. Pick a working UPSTREAM the proxy can actually reach: its docker gateway →
     the host's published app port (or 127.0.0.1 when the proxy shares the host
     network / runs on the host). A literal 127.0.0.1 inside a containerised
     proxy would point at the nginx container itself, not the app — the usual
     cause of a 502. Override with --upstream.
  2. If a source template is given/known and exists, insert/refresh our block in
     it (durable across rebuilds). Skipped with a note when absent.
  3. Find the LIVE config from `nginx -T` (the files nginx really loaded) and
     the default server block inside it. If that file is bind-mounted, edit the
     HOST copy in place (avoids `docker cp`'s "device or resource busy" on a
     mounted file, and is durable); otherwise `docker cp` a patched copy back
     in. A host nginx is edited directly.
  4. Validate with `nginx -t` BEFORE anything goes live, roll back on failure,
     and only then reload gracefully.

Re-running is safe and idempotent: an existing managed block is REPLACED in
place (so a changed --upstream is picked up), never duplicated. nginx matches
the most specific prefix location regardless of order, so /cc_es_analyzer/
cannot shadow the proxy's own `location /`, `/api/`, etc.

Usage:
    # just report what nginx this machine runs, change nothing:
    python3 deploy/setup_nginx_path.py --local --detect-only
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nginx_detect                                              # noqa: E402
from nginx_detect import NginxTarget                             # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_SNIPPET = os.path.join(PROJECT_ROOT, "deploy", "nginx", "cc-analyzer-path.conf")

# Optional durable-edit target (docs-platform reference env). Skipped if absent.
TEMPLATE_PATH    = "/opt/kvision_tools/docs-platform/reverse-proxy/nginx.conf.template"
HOST_STAGE       = "/opt/cc_es_analyzer/nginx"
DEFAULT_APP_PORT = "8801"          # host-published port of the analyzer container
UPSTREAM_TOKEN   = "__CC_UPSTREAM__"
SCHEME_TOKEN     = "__CC_SCHEME__"     # http, or https when the app serves TLS
PATH_TOKEN       = "__CC_PATH__"
# URL path the app is published at. The tool outgrew "es analyzer" — it is
# becoming one console over every CC datastore — so the path is cc_admin.
# Override with --url-path; it is a single token in the snippet, not a name
# baked into the config, so changing it later is one flag.
DEFAULT_URL_PATH = "cc_admin"
# Preferred line to insert our location block after. When it isn't there, the
# default server block is located from `nginx -T` instead (see _insert_point).
DEFAULT_ANCHOR   = "server_name _;"

# Markers delimiting our managed block. Kept at the old name deliberately: an
# instance published before the cc_admin rename already has THESE markers in
# its nginx config, and _apply_block finds the existing block by them. Changing
# them would leave the old block orphaned in place and insert a second one.
MARK_BEGIN = "# >>> cc_es_analyzer path (managed) >>>"
MARK_END   = "# <<< cc_es_analyzer path (managed) <<<"


def _apply_block(content: str, block: str, anchor: str | None = None,
                 line_no: int | None = None) -> tuple[str, str]:
    """Insert `block` into `content`, or REPLACE an existing managed block.

    Insertion happens after `anchor` (every occurrence) when given, else after
    line `line_no` (1-based) — the line-based form is used when the anchor text
    is absent or ambiguous, e.g. a `server {` line that repeats in the file.

    Returns (new_content, action) where action is one of:
      "replaced"  — managed block(s) already present, refreshed in place
      "nochange"  — managed block(s) already present and identical
      "inserted"  — no managed block yet; inserted
      "anchor-missing" — nothing to insert after (caller decides)
    """
    if MARK_BEGIN in content and MARK_END in content:
        pat = re.compile(re.escape(MARK_BEGIN) + ".*?" + re.escape(MARK_END), re.DOTALL)
        new = pat.sub(lambda _m: block, content)
        return new, ("nochange" if new == content else "replaced")
    if anchor and anchor in content:
        return content.replace(anchor, anchor + "\n" + block), "inserted"
    if line_no:
        lines = content.split("\n")
        if 1 <= line_no <= len(lines):
            lines.insert(line_no, block)
            return "\n".join(lines), "inserted"
    return content, "anchor-missing"


def _print_running(run) -> None:
    _rc, out, _ = run("docker ps --format '  {{.Names}}  ({{.Image}})'")
    print("[nginx-path] Running containers:\n" + (out.rstrip() or "  (none)"))


def _dump_server_names(target: NginxTarget) -> None:
    lines = []
    for path, text in target.dump():
        for i, line in enumerate(text.split("\n"), start=1):
            if "server_name" in line or re.match(r"^\s*listen\s", line):
                lines.append(f"{path}:{i}:{line.strip()}")
    if lines:
        print("[nginx-path] server_name / listen lines found (pick the default server and re-run\n"
              "             with --anchor '<that exact line>' and/or --live-conf-path <file>):")
        print("\n".join("  " + l for l in lines[:60]))


def _pick_upstream(target: NginxTarget, app_port: str) -> tuple[str, str]:
    """Choose an upstream address the PROXY can reach for the app.

    - nginx on the host, or a container on the host network → 127.0.0.1 works.
    - bridge / user-defined network → reach the host's published port via the
      network gateway IP (works for user-defined bridges, which compose uses).
    Returns (host:port, human_reason).
    """
    if target.kind == "host":
        return f"127.0.0.1:{app_port}", "nginx runs on the host itself"
    if target.network_mode() == "host":
        return f"127.0.0.1:{app_port}", "proxy uses host networking"
    gw = target.gateway()
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


def _insert_point(target: NginxTarget, anchor: str, live_conf_path: str | None):
    """Where to put the location block: (path, anchor_or_None, line_or_None).

    Preference: an explicit --live-conf-path, then any loaded config containing
    the anchor, then the default server block located structurally in the
    `nginx -T` dump (no text anchor needed).
    """
    dump = target.dump()
    if live_conf_path:
        for path, text in dump:
            if path == live_conf_path:
                return path, (anchor if anchor in text else None), _server_line(text)
        return live_conf_path, anchor, None
    for path, text in dump:
        if anchor and anchor in text:
            return path, anchor, None
    srv = nginx_detect.pick_default_server(target)
    if srv:
        print(f"[nginx-path] Anchor {anchor!r} not present — using the default server block at "
              f"{srv['path']}:{srv['line']} (listen {srv['listen']}, "
              f"server_name {srv['server_name'] or 'none'}).")
        return srv["path"], None, srv["line"]
    return None, None, None


def _server_line(text: str) -> int | None:
    """1-based line of the first `server {` in `text`."""
    for i, line in enumerate(text.split("\n"), start=1):
        if re.match(r"^\s*server\s*\{", line.strip()):
            return i
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Publish CC ES Analyzer at /cc_es_analyzer/ on an nginx proxy's own path space.")
    ap.add_argument("--local", action="store_true",
                    help="Run against THIS machine directly (subprocess + local files) instead of "
                         "over SSH. Use when running on the host itself; --host/--user are ignored.")
    ap.add_argument("--detect-only", action="store_true",
                    help="Only report which nginx this machine runs (container or host service), "
                         "its config files and default server — change nothing.")
    ap.add_argument("--skip-if-no-proxy", action="store_true",
                    help="Exit 0 with a note (instead of erroring) when no nginx reverse proxy "
                         "is found — for hosts where the app is reached directly on its port.")
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
                    help="Reverse-proxy nginx container name. AUTO-DETECTED if omitted "
                         "(including nginx running outside Docker).")
    ap.add_argument("--app-port", default=os.getenv("CC_APP_PORT", DEFAULT_APP_PORT),
                    help=f"Host-published port of the analyzer container (default {DEFAULT_APP_PORT}).")
    ap.add_argument("--upstream", default=os.getenv("CC_UPSTREAM"),
                    help="Explicit upstream host:port for proxy_pass (e.g. 172.17.0.1:8801 or "
                         "cc_es_analyzer:8000). AUTO-DETECTED if omitted.")
    ap.add_argument("--url-path", default=os.getenv("CC_URL_PATH", DEFAULT_URL_PATH),
                    help=f"URL path to publish at, without slashes (default "
                         f"{DEFAULT_URL_PATH!r}). Served on the proxy's existing "
                         f"port — 443 on a CC — so it needs no new port opened.")
    ap.add_argument("--template-path", default=os.getenv("CC_PROXY_TEMPLATE", TEMPLATE_PATH),
                    help="Host path to the proxy's nginx source template, for a durable edit. "
                         "Skipped (with a note) if it doesn't exist.")
    ap.add_argument("--live-conf-path", default=os.getenv("CC_PROXY_LIVE_CONF"),
                    help="Path (as nginx sees it) to the rendered default-server config. "
                         "Auto-detected from `nginx -T` if omitted.")
    ap.add_argument("--anchor", default=os.getenv("CC_PROXY_ANCHOR", DEFAULT_ANCHOR),
                    help=f"Config line to insert our location block after (default {DEFAULT_ANCHOR!r}). "
                         "If absent from the config, the default server block is found structurally.")
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

        def _read(path: str) -> str:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
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

        def _read(path: str) -> str:
            with sftp.file(path, "r") as f:
                return f.read().decode(errors="replace")

    try:
        sftp = ssh.open_sftp() if ssh is not None else None
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        if args.detect_only:
            return nginx_detect.report(run)

        # 0) Pick the reverse proxy: explicit container flag, else detect it —
        #    by who owns the web port, then by container scan, then by a host
        #    installation. No name matching anywhere.
        if args.proxy_container:
            _rc, out, _ = run("docker ps --format '{{.Names}}'")
            if args.proxy_container not in out.split():
                print(f"[nginx-path] ERROR: container {args.proxy_container!r} not running.")
                _print_running(run)
                return 2
            target = NginxTarget(run, "docker", container=args.proxy_container,
                                 binary=nginx_detect.binary_in(run, args.proxy_container) or "nginx",
                                 how="specified")
            print(f"[nginx-path] Using nginx: {target}.")
        else:
            target, candidates, note = nginx_detect.detect(run)
            if target is None and args.skip_if_no_proxy:
                print(f"[nginx-path] No nginx reverse proxy found ({note}). Skipping proxy setup — "
                      f"reach the app directly on its published port.")
                return 0
            if target is None:
                print(f"[nginx-path] ERROR: could not find an nginx reverse proxy ({note}).")
                if candidates:
                    print("[nginx-path] nginx candidates: " + ", ".join(candidates))
                    print("[nginx-path] Re-run with  --proxy-container <name>  to choose one.")
                else:
                    _print_running(run)
                return 2
            print(f"[nginx-path] Auto-detected nginx: {target}.")

        # 1) Choose the upstream the proxy can actually reach.
        if args.upstream:
            upstream, why = args.upstream, "specified"
        else:
            upstream, why = _pick_upstream(target, args.app_port)
        print(f"[nginx-path] Upstream for proxy_pass: {upstream}  ({why}).")

        # 1b) Scheme nginx uses to reach the app (https when SERVICE_SSL=true).
        if args.upstream_scheme != "auto":
            scheme, why_s = args.upstream_scheme, "specified"
        else:
            scheme, why_s = _pick_scheme(run, args.app_port)
        print(f"[nginx-path] Upstream scheme: {scheme}  ({why_s}).")

        url_path = args.url_path.strip().strip("/")
        print(f"[nginx-path] Publishing at /{url_path}/ on the proxy's own port.")
        snippet = (snippet_raw
                   .replace(UPSTREAM_TOKEN, upstream)
                   .replace(SCHEME_TOKEN, scheme)
                   .replace(PATH_TOKEN, url_path))
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
            tmpl = _read(template_path)
            new_tmpl, action = _apply_block(tmpl, block, anchor=anchor)
            if action == "anchor-missing":
                print(f"[nginx-path] WARN: anchor {anchor!r} not in template — skipping durable edit.")
            elif action == "nochange":
                print("[nginx-path] Template already up to date.")
            else:
                _write(template_path, new_tmpl)
                print(f"[nginx-path] {action.capitalize()} block in template (durable across rebuilds).")

        # 3) Locate the LIVE config nginx actually loaded, and the spot inside it.
        live_path, live_anchor, live_line = _insert_point(target, anchor, args.live_conf_path)
        if not live_path:
            print("[nginx-path] ERROR: none of the loaded nginx configs contains the anchor "
                  f"{anchor!r} and no default server block could be identified.")
            _dump_server_names(target)
            return 4
        print(f"[nginx-path] Live config: {live_path} (in {target.label}).")

        # 4) Read the live config and apply our block (insert or refresh).
        rc, live_content = target.read_file(live_path)
        if rc != 0:
            print(f"[nginx-path] ERROR: could not read {live_path}.")
            return 4
        new_live, action = _apply_block(live_content, block, anchor=live_anchor, line_no=live_line)
        if action == "anchor-missing":
            print(f"[nginx-path] ERROR: could not find an insertion point in {live_path} — aborting.")
            _dump_server_names(target)
            return 4
        if action == "nochange":
            print("[nginx-path] Live config already up to date — nothing to apply.")
        else:
            run(f"mkdir -p {HOST_STAGE}")
            orig_stage = f"{HOST_STAGE}/live.orig.{ts}.conf"
            _write(orig_stage, live_content)

            # Prefer editing the file on the HOST when we can reach it there —
            # always for a host nginx, and for a containerised one whenever the
            # file is bind-mounted: `docker cp` onto a mounted file fails
            # ("device or resource busy"), and an in-place host edit is durable
            # across restarts too. Fall back to `docker cp` for image-baked files.
            host_path = target.host_path_for(live_path)
            if host_path:
                where = ("editing it directly" if target.kind == "host"
                         else f"bind-mounted from host {host_path} — editing there in place "
                              f"(durable across restarts)")
                print(f"[nginx-path] {live_path} {where}.")
                run(f"cp -a {host_path} {host_path}.bak.{ts}")
                _write(host_path, new_live)
            else:
                patched_stage = f"{HOST_STAGE}/live.patched.{ts}.conf"
                _write(patched_stage, new_live)
                rc, _, err = run(f"docker cp {patched_stage} {target.container}:{live_path}")
                if rc != 0:
                    print(f"[nginx-path] ERROR: docker cp failed: {err.strip()} — aborting (nothing reloaded).")
                    return 5

            def _rollback():
                if host_path:
                    _write(host_path, live_content)
                else:
                    run(f"docker cp {orig_stage} {target.container}:{live_path}")

            # 5) Validate. Roll back on failure.
            rc, out, err = target.test()
            print("[nginx-path] nginx -t:\n" + (out + err).strip())
            if rc != 0:
                print("[nginx-path] ERROR: config test FAILED — restoring original config, NOT reloading.")
                _rollback()
                return 6

            rc, out, err = target.reload()
            if rc != 0:
                print(f"[nginx-path] ERROR: reload failed: {(out+err).strip()} — restoring original config.")
                _rollback()
                return 7
            print(f"[nginx-path] {action.capitalize()} block live and reloaded nginx gracefully.")

        if sftp is not None:
            sftp.close()

        # 6) Diagnostics: is the app itself reachable, and can the proxy reach it?
        _rc, out, _ = run(f"curl -sk -m 8 -o /dev/null -w '%{{http_code}}' {scheme}://127.0.0.1:{args.app_port}/api/health")
        print(f"[nginx-path] App health on host {scheme}://127.0.0.1:{args.app_port} -> HTTP {out.strip()}  "
              f"(200 = app up; 000/refused = app not reachable on that port).")
        _rc, out, _ = target.exec(
            f"(command -v curl >/dev/null && curl -sk -m 8 -o /dev/null "
            f"-w '%{{http_code}}' {scheme}://{upstream}/api/health) || echo 'no-curl-in-proxy'")
        print(f"[nginx-path] App reachable from {target.label} at {scheme}://{upstream} -> {out.strip()}  "
              f"(200 = proxy can reach the app).")

        print(f"\n[nginx-path] DONE. Upstream={scheme}://{upstream}. Open  http://{host_label}/cc_es_analyzer/  "
              f"(or https://{host_label}/cc_es_analyzer/).")
        return 0
    finally:
        if ssh is not None:
            ssh.close()


if __name__ == "__main__":
    raise SystemExit(main())
