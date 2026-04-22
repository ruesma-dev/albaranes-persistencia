# infrastructure/clients/http_valuation_trigger.py
from __future__ import annotations

import logging

import httpx

from domain.ports.valuation_trigger_port import (
    ValuationSyncResult,
    ValuationTrigger,
)

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[valuation-trigger]"


class HttpValuationTrigger(ValuationTrigger):
    """Adaptador HTTP contra el servicio 6.

    Dos timeouts distintos:
      - ``async_timeout_s``: corto (3-5s). Solo esperamos el 202 del
        endpoint /run-async.
      - ``sync_timeout_s``: largo (60-300s). El /re-run síncrono
        incluye la llamada a la IA del servicio 5 + persistencia.
    """

    def __init__(
        self,
        *,
        base_url: str,
        async_timeout_s: float = 3.0,
        sync_timeout_s: float = 300.0,
    ) -> None:
        if not base_url:
            raise ValueError("HttpValuationTrigger requiere base_url")
        self._base_url = base_url.rstrip("/")
        self._async_timeout_s = float(async_timeout_s)
        self._sync_timeout_s = float(sync_timeout_s)
        logger.info(
            "%s HttpValuationTrigger INSTANCIADO base_url=%s "
            "async_timeout=%s sync_timeout=%s",
            _LOG_PREFIX,
            self._base_url,
            self._async_timeout_s,
            self._sync_timeout_s,
        )

    # ------------------------------------------------------------------ #
    # Async (fire-and-forget)
    # ------------------------------------------------------------------ #
    def trigger_async(
        self,
        *,
        document_id: str,
        codigo_contrato: str | None,
        force: bool = False,
    ) -> bool:
        url = f"{self._base_url}/v1/valuation/run-async"
        payload: dict = {
            "document_id": document_id,
            "force": force,
            "line_already_valued": False,
        }
        if codigo_contrato:
            payload["codigo_contrato"] = codigo_contrato

        logger.info(
            "%s POST %s (async) document_id=%s contrato=%s force=%s",
            _LOG_PREFIX, url, document_id, codigo_contrato, force,
        )

        try:
            with httpx.Client(timeout=self._async_timeout_s) as client:
                response = client.post(url, json=payload)
        except Exception:
            logger.exception(
                "%s FALLO transporte async. document_id=%s",
                _LOG_PREFIX, document_id,
            )
            return False

        if response.status_code == 202:
            logger.info(
                "%s Aceptado (202). document_id=%s",
                _LOG_PREFIX, document_id,
            )
            return True

        logger.warning(
            "%s Respuesta async inesperada status=%s body=%s. document_id=%s",
            _LOG_PREFIX,
            response.status_code,
            (response.text or "")[:300],
            document_id,
        )
        return False

    # ------------------------------------------------------------------ #
    # Sync (bloqueante, espera al resultado)
    # ------------------------------------------------------------------ #
    def trigger_sync(
        self,
        *,
        document_id: str,
        codigo_contrato: str | None,
    ) -> ValuationSyncResult:
        url = (
            f"{self._base_url}/v1/valuation/{document_id}/re-run"
        )
        params: dict = {}
        if codigo_contrato:
            params["codigo_contrato"] = codigo_contrato

        logger.info(
            "%s POST %s (sync) document_id=%s contrato=%s",
            _LOG_PREFIX, url, document_id, codigo_contrato,
        )

        try:
            with httpx.Client(timeout=self._sync_timeout_s) as client:
                response = client.post(url, params=params)
        except Exception as exc:
            logger.exception(
                "%s FALLO transporte sync. document_id=%s",
                _LOG_PREFIX, document_id,
            )
            return ValuationSyncResult(
                accepted=False,
                http_status=0,
                error=f"transport_error: {exc!r}",
            )

        if response.status_code >= 400:
            logger.warning(
                "%s sync status=%s body=%s. document_id=%s",
                _LOG_PREFIX,
                response.status_code,
                (response.text or "")[:300],
                document_id,
            )
            return ValuationSyncResult(
                accepted=False,
                http_status=response.status_code,
                error=(response.text or "")[:500] or None,
            )

        try:
            data = response.json()
        except Exception as exc:
            return ValuationSyncResult(
                accepted=False,
                http_status=response.status_code,
                error=f"invalid_json: {exc!r}",
            )

        return ValuationSyncResult(
            accepted=True,
            http_status=response.status_code,
            valuation_id=data.get("valuation_id"),
            status=data.get("status"),
            total_valorado=data.get("total_valorado"),
            total_lines=data.get("total_lines"),
            review_required=data.get("review_required"),
        )
