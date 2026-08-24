import os

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    es_host: str = Field(default="localhost", alias="ES_HOST")
    es_port: int = Field(default=9200, alias="ES_PORT")
    es_scheme: str = Field(default="http", alias="ES_SCHEME")
    es_user: str = Field(default="", alias="ES_USER")
    es_password: str = Field(default="", alias="ES_PASSWORD")
    es_verify_certs: bool = Field(default=False, alias="ES_VERIFY_CERTS")

    # ── MariaDB (modules/maria) ──────────────────────────────────────────
    # Embedded, the CC's MariaDB sits on the same `vision` docker network, so
    # the container name resolves and no port has to be published for us.
    #
    # Credentials are RESOLVED AT RUNTIME, not taken from here — see
    # modules/maria/credentials.py. Preference is MARIA_USER/MARIA_PASSWORD
    # environment, then the CC's own mysql wrapper (below), then these
    # defaults. Reading the wrapper is what lets a CC that changes the account
    # be followed without a rebuild.
    #
    # The wrapper is the appliance's `mysql` command — a two-line shell script
    # that execs the client with -u/-p inline. It is bind-mounted read-only.
    # There is no better source: the CC has no credential store for this, and
    # the same account is hardcoded in five other product scripts.
    maria_cred_file: str = Field(default="/usr/local/bin/mysql", alias="MARIA_CRED_FILE")
    maria_host: str = Field(default="config_kvision-infra-mariadb_1", alias="MARIA_HOST")
    maria_port: int = Field(default=3306, alias="MARIA_PORT")
    # Last-resort fallback only. The CC's shipped default — a default, not a
    # secret, since anyone with the product has it. Still worth flagging rather
    # than burying: a credential in source is something a product security
    # review raises, and the answer that holds up is a dedicated read-only
    # account per install, with these as the fallback for a CC without one.
    maria_user: str = Field(default="common_host", alias="MARIA_USER")
    maria_password: str = Field(default="radware", alias="MARIA_PASSWORD")
    # Guard rails that apply to every statement this module runs. Both exist so
    # a careless query on a customer's production CC cannot become an outage.
    maria_timeout_s: int = Field(default=15, alias="MARIA_TIMEOUT_S")
    maria_max_rows: int = Field(default=1000, alias="MARIA_MAX_ROWS")

    # ── Host access (core/hostexec.py, modules/system) ───────────────────
    # Directory shared with deploy/host_agent.py, the root process that runs
    # the System dashboard's checks on the CC host. Bind-mounted into the
    # container; the same request/result file exchange update_dir already uses
    # for updates, and for the same reason — the container has no docker
    # socket and no view of the host filesystem.
    hostexec_dir: str = Field(
        default=os.path.join(os.path.dirname(__file__), ".hostexec"),
        alias="HOSTEXEC_DIR")
    # The compose file whose services the dashboard reports on. This is the
    # CC's SYSTEM compose, not the monitoring one that carries this app: the
    # question a support engineer is asking is whether the product is running.
    compose_file: str = Field(default="/deploy/config/docker-compose.yaml",
                              alias="COMPOSE_FILE")
    # Percent-used thresholds for the storage check. Data, not constants, so a
    # CC with an unusual partition layout can be tuned without a rebuild.
    disk_warn_pct: int = Field(default=80, alias="DISK_WARN_PCT")
    disk_crit_pct: int = Field(default=90, alias="DISK_CRIT_PCT")

    # ── Login (core/auth.py) ─────────────────────────────────────────────
    # One shared appliance credential, the shape a network device's console
    # password has. Enforced EMBEDDED only by default: standalone runs on the
    # engineer's own machine against a CC they already have credentials for, so
    # a second password there guards nothing and would only be typed past.
    # Set AUTH_REQUIRED explicitly to override in either direction.
    auth_required: bool | None = Field(default=None, alias="AUTH_REQUIRED")
    auth_user: str = Field(default="admin", alias="AUTH_USER")
    # Where the password hash lives. MUST be on a bind mount embedded, or the
    # password reverts to the shipped default every time the container is
    # recreated — which happens on every property-file change, so an operator
    # would silently lose their hardening the first time they unlocked a
    # capability. The default suits a standalone run from a checkout.
    auth_file: str = Field(
        default=os.path.join(os.path.dirname(__file__), "data", "auth.json"),
        alias="AUTH_FILE")

    # ── Time box (core/lifecycle.py) ─────────────────────────────────────
    # Embedded, this container is meant to be OFF and switched on for as long
    # as the job takes. It stops itself after this many minutes unless someone
    # extends it, and nothing restarts it but a deliberate `docker start`.
    # Zero disables the time box entirely.
    session_minutes: int = Field(default=60, alias="SESSION_MINUTES")
    session_warn_minutes: int = Field(default=5, alias="SESSION_WARN_MINUTES")
    # None = follow the profile (embedded on, standalone off).
    session_timebox: bool | None = Field(default=None, alias="SESSION_TIMEBOX")

    service_host: str = Field(default="0.0.0.0", alias="SERVICE_HOST")
    service_port: int = Field(default=8000, alias="SERVICE_PORT")

    # Serve over HTTPS. If enabled without a cert/key, a self-signed pair is
    # generated at startup (browsers will warn). Mount your own for production.
    service_ssl:  bool = Field(default=False, alias="SERVICE_SSL")
    ssl_certfile: str = Field(default="", alias="SSL_CERTFILE")
    ssl_keyfile:  str = Field(default="", alias="SSL_KEYFILE")

    # ── Deployment profile (core/policy.py) ──────────────────────────────
    # Two supported modes, both products. "standalone" (default) is the remote
    # tool an engineer points at a CC over the network — the only way to reach
    # the CCs already in the field, which do not carry the embedded build.
    # "embedded" is the copy that ships inside a CyberController and leaves the
    # data-fabricating features unregistered. The CC's docker-compose is what
    # pins an appliance to "embedded"; defaulting to "standalone" keeps a plain
    # `python main.py` working.
    profile: str = Field(default="standalone", alias="ANALYZER_PROFILE")

    # Property file on the system filesystem that unlocks capabilities the
    # profile does not carry (see core/policy.py). Absent on every
    # instance except those an entitled operator has deliberately unlocked.
    # The directory is the CC's existing convention — 26 sibling .properties
    # files live there (root:root 0644), so operators already know it and the
    # format is the one they read everywhere else in the product. The file
    # name tracks the service name, so it changes when the app is renamed.
    # cc_admin, not cc_analyzer: the comment above promised the file name
    # tracks the service name, and it did not follow the rename. The mismatch
    # is invisible until someone creates the file deploy/cc_admin.properties.sample
    # tells them to and nothing unlocks — a silent no-op is the worst possible
    # failure for a security control, since it reads as "the gate held".
    policy_file: str = Field(
        default="/opt/radware/mgt-server/properties/cc_admin.properties",
        alias="POLICY_FILE")

    # Where server-side index archives (<index>.csv.gz) are stored. In Docker
    # this is a mounted volume so archives survive container rebuilds.
    exports_dir: str = Field(
        default=os.path.join(os.path.dirname(__file__), "exports"),
        alias="EXPORTS_DIR")

    # Snapshot archives: where ES/OpenSearch fs-repository files live on the ES
    # MACHINE. snap_host_dir is the path on the host (SSH/zip side); snap_es_dir
    # is the same directory as the ES process sees it (its Docker mount) — this
    # is what goes into the repository "location" setting. CC defaults below.
    snap_host_dir: str = Field(default="/opt/radware/tmp/es", alias="SNAP_HOST_DIR")
    snap_es_dir: str = Field(default="/usr/share/opensearch/backup", alias="SNAP_ES_DIR")

    # The host to SSH into to zip and pull a snapshot. Standalone this is the CC
    # the app is connected to and is resolved from the ES connection, so this is
    # left empty. Embedded, the ES client talks to a container SERVICE NAME with
    # no sshd, while the snapshot files live on the CC HOST — so the snapshot
    # flow must SSH into the CC itself (its sshd, with system credentials). When
    # set, this pins that host address; when empty, embedded falls back to the
    # container's default-route gateway (the Docker host = the CC). Set it in
    # the embedded compose when the gateway is not the reachable CC address.
    snap_ssh_host: str = Field(default="", alias="SNAP_SSH_HOST")

    # ── Update checks (core/updater.py) ──────────────────────────────────
    # Directory shared with the host updater agent (deploy/update_agent.sh):
    # it reports what `git fetch` found there, and we drop the update request
    # into it. Bind-mounted into the container by docker-compose.yml.
    update_dir: str = Field(
        default=os.path.join(os.path.dirname(__file__), ".update"),
        alias="UPDATE_DIR")
    update_check_enabled: bool = Field(default=True, alias="UPDATE_CHECK_ENABLED")
    # Set false to make the UI check-only (no one-click update button).
    update_allow_apply: bool = Field(default=True, alias="UPDATE_ALLOW_APPLY")

    # Fallback check straight against Bitbucket, used only when there is no host
    # agent and no git checkout. Needs a token (or user + app password) because
    # the repo is private; without one the UI just says checks are unavailable.
    update_bb_workspace: str = Field(default="rdwr", alias="UPDATE_BB_WORKSPACE")
    update_bb_repo: str = Field(default="ams_qa_ai_toolkit", alias="UPDATE_BB_REPO")
    update_bb_branch: str = Field(default="dev", alias="UPDATE_BB_BRANCH")
    update_bb_path: str = Field(default="cc_es_analyzer", alias="UPDATE_BB_PATH")
    update_bb_user: str = Field(default="", alias="UPDATE_BB_USER")
    update_bb_password: str = Field(default="", alias="UPDATE_BB_PASSWORD")
    update_bb_token: str = Field(default="", alias="UPDATE_BB_TOKEN")

    model_config = {"env_file": ".env", "populate_by_name": True}

    @property
    def es_url(self) -> str:
        return f"{self.es_scheme}://{self.es_host}:{self.es_port}"


settings = Settings()

