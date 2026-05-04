# config/settings.py
from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

SharePointMode = Literal["drive_id", "folder_url", "site_path"]


class Settings(BaseSettings):
    graph_key: str = Field(..., alias="GRAPH_KEY")

    # ----------------------------------------------------------- #
    # SharePoint (igual que antes).
    # ----------------------------------------------------------- #
    sharepoint_mode: SharePointMode = Field(
        "drive_id",
        alias="SHAREPOINT_MODE",
    )
    sharepoint_folder_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SHAREPOINT_FOLDER_URL",
            "SHAREPOINT_SHARE_URL",
        ),
    )
    sharepoint_hostname: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SHAREPOINT_HOSTNAME",
            "SHAREPOINT_HOST",
        ),
    )
    sharepoint_site_path: str | None = Field(
        default=None,
        alias="SHAREPOINT_SITE_PATH",
    )
    sharepoint_drive_name: str = Field(
        "Documentos compartidos",
        alias="SHAREPOINT_DRIVE_NAME",
    )
    sharepoint_drive_id: str | None = Field(
        default=None,
        alias="SHAREPOINT_DRIVE_ID",
    )
    sharepoint_folder_root: str = Field(
        "albaranes",
        alias="SHAREPOINT_FOLDER_ROOT",
    )
    sharepoint_link_type: str = Field("view", alias="SHAREPOINT_LINK_TYPE")
    sharepoint_link_scope: str = Field(
        "organization",
        alias="SHAREPOINT_LINK_SCOPE",
    )
    sharepoint_create_link: bool = Field(
        True,
        alias="SHAREPOINT_CREATE_LINK",
    )

    # ----------------------------------------------------------- #
    # PostgreSQL (igual que antes).
    # ----------------------------------------------------------- #
    pg_host: str = Field("localhost", alias="PG_HOST")
    pg_port: int = Field(5432, alias="PG_PORT")
    pg_db: str = Field("albaranes", alias="PG_DB")
    pg_user: str = Field("postgres", alias="PG_USER")
    pg_password: str = Field(..., alias="PG_PASSWORD")

    pg_admin_db: str = Field("postgres", alias="PG_ADMIN_DB")
    pg_admin_user: str = Field("postgres", alias="PG_ADMIN_USER")
    pg_admin_password: str = Field(..., alias="PG_ADMIN_PASSWORD")

    # ----------------------------------------------------------- #
    # API.
    # ----------------------------------------------------------- #
    api_host: str = Field("127.0.0.1", alias="API_HOST")
    api_port: int = Field(8001, alias="API_PORT")
    http_timeout_s: int = Field(60, alias="HTTP_TIMEOUT_S")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_dir: str = Field("logs", alias="LOG_DIR")
    service_version: str = Field("1.0.0", alias="SERVICE_VERSION")

    # ----------------------------------------------------------- #
    # NUEVO — Sigrid API (estaba en .env.example pero no se cargaba).
    # sv3 llama a Sigrid para enriquecer obra y para descargar
    # contratos+PDFs. Si falta cualquiera, el enrichment se
    # autodesactiva (ver app.py: si las 3 variables no están, no
    # se construye el cliente y los enrichers quedan en None).
    # ----------------------------------------------------------- #
    sigrid_api_base_url: str | None = Field(
        default=None,
        alias="SIGRID_API_BASE_URL",
    )
    sigrid_api_function_key: str | None = Field(
        default=None,
        alias="SIGRID_API_FUNCTION_KEY",
    )
    sigrid_api_database: str | None = Field(
        default=None,
        alias="SIGRID_API_DATABASE",
    )
    sigrid_api_database_rep: str = Field(
        "ruesma_rep",
        alias="SIGRID_API_DATABASE_REP",
    )
    sigrid_api_timeout_s: float = Field(
        30.0,
        alias="SIGRID_API_TIMEOUT_S",
    )
    sigrid_api_pdf_timeout_s: float = Field(
        120.0,
        alias="SIGRID_API_PDF_TIMEOUT_S",
    )

    # Flag explícito para deshabilitar el enrichment de obra aunque
    # la API esté configurada (útil para debug o entornos sin Sigrid).
    obra_enrichment_enabled: bool = Field(
        True,
        alias="OBRA_ENRICHMENT_ENABLED",
    )

    # ----------------------------------------------------------- #
    # NUEVO — Valuation trigger (sv3 → sv6).
    # IMPORTANTE: con el orquestador sv7 desplegado, lo HABITUAL es
    # tener VALUATION_TRIGGER_ENABLED=false. Así sv3 NO dispara sv6
    # directamente; sv7 es quien orquesta la valoración tras leer la
    # respuesta del persist.
    #
    # Lo dejamos activable por bandera por dos razones:
    #  1. Permite rollback rápido si sv7 falla en producción.
    #  2. Permite usar sv3 como "todo en uno" en entornos de
    #     desarrollo donde no se quiere arrancar sv7.
    # ----------------------------------------------------------- #
    valuation_api_base_url: str | None = Field(
        default=None,
        alias="VALUATION_API_BASE_URL",
    )
    valuation_trigger_enabled: bool = Field(
        False,
        alias="VALUATION_TRIGGER_ENABLED",
        description="Si True y hay base_url, sv3 dispara sv6 al persistir. "
                    "Por defecto False porque sv7 lo orquesta.",
    )
    valuation_trigger_timeout_s: float = Field(
        3.0,
        alias="VALUATION_TRIGGER_TIMEOUT_S",
    )
    valuation_trigger_sync_timeout_s: float = Field(
        300.0,
        alias="VALUATION_TRIGGER_SYNC_TIMEOUT_S",
    )

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_sharepoint_mode(self) -> "Settings":
        if self.sharepoint_mode == "drive_id":
            if not (self.sharepoint_drive_id or "").strip():
                raise ValueError(
                    "SHAREPOINT_MODE=drive_id requiere SHAREPOINT_DRIVE_ID."
                )
            return self

        if self.sharepoint_mode == "folder_url":
            if not (self.sharepoint_folder_url or "").strip():
                raise ValueError(
                    "SHAREPOINT_MODE=folder_url requiere SHAREPOINT_FOLDER_URL."
                )
            return self

        if self.sharepoint_mode == "site_path":
            if not (self.sharepoint_hostname or "").strip():
                raise ValueError(
                    "SHAREPOINT_MODE=site_path requiere SHAREPOINT_HOSTNAME."
                )
            if not (self.sharepoint_site_path or "").strip():
                raise ValueError(
                    "SHAREPOINT_MODE=site_path requiere SHAREPOINT_SITE_PATH."
                )
            return self

        raise ValueError(
            "SHAREPOINT_MODE debe ser drive_id, folder_url o site_path."
        )

    @property
    def sigrid_configured(self) -> bool:
        """True si las 3 variables imprescindibles de Sigrid están presentes."""
        return bool(
            (self.sigrid_api_base_url or "").strip()
            and (self.sigrid_api_function_key or "").strip()
            and (self.sigrid_api_database or "").strip()
        )

    @property
    def valuation_trigger_configured(self) -> bool:
        return bool(
            self.valuation_trigger_enabled
            and (self.valuation_api_base_url or "").strip()
        )

    @property
    def database_url(self) -> str:
        user = quote_plus(self.pg_user)
        password = quote_plus(self.pg_password)
        database = quote_plus(self.pg_db)
        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{self.pg_host}:{self.pg_port}/{database}"
        )

    @property
    def admin_database_url(self) -> str:
        user = quote_plus(self.pg_admin_user)
        password = quote_plus(self.pg_admin_password)
        database = quote_plus(self.pg_admin_db)
        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{self.pg_host}:{self.pg_port}/{database}"
        )
