# interface_adapters/api/app.py
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any, Dict

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from application.pipelines.persist_albaran_pipeline import (
    PersistAlbaranPipeline,
    PersistAlbaranRequest,
)
from application.pipelines.select_contrato_pipeline import (
    SelectContratoPipeline,
    SelectContratoRequest,
)
from application.services.albaran_normalizer import AlbaranNormalizer
from application.services.contrato_enrichment_service import (
    ContratoEnrichmentService,
)
from application.services.obra_enrichment_service import ObraEnrichmentService
from config.settings import Settings
from infrastructure.clients.http_valuation_trigger import HttpValuationTrigger
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


# =============================================================== #
# DTOs internos del API
# =============================================================== #
class SelectedContratoPatch(BaseModel):
    """Body del PATCH /v1/albaranes/{id}/selected-contrato.

    Mantenido para compatibilidad con sv4 actual, aunque con el
    orquestador desplegado, sv4 emite directamente eventos a sv7
    en lugar de llamar a este endpoint.
    """
    codigo_contrato: str | None = None
    trigger_valuation: bool = True
    wait_for_valuation: bool = False


def build_app(settings: Settings) -> FastAPI:
    # ----------------------------------------------------------- #
    # Capa de persistencia.
    # ----------------------------------------------------------- #
    session_factory = SessionFactory(
        database_url=settings.database_url,
        admin_database_url=settings.admin_database_url,
        target_database_name=settings.pg_db,
    )
    repository = SqlAlchemyAlbaranRepository(session_factory)
    repository.initialize()

    # Caché de contratos (LRU + TTL en BBDD para evitar re-llamar
    # a Sigrid en cada persist con la misma combinación obra+CIF).
    contrato_cache = SqlAlchemyContratoCacheRepository(session_factory)

    # ----------------------------------------------------------- #
    # SharePoint storage (también implementa ContratoPdfStorage).
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
    # Sigrid clients (obra + contrato).
    # Si las 3 vars imprescindibles no están presentes, los enrichers
    # se quedan en None y el pipeline las salta (best-effort).
    # ----------------------------------------------------------- #
    obra_enrichment_service: ObraEnrichmentService | None = None
    contrato_enrichment_service: ContratoEnrichmentService | None = None

    if settings.sigrid_configured:
        logger.info(
            "[svc3-wiring] Sigrid configurado base_url=%s database=%s "
            "→ creando clientes Sigrid + enrichers",
            settings.sigrid_api_base_url,
            settings.sigrid_api_database,
        )

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
            database_rep=settings.sigrid_api_database_rep,
            pdf_timeout_s=settings.sigrid_api_pdf_timeout_s,
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
            pdf_storage=document_storage,
            enabled=True,
        )
    else:
        logger.warning(
            "[svc3-wiring] Sigrid NO configurado (faltan SIGRID_API_BASE_URL/"
            "FUNCTION_KEY/DATABASE). Los enrichers de obra y contrato "
            "quedan DESACTIVADOS — los albaranes se persistirán sin "
            "buscar contratos en el ERP."
        )

    # ----------------------------------------------------------- #
    # Valuation trigger (sv3 → sv6).
    # Por defecto DESACTIVADO porque sv7 orquesta. Solo se cablea si
    # VALUATION_TRIGGER_ENABLED=true y hay base_url, como mecanismo
    # de rollback o despliegue sin orquestador.
    # ----------------------------------------------------------- #
    valuation_trigger: HttpValuationTrigger | None = None
    if settings.valuation_trigger_configured:
        logger.warning(
            "[svc3-wiring] VALUATION_TRIGGER_ENABLED=true → sv3 disparará "
            "sv6 directamente. CUIDADO: si sv7 también está activo y "
            "orquestando, podrías tener doble valoración. Recomendado: "
            "VALUATION_TRIGGER_ENABLED=false cuando sv7 esté desplegado."
        )
        valuation_trigger = HttpValuationTrigger(
            base_url=settings.valuation_api_base_url,
            async_timeout_s=settings.valuation_trigger_timeout_s,
            sync_timeout_s=settings.valuation_trigger_sync_timeout_s,
        )
    else:
        logger.info(
            "[svc3-wiring] valuation_trigger DESACTIVADO "
            "(sv7 es quien orquesta la valoración tras el persist)"
        )

    # ----------------------------------------------------------- #
    # Pipelines.
    # ----------------------------------------------------------- #
    persist_pipeline = PersistAlbaranPipeline(
        repository=repository,
        document_storage=document_storage,
        normalizer=AlbaranNormalizer(),
        obra_enrichment_service=obra_enrichment_service,
        contrato_enrichment_service=contrato_enrichment_service,
        valuation_trigger=valuation_trigger,
    )
    select_contrato_pipeline = SelectContratoPipeline(
        repository=repository,
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
            "sigrid_configured": settings.sigrid_configured,
            "obra_enrichment_enabled": (
                obra_enrichment_service is not None
                and settings.obra_enrichment_enabled
            ),
            "contrato_enrichment_enabled": (
                contrato_enrichment_service is not None
            ),
            "valuation_trigger_enabled": settings.valuation_trigger_configured,
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
            result = persist_pipeline.run(
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
            logger.exception("Error persistiendo albarán")
            raise HTTPException(
                status_code=500,
                detail=f"Error persistiendo albarán: {exc}",
            ) from exc

    @app.patch("/v1/albaranes/{document_id}/selected-contrato")
    def patch_selected_contrato(
        document_id: str,
        payload: SelectedContratoPatch,
    ) -> Dict[str, Any]:
        """Cambia el contrato seleccionado del documento.

        NOTA: con sv7 desplegado, sv4 emite eventos directos al
        orquestador en lugar de llamar a este endpoint. El endpoint
        se mantiene por compatibilidad y para casos de pruebas
        manuales / scripts. El flag ``trigger_valuation`` solo tiene
        efecto si el HttpValuationTrigger está cableado (es decir,
        VALUATION_TRIGGER_ENABLED=true).
        """
        try:
            result = select_contrato_pipeline.run(
                SelectContratoRequest(
                    document_id=document_id,
                    codigo_contrato=payload.codigo_contrato,
                    trigger_valuation=payload.trigger_valuation,
                    wait_for_valuation=payload.wait_for_valuation,
                )
            )
            return asdict(result)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception(
                "Error en select-contrato document_id=%s",
                document_id,
            )
            raise HTTPException(
                status_code=500,
                detail=f"Error: {exc}",
            ) from exc

    return app
