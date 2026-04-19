# scripts/diagnose_sigrid_contrato.py
"""Diagnóstico standalone para la búsqueda de CONTRATO en sigrid-api.

Prepara el siguiente paso del pipeline de enriquecimiento: localizar el
contrato del proveedor en la obra, y traer sus datos (fechas, importe,
CIF, nombre). La query cruza ``ctr`` con ``con`` (dos veces) y con
``prv`` para filtrar por CIF + código de obra.

Ejecuta desde la terminal de PyCharm, en la raíz del servicio 3
(albaranes-persistencia):

    python scripts/diagnose_sigrid_contrato.py

Con argumentos (CIF y/o código de obra):

    python scripts/diagnose_sigrid_contrato.py B86359866
    python scripts/diagnose_sigrid_contrato.py B86359866 0695
    python scripts/diagnose_sigrid_contrato.py B86359866 695    (se normaliza)

Valores por defecto: CIF = B86359866 y obra = 0695 (los del ejemplo).

Este script NO depende de que el servicio 3 arranque ni de que hayas
aplicado parches. Solo necesita el `.env` del servicio con las variables
SIGRID_API_BASE_URL / SIGRID_API_FUNCTION_KEY / SIGRID_API_DATABASE y
acceso a internet para alcanzar la Function App.

Sigue el mismo patrón y la misma UX que ``diagnose_sigrid.py``.
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
print(" DIAGNÓSTICO SIGRID-API — CONTRATO (proveedor × obra)")
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
    """Limpieza mínima del CIF: trim y mayúsculas, sin espacios intermedios.

    No validamos el formato oficial (letra + 8 dígitos, etc.) porque la
    BBDD puede tener CIFs con formatos variados (NIF, NIE, DNI). Dejamos
    la decisión de qué es válido al AI/revisor humano.
    """
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
    print("   Ejemplo válido: python scripts/diagnose_sigrid_contrato.py B86359866 0695")
    sys.exit(3)
print("-" * 70)


# ---------------------------------------------------------------------------
# Paso 2: POST a sigrid-api con la query de contrato
# ---------------------------------------------------------------------------
try:
    import httpx
except ImportError:
    print("❌ Falta el paquete 'httpx'. Instálalo con:")
    print("     pip install httpx")
    sys.exit(4)

# Query tal cual la pidió el usuario, parametrizada con ? para pyodbc.
# ORDEN de los parámetros = orden de los ? en el SQL (primero CIF, luego obra).
SQL_QUERY = """\
SELECT
    con_ctr.cod         AS codigo_contrato,
    con_ctr.res         AS nombre_contrato,
    con_ctr.fec         AS fecha_alta_contrato,
    ctr.fecdoc          AS fecha_contrato,
    ctr.fecvig1         AS vigencia_desde,
    ctr.fecvig2         AS vigencia_hasta,
    ctr.tot             AS importe_total,
    ctr.entcif          AS cif_proveedor,
    ctr.entres          AS nombre_proveedor,
    con_obr.cod         AS codigo_obra,
    con_obr.res         AS nombre_obra
FROM ctr
JOIN con AS con_ctr  ON ctr.ide     = con_ctr.ide
JOIN con AS con_obr  ON ctr.obride  = con_obr.ide
JOIN prv             ON ctr.entide  = prv.ide
WHERE
    prv.cif     = ?
AND con_obr.cod = ?
"""

url = f"{base_url.rstrip('/')}/api/sql/read"
payload = {
    "database": database,
    "sql": SQL_QUERY,
    "parameters": [cif, obra],
    "timeout_seconds": int(timeout_s),
    "max_rows": 10,
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
if not rows:
    print(
        f"⚠️  0 filas: no hay contrato para CIF={cif!r} "
        f"con obra={obra!r} en {database}."
    )
    print()
    print("   Sugerencias para depurar:")
    print("   1) Verifica que el CIF existe en prv:")
    print("        python scripts/diagnose_sigrid_contrato.py <CIF>")
    print("        (usando una obra seguro válida como 0695)")
    print("   2) Verifica que la obra existe (con diagnose_sigrid.py):")
    print(f"        python scripts/diagnose_sigrid.py {obra}")
    print("   3) Comprueba en SSMS / Azure Data Studio:")
    print("        SELECT TOP 5 cif FROM prv WHERE cif LIKE '%" + cif[-6:] + "%'")
    print("        SELECT TOP 5 ctr.ide, con_obr.cod")
    print("        FROM ctr JOIN con con_obr ON ctr.obride = con_obr.ide")
    print(f"        WHERE con_obr.cod = '{obra}'")
    sys.exit(0)

print(f"✅ {len(rows)} fila(s) devuelta(s)")
print()
columns = body.get("columns") or []
for i, row in enumerate(rows, start=1):
    row_map = dict(zip(columns, row))
    print(f"  Contrato {i}:")
    for key, value in row_map.items():
        print(f"    {key:<24} = {value!r}")
    print()

if len(rows) > 1:
    print("-" * 70)
    print(
        f"⚠️  Se han encontrado {len(rows)} contratos para este "
        f"(CIF, obra). Cuando integremos este paso en el pipeline "
        f"tendremos que decidir cuál ganar (criterio provisional: el "
        f"más reciente por fecha_contrato / vigencia, o el que esté "
        f"vigente a fecha del albarán)."
    )

print()
print("=" * 70)
print(" ✅ TODO OK — la query de contrato responde y devuelve datos.")
print(" Con este formato de respuesta ya se puede integrar en el servicio 3")
print(" como un nuevo step de enriquecimiento después del de obra.")
print("=" * 70)