#!/usr/bin/env python3
"""
Safely publish CC ES Analyzer through the existing docs-platform nginx reverse
proxy using a dedicated (name-based) server block — WITHOUT modifying the docs
routing.

What it does, carefully and idempotently:
  1. Backs up the reverse-proxy source template on the host.
  2. Appends our vhost to that template (marker-guarded, so re-runs don't dup) —
     this makes the change survive a proxy image rebuild.
  3. Applies the vhost LIVE with zero downtime as a SEPARATE conf.d file
     (never edits the docs default.conf), validates with `nginx -t`, and only
     then does a graceful `nginx -s reload`. If validation fails, it removes the
     file and aborts — the docs platform is left untouched.
  4. Verifies the docs site still serves AND our app answers via the proxy.

Nothing else on the host is touched (no other container, image, or network).

This is an OPTIONAL integration for a host that already runs a path-based nginx
reverse proxy (the reference environment is a "docs-platform" proxy). The proxy
container name and template path are configurable, so it can target any such
host — nothing here is required to run the app itself.

Usage:
    python deploy/setup_nginx.py --host <host> --user root
    python deploy/setup_nginx.py --host <host> --proxy-container <name> \
        --template-path /path/to/nginx.conf.template
"""
from __future__ import annotations
import argparse, getpass, os, sys, datetime

try:
    import paramiko
except ImportError:
    sys.exit("paramiko is required:  pip install paramiko")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_VHOST  = os.path.join(PROJECT_ROOT, "deploy", "nginx", "cc-analyzer.conf")

# Reference-environment defaults; override with --proxy-container / --template-path
# (or env CC_PROXY_CONTAINER / CC_PROXY_TEMPLATE) for any other host.
PROXY_CONTAINER = "docs-reverse-proxy"
TEMPLATE_PATH   = "/opt/kvision_tools/docs-platform/reverse-proxy/nginx.conf.template"
LIVE_CONF_IN_CONTAINER = "/etc/nginx/conf.d/cc-analyzer.conf"
HOST_STAGE = "/opt/cc_es_analyzer/nginx"

MARK_BEGIN = "# >>> cc_es_analyzer vhost (managed) >>>"
MARK_END   = "# <<< cc_es_analyzer vhost (managed) <<<"


def main() -> int:
    global PROXY_CONTAINER, TEMPLATE_PATH
    ap = argparse.ArgumentParser(description="Publish CC ES Analyzer via an existing nginx reverse proxy (safe).")
    ap.add_argument("--host", default=os.getenv("CC_DEPLOY_HOST"),
                    help="Target host running the reverse proxy (or set CC_DEPLOY_HOST). Required.")
    ap.add_argument("--user", default=os.getenv("CC_DEPLOY_USER", "root"))
    ap.add_argument("--password", default=os.getenv("CC_DEPLOY_PASS"))
    ap.add_argument("--ssh-port", type=int, default=22)
    ap.add_argument("--proxy-container", default=os.getenv("CC_PROXY_CONTAINER", PROXY_CONTAINER),
                    help="Name of the host's reverse-proxy nginx container.")
    ap.add_argument("--template-path", default=os.getenv("CC_PROXY_TEMPLATE", TEMPLATE_PATH),
                    help="Path on the host to the proxy's nginx source template (for a durable edit).")
    args = ap.parse_args()
    if not args.host:
        ap.error("--host is required (or set CC_DEPLOY_HOST).")
    PROXY_CONTAINER = args.proxy_container
    TEMPLATE_PATH = args.template_path
    password = args.password or getpass.getpass(f"SSH password for {args.user}@{args.host}: ")

    with open(LOCAL_VHOST, "r", encoding="utf-8") as fh:
        vhost = fh.read().replace("\r\n", "\n")
    block = f"\n{MARK_BEGIN}\n{vhost.rstrip()}\n{MARK_END}\n"

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"[nginx] Connecting to {args.user}@{args.host} …")
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
            print(f"[nginx] ERROR: container {PROXY_CONTAINER} not running — aborting.")
            return 2
        rc, _, _ = run(f"test -f {TEMPLATE_PATH}")
        if rc != 0:
            print(f"[nginx] ERROR: template {TEMPLATE_PATH} not found — aborting.")
            return 2

        # 1) Back up the source template.
        rc, _, err = run(f"cp -a {TEMPLATE_PATH} {TEMPLATE_PATH}.bak.{ts}")
        print(f"[nginx] Backed up template -> {TEMPLATE_PATH}.bak.{ts}" if rc == 0
              else f"[nginx] WARN: template backup failed: {err.strip()}")

        # 2) Durable: append vhost to the source template (idempotent).
        with sftp.file(TEMPLATE_PATH, "r") as f:
            tmpl = f.read().decode(errors="replace")
        if MARK_BEGIN in tmpl:
            print("[nginx] Template already contains our vhost — leaving as-is.")
        else:
            with sftp.file(TEMPLATE_PATH, "a") as f:
                f.write(block)
            print("[nginx] Appended vhost to source template (durable across rebuilds).")

        # 3) Live apply as a SEPARATE conf.d file (zero downtime).
        run(f"mkdir -p {HOST_STAGE}")
        stage = f"{HOST_STAGE}/cc-analyzer.conf"
        with sftp.file(stage, "w") as f:
            f.write(vhost)
        sftp.close()
        rc, _, err = run(f"docker cp {stage} {PROXY_CONTAINER}:{LIVE_CONF_IN_CONTAINER}")
        if rc != 0:
            print(f"[nginx] ERROR: docker cp failed: {err.strip()} — aborting (nothing reloaded).")
            return 3

        # 4) Validate INSIDE the container. Abort + clean up on failure.
        rc, out, err = run(f"docker exec {PROXY_CONTAINER} nginx -t")
        print("[nginx] nginx -t:\n" + (out + err).strip())
        if rc != 0:
            print("[nginx] ERROR: config test FAILED — removing our file, NOT reloading.")
            run(f"docker exec {PROXY_CONTAINER} rm -f {LIVE_CONF_IN_CONTAINER}")
            return 4

        # 5) Graceful reload (no downtime, no restart).
        rc, out, err = run(f"docker exec {PROXY_CONTAINER} nginx -s reload")
        if rc != 0:
            print(f"[nginx] ERROR: reload failed: {(out+err).strip()}")
            return 5
        print("[nginx] Reloaded nginx gracefully.")

        # 6) Verify docs still serve AND our app answers through the proxy.
        rc, out, _ = run("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/")
        print(f"[nginx] Docs default site (http://127.0.0.1/) -> HTTP {out.strip()}")
        rc, out, _ = run("curl -s -m 8 -H 'Host: cc-analyzer' http://127.0.0.1/api/health || echo FAIL")
        print(f"[nginx] Our app via proxy (Host: cc-analyzer) -> {out.strip()}")

        print("\n[nginx] DONE. Point a hostname at the VM to use it, e.g. add to your hosts file:")
        print(f"        {args.host}  cc-analyzer  cc-analyzer.local")
        print("        then open  http://cc-analyzer/")
        return 0
    finally:
        ssh.close()


if __name__ == "__main__":
    raise SystemExit(main())

