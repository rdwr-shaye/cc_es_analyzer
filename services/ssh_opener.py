"""
SSH fallback helper.

When the Elasticsearch port on the target CC machine is closed (firewall),
we SSH into the box with the user-supplied credentials and open the port.

Command chosen by the SSH user:
  - root  →  /opt/radware/box/bin/net_firewall_open-port.sh set "<port>" open
  - other →  net firewall open-port set "<port>" open

The port number follows the user preference (default 9200) in both cases.
"""
import logging
import socket
import time

logger = logging.getLogger(__name__)


def port_is_open(host: str, port: int, timeout: float = 5.0) -> bool:
    """Return True if a TCP connection to host:port succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def build_open_port_command(ssh_user: str, port: int) -> str:
    """Return the firewall open-port command appropriate for the SSH user."""
    if (ssh_user or "").strip().lower() == "root":
        return f'/opt/radware/box/bin/net_firewall_open-port.sh set {port} open'
    return f'net firewall open-port set {port} open'


def build_list_ports_command(ssh_user: str) -> str:
    """Return the firewall list-ports command appropriate for the SSH user."""
    if (ssh_user or "").strip().lower() == "root":
        return '/opt/radware/box/bin/net_firewall_open-port.sh list'
    return 'net firewall open-port list'


def _port_open_in_listing(output: str, port: int) -> bool:
    """True if the firewall listing shows `port` as an open tcp port.

    Both the root and non-root list commands print a line like:  `tcp   9200`.
    """
    p = str(port)
    for line in (output or "").splitlines():
        toks = line.split()
        if not toks:
            continue
        low = [t.lower() for t in toks]
        # A listing row (has "tcp" and the exact port). The echoed command line
        # never contains the port number, so this won't false-match.
        if "tcp" in low and p in toks:
            return True
    return False


def ensure_port_open_via_ssh(host: str, ssh_user: str, ssh_password: str,
                             port: int, ssh_port: int = 22,
                             timeout: float = 20.0) -> dict:
    """
    SSH in and make sure the ES `port` is open in the CC machine's firewall,
    WITHOUT re-opening it needlessly:

      1. run the firewall `list` command and check whether `port` is already open,
      2. if already open  -> return {already_open: True} (caller skips ahead),
      3. if not           -> run the `open` command.

    Returns: { ok, already_open, opened, list_command, command,
               list_output, output, error }.
    """
    result = {
        "ok": False, "already_open": False, "opened": False,
        "list_command": "", "command": "",
        "list_output": "", "output": "", "error": "",
    }

    try:
        import paramiko
    except ImportError:
        result["error"] = "paramiko is not installed (see requirements.txt)."
        return result

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        logger.info("[ssh] connecting to %s@%s:%s to check/open port %s",
                    ssh_user, host, ssh_port, port)
        client.connect(hostname=host, port=ssh_port, username=ssh_user,
                       password=ssh_password, timeout=timeout,
                       banner_timeout=timeout, auth_timeout=timeout,
                       allow_agent=False, look_for_keys=False)

        # 1) Is the port already open in the firewall?
        list_cmd = build_list_ports_command(ssh_user)
        result["list_command"] = list_cmd
        list_out = _run_in_interactive_shell(client, list_cmd, timeout=timeout)
        result["list_output"] = list_out

        if _port_open_in_listing(list_out, port):
            result["already_open"] = True
            result["ok"] = True
            logger.info("[ssh] port %s is already open in the firewall — not re-opening", port)
            return result

        # 2) Not open — open it.
        open_cmd = build_open_port_command(ssh_user, port)
        result["command"] = open_cmd
        logger.info("[ssh] port %s not open; running: %s", port, open_cmd)
        out = _run_in_interactive_shell(client, open_cmd, timeout=timeout)
        result["output"] = out
        result["opened"] = True

        low = out.lower()
        if any(tok in low for tok in (
            "can't access tty", "permission denied", "not allowed",
            "invalid", "error", "failure", "failed", "unknown command",
        )):
            result["ok"] = False
            result["error"] = _first_error_line(out) or "Firewall open command reported an error."
        else:
            result["ok"] = True
        logger.info("[ssh] open-port done ok=%s output=%r", result["ok"], out)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if e.__class__.__name__ == "AuthenticationException":
            msg = "SSH authentication failed — check the SSH username/password."
        result["error"] = msg
        logger.error("[ssh] ensure_port_open failed: %s", e)
    finally:
        try: client.close()
        except Exception: pass

    return result


def open_port_via_ssh(host: str, ssh_user: str, ssh_password: str,
                      port: int, ssh_port: int = 22,
                      timeout: float = 20.0) -> dict:
    """
    SSH into `host` and run the firewall command to open `port`.

    The CC machine's login shell is often the Radware restricted product
    shell (`pshell`), which requires an interactive PTY — a plain
    `exec_command` fails with "can't access tty; job control turned off".
    We therefore drive the command through an interactive shell channel
    (which allocates a PTY).

    Returns a dict: { "ok": bool, "command": str, "stdout": str,
                      "stderr": str, "exit_status": int|None, "error": str }
    """
    result = {
        "ok": False,
        "command": "",
        "stdout": "",
        "stderr": "",
        "exit_status": None,
        "error": "",
    }

    try:
        import paramiko
    except ImportError:
        result["error"] = ("paramiko is not installed. Run "
                           "`pip install paramiko` (see requirements.txt).")
        logger.error("[ssh] %s", result["error"])
        return result

    command = build_open_port_command(ssh_user, port)
    result["command"] = command

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        logger.info("[ssh] connecting to %s@%s:%s to open port %s",
                    ssh_user, host, ssh_port, port)
        client.connect(
            hostname=host,
            port=ssh_port,
            username=ssh_user,
            password=ssh_password,
            timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )

        logger.info("[ssh] running (interactive PTY) command: %s", command)
        output = _run_in_interactive_shell(client, command, timeout=timeout)
        result["stdout"] = output

        # The restricted shell rarely returns a usable exit code, so we
        # confirm success by re-checking TCP reachability of the ES port.
        opened = False
        for _ in range(6):  # ~6s
            if port_is_open(host, port, timeout=2.0):
                opened = True
                break
            time.sleep(1.0)

        low = output.lower()
        had_error = any(tok in low for tok in (
            "can't access tty", "permission denied", "not allowed",
            "invalid", "error", "failure", "failed", "unknown command",
        ))

        if opened:
            result["ok"] = True
        elif had_error:
            result["ok"] = False
            result["error"] = _first_error_line(output) or "Firewall command reported an error."
        else:
            # No explicit error and the command ran — treat as best-effort ok;
            # the caller will retry the ES connection anyway.
            result["ok"] = True

        logger.info("[ssh] command done ok=%s port_open=%s output=%r",
                    result["ok"], opened, output)
    except Exception as e:
        result["error"] = str(e)
        logger.error("[ssh] failed: %s", e)
    finally:
        try:
            client.close()
        except Exception:
            pass

    return result


def _run_in_interactive_shell(client, command: str, timeout: float = 20.0) -> str:
    """
    Open an interactive shell (allocates a PTY), send `command`, then exit,
    and return the accumulated output. Works with restricted shells like
    Radware `pshell` that reject non-interactive `exec_command`.
    """
    chan = client.invoke_shell(width=220, height=60)
    chan.settimeout(timeout)

    buf = []

    def _drain(wait: float) -> None:
        end = time.time() + wait
        while time.time() < end:
            if chan.recv_ready():
                try:
                    data = chan.recv(65535).decode("utf-8", "replace")
                except Exception:
                    break
                if data:
                    buf.append(data)
                    end = time.time() + 0.6  # keep reading while data flows
            else:
                time.sleep(0.15)

    # 1) Let the login banner / prompt settle.
    _drain(2.0)
    # 2) Send the command.
    chan.send(command + "\n")
    # 3) Read the command output.
    _drain(4.0)
    # 4) Leave the shell cleanly.
    try:
        chan.send("exit\n")
        _drain(1.0)
    except Exception:
        pass

    try:
        chan.close()
    except Exception:
        pass

    return "".join(buf).strip()


def _first_error_line(output: str) -> str:
    for line in output.splitlines():
        low = line.lower()
        if any(tok in low for tok in (
            "permission denied", "not allowed", "invalid",
            "error", "failure", "failed", "unknown command",
            "can't access tty",
        )):
            return line.strip()
    return ""


