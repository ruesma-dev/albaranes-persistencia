# application/pipelines/persist_albaran_pipeline.py
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Dict

from application.services.albaran_normalizer import AlbaranNormalizer
from application.services.contrato_enrichment_service import (
    ContratoEnrichmentService,
)
from application.services.header_resolver_service import HeaderResolverService
from application.services.obra_enrichment_service import ObraEnrichmentService
from domain.models.extraction_models import (
    ExtractionEnvelope,
    ProviderExtractionEnvelope,
)
from domain.ports.albaran_repository import AlbaranRepository
from domain.ports.document_storage import DocumentStorage
from domain.ports.valuation_trigger_port import ValuationTrigger

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PersistAlbaranRequest:
    filename: str
    mime_type: str
    file_bytes: bytes
    extraction_envelope: Dict[str, Any]
    context: Dict[str, Any]
    # Re-fetch manual de contratos desde el portal (sv4): si True, el
    # enrichment de contratos bypasa la caché y re-consulta Sigrid.
    force_refetch: bool = False


@dataclass(frozen=True)
class PersistAlbaranResult:
    """Resultado del persist + enrichment.

    Campos NUEVOS añadidos para que el orquestador (sv7) pueda decidir
    el siguiente paso (valoración o esperar al revisor) sin tener que
    consultar BBDD él mismo:

      - ``contratos_count``: nº de contratos persistidos en
        albaran_contratos_merge para este documento (después del
        enrichment).
      - ``selected_contrato_codigo``: código auto-seleccionado por el
        ContratoEnrichmentService cuando hay exactamente 1 contrato.
        ``None`` si hay 0 ó >1 contratos (o si el enrichment falló).
    """
    ok: bool
    document_id: str
    sharepoint_url: str | None
    duplicate: bool
    stored_lines: int
    contratos_count: int = 0
    selected_contrato_codigo: str | None = None


class PersistAlbaranPipeline:
    def __init__(
        self,
        *,
        repository: AlbaranRepository,
        document_storage: DocumentStorage,
        normalizer: AlbaranNormalizer,
        header_resolver_service: HeaderResolverService | None = None,
        obra_enrichment_service: ObraEnrichmentService | None = None,
        contrato_enrichment_service: ContratoEnrichmentService | None = None,
        valuation_trigger: ValuationTrigger | None = None,
    ) -> None:
        self._repository = repository
        self._document_storage = document_storage
        self._normalizer = normalizer
        self._header_resolver_service = header_resolver_service
        self._obra_enrichment_service = obra_enrichment_service
        self._contrato_enrichment_service = contrato_enrichment_service
        self._valuation_trigger = valuation_trigger
        logger.info(
            "[pipeline] PersistAlbaranPipeline construido; "
            "obra_enrichment=%s contrato_enrichment=%s "
            "valuation_trigger=%s",
            "PRESENTE" if obra_enrichment_service is not None else "None",
            "PRESENTE" if contrato_enrichment_service is not None else "None",
            "PRESENTE" if valuation_trigger is not None else "None",
        )

    def run(self, request: PersistAlbaranRequest) -> PersistAlbaranResult:
        if not request.file_bytes:
            raise ValueError("Archivo vacío.")

        envelope = ExtractionEnvelope.model_validate(request.extraction_envelope)
        sha256 = hashlib.sha256(request.file_bytes).hexdigest()
        self._validate_sha256(envelope=envelope, sha256=sha256)

        existing = self._repository.get_by_sha256(sha256)
        if existing is not None:
            logger.info(
                "Documento duplicado detectado. sha256=%s document_id=%s",
                sha256,
                existing.document_id,
            )
            # Re-enriquecer para idempotencia: si la BBDD on-prem cambió,
            # el merge se actualiza. No duplica contratos (replace).
            self._resolve_header_deterministic_safely(
                merge_document_id=existing.document_id,
            )
            self._enrich_obra_safely(merge_document_id=existing.document_id)
            contratos_count = self._enrich_contratos_safely(
                merge_document_id=existing.document_id,
                force_refetch=request.force_refetch,
            )
            self._trigger_valuation_safely(
                merge_document_id=existing.document_id,
                force=request.force_refetch,
            )
            selected_codigo = self._read_selected_contrato_safely(
                merge_document_id=existing.document_id,
            )
            return PersistAlbaranResult(
                ok=True,
                document_id=existing.document_id,
                sharepoint_url=existing.sharepoint_url,
                duplicate=True,
                stored_lines=existing.stored_lines,
                contratos_count=contratos_count,
                selected_contrato_codigo=selected_codigo,
            )

        envelope = self._normalize(envelope)
        openai_debug = (
            envelope.debug if isinstance(envelope.debug, dict) else {}
        )
        gemini_debug = (
            envelope.gemini.debug
            if envelope.gemini is not None and isinstance(envelope.gemini.debug, dict)
            else {}
        )
        claude_debug = (
            envelope.claude.debug
            if envelope.claude is not None and isinstance(envelope.claude.debug, dict)
            else {}
        )

        stored_file = self._document_storage.upload(
            filename=request.filename,
            mime_type=request.mime_type,
            file_bytes=request.file_bytes,
            source_sha256=sha256,
            ia_input_payload=self._coerce_dict(
                openai_debug.get("openai_request")
            ),
            ia_output_payload=self._coerce_dict(
                openai_debug.get("openai_response")
            ),
            gem_input_payload=self._coerce_dict(
                gemini_debug.get("gemini_request")
            ),
            gem_output_payload=self._coerce_dict(
                gemini_debug.get("gemini_response")
            ),
            cla_input_payload=self._coerce_dict(
                claude_debug.get("claude_request")
            ),
            cla_output_payload=self._coerce_dict(
                claude_debug.get("claude_response")
            ),
        )
        saved = self._repository.save(
            envelope=envelope,
            context=request.context,
            stored_file=stored_file,
        )

        # Pipeline de enriquecimiento. ORDEN IMPORTA:
        #  1) Obra: sobrescribe nombre_obra y obra_direccion en el merge.
        #  2) Contratos: busca por (cif, obra) e inserta en
        #     albaran_contratos_merge. Si hay 1 solo contrato lo auto-
        #     selecciona en selected_contrato_codigo.
        #  3) Valoración: si hay selected_contrato_codigo con líneas,
        #     dispara /run-async del servicio 6 (fire-and-forget).
        # Todos los pasos son best-effort y no rompen la persistencia.
        # 0) Resolucion determinista de cabecera: si la IA no fijo
        #    obra_codigo / proveedor_cif, los deduce por texto contra
        #    Sigrid y los persiste marcados 'deterministic'. Va ANTES del
        #    enriquecimiento de obra (que necesita el codigo) para que la
        #    valoracion inicial arranque ya con la propuesta.
        self._resolve_header_deterministic_safely(
            merge_document_id=saved.document_id,
        )
        self._enrich_obra_safely(merge_document_id=saved.document_id)
        contratos_count = self._enrich_contratos_safely(
            merge_document_id=saved.document_id,
            force_refetch=request.force_refetch,
        )
        self._trigger_valuation_safely(
            merge_document_id=saved.document_id,
            force=request.force_refetch,
        )
        selected_codigo = self._read_selected_contrato_safely(
            merge_document_id=saved.document_id,
        )

        logger.info(
            "[pipeline] FIN persist document_id=%s duplicate=False "
            "contratos_count=%d selected_contrato_codigo=%s",
            saved.document_id,
            contratos_count,
            selected_codigo,
        )

        return PersistAlbaranResult(
            ok=True,
            document_id=saved.document_id,
            sharepoint_url=saved.sharepoint_url,
            duplicate=False,
            stored_lines=saved.stored_lines,
            contratos_count=contratos_count,
            selected_contrato_codigo=selected_codigo,
        )

    def _resolve_header_deterministic_safely(
        self, *, merge_document_id: str,
    ) -> None:
        """Resolucion determinista de obra_codigo / proveedor_cif por
        texto. Best-effort: nunca rompe la persistencia."""
        if self._header_resolver_service is None:
            logger.info(
                "[header-resolver][pipeline] SKIP: servicio no cableado."
            )
            return
        try:
            self._header_resolver_service.resolve_merge_document(
                merge_document_id=merge_document_id,
            )
        except Exception:
            logger.exception(
                "[header-resolver][pipeline] step falló; se continúa. "
                "document_id=%s",
                merge_document_id,
            )

    def reenrich_by_merge_id(
        self,
        *,
        merge_document_id: str,
        force_refetch: bool = False,
    ) -> bool:
        """Re-enriquece un merge document EXISTENTE por su MERGE id.

        Camino para los mensajes de sv4 (seleccion de contrato / re-fetch):
        el front solo conoce el merge id, no el document_id de los blobs
        (input/, envelopes/), asi que aqui NO se re-persiste desde blobs:
        se ejecutan los mismos pasos que la rama de duplicado (resolver
        cabecera, enriquecer obra y contratos con force, disparar la
        valoracion). ``force_refetch`` viaja hasta la valoracion
        (MensajeValoracion.force) para que sv6 re-ejecute aunque
        exista una valoracion previa. Devuelve False si el merge
        no existe.
        """
        try:
            cif, obra_raw = self._repository.get_merge_cif_and_obra(
                document_id=merge_document_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "[pipeline][reenrich] error comprobando merge %s",
                merge_document_id,
            )
            return False
        if cif is None and obra_raw is None:
            logger.warning(
                "[pipeline][reenrich] merge %s no existe; nada que hacer.",
                merge_document_id,
            )
            return False

        logger.info(
            "[pipeline][reenrich] merge=%s force_refetch=%s",
            merge_document_id,
            force_refetch,
        )
        self._resolve_header_deterministic_safely(
            merge_document_id=merge_document_id,
        )
        self._enrich_obra_safely(merge_document_id=merge_document_id)
        self._enrich_contratos_safely(
            merge_document_id=merge_document_id,
            force_refetch=force_refetch,
        )
        self._trigger_valuation_safely(
            merge_document_id=merge_document_id,
            force=force_refetch,
        )
        return True

    def _enrich_obra_safely(self, *, merge_document_id: str) -> None:
        logger.info(
            "[obra-enrichment][pipeline] pre-step: service_present=%s "
            "merge_document_id=%s",
            self._obra_enrichment_service is not None,
            merge_document_id,
        )
        if self._obra_enrichment_service is None:
            logger.warning(
                "[obra-enrichment][pipeline] SKIP: no hay servicio wire-ado."
            )
            return
        try:
            self._obra_enrichment_service.enrich_merge_document(
                merge_document_id=merge_document_id,
            )
        except Exception:
            logger.exception(
                "[obra-enrichment][pipeline] step falló; se continúa. "
                "document_id=%s",
                merge_document_id,
            )

    def _enrich_contratos_safely(
        self, *, merge_document_id: str, force_refetch: bool = False,
    ) -> int:
        """Devuelve el nº de contratos persistidos (0 si falló o no hay servicio).

        ``force_refetch`` (re-fetch manual desde el portal sv4): bypasa la
        caché de contratos del enrichment y re-consulta Sigrid.
        """
        logger.info(
            "[contrato-enrichment][pipeline] pre-step: service_present=%s "
            "merge_document_id=%s force_refetch=%s",
            self._contrato_enrichment_service is not None,
            merge_document_id,
            force_refetch,
        )
        if self._contrato_enrichment_service is None:
            logger.warning(
                "[contrato-enrichment][pipeline] SKIP: no hay servicio wire-ado."
            )
            return 0
        try:
            count = self._contrato_enrichment_service.enrich_merge_document(
                merge_document_id=merge_document_id,
                force_refetch=force_refetch,
            )
            return int(count) if count is not None else 0
        except Exception:
            logger.exception(
                "[contrato-enrichment][pipeline] step falló; se continúa. "
                "document_id=%s",
                merge_document_id,
            )
            return 0

    def _read_selected_contrato_safely(
        self,
        *,
        merge_document_id: str,
    ) -> str | None:
        """Lee selected_contrato_codigo del merge tras el enrichment.

        ``ContratoEnrichmentService`` auto-selecciona el código en BBDD
        cuando hay exactamente 1 contrato. Aquí lo leemos para incluirlo
        en la respuesta del API y que el orquestador decida.
        Best-effort: si falla, devolvemos None (sv7 entonces irá a
        awaiting_contract_selection).
        """
        try:
            return self._repository.get_selected_contrato_codigo(
                document_id=merge_document_id,
            )
        except KeyError:
            return None
        except Exception:
            logger.exception(
                "[pipeline] no se pudo leer selected_contrato_codigo "
                "tras el enrichment; se devuelve None. document_id=%s",
                merge_document_id,
            )
            return None

    def _trigger_valuation_safely(
        self,
        *,
        merge_document_id: str,
        force: bool = False,
    ) -> None:
        """Dispara el servicio 6 SOLO si hay contrato seleccionado con líneas.

        ``force`` (jul 2026) — FIX re-valorado: se propaga tal cual a
        ``trigger_async`` → ``MensajeValoracion.force`` → sv6. Antes
        estaba HARDCODEADO a ``False`` y el re-valorado desde sv4
        (cambio de contrato / 'Valorar ahora' sobre un documento YA
        valorado) moría en sv6 en el cortocircuito de idempotencia
        ('existe valoración previa; se devuelve sin re-ejecutar')
        sin llegar nunca a sv5 ni regenerar la valoración.

        SIEMPRE best-effort: cualquier fallo se loguea y no rompe el pipeline
        de persistencia. El front puede pulsar 'Valorar' manualmente como
        fallback si aquí falla algo.
        """
        logger.info(
            "[valuation-trigger][pipeline] pre-step: trigger_present=%s "
            "merge_document_id=%s force=%s",
            self._valuation_trigger is not None,
            merge_document_id,
            force,
        )
        if self._valuation_trigger is None:
            logger.info(
                "[valuation-trigger][pipeline] SKIP: trigger no cableado."
            )
            return

        try:
            has_lines, codigo = self._repository.has_selected_contrato_with_lines(
                document_id=merge_document_id,
            )
        except Exception:
            logger.exception(
                "[valuation-trigger][pipeline] Error comprobando líneas "
                "contrato; se omite trigger. document_id=%s",
                merge_document_id,
            )
            return

        if not has_lines:
            logger.info(
                "[valuation-trigger][pipeline] SKIP: no hay contrato "
                "seleccionado con líneas. document_id=%s codigo=%s",
                merge_document_id, codigo,
            )
            return

        try:
            accepted = self._valuation_trigger.trigger_async(
                document_id=merge_document_id,
                codigo_contrato=codigo,
                force=force,
            )
            logger.info(
                "[valuation-trigger][pipeline] trigger resultado=%s "
                "document_id=%s codigo=%s force=%s",
                "ACCEPTED" if accepted else "REJECTED",
                merge_document_id, codigo, force,
            )
        except Exception:
            logger.exception(
                "[valuation-trigger][pipeline] fallo inesperado al "
                "disparar trigger. document_id=%s",
                merge_document_id,
            )

    def _validate_sha256(
        self,
        *,
        envelope: ExtractionEnvelope,
        sha256: str,
    ) -> None:
        providers: list[tuple[str, ProviderExtractionEnvelope]] = [
            ("openai", envelope)
        ]
        if envelope.gemini is not None:
            providers.append(("gemini", envelope.gemini))
        if envelope.claude is not None:
            providers.append(("claude", envelope.claude))
        if envelope.google_document_ai is not None:
            providers.append(
                ("google_document_ai", envelope.google_document_ai)
            )
        if envelope.azure_document_intelligence is not None:
            providers.append(
                (
                    "azure_document_intelligence",
                    envelope.azure_document_intelligence,
                )
            )

        for provider_name, provider_envelope in providers:
            expected_sha = provider_envelope.meta.source_sha256.strip().lower()
            if expected_sha and expected_sha != sha256:
                raise ValueError(
                    "El sha256 del adjunto no coincide con el bloque "
                    f"{provider_name}."
                )

    def _normalize(self, envelope: ExtractionEnvelope) -> ExtractionEnvelope:
        normalized = envelope.model_copy(
            update={
                "data": self._normalizer.normalize_provider_document(
                    document=envelope.data,
                    provider_origin="openai",
                )
            }
        )

        provider_updates: dict[str, ProviderExtractionEnvelope] = {}
        optional_providers = {
            "gemini": normalized.gemini,
            "claude": normalized.claude,
            "google_document_ai": normalized.google_document_ai,
            "azure_document_intelligence": normalized.azure_document_intelligence,
        }
        for provider_name, provider_envelope in optional_providers.items():
            if provider_envelope is None:
                continue
            provider_updates[provider_name] = provider_envelope.model_copy(
                update={
                    "data": self._normalizer.normalize_provider_document(
                        document=provider_envelope.data,
                        provider_origin=provider_name,
                    )
                }
            )

        if provider_updates:
            normalized = normalized.model_copy(update=provider_updates)
        return normalized

    @staticmethod
    def _coerce_dict(value: Any) -> Dict[str, Any]:
        return value if isinstance(value, dict) else {}