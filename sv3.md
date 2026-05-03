# albaranes-persistence-api (Servicio 3 / sv3)

> **Núcleo de persistencia, merge multi-proveedor y enriquecimiento** del ecosistema
> Construcciones Ruesma. Recibe el envelope multi-proveedor que produce el sv2, lo
> normaliza, **fusiona** con scoring de confianza por campo, lo guarda en PostgreSQL,
> sube los originales y artefactos JSON a SharePoint, **enriquece** los datos de
> obra y contratos contra la BBDD on-prem (vía `sigrid-api`), y **dispara** la
> valoración del sv6.

---

## 1. ¿Qué hace exactamente?

`albaranes-persistence-api` es la pieza central que convierte la salida bruta del
extractor (sv2) en un dato canónico, trazable y enriquecido. Su pipeline principal:

1. Recibe el documento original + envelope multi-proveedor + contexto de email (sv1).
2. Valida el envelope con un modelo Pydantic estricto (`extra='forbid'`).
3. Comprueba **idempotencia por SHA-256**: si el fichero ya existe, re-ejecuta solo
   el enriquecimiento y devuelve `duplicate=true`.
4. **Normaliza** cada bloque de proveedor (fechas a ISO, CIF compactado, números con
   coma/punto, código_imputación con notación `XX.YY.ZZ`, identificadores en MAYÚSCULAS).
5. Sube a **SharePoint** el fichero original + 6 artefactos JSON (request/response
   de OpenAI, Gemini y Claude) y obtiene URLs persistentes.
6. **Persiste en PostgreSQL** dos vistas del documento:
   - `albaran_documents` / `albaran_lines`: una fila por proveedor (auditoría cruda).
   - `albaran_documents_merge` / `albaran_lines_merge`: la versión mergeada y puntuada.
7. Calcula la **confianza por campo** (`AlbaranConfidenceService`, 1.400 LOC) usando
   estados como `triple_match`, `match_normalized`, `majority_match`, `conflict`,
   pondera campos por importancia y emite `review_required` con razones.
8. **Enriquece la obra**: con `obra_codigo` normalizado a 4 dígitos, llama a
   `sigrid-api` (`POST /api/sql/read`) → SQL parametrizada sobre `obr/con/auxmun/auxpro`
   → sobrescribe `obra_nombre` y `obra_direccion` en el merge.
9. **Enriquece los contratos**: busca contratos `(cif, obra)` en Sigrid → lista de
   contratos con líneas → descarga el PDF de cada contrato (`gra_rep_ide`) vía
   `sigrid-api` → lo sube a SharePoint → persiste todo en `albaran_contratos_merge`
   y `albaran_contrato_lines_merge`. Si hay **un único** contrato, lo auto-selecciona.
10. **Dispara la valoración (sv6)**: si hay contrato seleccionado con líneas,
    `POST /v1/valuation/run-async` fire-and-forget al sv6 (timeout corto, 3 s).

Adicionalmente expone un `PATCH /v1/albaranes/{document_id}/selected-contrato`
para que el front pueda **cambiar el contrato seleccionado** y opcionalmente
disparar la valoración en modo síncrono o asíncrono.

> Es un servicio HTTP **stateful por BBDD**: PostgreSQL es la fuente de verdad.
> SharePoint es solo storage de binarios y JSONs auxiliares.

---

## 2. Lugar dentro del ecosistema (6 microservicios)

```
   ┌─────────────────────────┐         ┌──────────────────────────────┐
   │ sv1 · email-albaranes-  │         │ sv2 · albaranes-extractor    │
   │      ingestor           │         │      api                     │
   └────────────┬────────────┘         └──────────────────────────────┘
                │                              ▲
                │ POST /v1/albaranes/persist   │ envelope JSON multi-proveedor
                │ (multipart: file +           │
                │  extraction_json +           │
                │  context_json)               │
                ▼                              │
   ┌─────────────────────────────────────────────────────────────────┐
   │  sv3 · albaranes-persistence-api          ← ESTE SERVICIO       │
   │  (FastAPI + uvicorn + SQLAlchemy + psycopg)                     │
   │                                                                 │
   │   ┌────────────────────────────────────────────────────────┐    │
   │   │ Pipeline persist:                                      │    │
   │   │  validate → dedupe(sha256) → normalize → upload SP →   │    │
   │   │  save (multi-row + merge) → enrich obra →              │    │
   │   │  enrich contratos (+ PDFs) → trigger valuation         │    │
   │   └────────────────────────────────────────────────────────┘    │
   │                                                                 │
   │   ┌────────────────────────────────────────────────────────┐    │
   │   │ Pipeline select_contrato (PATCH):                      │    │
   │   │  validate → UPDATE → trigger valuation (sync|async)    │    │
   │   └────────────────────────────────────────────────────────┘    │
   └────┬──────────┬─────────────────┬─────────────────┬─────────────┘
        │          │                 │                 │
        ▼          ▼                 ▼                 ▼
   ┌─────────┐ ┌─────────────┐ ┌──────────────┐ ┌──────────────────┐
   │ Postgres│ │ SharePoint  │ │ sigrid-api   │ │ sv6 · valuation  │
   │ (ORM)   │ │ (Graph API) │ │ (Function)   │ │ (run-async/      │
   │         │ │  + 6 JSON   │ │              │ │  re-run)         │
   │         │ │  + PDFs     │ │              │ │                  │
   └─────────┘ └─────────────┘ └──────────────┘ └──────────────────┘
                                       │
                                       ▼
                                 SQL Server on-prem
                                 (obr/con/ctr/...)
```

`sv3` es el **único servicio que escribe en la BBDD principal** y el **único que
toca SharePoint**. Es además el orquestador de los tres pasos post-persistencia
(obra, contratos, valoración).

---

## 3. Arquitectura interna (Hexagonal / Clean)

```
albaranes-persistence-api/
├─ main.py                                          # uvicorn.run(build_app(settings))
├─ config/
│  ├─ settings.py                                   # Pydantic-settings + validator SHAREPOINT_MODE
│  └─ logging_config.py                             # RotatingFileHandler + consola
├─ domain/
│  ├─ models/
│  │  ├─ schema_base.py                             # StrictSchemaModel (extra='forbid')
│  │  ├─ contexto_linea.py                          # Bloque opcional (idéntico al de sv2/sv5)
│  │  ├─ extraction_models.py                       # ExtractionEnvelope (lo que llega de sv2)
│  │  ├─ persistence_models.py                      # StoredFile, ExistingDocument
│  │  ├─ contrato_models.py                         # ContratoEnrichmentResult, ContratoLineFromSigrid
│  │  └─ obra_models.py                             # ObraEnrichmentResult (con direccion_completa)
│  └─ ports/                                        # 9 interfaces ABC
│     ├─ albaran_repository.py                      # save / get_by_sha256 / initialize
│     ├─ document_storage.py                        # upload (file + JSONs)
│     ├─ obra_enrichment_port.py                    # fetch_obra_by_codigo
│     ├─ obra_merge_repository_port.py              # get/update obra fields del merge
│     ├─ contrato_enrichment_port.py                # fetch_contratos_by_proveedor_obra
│     ├─ contrato_cache_port.py                     # cache opcional (no usado en wiring actual)
│     ├─ contrato_merge_repository_port.py          # replace_contratos / select / has_lines
│     ├─ contrato_pdf_storage_port.py               # upload_contrato_pdf (StoredContratoPdf)
│     └─ valuation_trigger_port.py                  # trigger_async / trigger_sync (+ ValuationSyncResult)
├─ application/
│  ├─ pipelines/
│  │  ├─ persist_albaran_pipeline.py                # Pipeline POST /persist
│  │  └─ select_contrato_pipeline.py                # Pipeline PATCH /selected-contrato
│  └─ services/
│     ├─ albaran_normalizer.py                      # Texto, CIF, fecha ISO, código_imputación, números
│     ├─ albaran_confidence_service.py              # ⭐ MERGE multi-proveedor + scoring (1400 LOC)
│     ├─ contexto_linea_merger.py                   # Pick "más rico" entre 3 contextos
│     ├─ obra_code_normalizer.py                    # 4 dígitos con padding cero
│     ├─ obra_enrichment_service.py                 # Orquesta enrich obra (best-effort)
│     └─ contrato_enrichment_service.py             # Orquesta enrich contratos + PDFs
├─ infrastructure/
│  ├─ database/
│  │  ├─ session_factory.py                         # Crea BBDD si no existe + engine SQLAlchemy
│  │  ├─ orm_models.py                              # albaran_documents[_merge] + albaran_lines[_merge]
│  │  ├─ orm_contrato_models.py                     # albaran_contratos_merge + lines
│  │  ├─ orm_contrato_cache_models.py               # cache opcional de contratos
│  │  ├─ sqlalchemy_albaran_repository.py           # Repo principal (1262 LOC) - MERGE + DDL valoración
│  │  ├─ sqlalchemy_contrato_cache_repository.py    # Cache opcional
│  │  └─ sqlite_albaran_repository.py               # (legacy / vacío)
│  ├─ graph/
│  │  └─ token_provider.py                          # OAuth2 client_credentials para Graph (= sv1)
│  ├─ storage/
│  │  └─ sharepoint_document_storage.py             # SharePoint via Graph (file + 6 JSON + PDFs)
│  ├─ sigrid/
│  │  ├─ sigrid_api_obra_client.py                  # POST /api/sql/read sobre obr/con/auxmun/auxpro
│  │  └─ sigrid_api_contrato_client.py              # /api/sql/read + /api/documents/read (PDFs)
│  └─ clients/
│     └─ http_valuation_trigger.py                  # Cliente sv6 (async corto, sync largo)
└─ interface_adapters/
   └─ api/
      └─ app.py                                     # FastAPI: build_app() + 3 endpoints
```

### Patrones aplicados

| Patrón                              | Dónde                                                | Por qué                                                                                          |
|-------------------------------------|------------------------------------------------------|--------------------------------------------------------------------------------------------------|
| **Hexagonal / Ports & Adapters**    | `domain/ports` ↔ `infrastructure/*`                  | 9 puertos: repo principal, storage, obra/contrato enrichment, valuation trigger…                 |
| **Pipeline**                        | `PersistAlbaranPipeline`, `SelectContratoPipeline`   | Pasos lineales con responsabilidades claras y enriquecimientos best-effort.                      |
| **Strategy + Scoring**              | `AlbaranConfidenceService`                           | 11 estados de comparación (`triple_match`, `match_exact`, `match_tolerant`, `conflict`…) con pesos. |
| **Repository**                      | `SqlAlchemyAlbaranRepository`                        | Aísla SQL/ORM de la lógica de negocio.                                                           |
| **Composition root**                | `build_app(settings)` con wiring condicional         | Construye servicios opcionales (obra/contrato/valuation) si están configurados.                  |
| **Best-effort + idempotencia**      | `_enrich_*_safely`, `get_by_sha256` re-enriquece     | Un fallo de enrichment NO rompe el persist. Si el doc ya existe, se re-enriquece igual.          |
| **Polimorfismo por SharePoint mode**| `SHAREPOINT_MODE = drive_id|folder_url|site_path`    | Tres formas de localizar la carpeta destino sin acoplarse a una.                                 |
| **DDL embebido idempotente**        | `_VALUATION_DDL` en repo                             | El sv3 crea las 9 tablas del sv6 al arrancar (`CREATE TABLE IF NOT EXISTS`).                     |
| **Two-phase commit lógico**         | Persist → Enrich → Trigger                           | Persist es transaccional; enrich y trigger son fire-and-forget para no acoplar disponibilidad.   |

---

## 4. Endpoints HTTP

### 4.1 `GET /health`

Devuelve estado y todo el wiring efectivo. Útil para detectar enrichments no
cableados antes de que un PDF lo descubra.

```json
{
  "ok": true,
  "service": "albaranes-persistence",
  "version": "1.0.0",
  "database": "albaranes",
  "sharepoint_mode": "drive_id",
  "sharepoint_drive_id": "b!...",
  "sharepoint_drive_name": "Documentos compartidos",
  "sharepoint_folder_root": "albaranes",
  "obra_enrichment_enabled": true,
  "obra_enrichment_wired": true,
  "contrato_enrichment_enabled": true,
  "contrato_enrichment_wired": true,
  "contrato_pdf_storage_wired": true,
  "valuation_trigger_enabled": true,
  "valuation_trigger_wired": true,
  "valuation_api_base_url": "http://127.0.0.1:8005",
  "sigrid_api_base_url": "https://func-sigrid-api-...azurewebsites.net",
  "sigrid_api_database": "ruesma"
}
```

> Diferencia clave **enabled vs wired**: una flag `enabled=true` puede dar `wired=false`
> si falta una variable de entorno (p. ej. `SIGRID_API_BASE_URL` vacío). El log al
> arranque indica el motivo exacto.

### 4.2 `POST /v1/albaranes/persist`

Endpoint principal. Recibe el documento + el envelope que produjo sv2 + el contexto
de email que adjuntó sv1.

**Request:**

```http
POST /v1/albaranes/persist HTTP/1.1
Content-Type: multipart/form-data; boundary=...

(form-field "file":            archivo binario .pdf | .jpg | .jpeg | .png | .webp)
(form-field "extraction_json": JSON serializado del envelope sv2)
(form-field "context_json":    JSON serializado del contexto sv1, opcional, default "{}")
```

**Validaciones:**

| Validación                                                      | Error                                              |
|-----------------------------------------------------------------|----------------------------------------------------|
| `file` vacío                                                    | `400 Archivo vacío.`                               |
| `extraction_json` no parseable                                  | `400 extraction_json inválido: ...`                |
| `context_json` no parseable                                     | `400 context_json inválido: ...`                   |
| Envelope no cumple `ExtractionEnvelope` (Pydantic estricto)     | `500` con mensaje de validación                    |
| `meta.source_sha256` ≠ SHA-256 real del fichero (en cualquier proveedor) | `400 El sha256 del adjunto no coincide con el bloque <provider>.` |
| Fallo SQL/SharePoint/Graph                                      | `500 Error persistiendo albarán: ...`              |

**Respuesta `200 OK`:**

```json
{
  "ok": true,
  "document_id": "8d9a2f66-...-a3e1",
  "sharepoint_url": "https://acens.sharepoint.com/sites/.../albaran.pdf",
  "duplicate": false,
  "stored_lines": 7
}
```

> Si el fichero **ya existe** (mismo SHA-256), `duplicate=true` y se devuelve el
> `document_id` previo. Aún así se **re-ejecuta** el enrichment de obra y contratos
> (la BBDD on-prem puede haber cambiado) y se re-dispara la valoración.

### 4.3 `PATCH /v1/albaranes/{document_id}/selected-contrato`

Cambia el contrato seleccionado del albarán y, opcionalmente, dispara la valoración.

**Request body:**

```json
{
  "codigo_contrato": "C-2026-014",
  "trigger_valuation": true,
  "wait_for_valuation": false
}
```

| Campo                | Tipo               | Default | Significado                                                                  |
|----------------------|--------------------|---------|------------------------------------------------------------------------------|
| `codigo_contrato`    | `string \| null`   | —       | Contrato a marcar como seleccionado. `null` → deselecciona.                  |
| `trigger_valuation`  | `bool`             | `true`  | Si `false`, sólo hace UPDATE.                                                |
| `wait_for_valuation` | `bool`             | `false` | `false` → fire-and-forget contra `/run-async`. `true` → bloqueante `/re-run`. |

**Errores específicos:**
- `404` — `document_id` no existe en `albaran_documents_merge`.
- `400` — `codigo_contrato` no existe entre los contratos asociados al documento.
- `500` — fallo BBDD.

**Respuesta `200 OK`:**

```json
{
  "ok": true,
  "document_id": "8d9a2f66-...-a3e1",
  "previous_selected_contrato_codigo": null,
  "selected_contrato_codigo": "C-2026-014",
  "valuation_triggered": true,
  "valuation_mode": "async",
  "valuation_async_accepted": true,
  "valuation_sync": null
}
```

> En modo `sync`, `valuation_sync` trae `{accepted, http_status, valuation_id, status, total_valorado, total_lines, review_required, error}`. El front recibe el resumen completo en la misma llamada.

> El disparo es **best-effort**: si el sv6 está caído, el UPDATE se commitea igual y
> `valuation_triggered=false` con el error concreto. El front puede reintentar
> manualmente pulsando "Valorar".

---

## 5. El merge multi-proveedor (`AlbaranConfidenceService`)

Es la pieza más compleja del servicio (~1.400 LOC). En un párrafo:

> **Toma 3 versiones del mismo albarán (OpenAI, Gemini, Claude) y produce una versión
> única "mergeada" con un score de confianza por documento, por línea y por campo.**
> Decide qué proveedor manda como base, hace coalesce de campos, empareja líneas
> entre proveedores, y emite razones de revisión humana cuando hay disenso crítico.**

### 5.1 Decisión de "proveedor base"

| Caso                                                 | Base               | Origen                  |
|------------------------------------------------------|--------------------|-------------------------|
| Gemini presente                                      | Gemini             | `gemini_filled`         |
| Gemini ausente, Claude con líneas útiles             | OpenAI rellenado por Claude | `openai_filled`  |
| Solo OpenAI (o Claude sin líneas)                    | OpenAI             | `openai_fallback`       |

### 5.2 Estados de comparación por campo

```
triple_match     98  pts  → los 3 coinciden (tras normalización)
match_exact      96  pts  → 2 fuentes idénticas
match_normalized 92  pts  → coinciden tras normalizar
match_tolerant   88  pts  → numéricos dentro de tolerancia
majority_match   86  pts  → 2 de 3 coinciden, una discrepa
openai_only      76  pts  → solo OpenAI lo aportó
gemini_only      66  pts  → solo Gemini
claude_only      66  pts  → solo Claude
both_empty_required  20 pts  → vacío en todos pero el campo es obligatorio
both_empty_optional  NaN     → vacío en todos pero opcional (no penaliza)
conflict         28  pts  → desacuerdo total entre fuentes
invalid          10  pts  → valor no parseable
```

### 5.3 Pesos de campos

**Cabecera (peso, requerido, crítico, tipo):**
- `proveedor_nombre` (18, sí, sí, text)
- `numero_albaran` (18, sí, sí, identifier)
- `fecha` (14, sí, sí, date)
- `obra_nombre` (14, no, no, text)
- `proveedor_cif` (10, no, no, cif)
- `obra_codigo` (6, no, no, identifier)
- `forma_pago` (4, no, no, text)
- `obra_direccion` (4, no, no, text)

**Líneas:**
- `concepto` (25, sí, sí, text)
- `precio_neto` (20, sí, sí, number, tol 0.15)
- `codigo_imputacion` (20, no, sí, identifier)
- `cantidad` (15, sí, sí, number, tol 0.05)
- `precio` (10, no, no, number, tol 0.05)
- `codigo` / `unidad_medida` (5, no, no, identifier)
- `descuento` (5, no, no, number, tol 0.2)

### 5.4 Confianza final del documento

```
doc_rule_score   = 0.35 * header_score + 0.50 * lines_score + 0.15 * coherence_score
doc_confidence   = 0.75 * doc_rule_score + 0.25 * raw_signal(openai_confianza_pct)
doc_confidence  ← _apply_document_caps(...)   # caps por situación crítica
doc_confidence  ∈ [0, 100]
```

Se aplican **caps** (techos máximos) cuando hay situaciones graves: campos críticos
en conflicto, ausencia de campos requeridos, baja coherencia agregada, etc. Esto
evita que un documento con 4 campos críticos en `conflict` saque 80%.

### 5.5 `contexto_linea` mergeado aparte

`pick_best_contexto_linea` selecciona el **contexto más rico** entre los 3 proveedores
(no fusiona campo a campo). Razón explícita: los campos de `contexto_linea` están
correlacionados semánticamente — un `rol_linea` de OpenAI encaja con la
`descripcion_extendida` de OpenAI, no con la de Claude. Mezclarlos produciría
contextos incoherentes.

---

## 6. Modelo de datos (PostgreSQL)

### 6.1 Tablas creadas por sv3

| Tabla                              | Granularidad                               | Propósito                                                           |
|------------------------------------|--------------------------------------------|---------------------------------------------------------------------|
| `albaran_documents`                | 1 fila por (sha256, provider)              | Auditoría cruda: qué dijo cada proveedor.                           |
| `albaran_lines`                    | N por documento por proveedor              | Idem para las líneas.                                               |
| `albaran_documents_merge`          | 1 fila por sha256 (UQ)                     | **Versión canónica mergeada** — fuente de verdad.                   |
| `albaran_lines_merge`              | N por merge document                       | Líneas mergeadas con `line_match_score`, `field_scores_json`, etc.  |
| `albaran_contratos_merge`          | N por merge document                       | Contratos del proveedor para esa obra (de Sigrid).                  |
| `albaran_contrato_lines_merge`     | N por contrato                              | Líneas de detalle de cada contrato (con partida).                   |

### 6.2 Tablas creadas para sv6 (DDL idempotente embebido)

El sv3 ejecuta al arranque el `_VALUATION_DDL` con `CREATE TABLE IF NOT EXISTS`
para 9 tablas del sv6 (`albaran_valuations`, etc.). Esto desacopla el orden de
arranque entre microservicios: sv3 puede arrancar primero y dejar la BBDD lista.

### 6.3 Columnas relevantes de `albaran_documents_merge`

Mixin compartido por la versión raw y la merge:
- **Identificación:** `id` (UUID v4), `provider_origin`, `source_sha256` (UQ en merge),
  `source_filename`, `source_mime_type`.
- **Cabecera albarán:** `proveedor_nombre/cif`, `fecha`, `numero_albaran`, `forma_pago`,
  `obra_codigo/nombre/direccion`.
- **SharePoint:** `sharepoint_drive_id`, `sharepoint_item_id`, `sharepoint_relative_path`,
  `sharepoint_web_url`, `sharepoint_share_url`.
- **Artefactos IA:** `ia_input_json/output_json` (texto inline), más
  `{ia,gem,cla}_{input,output}_{relative_path,web_url}` para los JSONs que se suben
  a SharePoint (request y response de cada proveedor).
- **Contexto email:** `email_id`, `email_subject`, `email_sender`, `email_received_datetime`,
  `raw_context_json`.
- **Trazabilidad:** `raw_extraction_json` (envelope completo serializado),
  `confidence_pct_calc`, `review_required`, `review_reasons_json`,
  `comparison_summary_json`, `created_at_utc`.
- **Pagina (si fue split):** `page_number`, `page_count`.

### 6.4 Columnas en `albaran_lines_merge`

Igual que las raw, más:
- `confidence_pct_calc` — confianza calculada de la línea.
- `line_match_score` — qué tan bien se emparejó esta línea entre proveedores.
- `comparison_status_json` — estado por campo (triple_match, conflict, …).
- `field_scores_json` — score por campo.
- `contexto_linea_json` — bloque opcional serializado JSON.

> El campo **`unidad_medida`** existe en sv3 pero **no en el modelo del sv2**: viaja
> en el envelope tal cual lo emite el LLM y aquí se persiste. Decisión: el modelo
> `LineaAlbaran` de sv3 es **superset** del de sv2 (acepta campos extra que el
> envelope traiga). El campo lo consumen sv5 (clasificación de categoría) y sv6
> (conversión de cantidades).

---

## 7. Configuración (variables de entorno)

Archivo `.env` en la raíz del proyecto.

### 7.1 Microsoft Graph (compartido con sv1)

| Variable      | Default | Descripción                                                                  |
|---------------|---------|------------------------------------------------------------------------------|
| `GRAPH_KEY`   | *obl.*  | JSON o JSON-base64 con `tenant_id`, `client_id`, `client_secret`. Mismo formato que sv1. |

### 7.2 SharePoint

| Variable                      | Default                  | Descripción                                                            |
|-------------------------------|--------------------------|------------------------------------------------------------------------|
| `SHAREPOINT_MODE`             | `drive_id`               | `drive_id` \| `folder_url` \| `site_path`.                             |
| `SHAREPOINT_DRIVE_ID`         | *si modo drive_id*       | ID de drive ya conocido (lo más eficiente).                            |
| `SHAREPOINT_FOLDER_URL`       | *si modo folder_url*     | URL completa de carpeta compartida.                                    |
| `SHAREPOINT_HOSTNAME`         | *si modo site_path*      | p. ej. `acens.sharepoint.com`.                                         |
| `SHAREPOINT_SITE_PATH`        | *si modo site_path*      | p. ej. `/sites/Construccion`.                                          |
| `SHAREPOINT_DRIVE_NAME`       | `Documentos compartidos` | Nombre del drive cuando se resuelve por path.                          |
| `SHAREPOINT_FOLDER_ROOT`      | `albaranes`              | Carpeta raíz dentro del drive donde se cuelga todo.                    |
| `SHAREPOINT_LINK_TYPE`        | `view`                   | `view` \| `edit`.                                                      |
| `SHAREPOINT_LINK_SCOPE`       | `organization`           | `organization` \| `anonymous` \| `users`.                              |
| `SHAREPOINT_CREATE_LINK`      | `true`                   | Si `false`, no crea share_url y solo deja el `web_url` autenticado.    |

### 7.3 PostgreSQL

| Variable             | Default       | Descripción                                                   |
|----------------------|---------------|---------------------------------------------------------------|
| `PG_HOST`            | `localhost`   |                                                               |
| `PG_PORT`            | `5432`        |                                                               |
| `PG_DB`              | `albaranes`   | BBDD de la aplicación. La crea sv3 si no existe (vía admin).  |
| `PG_USER`            | `postgres`    | Usuario de la app.                                            |
| `PG_PASSWORD`        | *obl.*        |                                                               |
| `PG_ADMIN_DB`        | `postgres`    | BBDD admin para `CREATE DATABASE` si la app no existe.        |
| `PG_ADMIN_USER`      | `postgres`    |                                                               |
| `PG_ADMIN_PASSWORD`  | *obl.*        |                                                               |

> **Driver**: `postgresql+psycopg` (psycopg v3 nativo). El usuario de la app puede
> ser el mismo que el admin si se quiere; el desdoble es para entornos donde el
> admin tiene `CREATEDB` pero la app va con privilegios mínimos.

### 7.4 Sigrid API (sv-aux: la Function App `sigrid-api`)

| Variable                  | Default   | Descripción                                                         |
|---------------------------|-----------|---------------------------------------------------------------------|
| `SIGRID_API_BASE_URL`     | *opc.*    | URL de la Function App. Si vacío → enrichment off.                  |
| `SIGRID_API_FUNCTION_KEY` | *opc.*    | `x-functions-key`.                                                  |
| `SIGRID_API_DATABASE`     | `ruesma`  | Nombre BBDD on-prem (debe estar en `ALLOWED_DATABASES` del sigrid-api). |
| `SIGRID_API_TIMEOUT_S`    | `30.0`    | Timeout HTTP por consulta SQL.                                      |
| `OBRA_ENRICHMENT_ENABLED` | `true`    | Apaga obra enrichment sin desconfigurar Sigrid.                     |

### 7.5 Trigger valoración (sv6)

| Variable                              | Default | Descripción                                                          |
|---------------------------------------|---------|----------------------------------------------------------------------|
| `VALUATION_API_BASE_URL`              | *opc.*  | URL del sv6. Si vacío → trigger off.                                 |
| `VALUATION_TRIGGER_ENABLED`           | `true`  | Apaga el trigger sin desconfigurar URL.                              |
| `VALUATION_TRIGGER_TIMEOUT_S`         | `3.0`   | Timeout para `/run-async` (solo esperamos el 202).                   |
| `VALUATION_TRIGGER_SYNC_TIMEOUT_S`    | `300.0` | Timeout para `/{doc}/re-run` (incluye llamada a IA del sv5 + persist). |

### 7.6 Generales

| Variable          | Default     |                                                          |
|-------------------|-------------|----------------------------------------------------------|
| `API_HOST`        | `127.0.0.1` |                                                          |
| `API_PORT`        | `8001`      | (sv2 usa 8000)                                           |
| `HTTP_TIMEOUT_S`  | `60`        | Timeout HTTP genérico (Graph, etc.).                     |
| `LOG_LEVEL`       | `INFO`      |                                                          |
| `LOG_DIR`         | `logs`      |                                                          |
| `SERVICE_VERSION` | `1.0.0`     |                                                          |

### 7.7 Validators cruzados

`Settings.validate_sharepoint_mode` aborta si:
- `mode=drive_id` sin `SHAREPOINT_DRIVE_ID`.
- `mode=folder_url` sin `SHAREPOINT_FOLDER_URL`.
- `mode=site_path` sin `SHAREPOINT_HOSTNAME` y `SHAREPOINT_SITE_PATH`.

Properties derivadas: `database_url`, `admin_database_url`, `sigrid_api_configured`,
`valuation_trigger_configured`. El wiring las consulta antes de instanciar adapters.

---

## 8. Flujo detallado del pipeline `/persist`

```
1. POST /v1/albaranes/persist
   ├─ leer file_bytes (UploadFile.read)
   ├─ json.loads(extraction_json)
   └─ json.loads(context_json or "{}")

2. PersistAlbaranPipeline.run(request):
   a) ExtractionEnvelope.model_validate(envelope)        # estricto Pydantic
   b) sha256 = sha256(file_bytes)
   c) _validate_sha256(envelope, sha256)
        └─ Para CADA bloque (openai, gemini, claude, gdai, azure):
             si meta.source_sha256 ≠ sha256 → raise ValueError

   d) existing = repository.get_by_sha256(sha256)
      if existing:
          enrich_obra_safely(existing.id)
          enrich_contratos_safely(existing.id)
          trigger_valuation_safely(existing.id)
          return PersistAlbaranResult(duplicate=True, ...)

   e) envelope = _normalize(envelope)                   # AlbaranNormalizer
        ├─ Normaliza data (OpenAI a nivel raíz)
        └─ Normaliza data de gemini, claude, gdai, azure si presentes

   f) stored_file = document_storage.upload(
            filename, mime_type, file_bytes, source_sha256=sha256,
            ia_input/output_payload  ← envelope.debug.openai_*
            gem_input/output_payload ← envelope.gemini.debug.gemini_*
            cla_input/output_payload ← envelope.claude.debug.claude_*
        )                                                 # → SharePoint
        # Sube hasta 7 ítems: el original + 6 JSONs

   g) saved = repository.save(envelope, context, stored_file)
        ├─ INSERT albaran_documents x N proveedores
        ├─ INSERT albaran_lines     x N proveedores
        ├─ AlbaranConfidenceService.build_merge_analysis(...)
        ├─ INSERT albaran_documents_merge (con score, review_required, ...)
        ├─ INSERT albaran_lines_merge (con line_match_score, field_scores)
        └─ commit

   h) _enrich_obra_safely(saved.document_id)
        ├─ obra_codigo = repo.get_merge_obra_codigo(doc_id)
        ├─ obra_codigo_normalizado = pad-zero a 4 dígitos
        ├─ result = sigrid_obra_client.fetch_obra_by_codigo(codigo)
        │     └─ POST sigrid-api /api/sql/read con SELECT obr/con/auxmun/auxpro
        └─ repo.update_merge_obra_fields(doc_id, nombre, direccion)

   i) _enrich_contratos_safely(saved.document_id)
        ├─ (cif, obra) = repo.get_merge_proveedor_cif_obra_codigo(doc_id)
        ├─ contratos = sigrid_contrato_client.fetch_contratos_by_proveedor_obra(cif, obra)
        │     └─ POST sigrid-api /api/sql/read  (cabecera + ctrpro lines + obrparpar partida)
        ├─ Mapa de PDFs ya guardados antes de replace (para reutilizar)
        ├─ repo.replace_contratos(doc_id, contratos)        # delete-insert con cascade
        ├─ Para cada contrato cuyo gra_rep_ide cambió o es nuevo:
        │     ├─ pdf_bytes = sigrid_contrato_client.fetch_contrato_pdf(gra_rep_ide)
        │     │       └─ POST sigrid-api /api/documents/read sobre dbo.gra
        │     ├─ stored_pdf = document_storage.upload_contrato_pdf(...)
        │     └─ repo.update_contrato_pdf_paths(...)
        └─ Si len(contratos) == 1: repo.set_selected_contrato(doc_id, codigo)

   j) _trigger_valuation_safely(saved.document_id)
        ├─ has_lines, codigo = repo.has_selected_contrato_with_lines(doc_id)
        ├─ if not has_lines: SKIP
        └─ valuation_trigger.trigger_async(doc_id, codigo, force=False)
              └─ POST sv6 /v1/valuation/run-async  (timeout 3s, esperamos 202)

   k) return PersistAlbaranResult(ok=True, duplicate=False, ...)
```

### Best-effort en los 3 enrichments

Los tres pasos `_enrich_obra_safely`, `_enrich_contratos_safely` y
`_trigger_valuation_safely` están envueltos en `try/except Exception` con `logger.exception`.
**Nunca rompen el persist principal**. Esto es deliberado: la persistencia ya
ocurrió y commitó, y la BBDD on-prem (vía sigrid-api) o el sv6 pueden estar caídos
sin que perdamos el albarán.

---

## 9. Cómo se invoca este servicio

### 9.1 Quién lo llama hoy

- **`sv1`** llama `POST /v1/albaranes/persist` por cada documento lógico (= página de PDF).
  Su timeout configurado en sv1 es **120 s**.
- **El frontend humano** (sv4, no analizado todavía) llama `PATCH .../selected-contrato`
  cuando un revisor cambia el contrato seleccionado o pulsa "Valorar".

### 9.2 Curl de prueba

```bash
curl -X POST http://127.0.0.1:8001/v1/albaranes/persist \
  -F "file=@/ruta/albaran.pdf" \
  -F 'extraction_json={"meta":{...},"data":{...},"debug":{...},"gemini":{...},"claude":{...}}' \
  -F 'context_json={"email":{...},"attachment":{...},"document":{...}}'
```

### 9.3 Arranque local

```powershell
# Prerequisitos: PostgreSQL 14+ corriendo, BBDD admin accesible
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env       # rellenar GRAPH_KEY, SHAREPOINT_*, PG_*, SIGRID_*, VALUATION_*
python main.py
```

> Al arrancar, el servicio:
> 1. Conecta a `PG_ADMIN_*` y crea la BBDD `PG_DB` si no existe (`CREATE DATABASE`).
> 2. Reabre conexión con `PG_USER`/`PG_PASSWORD` sobre la BBDD recién creada.
> 3. Ejecuta `Base.metadata.create_all(...)` (todas las tablas ORM del sv3).
> 4. Ejecuta `_VALUATION_DDL` (las 9 tablas del sv6, idempotente).
> 5. Arranca uvicorn en `API_HOST:API_PORT`.

### 9.4 Decisión de despliegue Azure

Por las dependencias (PostgreSQL, SharePoint via Graph, sigrid-api, sv6) y por
el tiempo de petición que puede ir entre 5 s y 30 s en condiciones normales:

**Azure Container App** en el spoke DEV es la elección natural:
- `minReplicas=1` para evitar cold start (la primera petición tras frío inicializa
  la BBDD si hace falta y eso es lento).
- `maxReplicas` 3-5 según volumen.
- Ingress **interno** (no debería ser accesible desde fuera del spoke; lo llama sv1
  y el frontend, ambos en el mismo spoke).
- *Identidad gestionada* asignada para acceder a Key Vault (donde estarán
  `GRAPH_KEY`, `PG_PASSWORD`, `SIGRID_API_FUNCTION_KEY` como Key Vault references
  en App Settings).

> **No** usar Azure Functions: las peticiones pueden superar los 230 s del HTTP
> Trigger en planes consumo cuando coincide split+contratos+upload de varios PDFs.

---

## 10. Inputs / Outputs del servicio

### Inputs

| Origen     | Naturaleza                                | Detalle                                                                       |
|------------|-------------------------------------------|-------------------------------------------------------------------------------|
| HTTP       | `POST /persist` multipart                 | `file` + `extraction_json` (envelope sv2) + `context_json` (contexto sv1).    |
| HTTP       | `PATCH /selected-contrato`                | JSON con `codigo_contrato`, `trigger_valuation`, `wait_for_valuation`.        |
| `.env`     | Configuración estática                    | Graph + SharePoint + Postgres + sigrid + valuation + generales.               |
| Sigrid API | Respuestas SQL/binario                    | Datos de obra, contratos, líneas y PDFs.                                      |

### Outputs

| Destino     | Naturaleza                                 | Detalle                                                                         |
|-------------|--------------------------------------------|---------------------------------------------------------------------------------|
| HTTP        | `application/json`                         | `PersistAlbaranResult` o `SelectContratoResult`.                                |
| PostgreSQL  | INSERT/UPDATE en 6 tablas + 9 del sv6      | Documentos raw, merge, líneas, contratos, partidas, DDL valuación.              |
| SharePoint  | Upload via Graph                           | 1 PDF/imagen original + 6 JSON (request+response de OpenAI/Gemini/Claude) + N PDFs de contratos. |
| sv6         | `POST /v1/valuation/run-async` (FaF)       | Trigger automático tras persistir.                                              |
| sv6         | `POST /v1/valuation/{doc}/re-run` (sync)   | Solo si el front llama PATCH con `wait_for_valuation=true`.                     |
| Filesystem  | Logs rotados                               | `logs/<fichero>.log`.                                                           |

---

## 11. Decisiones técnicas relevantes

1. **Idempotencia por SHA-256**, no por email_id ni filename. Garantiza que duplicados
   por reenvío de email o reprocesamiento manual no creen filas nuevas, pero **sí
   re-enriquecen** (la BBDD on-prem evoluciona).
2. **Persistencia de los 6 artefactos JSON en SharePoint** (request + response de
   cada LLM). Permite *post-mortem* sobre por qué un proveedor extrajo X. El JSON
   inline en la BBDD (`ia_input_json` / `ia_output_json`) está deprecated en favor
   de los `relative_path` / `web_url`.
3. **Doble persistencia raw + merge.** Permite volver a recalcular la confianza
   sin re-llamar a sv2: las 3 versiones (OpenAI, Gemini, Claude) están en
   `albaran_documents` y `albaran_lines`. Si cambia el algoritmo de merge,
   relanzar es local y barato.
4. **Validación de SHA-256 contra cada bloque del envelope.** Detecta corrupciones
   o desincronía sv1↔sv2↔sv3 (p. ej. si sv2 hubiera procesado otro fichero por error).
5. **`replace_contratos` (delete + insert con cascade).** No hace upsert: cada vez
   que se enriquece, los contratos del documento se sobrescriben. Esto evita
   "contratos zombi" cuando el ERP los retira. La excepción son los PDFs ya
   subidos cuyo `gra_rep_ide` no cambió: se reutilizan los paths sin volver a
   descargar.
6. **Auto-selección si solo hay 1 contrato.** Si Sigrid devuelve un único contrato
   para `(cif, obra)`, sv3 lo marca automáticamente como `selected_contrato_codigo`,
   lo que dispara la valoración sin intervención humana. En caso contrario,
   espera a que el front haga PATCH.
7. **Dos timeouts en `HttpValuationTrigger`** (3 s async / 300 s sync). Async solo
   espera el 202; sync espera la valoración completa que incluye llamada a IA del
   sv5 y persistencia.
8. **El sv3 crea las tablas del sv6 al arrancar.** Decisión pragmática para
   desacoplar el orden de arranque: sv6 puede levantar después y encontrarse las
   tablas listas. El DDL es idempotente (`IF NOT EXISTS`).
9. **Wiring condicional con logs explícitos.** Los logs `[wiring]` al arrancar
   indican exactamente qué servicios opcionales se han cableado y por qué no
   los demás. Permite diagnosticar fácilmente "por qué no se enriquece la obra"
   sin leer código.
10. **Diferente nivel de `extra=` por modelo:** `StrictSchemaModel` con `extra='forbid'`
    para detectar derivas en el envelope; `ContextoLinea` con `extra='ignore'` para
    tolerar evolución del prompt (igual que en sv2).

---

## 12. Frontera del microservicio: ¿se sale algo?

Comentario del análisis "en extremo" que pediste: **sí**, este servicio hace
demasiado para un único microservicio bien delimitado. Concretamente:

| Responsabilidad                              | ¿Encaja con "persistencia"? | Comentario                                                                                          |
|----------------------------------------------|-----------------------------|-----------------------------------------------------------------------------------------------------|
| Validar envelope y persistir en BBDD         | Sí                          | Es lo central.                                                                                      |
| Subir fichero original a SharePoint          | Sí                          | Storage de blobs, OK.                                                                               |
| Subir JSONs IA a SharePoint                  | Frontera                    | Discutible; podría vivir en sv2 o en un servicio aparte.                                            |
| Merge multi-proveedor + scoring (1.400 LOC)  | **No**                      | Es un dominio propio: "albaranes-confidence-engine". Reutilizable por otros pipelines.              |
| Enrichment de obra contra Sigrid             | Frontera                    | Tiene sentido tenerlo aquí porque modifica la fila merge, pero ya se ve "raro".                     |
| Enrichment de contratos + descarga de PDFs   | **No**                      | Ciclo de vida propio: contratos se actualizan independientemente del albarán. Candidato a sv aparte. |
| Trigger valoración sv6                       | Frontera                    | Es solo un POST FaF, no rompe nada que esté aquí.                                                   |
| Crear DDL del sv6                            | **No**                      | Acoplamiento de schema entre microservicios. Idealmente cada servicio gestiona su propio DDL.       |

### Propuestas concretas (si quisieras separar)

1. **Sacar `albaran_confidence_service.py`** + `contexto_linea_merger.py` a un
   microservicio `albaranes-confidence-engine` con un solo endpoint
   `POST /v1/merge` que reciba `{openai, gemini, claude}` y devuelva
   `MergeAnalysis`. Lo llamarían sv3 al persistir y sv6 al re-valorar.
2. **Sacar el ciclo de PDFs de contratos** a un microservicio
   `contratos-pdf-syncer` con un Timer Trigger (cada N min) que lea contratos
   de `albaran_contratos_merge` con `pdf_sharepoint_relative_path IS NULL` y
   los sincronice. Eso desacopla la latencia del persist principal.
3. **Mover el `_VALUATION_DDL`** al sv6 (que es su dueño legítimo). sv3 quedaría
   solo con su propio DDL.

> Si esto te interesa, dímelo cuando hayas pasado todos los servicios y vemos el
> mapa completo antes de proponer separaciones. Por ahora lo señalo y sigo.

---

## 13. Limitaciones conocidas y mejoras propuestas

| #  | Limitación                                                                                                  | Mejora propuesta                                                                                                  |
|----|-------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------|
| 1  | Sin auth en endpoints                                                                                       | API key header o Easy Auth (Entra ID) en Container App.                                                           |
| 2  | El `confidence_service` es enorme (1.400 LOC) y mezcla parsing, scoring y matching de líneas                | Sacar a microservicio dedicado (ver §12.1) o, como mínimo, partir en módulos `header_merge`, `line_match`, `scoring`. |
| 3  | Enrichment no se reintenta si Sigrid está caído                                                             | Cola de retry diferida (Storage Queue / Service Bus) que vacíe periódicamente.                                    |
| 4  | El `replace_contratos` borra y reinserta; no detecta cambios incrementales                                  | Diff-based update: solo INSERT/UPDATE/DELETE filas que cambien. Necesita identidad estable en `ctr.cod` (sí la hay). |
| 5  | Trigger valoración fire-and-forget puede fallar silenciosamente                                             | Tabla `outbox_events` con dispatcher periódico (transactional outbox pattern).                                    |
| 6  | El DDL del sv6 vive en sv3 (acoplamiento de schema entre microservicios)                                    | Mover a Alembic en sv6 o a un job de migración compartido.                                                        |
| 7  | Logs solo en disco                                                                                          | Handler adicional a Application Insights.                                                                         |
| 8  | `raw_extraction_json` y los `*_input_json` / `*_output_json` viven en `Text` columns (pueden ser MUY grandes)| Migrar a `JSONB` con índices GIN para poder hacer queries y mover los JSONs grandes a SharePoint sin duplicar.    |
| 9  | El `model_validate` de Pydantic se hace 1 vez sobre el envelope completo, lo que puede fallar entero si un proveedor manda algo raro | Validación tolerante por bloque: si Gemini falla, seguir con OpenAI+Claude y loguear.                  |
| 10 | El SHA-256 valida igualdad entre proveedores asumiendo que sv2 lo calculó bien                              | Recalcular en sv3 y comparar; ya se hace, pero el mismo SHA debería viajar también en `context.attachment.sha256` (lo hace sv1). Corroborar consistencia los tres.  |

---

## 14. Resumen de un vistazo

| Característica         | Valor                                                                                  |
|-----------------------|-----------------------------------------------------------------------------------------|
| Tipo                  | API HTTP (FastAPI + uvicorn)                                                            |
| Lenguaje              | Python 3.12                                                                             |
| Entradas              | `POST /v1/albaranes/persist`, `PATCH /v1/albaranes/{id}/selected-contrato`              |
| Salida                | JSON con `document_id`, scores, contrato seleccionado, valuation status                 |
| Persistencia propia   | PostgreSQL 14+ (SQLAlchemy + psycopg) — fuente de verdad                                |
| Storage de binarios   | SharePoint (vía Microsoft Graph) — fichero original + 6 JSON IA + N PDFs de contratos   |
| Enrichments           | Obra (sigrid-api) · Contratos (sigrid-api + descarga PDFs) · Trigger sv6                |
| Concurrencia          | Una request por worker uvicorn; pasos secuenciales por request                          |
| Despliegue objetivo   | Azure Container App (recomendado, internal ingress)                                     |
| Punto de entrada      | `python main.py`                                                                        |
| Dependencias clave    | `fastapi`, `uvicorn`, `sqlalchemy`, `psycopg`, `pydantic-settings`, `httpx`             |
| Servicios upstream    | sv1 (persist) + frontend humano (PATCH)                                                 |
| Servicios downstream  | sigrid-api · SharePoint/Graph · sv6 (valuation)                                         |

---

*Documento generado a partir del análisis del código del paquete `sv3.zip` aportado.*
