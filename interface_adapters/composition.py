# interface_adapters/composition.py
"""Composition root del pipeline de persistencia de sv3.

Construye el ``PersistAlbaranPipeline`` con sus dependencias (BBDD, SharePoint,
Sigrid) y un ``ValuationTrigger`` INYECTABLE. Asi el mismo pipeline sirve para:

  - la API HTTP (``build_app``), con ``HttpValuationTrigger`` (o ninguno).
  - el worker de cola (``main_worker.py``), con ``ColaValuationTrigger`` que
    publica en ``q-valoracion``.

Solo se construyen las dependencias que el PIPELINE necesita (repository,
document_storage, header_resolver, obra_enrichment, contrato_enrichment). Los
servicios que solo usan endpoints (refetch, grounding) NO se cablean aqui.

El wiring replica el de ``build_app``; si cambias proveedores/credenciales alli,
reflejalo aqui (o, mejor, haz que ``build_app`` llame a esta factoria).
"""
from __future__ import annotations

import logging

from application.pipelines.persist_albaran_pipeline import PersistAlbaranPipeline
from application.services.albaran_normalizer import AlbaranNormalizer
from application.services.contrato_enrichment_service import (
    ContratoEnrichmentService,
)
from application.services.header_resolver_service import HeaderResolverService
from application.services.obra_enrichment_service import ObraEnrichmentService
from config.settings import Settings
from domain.ports.valuation_trigger_port import ValuationTrigger
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


def build_persist_pipeline(
    settings: Settings,
    *,
    valuation_trigger: ValuationTrigger | None,
) -> PersistAlbaranPipeline:
    """Construye el pipeline de persistencia con el trigger indicado."""
    session_factory = SessionFactory(
        database_url=settings.database_url,
        admin_database_url=settings.admin_database_url,
        target_database_name=settings.pg_db,
    )
    repository = SqlAlchemyAlbaranRepository(session_factory)
    repository.initialize()
    contrato_cache = SqlAlchemyContratoCacheRepository(session_factory)

    # Schema de fase 2 (ALTER idempotente) — igual que en build_app.
    apply_phase2_ddl(session_factory)

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

    header_resolver_service: HeaderResolverService | None = None
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
        header_resolver_service = HeaderResolverService(
            obra_client=sigrid_obra_client,
            proveedor_client=sigrid_contrato_client,
            repository=repository,
            min_score=settings.header_resolver_min_score,
            enabled=settings.header_resolver_enabled,
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
        logger.info("[svc3][worker-wiring] Sigrid CABLEADO")
    else:
        logger.info("[svc3][worker-wiring] Sigrid NO cableado (sin credenciales)")

    return PersistAlbaranPipeline(
        repository=repository,
        document_storage=document_storage,
        normalizer=AlbaranNormalizer(),
        header_resolver_service=header_resolver_service,
        obra_enrichment_service=obra_enrichment_service,
        contrato_enrichment_service=contrato_enrichment_service,
        valuation_trigger=valuation_trigger,
    )
