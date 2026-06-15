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
    """Configuración de sv3 (albaranes-persistence-api).

    Bloques:
      - Microsoft Graph + SharePoint (almacenamiento de PDFs).
      - PostgreSQL (BBDD principal + admin).
      - API (host/port).
      - Sigrid API on-prem (enriquecimiento obra + contrato).
      - Valuation trigger (sv6) — DESACTIVADO si lo orquesta sv7.
    """

    # ------------------------------------------------------------ #
    # Microsoft Graph + SharePoint.
    # ------------------------------------------------------------ #
    graph_key: str = Field(..., alias="GRAPH_KEY")

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

    # Conversor Word→PDF de contratos: "graph" (Microsoft 365, máxima
    # fidelidad, sirve igual en local y Azure) o "libreoffice" (headless;
    # necesita soffice / imagen con libreoffice-writer).
    word_to_pdf_backend: str = Field("graph", alias="WORD_TO_PDF_BACKEND")
    sharepoint_link_scope: str = Field(
        "organization",
        alias="SHAREPOINT_LINK_SCOPE",
    )
    sharepoint_create_link: bool = Field(
        True,
        alias="SHAREPOINT_CREATE_LINK",
    )

    # ------------------------------------------------------------ #
    # PostgreSQL.
    # ------------------------------------------------------------ #
    pg_host: str = Field("localhost", alias="PG_HOST")
    pg_port: int = Field(5432, alias="PG_PORT")
    pg_db: str = Field("albaranes", alias="PG_DB")
    pg_user: str = Field("postgres", alias="PG_USER")
    pg_password: str = Field(..., alias="PG_PASSWORD")

    pg_admin_db: str = Field("postgres", alias="PG_ADMIN_DB")
    pg_admin_user: str = Field("postgres", alias="PG_ADMIN_USER")
    pg_admin_password: str = Field(..., alias="PG_ADMIN_PASSWORD")

    # ------------------------------------------------------------ #
    # API + observabilidad.
    # ------------------------------------------------------------ #
    api_host: str = Field("127.0.0.1", alias="API_HOST")
    api_port: int = Field(8001, alias="API_PORT")
    http_timeout_s: int = Field(60, alias="HTTP_TIMEOUT_S")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_dir: str = Field("logs", alias="LOG_DIR")
    service_version: str = Field("1.0.0", alias="SERVICE_VERSION")

    # ------------------------------------------------------------ #
    # Sigrid API on-prem (Function App de Azure).
    # Si las 3 credenciales están presentes, se cablean los servicios
    # ObraEnrichmentService y ContratoEnrichmentService. Si falta
    # alguna, sv3 arranca pero ese enriquecimiento queda desactivado
    # (best-effort).
    # ------------------------------------------------------------ #
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
    sigrid_api_timeout_s: float = Field(
        30.0,
        alias="SIGRID_API_TIMEOUT_S",
    )
    obra_enrichment_enabled: bool = Field(
        True,
        alias="OBRA_ENRICHMENT_ENABLED",
    )

    # Resolucion determinista de cabecera (obra_codigo / proveedor_cif
    # por coincidencia de texto contra Sigrid cuando la IA no los fijo).
    header_resolver_enabled: bool = Field(
        True,
        alias="HEADER_RESOLVER_ENABLED",
    )
    header_resolver_min_score: float = Field(
        0.5,
        alias="HEADER_RESOLVER_MIN_SCORE",
    )

    # ------------------------------------------------------------ #
    # Grounding de cabecera para la fase 2 (jun 2026).
    #
    # Endpoint POST /v1/sigrid/header-grounding consumido por sv7
    # ANTES de la 2ª IA: validación determinista por CIF (proveedor)
    # y por código (obra) + listas de candidatos para lo no validado.
    # Los topes limitan el tamaño del prompt de fase 2.
    # ------------------------------------------------------------ #
    header_grounding_enabled: bool = Field(
        True,
        alias="HEADER_GROUNDING_ENABLED",
    )
    grounding_max_obras_candidatas: int = Field(
        300,
        alias="GROUNDING_MAX_OBRAS_CANDIDATAS",
    )
    grounding_max_proveedores_candidatos: int = Field(
        200,
        alias="GROUNDING_MAX_PROVEEDORES_CANDIDATOS",
    )

    # ------------------------------------------------------------ #
    # Valuation trigger (sv6).
    #
    # IMPORTANTE: cuando el orquestador (sv7) está en producción,
    # ESTE trigger debe estar a false porque el orquestador es quien
    # llama al sv6. Si lo dejas a true, sv3 dispara la valoración
    # nada más persistir y duplicas trabajo.
    # ------------------------------------------------------------ #
    valuation_api_base_url: str | None = Field(
        default=None,
        alias="VALUATION_API_BASE_URL",
    )
    valuation_trigger_enabled: bool = Field(
        False,
        alias="VALUATION_TRIGGER_ENABLED",
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
    def sigrid_credentials_present(self) -> bool:
        """True si las 3 credenciales necesarias están presentes."""
        return bool(
            (self.sigrid_api_base_url or "").strip()
            and (self.sigrid_api_function_key or "").strip()
            and (self.sigrid_api_database or "").strip()
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
