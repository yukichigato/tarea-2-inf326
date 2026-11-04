import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class Settings(BaseModel):
    model_config = ConfigDict(validate_default=True)

    architecture: Literal["v1", "v2"] = "v1"
    database_path: Path = Path("/data/urls.sqlite3")
    public_base_url: HttpUrl = HttpUrl("https://localhost:8443")
    tls_directory: Path = Path("/certs")
    grpc_target: str = "log:50051"
    grpc_timeout: float = Field(default=2, gt=0)
    rate_limit_requests: int = Field(default=60, gt=0)
    cache_ttl: int = Field(default=60, gt=0)

    @field_validator("public_base_url")
    @classmethod
    def validate_public_base_url(cls, value: HttpUrl) -> HttpUrl:
        if (
            value.scheme != "https"
            or value.path not in (None, "/")
            or value.query
            or value.fragment
            or value.username
            or value.password
        ):
            raise ValueError(
                "PUBLIC_BASE_URL must be an HTTPS origin without a path or credentials"
            )
        return value

    @classmethod
    def from_env(cls) -> "Settings":
        return cls.model_validate(
            {
                name: os.environ[name.upper()]
                for name in cls.model_fields
                if name.upper() in os.environ
            }
        )
