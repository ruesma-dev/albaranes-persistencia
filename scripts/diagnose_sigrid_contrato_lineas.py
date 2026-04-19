# scripts/diagnose_sigrid_contrato_lineas.py
"""Diagnóstico standalone para CONTRATO + LÍNEAS DE DETALLE en sigrid-api.

Evolución del diagnóstico de contrato: en una sola query devuelve la
cabecera del contrato (proveedor, obra, importes, fechas) junto con
todas sus líneas de detalle (productos, cantidades, precios, importes,
doc. origen).

Ejecuta desde la terminal de PyCharm, en la raíz del servicio 3
(albaranes-persistencia):

    python scripts/diagnose_sigrid_contrato_lineas.py

Con argumentos (CIF y/o código de obra):

    python scripts/diagnose_sigrid_contrato_lineas.py B86359866
    python scripts/diagnose_sigrid_contrato_lineas.py B86359866 0695
    python scripts/diagnose_sigrid_contrato_lineas.py B86359866 695    (se normaliza)

Valores por defecto: CIF = B86359866 y obra = 0695 (los del ejemplo).

Este script NO depende de que el servicio 3 arranque ni de que hayas
aplicado parches. Solo necesita el `.env` del servicio con las variables
SIGRID_API_BASE_URL / SIGRID_API_FUNCTION_KEY / SIGRID_API_DATABASE y
acceso a internet para alcanzar la Function App.

Sigue el mismo patrón y la misma UX que ``diagnose_sigrid_contrato.py``.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Paso 0: localizar y cargar el .env
# ---------------------------------------------------------------------------
def load_dotenv_manually(env_path: Path) -> dict[str, str]:
    """Carga un .env sin depender de python-dotenv."""
    if not env_path.exists():
        return {}
    result: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"

print("=" * 70)
print(" DIAGNÓSTICO SIGRID-API — CONTRATO + LÍNEAS (proveedor × obra)")
print("=" * 70)
print(f"Raíz detectada : {_PROJECT_ROOT}")
print(f"Ruta del .env  : {_ENV_PATH}")
print(f"¿Existe?       : {_ENV_PATH.exists()}")
print("-" * 70)

env = load_dotenv_manually(_ENV_PATH)


def get_cfg(name: str) -> str | None:
    return os.environ.get(name) or env.get(name)


base_url = get_cfg("SIGRID_API_BASE_URL")
function_key = get_cfg("SIGRID_API_FUNCTION_KEY")
database = get_cfg("SIGRID_API_DATABASE") or "ruesma"
timeout_s = float(get_cfg("SIGRID_API_TIMEOUT_S") or "30")

print("Variables detectadas:")
print(f"  SIGRID_API_BASE_URL     = {base_url!r}")
if function_key:
    print(
        f"  SIGRID_API_FUNCTION_KEY = <presente, len={len(function_key)}, "
        f"últimos 4={function_key[-4:]!r}>"
    )
else:
    print(f"  SIGRID_API_FUNCTION_KEY = {function_key!r}")
print(f"  SIGRID_API_DATABASE     = {database!r}")
print(f"  SIGRID_API_TIMEOUT_S    = {timeout_s}")
print("-" * 70)

problems: list[str] = []
if not base_url:
    problems.append("❌ SIGRID_API_BASE_URL vacío o no definido")
elif not base_url.startswith(("http://", "https://")):
    problems.append(
        f"❌ SIGRID_API_BASE_URL no empieza por http(s):// (valor={base_url!r})"
    )
if not function_key:
    problems.append("❌ SIGRID_API_FUNCTION_KEY vacío o no definido")
if not database:
    problems.append("❌ SIGRID_API_DATABASE vacío")

if any(p.startswith("❌") for p in problems):
    print("PROBLEMAS DETECTADOS EN .env:")
    for problem in problems:
        print(f"  {problem}")
    print()
    print("Arréglalo en el .env del proyecto del servicio 3 y vuelve a ejecutar.")
    sys.exit(2)
else:
    print("✅ .env cargado y variables básicas presentes")
    print()


# ---------------------------------------------------------------------------
# Paso 1: normalizar los parámetros de entrada
# ---------------------------------------------------------------------------
def normalize_obra_code(raw_value: str | None) -> str | None:
    """Misma regla que aplica el servicio 3 (4 dígitos, primer dígito 0)."""
    if raw_value is None:
        return None
    cleaned = str(raw_value).strip()
    if not cleaned or not cleaned.isdigit():
        return None
    if len(cleaned) == 3:
        return "0" + cleaned
    if len(cleaned) == 4 and cleaned.startswith("0"):
        return cleaned
    return None


def normalize_cif(raw_value: str | None) -> str | None:
    """Limpieza mínima del CIF: trim y mayúsculas, sin espacios intermedios."""
    if raw_value is None:
        return None
    cleaned = str(raw_value).strip().upper().replace(" ", "")
    return cleaned or None


# Argumentos CLI: [cif] [codigo_obra]
cif_input = sys.argv[1] if len(sys.argv) > 1 else "B86359866"
obra_input = sys.argv[2] if len(sys.argv) > 2 else "0695"

cif = normalize_cif(cif_input)
obra = normalize_obra_code(obra_input)

print(f"Parámetros de prueba:")
print(f"  cif raw={cif_input!r} -> normalizado={cif!r}")
print(f"  obra raw={obra_input!r} -> normalizado={obra!r}")

if cif is None:
    print("❌ CIF inválido (vacío tras normalizar).")
    sys.exit(3)
if obra is None:
    print(
        "❌ Código de obra no supera la normalización "
        "(regla: 4 dígitos con 0 inicial, o 3 dígitos que se rellenan con 0)."
    )
    print("   Ejemplo válido: python scripts/diagnose_sigrid_contrato_lineas.py B86359866 0695")
    sys.exit(3)
print("-" * 70)


# ---------------------------------------------------------------------------
# Paso 2: POST a sigrid-api con la query combinada contrato + líneas
# ---------------------------------------------------------------------------
try:
    import httpx
except ImportError:
    print("❌ Falta el paquete 'httpx'. Instálalo con:")
    print("     pip install httpx")
    sys.exit(4)

# Query combinada: cabecera del contrato + líneas de detalle (ctrpro).
# En cada fila vienen repetidos los campos de cabecera junto con una línea.
# ORDEN de los parámetros = orden de los ? en el SQL (primero CIF, luego obra).
SQL_QUERY = """\
SELECT
    -- CABECERA DEL CONTRATO
    ctr.ide             AS contrato_ide,
    con_ctr.cod         AS codigo_contrato,
    con_ctr.res         AS nombre_contrato,
    con_ctr.fec         AS fecha_alta_contrato,
    ctr.fecdoc          AS fecha_contrato,
    ctr.fecvig1         AS vigencia_desde,
    ctr.fecvig2         AS vigencia_hasta,
    ctr.tot             AS importe_total_contrato,
    ctr.entcif          AS cif_proveedor,
    ctr.entres          AS nombre_proveedor,
    con_obr.cod         AS codigo_obra,
    con_obr.res         AS nombre_obra,

    -- LINEAS DE DETALLE
    ctrpro.pos          AS linea,
    ctrpro.numlin       AS numero_linea,
    con_pro.cod         AS codigo_producto,
    ctrpro.cod2         AS codigo_alternativo,
    ctrpro.unimed       AS unidad_medida,
    ctrpro.res          AS descripcion_linea,
    ctrpro.can          AS uds,
    ctrpro.canser       AS cantidad_servida,
    ctrpro.canfac       AS cantidad_facturada,
    (ctrpro.can - ISNULL(ctrpro.canser, 0)) AS pendiente_servir,
    ctrpro.pre          AS precio_unitario,
    ctrpro.tar          AS precio_bruto,
    ctrpro.dto          AS descuentos,
    ctrpro.tot          AS importe_linea,
    ctrpro.ivacuo       AS cuota_iva,
    ctrpro.docoricod    AS doc_origen
FROM ctr
JOIN con AS con_ctr  ON ctr.ide     = con_ctr.ide
JOIN con AS con_obr  ON ctr.obride  = con_obr.ide
JOIN prv             ON ctr.entide  = prv.ide
JOIN ctrpro          ON ctrpro.docide = ctr.ide
LEFT JOIN pro             ON ctrpro.proide = pro.ide
LEFT JOIN con AS con_pro  ON pro.ide       = con_pro.ide
WHERE
    prv.cif     = ?
AND con_obr.cod = ?
ORDER BY con_ctr.cod, ctrpro.pos
"""

url = f"{base_url.rstrip('/')}/api/sql/read"
payload = {
    "database": database,
    "sql": SQL_QUERY,
    "parameters": [cif, obra],
    "timeout_seconds": int(timeout_s),
    "max_rows": 500,
}
headers = {
    "x-functions-key": function_key,
    "Content-Type": "application/json",
}

print(f"Enviando: POST {url}")
print(f"  database   = {database}")
print(f"  parameters = [{cif!r}, {obra!r}]")
print(f"  timeout    = {timeout_s}s")
print(f"  max_rows   = {payload['max_rows']}")
print()

try:
    with httpx.Client(timeout=timeout_s) as client:
        response = client.post(url, json=payload, headers=headers)
except httpx.ConnectTimeout:
    print("❌ TIMEOUT al conectar. Posibles causas:")
    print("   • La URL es incorrecta o la Function App no está arrancada.")
    print("   • Problema de red entre tu equipo y Azure.")
    sys.exit(5)
except httpx.ConnectError as exc:
    print(f"❌ ERROR DE CONEXIÓN: {exc!r}")
    print("   La URL parece no resolverse. Revisa SIGRID_API_BASE_URL.")
    sys.exit(5)
except Exception as exc:
    print(f"❌ EXCEPCIÓN inesperada: {exc!r}")
    sys.exit(5)

print(f"HTTP status: {response.status_code}")
print("Body (primeros 2000 chars):")
print("-" * 70)
print(response.text[:2000])
print("-" * 70)

if response.status_code == 401:
    print("❌ 401 Unauthorized: SIGRID_API_FUNCTION_KEY es incorrecta o expiró.")
    sys.exit(6)
if response.status_code == 404:
    print("❌ 404 Not Found: la ruta /api/sql/read no existe en esa Function App.")
    sys.exit(6)
if response.status_code >= 500:
    print(f"❌ {response.status_code} error de servidor.")
    print("   Mira los logs de la Function App:")
    print("     az functionapp log tail --name <func-name> --resource-group <rg>")
    sys.exit(6)
if response.status_code != 200:
    print(f"❌ Código HTTP inesperado: {response.status_code}")
    sys.exit(6)

try:
    body = response.json()
except json.JSONDecodeError:
    print("❌ La respuesta no es JSON válido.")
    sys.exit(6)

print()
print("Respuesta parseada:")
print(f"  ok         = {body.get('ok')}")
print(f"  database   = {body.get('database')}")
print(f"  columns    = {body.get('columns')}")
print(f"  row_count  = {body.get('row_count')}")
print(f"  truncated  = {body.get('truncated')}")
print()

if not body.get("ok"):
    print("❌ sigrid-api respondió ok=false")
    sys.exit(6)

rows = body.get("rows") or []
columns = body.get("columns") or []

if not rows:
    print(
        f"⚠️  0 filas: no hay contrato con líneas para CIF={cif!r} "
        f"con obra={obra!r} en {database}."
    )
    print()
    print("   Sugerencias para depurar:")
    print("   1) Ejecuta primero el diagnóstico de solo cabecera para")
    print("      ver si el contrato existe pero no tiene líneas:")
    print(f"        python scripts/diagnose_sigrid_contrato.py {cif} {obra}")
    print("   2) Verifica que el CIF existe en prv:")
    print("        SELECT TOP 5 cif, raz FROM prv WHERE cif LIKE '%" + cif[-6:] + "%'")
    print("   3) Verifica que la obra existe:")
    print(f"        python scripts/diagnose_sigrid.py {obra}")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Paso 3: agrupar por contrato y mostrar cabecera + líneas
# ---------------------------------------------------------------------------
# Campos de cabecera (se repiten en cada fila, los deduplicamos)
HEADER_COLS = [
    "contrato_ide", "codigo_contrato", "nombre_contrato",
    "fecha_alta_contrato", "fecha_contrato", "vigencia_desde",
    "vigencia_hasta", "importe_total_contrato", "cif_proveedor",
    "nombre_proveedor", "codigo_obra", "nombre_obra",
]
# Campos de línea (varían por fila)
LINE_COLS = [
    "linea", "numero_linea", "codigo_producto", "codigo_alternativo",
    "unidad_medida", "descripcion_linea", "uds", "cantidad_servida",
    "cantidad_facturada", "pendiente_servir", "precio_unitario",
    "precio_bruto", "descuentos", "importe_linea", "cuota_iva",
    "doc_origen",
]

# Agrupar filas por contrato_ide (puede haber >1 contrato)
contratos: dict[int | str, dict] = {}
for row in rows:
    row_map = dict(zip(columns, row))
    ctr_id = row_map.get("contrato_ide")
    if ctr_id not in contratos:
        contratos[ctr_id] = {
            "header": {k: row_map.get(k) for k in HEADER_COLS},
            "lines": [],
        }
    contratos[ctr_id]["lines"].append(
        {k: row_map.get(k) for k in LINE_COLS}
    )

print(f"✅ {len(rows)} fila(s) devuelta(s) — {len(contratos)} contrato(s)")
print()

for ctr_idx, (ctr_id, data) in enumerate(contratos.items(), start=1):
    hdr = data["header"]
    lines = data["lines"]

    print(f"{'━' * 70}")
    print(f"  CONTRATO {ctr_idx}: {hdr.get('codigo_contrato')}")
    print(f"{'━' * 70}")
    for key in HEADER_COLS:
        print(f"    {key:<28} = {hdr[key]!r}")
    print()
    print(f"  📋 {len(lines)} línea(s) de detalle:")
    print(f"  {'─' * 66}")

    for i, line in enumerate(lines, start=1):
        print(f"    Línea {i}:")
        for key in LINE_COLS:
            print(f"      {key:<24} = {line[key]!r}")
        print()

    # Resumen numérico del contrato
    total_lineas = sum(
        (ln.get("importe_linea") or 0) for ln in lines
    )
    print(f"  Σ importe_linea          = {total_lineas:,.2f}")
    print(f"  importe_total_contrato   = {hdr.get('importe_total_contrato')!r}")
    print()

if len(contratos) > 1:
    print("-" * 70)
    print(
        f"⚠️  Se han encontrado {len(contratos)} contratos para este "
        f"(CIF, obra). Cuando integremos este paso en el pipeline "
        f"tendremos que decidir cuál ganar (criterio provisional: el "
        f"más reciente por fecha_contrato / vigencia, o el que esté "
        f"vigente a fecha del albarán)."
    )

if body.get("truncated"):
    print()
    print(
        f"⚠️  La respuesta está truncada a {payload['max_rows']} filas. "
        f"Puede haber más líneas. Sube el max_rows si necesitas todas."
    )

print()
print("=" * 70)
print(" ✅ TODO OK — la query combinada contrato+líneas responde.")
print(" Los datos de cabecera se repiten en cada fila; el agrupamiento")
print(" por contrato_ide ya te da la estructura {cabecera, líneas[]}.")
print("=" * 70)
