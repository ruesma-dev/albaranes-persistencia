# application/services/contrato_refetch_service.py
from __future__ import annotations

import logging

from application.services.contrato_enrichment_service import (
    ContratoEnrichmentService,
)
from application.services.obra_code_normalizer import normalize_obra_code
from domain.models.contrato_refetch_models import ContratoRefetchOutcome
from domain.ports.contrato_merge_repository_port import ContratoMergeRepository

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[contrato-refetch]"


class ContratoRefetchService:
    """Wrapper fino sobre :class:`ContratoEnrichmentService` para el caso
    de uso "el usuario pulsó 'Volver a buscar' en el portal".

    El sv3 ya tiene la lógica completa de búsqueda + persistencia
    (``ContratoEnrichmentService``) que se ejecuta automáticamente en
    el pipeline al persistir un albarán. Cuando el usuario edita CIF
    u obra en el portal y pulsa "Volver a buscar", el sv4 invoca al
    sv3 vía HTTP, y el sv3 reutiliza esa misma lógica forzando
    ``force_refetch=True`` (saltarse la caché de contratos, ir
    directos a Sigrid).

    La diferencia con respecto a llamar directamente al enrichment es
    que aquí necesitamos devolver un ``ContratoRefetchOutcome`` con
    semántica rica (``status``, ``message``, ``selected_contrato_codigo``)
    para que el front pinte feedback útil al usuario. Este servicio
    ÚNICAMENTE se encarga de esa traducción int → outcome; no duplica
    la lógica de negocio.

    Antes de invocar al enrichment hace dos cosas:
      1. Lee CIF y obra del merge.
      2. Comprueba que estén presentes y normalizables. Si no, devuelve
         ``skipped_missing_data`` SIN tocar Sigrid (ahorra la llamada).

    Y tras la invocación, lee el estado final del merge para detectar
    si la auto-selección ocurrió (``selected_contrato_codigo``).
    """

    def __init__(
        self,
        *,
        enrichment: ContratoEnrichmentService,
        repository: ContratoMergeRepository,
    ) -> None:
        self._enrichment = enrichment
        self._repository = repository

    def refetch(self, *, document_id: str) -> ContratoRefetchOutcome:
        """Re-busca contratos para un documento ya persistido.

        Idempotente. El usuario puede pulsar "Volver a buscar" tantas
        veces como quiera; cada pulsación llama a Sigrid (force_refetch)
        y hace UPSERT por ``sigrid_ide`` en BBDD.
        """
        logger.info(
            "%s refetch() INVOCADO document_id=%s",
            _LOG_PREFIX, document_id,
        )

        # Lectura previa para feedback al usuario (CIF + obra normalizada).
        try:
            cif, obra_raw = self._repository.get_merge_cif_and_obra(
                document_id=document_id,
            )
        except KeyError:
            # Documento inexistente → propagamos para que el endpoint
            # devuelva 404. NO devolvemos outcome porque no es un caso
            # de "Sigrid no encontró", es un error de input.
            raise

        cif_clean = (cif or "").strip().upper().replace(" ", "") or None
        obra_norm = normalize_obra_code(obra_raw)

        if not cif_clean or not obra_norm:
            logger.info(
                "%s SKIP missing data document_id=%s cif=%r obra=%r",
                _LOG_PREFIX, document_id, cif_clean, obra_norm,
            )
            return ContratoRefetchOutcome(
                status="skipped_missing_data",
                count=0,
                selected_contrato_codigo=None,
                message=(
                    "Faltan CIF u obra (o no son válidos). "
                    "Comprueba ambos campos y vuelve a intentarlo."
                ),
                cif=cif_clean,
                obra_codigo=obra_norm,
            )

        # Delegación al enrichment del sv3 con force_refetch=True para
        # saltarse la caché de contratos y consultar Sigrid en vivo.
        # ``enrich_merge_document`` ya hace UPSERT por sigrid_ide,
        # auto-selección si exactamente 1, y best-effort para PDFs.
        try:
            count = self._enrichment.enrich_merge_document(
                merge_document_id=document_id,
                force_refetch=True,
            )
        except Exception as exc:
            # No queremos propagar excepciones internas al front. La
            # traducimos a un outcome con status sigrid_error para que
            # el usuario sepa que algo falló en Sigrid (y los contratos
            # previos siguen intactos en BBDD).
            logger.exception(
                "%s ERROR en enrichment document_id=%s",
                _LOG_PREFIX, document_id,
            )
            return ContratoRefetchOutcome(
                status="sigrid_error",
                count=0,
                selected_contrato_codigo=None,
                message=(
                    "Error consultando Sigrid. Inténtalo de nuevo en "
                    f"unos minutos. Detalle técnico: {exc.__class__.__name__}"
                ),
                cif=cif_clean,
                obra_codigo=obra_norm,
            )

        # Tras el enrichment, leemos el estado final del merge para
        # detectar si hubo auto-selección.
        selected_codigo: str | None = None
        try:
            selected_codigo = self._repository.get_selected_contrato_codigo(
                document_id=document_id,
            )
        except Exception:
            logger.exception(
                "%s No pudimos leer selected_contrato_codigo tras refetch "
                "document_id=%s. Continuamos con outcome sin selección.",
                _LOG_PREFIX, document_id,
            )

        if count == 0:
            return ContratoRefetchOutcome(
                status="no_results",
                count=0,
                selected_contrato_codigo=None,
                message=(
                    f"No se encontró ningún contrato en el ERP para la "
                    f"combinación CIF {cif_clean} + obra {obra_norm}. "
                    "Revisa que ambos valores sean correctos."
                ),
                cif=cif_clean,
                obra_codigo=obra_norm,
            )

        if count == 1:
            return ContratoRefetchOutcome(
                status="found_single",
                count=1,
                selected_contrato_codigo=selected_codigo,
                message=(
                    f"Encontrado 1 contrato ({selected_codigo or '?'}); "
                    "se ha seleccionado automáticamente."
                ),
                cif=cif_clean,
                obra_codigo=obra_norm,
            )

        return ContratoRefetchOutcome(
            status="found_multiple",
            count=count,
            selected_contrato_codigo=selected_codigo,
            message=(
                f"Se encontraron {count} contratos. Selecciona el "
                "correcto en el portal."
            ),
            cif=cif_clean,
            obra_codigo=obra_norm,
        )
