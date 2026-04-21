# interface_adapters/api/app.py
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any, Dict

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from application.pipelines.persist_albaran_pipeline import (
    PersistAlbaranPipeline,
    PersistAlbaranRequest,
)
from application.services.albaran_normalizer import AlbaranNormalizer
from application.services.contrato_enrichment_service import (
    ContratoEnrichmentService,
)
from application.services.obra_enrichment_service import ObraEnrichmentService
from config.settings import Settings
from infrastructure.database.session_factory import SessionFactory
from infrastructure.database.sqlalchemy_albaran_repository import (
    SqlAlchemyAlbaranRepository,
)
from infrastructure.sigrid.sigrid_api_contrato_client import (
    SigridApiContratoClient,
)
from infrastructure.sigrid.sigrid_api_obra_client import SigridApiObraClient
from infrastructure.storage.sharepoint_document_storage import (
    SharePointDocumentStorage,
)

logger = logging.getLogger(__name__)


def build_app(settings: Settings) -> FastAPI:
    session_factory = SessionFactory(
        database_url=settings.database_url,
        admin_database_url=settings.admin_database_url,
        target_database_name=settings.pg_db,
    )
    repository = SqlAlchemyAlbaranRepository(session_factory)
    repository.initialize()

    document_storage = SharePointDocumentStorage(
        graph_key=settings.graph_key,
        timeout_s=settings.http_timeout_s,
        mode=settings.sharepoint_mode,
        hostname=settings.sharepoint_hostname,
        site_path=settings.sharepoint_site_path,
        drive_name=settings.sharepoint_drive_name,
        drive_id=settings.sharepoint_drive_id,
        folder_root=settings.sharepoint_folder_root,
        folder_url=settings.sharepoint_folder_url,
        link_type=settings.sharepoint_link_type,
        link_scope=settings.sharepoint_link_scope,
        create_link=settings.sharepoint_create_link,
    )

    # ------------------------------------------------------------------ #
    # Construcción del servicio de enriquecimiento de obra (Sigrid on-prem).
    # Si faltan credenciales en .env, queda como None y el pipeline lo salta.
    # ------------------------------------------------------------------ #
    obra_enrichment_service: ObraEnrichmentService | None = None
    logger.info(
        "[obra-enrichment][wiring] obra_enrichment_enabled=%s "
        "sigrid_api_configured=%s base_url=%s database=%s",
        settings.obra_enrichment_enabled,
        settings.sigrid_api_configured,
        settings.sigrid_api_base_url,
        settings.sigrid_api_database,
    )
    if settings.obra_enrichment_enabled and settings.sigrid_api_configured:
        sigrid_obra_client = SigridApiObraClient(
            base_url=settings.sigrid_api_base_url,
            function_key=settings.sigrid_api_function_key,
            database=settings.sigrid_api_database,
            timeout_s=settings.sigrid_api_timeout_s,
        )
        obra_enrichment_service = ObraEnrichmentService(
            client=sigrid_obra_client,
            repository=repository,
            enabled=True,
        )
        logger.info(
            "[obra-enrichment][wiring] ObraEnrichmentService CREADO y listo."
        )
    else:
        logger.warning(
            "[obra-enrichment][wiring] NO se crea ObraEnrichmentService. "
            "Motivo: enabled=%s configured=%s. "
            "Revisa SIGRID_API_BASE_URL / SIGRID_API_FUNCTION_KEY / "
            "SIGRID_API_DATABASE / OBRA_ENRICHMENT_ENABLED en tu .env.",
            settings.obra_enrichment_enabled,
            settings.sigrid_api_configured,
        )

    # ------------------------------------------------------------------ #
    # Construcción del servicio de enriquecimiento de CONTRATOS.
    # Reutiliza las credenciales Sigrid y el MISMO SharePointDocumentStorage
    # del upload de albaranes para subir también los PDFs de contrato a
    # <base>/<YYYY>/<MM>/contratos/ (descarga vía /api/documents/read).
    #
    # Flag independiente (contrato_enrichment_enabled): permite apagar
    # SOLO el de contratos sin tocar el de obra. Por defecto True si no
    # está definido en Settings (compatibilidad hacia atrás via getattr).
    # ------------------------------------------------------------------ #
    contrato_enrichment_service: ContratoEnrichmentService | None = None
    contrato_enabled_flag = getattr(
        settings, "contrato_enrichment_enabled", True
    )
    logger.info(
        "[contrato-enrichment][wiring] contrato_enrichment_enabled=%s "
        "sigrid_api_configured=%s base_url=%s database=%s",
        contrato_enabled_flag,
        settings.sigrid_api_configured,
        settings.sigrid_api_base_url,
        settings.sigrid_api_database,
    )
    if contrato_enabled_flag and settings.sigrid_api_configured:
        contrato_client = SigridApiContratoClient(
            base_url=settings.sigrid_api_base_url,
            function_key=settings.sigrid_api_function_key,
            database=settings.sigrid_api_database,
            timeout_s=settings.sigrid_api_timeout_s,
        )
        contrato_enrichment_service = ContratoEnrichmentService(
            client=contrato_client,
            repository=repository,
            pdf_storage=document_storage,
            enabled=True,
        )
        logger.info(
            "[contrato-enrichment][wiring] ContratoEnrichmentService CREADO "
            "(pdf_storage=%s).",
            type(document_storage).__name__,
        )
    else:
        logger.warning(
            "[contrato-enrichment][wiring] NO se crea ContratoEnrichmentService. "
            "Motivo: enabled=%s configured=%s.",
            contrato_enabled_flag,
            settings.sigrid_api_configured,
        )

    pipeline = PersistAlbaranPipeline(
        repository=repository,
        document_storage=document_storage,
        normalizer=AlbaranNormalizer(),
        obra_enrichment_service=obra_enrichment_service,
        contrato_enrichment_service=contrato_enrichment_service,
    )

    app = FastAPI(
        title="Albaranes Persistence API",
        version=settings.service_version,
    )

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {
            "ok": True,
            "service": "albaranes-persistence",
            "version": settings.service_version,
            "database": settings.pg_db,
            "sharepoint_mode": settings.sharepoint_mode,
            "sharepoint_drive_id": settings.sharepoint_drive_id,
            "sharepoint_drive_name": settings.sharepoint_drive_name,
            "sharepoint_folder_root": settings.sharepoint_folder_root,
            "sharepoint_folder_url": settings.sharepoint_folder_url,
            "sharepoint_site_path": settings.sharepoint_site_path,
            "obra_enrichment_enabled": settings.obra_enrichment_enabled,
            "obra_enrichment_wired": obra_enrichment_service is not None,
            "contrato_enrichment_enabled": contrato_enabled_flag,
            "contrato_enrichment_wired": contrato_enrichment_service is not None,
            "contrato_pdf_storage_wired": (
                contrato_enrichment_service is not None
                and document_storage is not None
            ),
            "sigrid_api_base_url": settings.sigrid_api_base_url,
            "sigrid_api_database": settings.sigrid_api_database,
        }

    @app.post("/v1/albaranes/persist")
    async def persist(
        file: UploadFile = File(...),
        extraction_json: str = Form(...),
        context_json: str = Form("{}"),
    ) -> Dict[str, Any]:
        file_bytes = await file.read()
        if not file_bytes:
            raise HTTPException(status_code=400, detail="Archivo vacío.")

        try:
            extraction_envelope = json.loads(extraction_json)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"extraction_json inválido: {exc}",
            ) from exc

        try:
            context = json.loads(context_json or "{}")
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"context_json inválido: {exc}",
            ) from exc

        try:
            result = pipeline.run(
                PersistAlbaranRequest(
                    filename=file.filename or "document.bin",
                    mime_type=file.content_type or "application/octet-stream",
                    file_bytes=file_bytes,
                    extraction_envelope=extraction_envelope,
                    context=context if isinstance(context, dict) else {},
                )
            )
            return asdict(result)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Error persistiendo albarán: {exc}",
            ) from exc

    return app