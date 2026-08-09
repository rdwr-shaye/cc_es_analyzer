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
    policy_file: str = Field(
        default="/opt/radware/mgt-server/properties/cc_analyzer.properties",
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

