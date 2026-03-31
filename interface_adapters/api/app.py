# interface_adapters/api/app.py
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Dict

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from application.pipelines.persist_albaran_pipeline import (
    PersistAlbaranPipeline,
    PersistAlbaranRequest,
)
from application.services.albaran_normalizer import AlbaranNormalizer
from config.settings import Settings
from infrastructure.database.session_factory import SessionFactory
from infrastructure.database.sqlalchemy_albaran_repository import (
    SqlAlchemyAlbaranRepository,
)
from infrastructure.storage.sharepoint_document_storage import (
    SharePointDocumentStorage,
)


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
    pipeline = PersistAlbaranPipeline(
        repository=repository,
        document_storage=document_storage,
        normalizer=AlbaranNormalizer(),
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
