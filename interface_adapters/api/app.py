# interface_adapters/api/app.py
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any, Dict, Optional

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
from infrastructure.sigrid.sigrid_api_contrato_client import (
    SigridApiContratoClient,
)
from infrastructure.sigrid.sigrid_api_obra_client import SigridApiObraClient
from infrastructure.storage.sharepoint_document_storage import (
    SharePointDocumentStorage,
)

logger = logging.getLogger(__name__)


class SelectContratoBody(BaseModel):
    codigo_contrato: Optional[str] = None
    trigger_valuation: bool = True
    wait_for_valuation: bool = False


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
            "Motivo: enabled=%s configured=%s.",
            settings.obra_enrichment_enabled,
            settings.sigrid_api_configured,
        )

    # ------------------------------------------------------------------ #
    # Construcción del servicio de enriquecimiento de CONTRATOS.
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

    # ------------------------------------------------------------------ #
    # Trigger automático de valoración (servicio 6).
    # Se cablea solo si hay URL configurada y el flag está activo. En
    # cualquier otro caso queda a None y el pipeline lo salta con un log
    # informativo — el front sigue pudiendo disparar manualmente.
    # ------------------------------------------------------------------ #
    valuation_trigger: HttpValuationTrigger | None = None
    if settings.valuation_trigger_configured:
        valuation_trigger = HttpValuationTrigger(
            base_url=settings.valuation_api_base_url,
            async_timeout_s=settings.valuation_trigger_timeout_s,
            sync_timeout_s=settings.valuation_trigger_sync_timeout_s,
        )
        logger.info(
            "[valuation-trigger][wiring] HttpValuationTrigger CREADO "
            "base_url=%s async_timeout=%s sync_timeout=%s",
            settings.valuation_api_base_url,
            settings.valuation_trigger_timeout_s,
            settings.valuation_trigger_sync_timeout_s,
        )
    else:
        logger.warning(
            "[valuation-trigger][wiring] NO se crea trigger. enabled=%s "
            "base_url=%r. El svc 3 terminará /persist sin disparar "
            "valoración. El front tendrá que pulsar 'Valorar' manualmente.",
            settings.valuation_trigger_enabled,
            settings.valuation_api_base_url,
        )

    pipeline = PersistAlbaranPipeline(
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
            "valuation_trigger_enabled": settings.valuation_trigger_enabled,
            "valuation_trigger_wired": valuation_trigger is not None,
            "valuation_api_base_url": settings.valuation_api_base_url,
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

    @app.patch("/v1/albaranes/{document_id}/selected-contrato")
    def patch_selected_contrato(
        document_id: str,
        body: SelectContratoBody,
    ) -> Dict[str, Any]:
        """Actualiza el contrato seleccionado del albarán y (opcional)
        dispara la valoración.

        Body:
          - codigo_contrato: string del contrato, o null para deseleccionar.
          - trigger_valuation: si True (default), dispara la valoración.
          - wait_for_valuation:
              False (default) → fire-and-forget contra /v1/valuation/run-async.
                El PATCH responde inmediato; el front hace polling contra
                GET /v1/valuation/{document_id} del servicio 6.
              True → bloqueante contra /v1/valuation/{doc}/re-run.
                El PATCH no responde hasta que la valoración ha terminado;
                el front recibe el resumen en la misma llamada.

        Errores:
          - 404 si el documento no existe en albaran_documents_merge.
          - 400 si el contrato solicitado no existe para ese documento.
          - 500 si falla la BBDD.

        El disparo de la valoración es best-effort: si el servicio 6 está
        caído, el UPDATE se hace igualmente y la respuesta trae
        ``valuation_triggered=false`` con el error concreto. El front puede
        reintentar manualmente.
        """
        try:
            result = select_contrato_pipeline.run(
                SelectContratoRequest(
                    document_id=document_id,
                    codigo_contrato=body.codigo_contrato,
                    trigger_valuation=body.trigger_valuation,
                    wait_for_valuation=body.wait_for_valuation,
                )
            )
            return asdict(result)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception(
                "Error en PATCH selected-contrato document_id=%s codigo=%s",
                document_id, body.codigo_contrato,
            )
            raise HTTPException(
                status_code=500,
                detail=f"Error actualizando contrato seleccionado: {exc}",
            ) from exc

    return app
