# scripts/diagnose_sigrid_contrato_docs_v2.py
"""Exploración EXHAUSTIVA de documentos de un contrato.

Este script prueba TODAS las vías de vinculación documental en Sigrid
para intentar localizar los PDFs adjuntos a un contrato que NO aparecen
por la vía RCG estándar.

Basándonos en que los documentos PUEDEN estar en:
  - ruesma_rep.gra   (el que ya conocemos)
  - ruesma.dog       (Documentos multimedia, tabla independiente)
  - dog.graima       (binario embebido directamente en dog)

Y las vías oficiales reales según el PDF de tablas son:

  HACIA gra (desde Referencias de gra):
    - rcg.gra
    - PFfir.graide
    - acugra.graide
    - k_acd.graide
    - gra.graant (autoref)

  HACIA dog (desde Referencias de dog):
    - condog.dogide       (N:N concepto-dog)
    - condoppre.dogide    (Documentos presentados en conceptos)
    - condoppredog.dogide (Documentos asociados a presentados)
    - conplidog.dogide    (Documentos de pliegos)
    - PFfir.dogide        (firmas con dog)
    - k_acd.dogide
    - rac.dogide          (contabilización)
    - Movimientos.dogide
    - prvblo/prvces.dogide, etc.

  Campos DIRECTOS en dog (dog → entidad):
    - dog.ctride     → ctr.ide  ⭐ relación directa al contrato
    - dog.conide     → con.ide  ⭐ concepto principal
    - dog.obride     → obr.ide
    - dog.entide     → con.ide (entidad)

FASES del script:

  FASE 1: Localizar el contrato (ctr.ide).
  FASE 2: Probar TODAS las vías de vínculo conocidas.
  FASE 3: Mostrar el inventario completo (gra + dog) encontrado.
  FASE 4: Si hay --find, hace búsqueda inversa desde el nombre.
  FASE 5: Con --download, descarga todo lo que tenga binario.

Ejecutar:
    python scripts/diagnose_sigrid_contrato_docs_v2.py B86359866 0695
    python scripts/diagnose_sigrid_contrato_docs_v2.py B86359866 0695 --download
    python scripts/diagnose_sigrid_contrato_docs_v2.py B86359866 0695 --find "PED1.r__1"

CORRECCIÓN 2026-09-05 — cruce con la base documental:
  Las cuatro vías hacia `gra` cruzaban a `ruesma_rep.dbo.gra` por `ide`
  (`ON rcg.gra = g.ide`) y la FASE 4 metía los `ide` DOCUMENTALES de la
  búsqueda por nombre en `rcg.gra` / `graide`, que son de NEGOCIO. Está
  medido que ambas cosas devuelven documentos ajenos: los `ide` de
  `ruesma.gra` y `ruesma_rep.gra` solo coinciden en 426 filas de 2009.
  La relación real es por **(emp, cod)**, con índice único `gra_empcod` en
  las dos bases. Ahora las vías van `<tabla>.graide → ruesma.gra (g_neg) →
  ruesma_rep.gra (g_rep) ON g_rep.cod = g_neg.cod AND g_rep.emp = g_neg.emp`
  y la FASE 4 traduce documental → negocio con
  `traducir_documentales_a_negocio()`. `_target_ide` de un `gra` es el ide
  DOCUMENTAL (el que acepta `documents/read`); `_neg_ide`, el de negocio.
  Las vías hacia `dog` no cambian: `dog` vive en `ruesma` con su propio
  binario. Ver `sigrid-api/progress/explore_F-004_relacion_gra.md`.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from collections import Counter


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------
def load_dotenv_manually(env_path: Path) -> dict[str, str]:
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
print(" DIAGNÓSTICO SIGRID v2 — EXPLORACIÓN EXHAUSTIVA (gra + dog)")
print("=" * 70)
env = load_dotenv_manually(_ENV_PATH)


def get_cfg(name: str) -> str | None:
    return os.environ.get(name) or env.get(name)


base_url = get_cfg("SIGRID_API_BASE_URL")
function_key = get_cfg("SIGRID_API_FUNCTION_KEY")
database = get_cfg("SIGRID_API_DATABASE") or "ruesma"
database_rep = get_cfg("SIGRID_API_DATABASE_REP") or "ruesma_rep"
timeout_s = float(get_cfg("SIGRID_API_TIMEOUT_S") or "30")

if not base_url or not function_key:
    print("❌ Faltan SIGRID_API_BASE_URL o SIGRID_API_FUNCTION_KEY")
    sys.exit(2)

print(f"  database     = {database!r}")
print(f"  database_rep = {database_rep!r}")
print("-" * 70)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def normalize_obra_code(raw: str | None) -> str | None:
    if raw is None:
        return None
    c = str(raw).strip()
    if not c or not c.isdigit():
        return None
    if len(c) == 3:
        return "0" + c
    if len(c) == 4 and c.startswith("0"):
        return c
    return None


def normalize_cif(raw: str | None) -> str | None:
    if raw is None:
        return None
    c = str(raw).strip().upper().replace(" ", "")
    return c or None


def sanitize_filename(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', '_', name)
    return s.strip().rstrip('.') or "documento"


try:
    import httpx
except ImportError:
    print("❌ pip install httpx")
    sys.exit(4)


# Parse args
positional_args: list[str] = []
flag_download = False
find_query: str | None = None

args_iter = iter(sys.argv[1:])
for arg in args_iter:
    if arg == "--download":
        flag_download = True
    elif arg == "--find":
        find_query = next(args_iter, None)
    else:
        positional_args.append(arg)

cif_input = positional_args[0] if len(positional_args) > 0 else "B86359866"
obra_input = positional_args[1] if len(positional_args) > 1 else "0695"

cif = normalize_cif(cif_input)
obra = normalize_obra_code(obra_input)

print(f"Parámetros:  cif={cif!r}  obra={obra!r}  "
      f"--download={flag_download}  --find={find_query!r}")
if not cif or not obra:
    print("❌ CIF u obra inválidos.")
    sys.exit(3)
print("-" * 70)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def run_query(
    label: str, sql: str, parameters: list,
    max_rows: int = 500, target_database: str | None = None,
    verbose: bool = True,
) -> dict | None:
    db = target_database or database
    url = f"{base_url.rstrip('/')}/api/sql/read"
    payload = {
        "database": db, "sql": sql, "parameters": parameters,
        "timeout_seconds": int(timeout_s), "max_rows": max_rows,
    }
    headers = {"x-functions-key": function_key, "Content-Type": "application/json"}
    if verbose:
        print(f"  📡 {label}")
    try:
        with httpx.Client(timeout=timeout_s) as c:
            r = c.post(url, json=payload, headers=headers)
    except Exception as exc:
        print(f"    ❌ {exc!r}")
        return None
    if r.status_code != 200:
        print(f"    ❌ HTTP {r.status_code}: {r.text[:200]}")
        return None
    try:
        body = r.json()
    except json.JSONDecodeError:
        return None
    if not body.get("ok"):
        if verbose:
            print(f"    ❌ ok=false: {json.dumps(body, ensure_ascii=False)[:200]}")
        return None
    rc = body.get("row_count", 0)
    if verbose:
        print(f"    ✅ {rc}")
    return body


def rows_to_dicts(body: dict | None) -> list[dict]:
    if not body or not body.get("rows"):
        return []
    cols = body["columns"]
    return [dict(zip(cols, row)) for row in body["rows"]]


def traducir_documentales_a_negocio(gra_rep_rows: list[dict]) -> dict[int, dict]:
    """Traduce filas de `ruesma_rep.gra` a su fila de `ruesma.gra`.

    La correspondencia es por `(emp, cod)`, nunca por `ide` (ver la
    CORRECCIÓN 2026-09-05 de la cabecera). Devuelve
    `{gra_rep_ide: {"neg_ide": int|None, "emp": ..., "cod": ...}}`.
    `neg_ide` es None cuando la documental no tiene pareja en negocio: hay
    76.187 así y muchas son legítimas (otros módulos, altas borradas en
    negocio), por eso se conservan en la salida en vez de descartarlas.
    """
    vacio = {r["ide"]: {"neg_ide": None, "emp": r.get("emp"), "cod": r.get("cod")}
             for r in gra_rep_rows}
    cods = sorted({r.get("cod") for r in gra_rep_rows if r.get("cod")})
    if not cods:
        return vacio
    marcadores = ",".join("?" for _ in cods)
    body = run_query(
        f"Traducir {len(cods)} cod documental(es) a ruesma.gra por (emp, cod)",
        f"""\
SELECT ide AS gra_neg_ide, emp AS gra_emp, cod AS gra_cod
FROM gra
WHERE cod IN ({marcadores})
""", list(cods), max_rows=500)
    por_clave = {(r.get("gra_emp"), r.get("gra_cod")): r["gra_neg_ide"]
                 for r in rows_to_dicts(body)}
    for datos in vacio.values():
        datos["neg_ide"] = por_clave.get((datos["emp"], datos["cod"]))
    return vacio


def download_doc(store: str, blob_column: str, target_db: str,
                 table: str, ide: int, filename_cols: list[str],
                 out_dir: Path, fallback_name: str) -> bool:
    url = f"{base_url.rstrip('/')}/api/documents/read"
    payload = {
        "database": target_db, "schema": "dbo", "table": table,
        "id_column": "ide", "id_value": ide,
        "blob_column": blob_column,
        "filename_columns": filename_cols,
        "disposition": "attachment",
    }
    headers = {"x-functions-key": function_key, "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=120) as c:
            r = c.post(url, json=payload, headers=headers)
    except Exception as exc:
        print(f"    ❌ {exc!r}")
        return False
    if r.status_code != 200:
        print(f"    ❌ HTTP {r.status_code}: {r.text[:200]}")
        return False
    if not r.content:
        print(f"    ❌ Respuesta vacía")
        return False
    fname = sanitize_filename(r.headers.get("X-Document-Filename", fallback_name))
    out_path = out_dir / fname
    if out_path.exists():
        out_path = out_dir / f"{out_path.stem}_{store}{ide}{out_path.suffix}"
    out_path.write_bytes(r.content)
    ct = r.headers.get("Content-Type", "?")
    print(f"    ✅ {out_path.name} ({len(r.content):,} bytes, {ct})")
    return True


# ═══════════════════════════════════════════════════════════════════════════
# FASE 1: Localizar contrato
# ═══════════════════════════════════════════════════════════════════════════
print(f"\n{'━' * 70}")
print(f"  FASE 1: Localizar contrato")
print(f"{'━' * 70}")

body = run_query("Q1: buscar ctr por CIF+obra", """\
SELECT ctr.ide AS ctr_ide, con_ctr.cod AS ctr_cod, con_ctr.res AS ctr_res,
       ctr.obride AS obr_ide, con_obr.cod AS obr_cod, con_obr.res AS obr_res,
       ctr.entide AS prv_ide, prv.cif AS prv_cif, prv.raz AS prv_raz
FROM ctr
JOIN con AS con_ctr ON ctr.ide = con_ctr.ide
JOIN con AS con_obr ON ctr.obride = con_obr.ide
JOIN prv ON ctr.entide = prv.ide
WHERE prv.cif = ? AND con_obr.cod = ?
""", [cif, obra], max_rows=20)

contratos = rows_to_dicts(body)
if not contratos:
    print(f"\n⚠️  No hay contrato para CIF={cif} obra={obra}")
    sys.exit(0)

for c in contratos:
    print(f"\n  ✅ Contrato localizado:")
    print(f"     ctr.ide        = {c['ctr_ide']}")
    print(f"     contrato cod   = {c['ctr_cod']!r}")
    print(f"     obra.ide       = {c['obr_ide']}")
    print(f"     obra.cod       = {c['obr_cod']!r}")
    print(f"     proveedor.ide  = {c['prv_ide']}")

ctr_ide = contratos[0]["ctr_ide"]
ctr_cod = contratos[0]["ctr_cod"]
obr_ide = contratos[0]["obr_ide"]
prv_ide = contratos[0]["prv_ide"]
ides_csv = str(ctr_ide)


# ═══════════════════════════════════════════════════════════════════════════
# FASE 2: Probar TODAS las vías
# ═══════════════════════════════════════════════════════════════════════════
print(f"\n{'━' * 70}")
print(f"  FASE 2: Probando TODAS las vías de vinculación")
print(f"         ctr.ide = {ctr_ide}")
print(f"{'━' * 70}\n")

# Cada vínculo será un dict con: _via, _store (gra|dog), _target_ide,
# _name, _size, _extra
all_links: list[dict] = []


# -----------------------------------------------------------------------
# Vías hacia GRA
# -----------------------------------------------------------------------

# V1: rcg
body = run_query("V1: rcg.gra", f"""\
SELECT g_neg.ide AS gra_neg_ide, g_rep.ide AS gra_rep_ide,
       rcg.cla AS cla, rcg.pos AS pos,
       g_neg.nom AS nom, g_neg.nomori AS nomori,
       g_rep.nom AS rep_nom, g_rep.nomori AS rep_nomori,
       g_neg.cod AS cod, g_neg.emp AS emp, g_neg.fec AS fec,
       DATALENGTH(g_rep.ima) AS ima_bytes
FROM rcg
LEFT JOIN gra AS g_neg ON rcg.gra = g_neg.ide
LEFT JOIN {database_rep}.dbo.gra AS g_rep
       ON g_rep.cod = g_neg.cod AND g_rep.emp = g_neg.emp
WHERE rcg.con = ?
""", [ctr_ide], max_rows=100)
for r in rows_to_dicts(body):
    all_links.append({
        "_via": "rcg", "_store": "gra",
        "_target_ide": r.get("gra_rep_ide") or 0,
        "_neg_ide": r.get("gra_neg_ide"),
        "_name": (r.get("nomori") or r.get("nom")
                  or r.get("rep_nomori") or r.get("rep_nom") or "?"),
        "_cod": r.get("cod"), "_fec": r.get("fec"),
        "_size": r.get("ima_bytes") or 0,
        "_extra": f"cla={r.get('cla')} pos={r.get('pos')}",
    })

# V2: PFfir.graide
body = run_query("V2: PFfir.graide", f"""\
SELECT g_neg.ide AS gra_neg_ide, g_rep.ide AS gra_rep_ide,
       PFfir.tipfir, PFfir.estfir,
       g_neg.nom AS nom, g_neg.nomori AS nomori,
       g_rep.nom AS rep_nom, g_rep.nomori AS rep_nomori,
       g_neg.cod AS cod, g_neg.emp AS emp, g_neg.fec AS fec,
       DATALENGTH(g_rep.ima) AS ima_bytes
FROM PFfir
LEFT JOIN gra AS g_neg ON PFfir.graide = g_neg.ide
LEFT JOIN {database_rep}.dbo.gra AS g_rep
       ON g_rep.cod = g_neg.cod AND g_rep.emp = g_neg.emp
WHERE PFfir.conide = ? AND PFfir.graide IS NOT NULL AND PFfir.graide <> 0
""", [ctr_ide], max_rows=100)
for r in rows_to_dicts(body):
    all_links.append({
        "_via": "PFfir→gra", "_store": "gra",
        "_target_ide": r.get("gra_rep_ide") or 0,
        "_neg_ide": r.get("gra_neg_ide"),
        "_name": (r.get("nomori") or r.get("nom")
                  or r.get("rep_nomori") or r.get("rep_nom") or "?"),
        "_cod": r.get("cod"), "_fec": r.get("fec"),
        "_size": r.get("ima_bytes") or 0,
        "_extra": f"tipfir={r.get('tipfir')} estfir={r.get('estfir')}",
    })

# V3: acugra
body = run_query("V3: acugra.graide", f"""\
SELECT g_neg.ide AS gra_neg_ide, g_rep.ide AS gra_rep_ide,
       g_neg.nom AS nom, g_neg.nomori AS nomori,
       g_rep.nom AS rep_nom, g_rep.nomori AS rep_nomori,
       g_neg.cod AS cod, g_neg.emp AS emp, g_neg.fec AS fec,
       DATALENGTH(g_rep.ima) AS ima_bytes
FROM acugra
LEFT JOIN gra AS g_neg ON acugra.graide = g_neg.ide
LEFT JOIN {database_rep}.dbo.gra AS g_rep
       ON g_rep.cod = g_neg.cod AND g_rep.emp = g_neg.emp
WHERE acugra.acuide = ?
""", [ctr_ide], max_rows=100)
for r in rows_to_dicts(body):
    all_links.append({
        "_via": "acugra", "_store": "gra",
        "_target_ide": r.get("gra_rep_ide") or 0,
        "_neg_ide": r.get("gra_neg_ide"),
        "_name": (r.get("nomori") or r.get("nom")
                  or r.get("rep_nomori") or r.get("rep_nom") or "?"),
        "_cod": r.get("cod"), "_fec": r.get("fec"),
        "_size": r.get("ima_bytes") or 0,
        "_extra": "",
    })

# V4: k_acd.graide
body = run_query("V4: k_acd.graide", f"""\
SELECT g_neg.ide AS gra_neg_ide, g_rep.ide AS gra_rep_ide,
       g_neg.nom AS nom, g_neg.nomori AS nomori,
       g_rep.nom AS rep_nom, g_rep.nomori AS rep_nomori,
       g_neg.cod AS cod, g_neg.emp AS emp, g_neg.fec AS fec,
       DATALENGTH(g_rep.ima) AS ima_bytes
FROM k_acd
LEFT JOIN gra AS g_neg ON k_acd.graide = g_neg.ide
LEFT JOIN {database_rep}.dbo.gra AS g_rep
       ON g_rep.cod = g_neg.cod AND g_rep.emp = g_neg.emp
WHERE k_acd.aceide = ?
""", [ctr_ide], max_rows=100)
for r in rows_to_dicts(body):
    all_links.append({
        "_via": "k_acd→gra", "_store": "gra",
        "_target_ide": r.get("gra_rep_ide") or 0,
        "_neg_ide": r.get("gra_neg_ide"),
        "_name": (r.get("nomori") or r.get("nom")
                  or r.get("rep_nomori") or r.get("rep_nom") or "?"),
        "_cod": r.get("cod"), "_fec": r.get("fec"),
        "_size": r.get("ima_bytes") or 0,
        "_extra": "",
    })


# -----------------------------------------------------------------------
# Vías hacia DOG
# -----------------------------------------------------------------------

# V5: condog (N:N concepto-dog)
body = run_query("V5: condog", """\
SELECT condog.dogide AS dog_ide, condog.cla, condog.pos,
       dog.granom, dog.nomori, dog.codrep, dog.fec,
       dog.auxdopide, auxdop.cod AS auxdop_cod, auxdop.res AS auxdop_res,
       DATALENGTH(dog.graima) AS graima_bytes
FROM condog
JOIN dog ON condog.dogide = dog.ide
LEFT JOIN auxdop ON dog.auxdopide = auxdop.ide
WHERE condog.conide = ?
""", [ctr_ide], max_rows=100)
for r in rows_to_dicts(body):
    all_links.append({
        "_via": "condog", "_store": "dog", "_target_ide": r["dog_ide"],
        "_name": r.get("nomori") or r.get("granom") or "?",
        "_cod": r.get("codrep"), "_fec": r.get("fec"),
        "_size": r.get("graima_bytes") or 0,
        "_extra": f"cla={r.get('cla')} auxdop={r.get('auxdop_cod')!r}({r.get('auxdop_res')!r})",
    })

# V6: condoppre (Documentos presentados en conceptos)
body = run_query("V6: condoppre", """\
SELECT condoppre.dogide AS dog_ide,
       condoppre.dopide, condoppre.cod AS condoppre_cod, condoppre.fec,
       condoppre.obride AS condoppre_obride,
       auxdop.cod AS auxdop_cod, auxdop.res AS auxdop_res,
       dog.granom, dog.nomori, dog.codrep, dog.fec AS dog_fec,
       DATALENGTH(dog.graima) AS graima_bytes
FROM condoppre
LEFT JOIN auxdop ON condoppre.dopide = auxdop.ide
LEFT JOIN dog ON condoppre.dogide = dog.ide
WHERE condoppre.conide = ?
""", [ctr_ide], max_rows=100)
for r in rows_to_dicts(body):
    all_links.append({
        "_via": "condoppre", "_store": "dog", "_target_ide": r.get("dog_ide"),
        "_name": r.get("nomori") or r.get("granom") or r.get("condoppre_cod") or "?",
        "_cod": r.get("codrep") or r.get("condoppre_cod"),
        "_fec": r.get("dog_fec") or r.get("fec"),
        "_size": r.get("graima_bytes") or 0,
        "_extra": f"auxdop={r.get('auxdop_cod')!r}({r.get('auxdop_res')!r}) obride={r.get('condoppre_obride')}",
    })

# V7: condoppredog (Documentos asociados a documentos presentados)
body = run_query("V7: condoppredog vía condoppre", """\
SELECT condoppredog.dogide AS dog_ide,
       condoppredog.pos, condoppredog.fec,
       condoppre.cod AS condoppre_cod,
       dog.granom, dog.nomori, dog.codrep,
       DATALENGTH(dog.graima) AS graima_bytes
FROM condoppredog
JOIN condoppre ON condoppredog.doppreide = condoppre.ide
LEFT JOIN dog ON condoppredog.dogide = dog.ide
WHERE condoppre.conide = ?
""", [ctr_ide], max_rows=100)
for r in rows_to_dicts(body):
    all_links.append({
        "_via": "condoppredog", "_store": "dog", "_target_ide": r["dog_ide"],
        "_name": r.get("nomori") or r.get("granom") or "?",
        "_cod": r.get("codrep"), "_fec": r.get("fec"),
        "_size": r.get("graima_bytes") or 0,
        "_extra": f"via_condoppre_cod={r.get('condoppre_cod')!r}",
    })

# V8: dog.ctride (DIRECTO al contrato)
body = run_query("V8: dog.ctride (directo)", """\
SELECT dog.ide AS dog_ide,
       dog.granom, dog.nomori, dog.codrep, dog.fec,
       dog.auxdopide, auxdop.cod AS auxdop_cod, auxdop.res AS auxdop_res,
       DATALENGTH(dog.graima) AS graima_bytes
FROM dog
LEFT JOIN auxdop ON dog.auxdopide = auxdop.ide
WHERE dog.ctride = ?
""", [ctr_ide], max_rows=100)
for r in rows_to_dicts(body):
    all_links.append({
        "_via": "dog.ctride", "_store": "dog", "_target_ide": r["dog_ide"],
        "_name": r.get("nomori") or r.get("granom") or "?",
        "_cod": r.get("codrep"), "_fec": r.get("fec"),
        "_size": r.get("graima_bytes") or 0,
        "_extra": f"auxdop={r.get('auxdop_cod')!r}({r.get('auxdop_res')!r})",
    })

# V9: dog.conide (DIRECTO al concepto-contrato)
body = run_query("V9: dog.conide (directo)", """\
SELECT dog.ide AS dog_ide,
       dog.granom, dog.nomori, dog.codrep, dog.fec,
       dog.auxdopide, auxdop.cod AS auxdop_cod, auxdop.res AS auxdop_res,
       DATALENGTH(dog.graima) AS graima_bytes
FROM dog
LEFT JOIN auxdop ON dog.auxdopide = auxdop.ide
WHERE dog.conide = ?
""", [ctr_ide], max_rows=100)
for r in rows_to_dicts(body):
    all_links.append({
        "_via": "dog.conide", "_store": "dog", "_target_ide": r["dog_ide"],
        "_name": r.get("nomori") or r.get("granom") or "?",
        "_cod": r.get("codrep"), "_fec": r.get("fec"),
        "_size": r.get("graima_bytes") or 0,
        "_extra": f"auxdop={r.get('auxdop_cod')!r}({r.get('auxdop_res')!r})",
    })

# V10: dog.entide (DIRECTO como entidad)
body = run_query("V10: dog.entide (directo)", """\
SELECT dog.ide AS dog_ide,
       dog.granom, dog.nomori, dog.codrep, dog.fec,
       dog.auxdopide, auxdop.cod AS auxdop_cod, auxdop.res AS auxdop_res,
       DATALENGTH(dog.graima) AS graima_bytes
FROM dog
LEFT JOIN auxdop ON dog.auxdopide = auxdop.ide
WHERE dog.entide = ?
""", [ctr_ide], max_rows=100)
for r in rows_to_dicts(body):
    all_links.append({
        "_via": "dog.entide", "_store": "dog", "_target_ide": r["dog_ide"],
        "_name": r.get("nomori") or r.get("granom") or "?",
        "_cod": r.get("codrep"), "_fec": r.get("fec"),
        "_size": r.get("graima_bytes") or 0,
        "_extra": f"auxdop={r.get('auxdop_cod')!r}({r.get('auxdop_res')!r})",
    })


# ═══════════════════════════════════════════════════════════════════════════
# FASE 3: Resumen de lo encontrado
# ═══════════════════════════════════════════════════════════════════════════
print(f"\n{'━' * 70}")
print(f"  FASE 3: Resumen de vínculos")
print(f"{'━' * 70}\n")

via_counts = Counter(l["_via"] for l in all_links)
print(f"  Total vínculos: {len(all_links)}")
print(f"\n  Desglose por vía:")
for via in ["rcg", "PFfir→gra", "acugra", "k_acd→gra",
            "condog", "condoppre", "condoppredog",
            "dog.ctride", "dog.conide", "dog.entide"]:
    count = via_counts.get(via, 0)
    marker = "✅" if count > 0 else "  "
    print(f"    {marker} {via:<20} {count}")

# Deduplicar por (store, target_ide, neg_ide) acumulando vías. El ide de
# negocio entra en la clave porque `_target_ide` vale 0 en los gra sin
# pareja documental y, sin él, dos documentos distintos colapsarían.
dedup: dict[tuple, dict] = {}
for link in all_links:
    key = (link["_store"], link["_target_ide"], link.get("_neg_ide"))
    if key in dedup:
        if link["_via"] not in dedup[key]["_via"]:
            dedup[key]["_via"] = f"{dedup[key]['_via']} + {link['_via']}"
    else:
        dedup[key] = dict(link)
unique_docs = list(dedup.values())

print(f"\n  Documentos únicos (tras deduplicar): {len(unique_docs)}")

docs_con_binario: list[dict] = []
for i, d in enumerate(unique_docs, start=1):
    size = d["_size"]
    sz = f"{size/1024:.0f} KB" if 0 < size < 1024*1024 else (
        f"{size/(1024*1024):.1f} MB" if size >= 1024*1024 else "SIN BINARIO"
    )
    print(f"\n  Doc {i}:  vías={d['_via']}")
    if d["_store"] == "gra":
        ide_txt = (f"gra_rep_ide={d['_target_ide'] or '—'}  "
                   f"gra_neg_ide={d.get('_neg_ide') or '—'}")
    else:
        ide_txt = f"ide={d['_target_ide']}"
    print(f"    store           = {d['_store']}  ({ide_txt})")
    print(f"    nombre          = {d['_name']!r}")
    print(f"    cod             = {d.get('_cod')!r}")
    print(f"    fec             = {d.get('_fec')}")
    print(f"    extra           = {d['_extra']}")
    print(f"    tamaño          = {sz}")
    if size > 0:
        docs_con_binario.append(d)


# ═══════════════════════════════════════════════════════════════════════════
# FASE 4: Búsqueda inversa por nombre (si --find)
# ═══════════════════════════════════════════════════════════════════════════
if find_query:
    print(f"\n{'━' * 70}")
    print(f"  FASE 4: Búsqueda inversa por nombre {find_query!r}")
    print(f"{'━' * 70}\n")

    # Buscar en gra (ruesma_rep)
    body = run_query("Find en ruesma_rep.gra", """\
SELECT TOP 20 ide, emp, cod, nom, nomori, fec,
       DATALENGTH(ima) AS ima_bytes
FROM gra
WHERE nom LIKE ? OR nomori LIKE ? OR cod LIKE ?
ORDER BY fec DESC
""", [f"%{find_query}%", f"%{find_query}%", f"%{find_query}%"],
        max_rows=20, target_database=database_rep)
    gra_found = rows_to_dicts(body)
    print(f"\n  Encontrados en ruesma_rep.gra: {len(gra_found)} "
          f"(ides DOCUMENTALES)")
    for d in gra_found:
        print(f"    gra_rep_ide={d['ide']}  nom={d.get('nom')!r}  "
              f"fec={d.get('fec')}")

    # Buscar en dog (ruesma)
    body = run_query("Find en ruesma.dog", """\
SELECT TOP 20 ide, codrep, granom, nomori, fec,
       ctride, conide, obride, entide, auxdopide,
       DATALENGTH(graima) AS graima_bytes
FROM dog
WHERE granom LIKE ? OR nomori LIKE ? OR codrep LIKE ?
ORDER BY fec DESC
""", [f"%{find_query}%", f"%{find_query}%", f"%{find_query}%"], max_rows=20)
    dog_found = rows_to_dicts(body)
    print(f"\n  Encontrados en ruesma.dog: {len(dog_found)}")
    for d in dog_found:
        print(f"    dog.ide={d['ide']}  nom={d.get('nomori') or d.get('granom')!r}")
        print(f"      ctride={d.get('ctride')}  conide={d.get('conide')}  "
              f"obride={d.get('obride')}  entide={d.get('entide')}")
        print(f"      auxdopide={d.get('auxdopide')}  codrep={d.get('codrep')!r}")

    # Para cada gra.ide encontrado, buscar en qué tablas está vinculado
    if gra_found:
        # `rcg.gra` y los `graide` son ides de NEGOCIO: hay que traducir
        # los documentales por (emp, cod) antes de buscar los vínculos.
        rep_a_neg = traducir_documentales_a_negocio(gra_found)
        neg_a_rep = {v["neg_ide"]: k for k, v in rep_a_neg.items()
                     if v["neg_ide"]}
        sin_negocio = [(k, v) for k, v in rep_a_neg.items()
                       if not v["neg_ide"]]
        print(f"\n  ↔️  {len(neg_a_rep)}/{len(gra_found)} con fila de negocio "
              f"por (emp, cod); {len(sin_negocio)} sin fila de negocio")
        for rep_ide, datos in sin_negocio:
            print(f"    gra_rep_ide={rep_ide}  cod={datos['cod']!r}  "
                  f"emp={datos['emp']}  — sin fila de negocio")

        gra_ides_csv = ",".join(str(i) for i in neg_a_rep) or "0"

        print(f"\n  🔗 Vínculos inversos desde gra (por ide de negocio):")
        for via_name, sql in [
            ("rcg", f"SELECT gra AS gra_ide, con AS ref_ide, cla FROM rcg WHERE gra IN ({gra_ides_csv})"),
            ("PFfir", f"SELECT graide AS gra_ide, conide AS ref_ide, tipfir, estfir FROM PFfir WHERE graide IN ({gra_ides_csv})"),
            ("acugra", f"SELECT graide AS gra_ide, acuide AS ref_ide FROM acugra WHERE graide IN ({gra_ides_csv})"),
            ("k_acd", f"SELECT graide AS gra_ide, aceide AS ref_ide FROM k_acd WHERE graide IN ({gra_ides_csv})"),
        ]:
            body = run_query(f"  Inv.{via_name}", sql, [], max_rows=50, verbose=False)
            rows = rows_to_dicts(body)
            if rows:
                print(f"\n    {via_name}: {len(rows)} vínculo(s)")
                for r in rows:
                    # Resolver el concepto de destino
                    body_ctx = run_query(
                        "",
                        """SELECT c.ide, c.cod, c.res, c.tip,
                           ctr.ide AS is_ctr, dcf.ide AS is_dcf,
                           obr.ide AS is_obr, prv.ide AS is_prv
                           FROM con c
                           LEFT JOIN ctr ON c.ide = ctr.ide
                           LEFT JOIN dcf ON c.ide = dcf.ide
                           LEFT JOIN obr ON c.ide = obr.ide
                           LEFT JOIN prv ON c.ide = prv.ide
                           WHERE c.ide = ?""",
                        [r["ref_ide"]], max_rows=1, verbose=False,
                    )
                    ctx_rows = rows_to_dicts(body_ctx)
                    if ctx_rows:
                        c = ctx_rows[0]
                        tipo = ("CTR" if c.get("is_ctr") else
                                "DCF" if c.get("is_dcf") else
                                "OBR" if c.get("is_obr") else
                                "PRV" if c.get("is_prv") else
                                f"tip={c.get('tip')}")
                        print(f"      gra_rep_ide={neg_a_rep.get(r['gra_ide'])} "
                              f"(negocio {r['gra_ide']}) → con.ide={r['ref_ide']} "
                              f"cod={c.get('cod')!r} [{tipo}]")
                    else:
                        print(f"      gra_rep_ide={neg_a_rep.get(r['gra_ide'])} "
                              f"(negocio {r['gra_ide']}) → ide={r['ref_ide']} "
                              f"(sin con)")
            else:
                print(f"\n    {via_name}: 0")


# ═══════════════════════════════════════════════════════════════════════════
# FASE 5: Descarga
# ═══════════════════════════════════════════════════════════════════════════
if docs_con_binario and flag_download:
    download_dir = Path.home() / "Downloads" / "sigrid_docs" / sanitize_filename(ctr_cod)
    download_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'━' * 70}")
    print(f"  FASE 5: Descargando {len(docs_con_binario)} documento(s)")
    print(f"  📁 {download_dir}")
    print(f"{'━' * 70}\n")

    ok = 0
    for idx, d in enumerate(docs_con_binario, start=1):
        ide = d["_target_ide"]
        name = d.get("_name") or f"doc_{ide}"
        store = d["_store"]
        print(f"  [{idx}/{len(docs_con_binario)}] {store}.ide={ide}  vía={d['_via']}")
        if store == "gra":
            ok_dl = download_doc("gra", "ima", database_rep, "gra", ide,
                                 ["nomori", "nom"], download_dir, name)
        else:
            ok_dl = download_doc("dog", "graima", database, "dog", ide,
                                 ["nomori", "granom"], download_dir, name)
        if ok_dl:
            ok += 1

    print(f"\n  📊 {ok}/{len(docs_con_binario)} descargados")
    print(f"  📁 {download_dir}")

elif docs_con_binario:
    print(f"\n  ℹ️  {len(docs_con_binario)} doc(s) descargables. Usa --download.")

print()
print("=" * 70)
print(" Resumen de vías PROBADAS:")
print("   gra: rcg, PFfir, acugra, k_acd")
print("   dog: condog, condoppre, condoppredog,")
print("        dog.ctride (directo), dog.conide (directo), dog.entide (directo)")
print("=" * 70)
