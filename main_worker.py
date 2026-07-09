# main_worker.py
"""Entrypoint del WORKER de sv3 (consumidor de q-persistencia).

Consume q-persistencia con ``{document_id}``, persiste con el pipeline real de
sv3 (sube el PDF a SharePoint, enriquece Sigrid) y, via el trigger de cola,
publica q-valoracion cuando hay contrato único.

Hand-off por Blob (adaptadores de producción, ya NO disco):
  - Lee el envelope de ``envelopes/{document_id}_phase_1.json`` (lo dejó sv2).
  - Lee el PDF de ``input/{document_id}.pdf`` (lo dejó sv1).

Variables de entorno:
  - COLAS_CONNECTION_STRING (local/Azurite) o COLAS_ACCOUNT_URL (nube).
  - BLOBS_CONNECTION_STRING (o, en su defecto, COLAS_CONNECTION_STRING) para
    el Blob; en la nube, BLOBS_ACCOUNT_URL. En local con Azurite basta
    COLAS_CONNECTION_STRING (el BlobEndpoint se deriva solo).
  + todas las de sv3 (PG_*, GRAPH_KEY, SHAREPOINT_*, SIGRID_API_*, y
    VALUATION_TRIGGER_ENABLED=false para no duplicar con el trigger de cola).
"""
from __future__ import annotations

import logging
from pathlib import Path

from config.logging_config import configure_logging
from config.settings import Settings
from infrastructure.clients.cola_valuation_trigger import ColaValuationTrigger
from interface_adapters.composition import build_persist_pipeline
from interface_adapters.worker.blob_adapters import (
    FuenteDocumentoBlob,
    FuenteEnvelopeBlob,
)
from interface_adapters.worker.persistence_worker import (
    construir_handler_persistencia,
)
from ruesma_comun.blobs import construir_almacen_desde_entorno
from ruesma_comun.colas import COLA_PERSISTENCIA
from ruesma_comun.colas.arranque import construir_publicador, ejecutar_worker


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    settings = Settings()
    configure_logging(Path(settings.log_dir), settings.log_level)
    logging.getLogger("azure").setLevel(logging.WARNING)

    almacen = construir_almacen_desde_entorno()

    publicador = construir_publicador(emitido_por="ca-sv3-persistencia")
    valuation_trigger = ColaValuationTrigger(publicador)
    pipeline = build_persist_pipeline(settings, valuation_trigger=valuation_trigger)
    handler = construir_handler_persistencia(
        pipeline=pipeline,
        fuente_documento=FuenteDocumentoBlob(almacen),
        fuente_envelope=FuenteEnvelopeBlob(almacen),
    )
    return ejecutar_worker(
        nombre_cola=COLA_PERSISTENCIA,
        tipo_mensaje="persistencia",
        handler=handler,
        emitido_por="ca-sv3-persistencia",
    )


if __name__ == "__main__":
    raise SystemExit(main())
