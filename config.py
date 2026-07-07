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

    model_config = {"env_file": ".env", "populate_by_name": True}

    @property
    def es_url(self) -> str:
        return f"{self.es_scheme}://{self.es_host}:{self.es_port}"


settings = Settings()

