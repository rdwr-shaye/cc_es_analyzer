#!/usr/bin/env python3
"""
Publish CC ES Analyzer under the docs-platform's existing PATH space, at
/cc_es_analyzer/ on the same IP/hostname the docs site already answers on.

This is the more invasive sibling of setup_nginx.py: a `location` block only
takes effect inside the server{} block that actually matches the incoming
request, so reaching /cc_es_analyzer/ on the docs proxy's own `server_name _;`
default_server requires INSERTING a location into that existing block — an
isolated conf.d file (like setup_nginx.py's name-based vhost) cannot do this.

Safety measures, mirroring setup_nginx.py:
  1. Backs up the reverse-proxy source template on the host before editing it.
  2. Inserts our location block (marker-guarded, idempotent) right after every
     `server_name _;` line in the template — durable across proxy rebuilds.
     nginx matches the most specific *prefix* location regardless of source
     order, so this cannot shadow the docs site's own `location /`, `/api/`,
     `/mcp/`, etc.
  3. Auto-detects the LIVE rendered config file inside the running proxy
     container (by a distinctive signature string), patches a copy of it the
     same way, validates with `nginx -t` before touching the running server,
     and rolls the live file back untouched if validation fails.
  4. Only reloads (graceful, zero-downtime) if validation passed.
  5. Verifies the docs site AND the new path both answer afterwards.

Nothing else on the host is touched (no other container/image/network), and no
other location in the docs server block is modified or removed.

Usage:
    python deploy/setup_nginx_path.py --host <host> --user root
    python deploy/setup_nginx_path.py --host <host> --live-conf-path /etc/nginx/conf.d/default.conf
"""
from __future__ import annotations
import argparse, getpass, os, sys, datetime

try:
    import paramiko
except ImportError:
    sys.exit("paramiko is required:  pip install paramiko")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_SNIPPET = os.path.join(PROJECT_ROOT, "deploy", "nginx", "cc-analyzer-path.conf")

# Reference-environment defaults; override with flags/env for any other host.
PROXY_CONTAINER = "docs-reverse-proxy"
TEMPLATE_PATH   = "/opt/kvision_tools/docs-platform/reverse-proxy/nginx.conf.template"
HOST_STAGE      = "/opt/cc_es_analyzer/nginx"
ANCHOR          = "server_name _;"
# A string unique to the docs default_server block, used to auto-locate the
# LIVE rendered config file inside the container (the template gets envsubst'd
# to some conf.d/*.conf path we don't otherwise know).
LIVE_SIGNATURE  = "location /mcp/"

MARK_BEGIN = "# >>> cc_es_analyzer path (managed) >>>"
MARK_END   = "# <<< cc_es_analyzer path (managed) <<<"


def _inject(content: str, block: str) -> tuple[str, int]:
    """Insert `block` right after every ANCHOR line. Returns (new_content, count)."""
    marked = f"{ANCHOR}\n{block}"
    count = content.count(ANCHOR)
    return content.replace(ANCHOR, marked), count


def main() -> int:
    global PROXY_CONTAINER, TEMPLATE_PATH
    ap = argparse.ArgumentParser(description="Publish CC ES Analyzer at /cc_es_analyzer/ on the docs proxy's own path space.")
    ap.add_argument("--host", default=os.getenv("CC_DEPLOY_HOST"),
                    help="Target host running the reverse proxy (or set CC_DEPLOY_HOST). Required.")
    ap.add_argument("--user", default=os.getenv("CC_DEPLOY_USER", "root"))
    ap.add_argument("--password", default=os.getenv("CC_DEPLOY_PASS"))
    ap.add_argument("--ssh-port", type=int, default=22)
    ap.add_argument("--proxy-container", default=os.getenv("CC_PROXY_CONTAINER", PROXY_CONTAINER),
                    help="Name of the host's reverse-proxy nginx container.")
    ap.add_argument("--template-path", default=os.getenv("CC_PROXY_TEMPLATE", TEMPLATE_PATH),
                    help="Path on the HOST filesystem to the proxy's nginx source template.")
    ap.add_argument("--live-conf-path", default=os.getenv("CC_PROXY_LIVE_CONF"),
                    help="Path INSIDE the container to the rendered docs default-server config. "
                         "Auto-detected via a signature search if omitted.")
    args = ap.parse_args()
    if not args.host:
        ap.error("--host is required (or set CC_DEPLOY_HOST).")
    PROXY_CONTAINER = args.proxy_container
    TEMPLATE_PATH = args.template_path
    password = args.password or getpass.getpass(f"SSH password for {args.user}@{args.host}: ")

    with open(LOCAL_SNIPPET, "r", encoding="utf-8") as fh:
        snippet = fh.read().replace("\r\n", "\n")
    block = f"{MARK_BEGIN}\n{snippet.rstrip()}\n{MARK_END}"

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"[nginx-path] Connecting to {args.user}@{args.host} …")
    ssh.connect(args.host, port=args.ssh_port, username=args.user, password=password, timeout=20)

    def run(cmd: str):
        _i, o, e = ssh.exec_command(cmd)
        rc = o.channel.recv_exit_status()
        return rc, o.read().decode(errors="replace"), e.read().decode(errors="replace")

    try:
        sftp = ssh.open_sftp()
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        # 0) Sanity: proxy container + template must exist.
        rc, out, _ = run(f"docker ps --format '{{{{.Names}}}}' | grep -x {PROXY_CONTAINER} || true")
        if PROXY_CONTAINER not in out:
            print(f"[nginx-path] ERROR: container {PROXY_CONTAINER} not running — aborting.")
            return 2
        rc, _, _ = run(f"test -f {TEMPLATE_PATH}")
        if rc != 0:
            print(f"[nginx-path] ERROR: template {TEMPLATE_PATH} not found — aborting.")
            return 2

        # 1) Back up the source template.
        rc, _, err = run(f"cp -a {TEMPLATE_PATH} {TEMPLATE_PATH}.bak.{ts}")
        print(f"[nginx-path] Backed up template -> {TEMPLATE_PATH}.bak.{ts}" if rc == 0
              else f"[nginx-path] WARN: template backup failed: {err.strip()}")

        # 2) Durable: insert location block into the source template (idempotent).
        with sftp.file(TEMPLATE_PATH, "r") as f:
            tmpl = f.read().decode(errors="replace")
        if MARK_BEGIN in tmpl:
            print("[nginx-path] Template already contains our location block — leaving as-is.")
        else:
            new_tmpl, n = _inject(tmpl, block)
            if n == 0:
                print(f"[nginx-path] ERROR: anchor {ANCHOR!r} not found in template — aborting "
                      f"(nothing changed). Pass a different anchor via source if the docs "
                      f"template's default_server uses a different server_name line.")
                return 3
            with sftp.file(TEMPLATE_PATH, "w") as f:
                f.write(new_tmpl.encode("utf-8"))
            print(f"[nginx-path] Inserted location block after {n} occurrence(s) of "
                  f"{ANCHOR!r} in the source template (durable across rebuilds).")

        # 3) Locate the LIVE rendered config file inside the container.
        live_path = args.live_conf_path
        if not live_path:
            rc, out, _ = run(
                f"docker exec {PROXY_CONTAINER} sh -c \"grep -rl -- '{LIVE_SIGNATURE}' /etc/nginx 2>/dev/null | head -1\""
            )
            live_path = out.strip()
        if not live_path:
            print(f"[nginx-path] ERROR: could not auto-locate the live docs config inside "
                  f"{PROXY_CONTAINER} (searched for signature {LIVE_SIGNATURE!r}). The durable "
                  f"template edit is in place, but nothing is applied live yet — re-run with "
                  f"--live-conf-path pointing at the rendered default-server file, or restart "
                  f"the proxy container to pick up the template.")
            return 4
        print(f"[nginx-path] Live docs config located at {live_path} (in {PROXY_CONTAINER}).")

        # 4) Read + patch a copy of the live file; skip if already applied.
        rc, live_content, err = run(f"docker exec {PROXY_CONTAINER} cat {live_path}")
        if rc != 0:
            print(f"[nginx-path] ERROR: could not read {live_path}: {err.strip()}")
            return 4
        if MARK_BEGIN in live_content:
            print("[nginx-path] Live config already contains our location block — skipping live patch.")
        else:
            new_live, n = _inject(live_content, block)
            if n == 0:
                print(f"[nginx-path] ERROR: anchor {ANCHOR!r} not found in live config — aborting live patch.")
                return 4

            run(f"mkdir -p {HOST_STAGE}")
            backup_stage = f"{HOST_STAGE}/live.orig.{ts}.conf"
            patched_stage = f"{HOST_STAGE}/live.patched.{ts}.conf"
            with sftp.file(backup_stage, "w") as f:
                f.write(live_content.encode("utf-8"))
            with sftp.file(patched_stage, "w") as f:
                f.write(new_live.encode("utf-8"))

            rc, _, err = run(f"docker cp {patched_stage} {PROXY_CONTAINER}:{live_path}")
            if rc != 0:
                print(f"[nginx-path] ERROR: docker cp failed: {err.strip()} — aborting (nothing reloaded).")
                return 5

            # 5) Validate INSIDE the container. Roll back the live file on failure.
            rc, out, err = run(f"docker exec {PROXY_CONTAINER} nginx -t")
            print("[nginx-path] nginx -t:\n" + (out + err).strip())
            if rc != 0:
                print("[nginx-path] ERROR: config test FAILED — restoring original live config, NOT reloading.")
                run(f"docker cp {backup_stage} {PROXY_CONTAINER}:{live_path}")
                return 6

            # 6) Graceful reload (no downtime, no restart).
            rc, out, err = run(f"docker exec {PROXY_CONTAINER} nginx -s reload")
            if rc != 0:
                print(f"[nginx-path] ERROR: reload failed: {(out+err).strip()}")
                return 7
            print("[nginx-path] Reloaded nginx gracefully.")

        sftp.close()

        # 7) Verify docs still serve AND our app answers at the new path.
        rc, out, _ = run("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/")
        print(f"[nginx-path] Docs default site (http://127.0.0.1/) -> HTTP {out.strip()}")
        rc, out, _ = run("curl -s -m 8 -o /dev/null -w '%{http_code}' http://127.0.0.1/cc_es_analyzer/api/health")
        print(f"[nginx-path] Our app via /cc_es_analyzer/ -> HTTP {out.strip()}")

        print(f"\n[nginx-path] DONE. Open  http://{args.host}/cc_es_analyzer/  (or https://{args.host}/cc_es_analyzer/).")
        return 0
    finally:
        ssh.close()


if __name__ == "__main__":
    raise SystemExit(main())
