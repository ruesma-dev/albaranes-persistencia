# Worker de sv3 (q-persistencia -> persiste -> q-valoracion)

Convierte sv3 en worker: consume `q-persistencia` con `{document_id}`, recupera
el envelope (de sv2) y el PDF, ejecuta el pipeline REAL de sv3 (persiste en
PostgreSQL, sube el PDF a SharePoint, enriquece con Sigrid) y publica
`q-valoracion` cuando hay contrato unico. El `main.py` HTTP sigue intacto.

## Ficheros (nuevos; no se toca app.py)
- `infrastructure/clients/cola_valuation_trigger.py` — `ColaValuationTrigger`:
  implementa el puerto `ValuationTrigger` publicando en `q-valoracion` (sustituye
  al `HttpValuationTrigger` que llamaba a sv6).
- `interface_adapters/composition.py` — `build_persist_pipeline(settings,
  valuation_trigger=...)`: construye el pipeline real de sv3 con el trigger
  inyectable. Replica el wiring de `build_app` (solo las dependencias del
  pipeline). Si cambias proveedores/Sigrid en `build_app`, reflejalo aqui.
- `interface_adapters/worker/ports.py` — `FuenteEnvelope`, `FuenteDocumento`.
- `interface_adapters/worker/local_stubs.py` — stubs LOCALES (envelope desde el
  JSON de sv2, PDF desde disco).
- `interface_adapters/worker/persistence_worker.py` — el handler.
- `main_worker.py` — entrypoint del worker.

> Validado a nivel de wiring con fakes: `q-persistencia -> pipeline.run(PDF +
> envelope) -> ColaValuationTrigger -> q-valoracion`. El e2e REAL (con tu
> PostgreSQL + SharePoint + Sigrid) lo corres tu con estos pasos.

## Importante: evitar doble disparo
El worker SIEMPRE usa el trigger de cola. Si ademas tienes el servicio HTTP de
sv3 corriendo con `VALUATION_TRIGGER_ENABLED=true`, ese llamaria a sv6 por HTTP
y se duplicaria. Para el modo cola: **`VALUATION_TRIGGER_ENABLED=false`** en el
`.env` (el worker no usa esa rama, pero asi el HTTP tampoco dispara).

## Probarlo encadenado con sv2 (recomendado)
1. **Azurite** (una consola):
   ```powershell
   azurite-queue --silent --location C:\azurite --queuePort 10001 --skipApiVersionCheck
   ```
2. **Worker sv2** (consola 2, en el proyecto de sv2, su venv y .env):
   ```powershell
   # .env de sv2: COLAS_CONNECTION_STRING, WORKER_PDF_PATH='...pdf', WORKER_OUT_DIR='worker_out'
   python main_worker.py
   ```
3. **Worker sv3** (consola 3, en el proyecto de sv3, su venv y .env):
   ```powershell
   # En el .env de sv3 (comillas simples, barras /):
   #   COLAS_CONNECTION_STRING="...Azurite..."
   #   WORKER_PDF_PATH='C:/.../mismo_albaran.pdf'
   #   WORKER_ENVELOPE_DIR='C:/Users/pgris/PycharmProjects/albaranes-api/worker_out'  # el worker_out de sv2
   #   VALUATION_TRIGGER_ENABLED=false
   #   (+ PG_*, GRAPH_KEY, SHAREPOINT_*, SIGRID_API_* habituales)
   pip install -e ..\comun   # si no esta ya
   python main_worker.py
   ```
4. **Encola** (consola 4, en sv2): `python encolar_extraccion.py DOC-PRUEBA-1`

Flujo esperado:
- sv2: extrae -> escribe `worker_out\DOC-PRUEBA-1_phase_1.json` -> publica `q-persistencia`.
- sv3: lee ese envelope + el PDF -> persiste en BBDD + sube a SharePoint -> si
  contrato unico, `[colas] publicado tipo=valoracion ... -> q-valoracion`.

## Verificar
- Fila del albaran en tu PostgreSQL (schema de persistencia).
- PDF subido a SharePoint (URL en el log / en BBDD).
- Log de sv3: `[sv3-worker] ... OK -> persistido (doc=... contratos=N contrato=...)`.
- `q-valoracion` con 1 mensaje si hubo contrato unico (Azure Storage Explorer).

## Nota sobre el envelope
El piloto encadena con el envelope de **fase 1** de sv2 (`WORKER_CON_FASE2=false`).
El pipeline de sv3 valida y persiste igual; cuando actives fase 2 en sv2, sv3
recibira el envelope completo sin cambios.

## Siguiente paso
Adaptadores de produccion: `FuenteEnvelope` -> tabla donde sv2 persiste el
envelope; `FuenteDocumento` -> descarga SharePoint. Luego el **worker del
valorador (sv5+sv6)** sobre `q-valoracion`.
