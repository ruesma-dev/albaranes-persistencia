# interface_adapters/api/app.py
"""Wiring completo del servicio 3 (albaranes-persistence-api).

Construye y conecta:
  - Repositorios SQLAlchemy: AlbaranRepository, ContratoCacheRepository.
  - SharePointDocumentStorage (también cumple ContratoPdfStorage por
    duck-typing — un solo storage cubre los dos tipos de subida).
  - Sigrid clients + Enrichment services (obra y contrato).
  - HttpValuationTrigger hacia sv6 (DESACTIVADO por defecto si lo
    orquesta sv7 — controlable por ENV VALUATION_TRIGGER_ENABLED).
  - Phase2PersistenceService (revisión IA fase 2 — UPDATE post-save).
  - Pipeline PersistAlbaranPipeline con TODOS esos colaboradores.

Los servicios opcionales (sigrid, valuation_trigger) se cablean
solo si las credenciales / variables están presentes en .env. Si
faltan, el pipeline arranca igual y los pasos correspondientes se
loguean como SKIP. Así el sv3 nunca se rompe por una credencial
ausente.
"""
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
from application.services.phase2_persistence_service import (
    Phase2PersistenceService,
)
from config.settings import Settings
from infrastructure.clients.http_valuation_trigger import HttpValuationTrigger
from infrastructure.database.phase2_ddl import apply_phase2_ddl
from infrastructure.database.session_factory import SessionFactory
from infrastructure.database.sqlalchemy_albaran_repository import (
    SqlAlchemyAlbaranRepository,
)
from infrastructure.database.sqlalchemy_contrato_cache_repository import (
    SqlAlchemyContratoCacheRepository,
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
    # ----------------------------------------------------------- #
    # Persistencia (BBDD).
    # ----------------------------------------------------------- #
    session_factory = SessionFactory(
        database_url=settings.database_url,
        admin_database_url=settings.admin_database_url,
        target_database_name=settings.pg_db,
    )
    repository = SqlAlchemyAlbaranRepository(session_factory)
    repository.initialize()
    contrato_cache = SqlAlchemyContratoCacheRepository(session_factory)

    # ----------------------------------------------------------- #
    # Fase 2 (revisión IA): ALTER TABLE idempotente + servicio que
    # hace el UPDATE post-save de los metadatos review_phase2_*.
    # ----------------------------------------------------------- #
    apply_phase2_ddl(session_factory)
    phase2_service = Phase2PersistenceService(session_factory)

    # ----------------------------------------------------------- #
    # SharePoint storage. Sirve para:
    #   - documentos de albarán (DocumentStorage)
    #   - PDFs de contrato (ContratoPdfStorage por duck-typing)
    # Una sola instancia que se inyecta en ambos puntos.
    # ----------------------------------------------------------- #
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

    # ----------------------------------------------------------- #
    # Sigrid — enriquecimiento obra + contrato.
    # Solo se cablean si las 3 credenciales están presentes.
    # ----------------------------------------------------------- #
    obra_enrichment_service: ObraEnrichmentService | None = None
    contrato_enrichment_service: ContratoEnrichmentService | None = None

    if settings.sigrid_credentials_present:
        sigrid_obra_client = SigridApiObraClient(
            base_url=settings.sigrid_api_base_url,
            function_key=settings.sigrid_api_function_key,
            database=settings.sigrid_api_database,
            timeout_s=settings.sigrid_api_timeout_s,
        )
        sigrid_contrato_client = SigridApiContratoClient(
            base_url=settings.sigrid_api_base_url,
            function_key=settings.sigrid_api_function_key,
            database=settings.sigrid_api_database,
            timeout_s=settings.sigrid_api_timeout_s,
        )

        obra_enrichment_service = ObraEnrichmentService(
            client=sigrid_obra_client,
            repository=repository,
            enabled=settings.obra_enrichment_enabled,
        )

        contrato_enrichment_service = ContratoEnrichmentService(
            client=sigrid_contrato_client,
            repository=repository,
            cache=contrato_cache,
            pdf_storage=document_storage,  # mismo storage cubre PDFs de contrato
            enabled=True,
        )

        logger.info(
            "[svc3][wiring] Sigrid CABLEADO base_url=%s db=%s "
            "obra_enabled=%s",
            settings.sigrid_api_base_url,
            settings.sigrid_api_database,
            settings.obra_enrichment_enabled,
        )
    else:
        logger.warning(
            "[svc3][wiring] Sigrid NO cableado: faltan SIGRID_API_BASE_URL "
            "/ SIGRID_API_FUNCTION_KEY / SIGRID_API_DATABASE en .env. "
            "El enriquecimiento de obra y contrato quedará desactivado."
        )

    # ----------------------------------------------------------- #
    # Trigger de valoración (sv6).
    #
    # IMPORTANTE: si el orquestador (sv7) está en uso, este trigger
    # DEBE estar deshabilitado (VALUATION_TRIGGER_ENABLED=false en
    # .env). El sv7 es quien llama al sv6. Si dejas este trigger
    # activo, ambos llamarán a sv6 y se duplica el trabajo.
    # ----------------------------------------------------------- #
    valuation_trigger: HttpValuationTrigger | None = None
    if (
        settings.valuation_trigger_enabled
        and (settings.valuation_api_base_url or "").strip()
    ):
        valuation_trigger = HttpValuationTrigger(
            base_url=settings.valuation_api_base_url,
            async_timeout_s=settings.valuation_trigger_timeout_s,
            sync_timeout_s=settings.valuation_trigger_sync_timeout_s,
        )
        logger.info(
            "[svc3][wiring] Valuation trigger CABLEADO base_url=%s",
            settings.valuation_api_base_url,
        )
    else:
        logger.info(
            "[svc3][wiring] Valuation trigger NO cableado "
            "(VALUATION_TRIGGER_ENABLED=%s, base_url=%s). "
            "Esperado si el orquestador sv7 está en uso.",
            settings.valuation_trigger_enabled,
            settings.valuation_api_base_url,
        )

    # ----------------------------------------------------------- #
    # Pipeline final con todos los colaboradores.
    # ----------------------------------------------------------- #
    pipeline = PersistAlbaranPipeline(
        repository=repository,
        document_storage=document_storage,
        normalizer=AlbaranNormalizer(),
        obra_enrichment_service=obra_enrichment_service,
        contrato_enrichment_service=contrato_enrichment_service,
        valuation_trigger=valuation_trigger,
    )

    # ----------------------------------------------------------- #
    # FastAPI app.
    # ----------------------------------------------------------- #
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
            "phase_2_persistence_wired": True,
            "sigrid_wired": settings.sigrid_credentials_present,
            "obra_enrichment_enabled": (
                obra_enrichment_service is not None
                and settings.obra_enrichment_enabled
            ),
            "contrato_enrichment_enabled": contrato_enrichment_service is not None,
            "valuation_trigger_enabled": valuation_trigger is not None,
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

            # Fase 2 (revisión IA): UPDATE post-save best-effort.
            # No rompe el persist principal si falla.
            try:
                phase2_service.persist_metadata(
                    document_id=result.document_id,
                    raw_envelope=extraction_envelope,
                )
            except Exception:
                logger.exception(
                    "[svc3] error persistiendo metadata fase 2 "
                    "doc=%s; el persist principal SÍ se aplicó",
                    result.document_id,
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
