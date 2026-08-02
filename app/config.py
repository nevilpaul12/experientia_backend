from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Experientia"
    secret_key: str = Field(
        default="experientia-dev-secret-change-in-production",
        validation_alias=AliasChoices("SECRET_KEY", "secret_key"),
    )
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7
    database_url: str = Field(
        default="sqlite:///./experientia.db",
        validation_alias=AliasChoices("DATABASE_URL", "database_url"),
    )
    upload_dir: str = "uploads"
    cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        validation_alias=AliasChoices("CORS_ORIGINS", "cors_origins"),
    )
    public_base_url: str = Field(
        default="http://localhost:8000",
        validation_alias=AliasChoices("PUBLIC_BASE_URL", "public_base_url"),
    )
    # Demo login OTPs (replace with SMS later)
    manager_otp: str = Field(
        default="325476",
        validation_alias=AliasChoices("MANAGER_OTP", "manager_otp"),
    )
    executor_otp: str = Field(
        default="123456",
        validation_alias=AliasChoices("EXECUTOR_OTP", "executor_otp"),
    )
    # SQLAlchemy pool — sized for App Runner multi-instance (~100+ users)
    db_pool_size: int = Field(default=10, validation_alias=AliasChoices("DB_POOL_SIZE"))
    db_max_overflow: int = Field(default=20, validation_alias=AliasChoices("DB_MAX_OVERFLOW"))

    aws_access_key_id: str = Field(
        default="",
        validation_alias=AliasChoices("AWS_ACCESS_KEY_ID", "ACCESS_KEY_AWS_ID"),
    )
    aws_secret_access_key: str = Field(
        default="",
        validation_alias=AliasChoices("AWS_SECRET_ACCESS_KEY", "SECRET_KEY_AWS"),
    )
    aws_region: str = Field(
        default="ap-south-1",
        validation_alias=AliasChoices("AWS_REGION", "REGION_AWS"),
    )
    s3_bucket: str = Field(
        default="",
        validation_alias=AliasChoices("S3_BUCKET", "S3_BUCKET_NAME"),
    )
    s3_endpoint_url: str = ""
    # Direct browser → S3 PUT requires bucket CORS for your frontend origin.
    # Default false: upload via /api/uploads/local (server puts to S3).
    s3_browser_upload: bool = Field(
        default=False,
        validation_alias=AliasChoices("S3_BROWSER_UPLOAD", "s3_browser_upload"),
    )

    seed_demo: bool = False
    create_tables: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def use_s3(self) -> bool:
        return bool(self.s3_bucket and self.aws_access_key_id)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()
