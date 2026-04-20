# scripts/diagnose_sigrid_contrato_gra.py
"""Extrae cabecera, líneas y PDFs de un contrato de compra en Sigrid.

Cadena de relaciones confirmada:

  CABECERA:
    prv.cif → prv.ide = ctr.entide
    ctr.obride → obr.ide = con.ide  (filtrar por con.cod = código obra)
    ctr.ide = con.ide → con.cod (código contrato), con.res (nombre)
    ctr.totbas = importe total sin IVA

  LÍNEAS:
    ctr.ide → ctrpro.docide
    ctrpro.paride → obrparpar.ide → obrparpar.cod (código partida)

  DOCUMENTOS (PDF del contrato):
    ctr.ide → rcg.con = ctr.ide → rcg.gra = ruesma.gra.ide
    ruesma.gra.cod → ruesma_rep.gra.cod → ruesma_rep.gra.ide
    Descarga vía POST /api/documents/read (solo PDFs)

Uso:
    python scripts/diagnose_sigrid_contrato_gra.py B86359866 0695
    python scripts/diagnose_sigrid_contrato_gra.py B86359866 0695 --download
    python scripts/diagnose_sigrid_contrato_gra.py --download-ide 274282
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


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
print(" SIGRID — CONTRATO: cabecera + líneas + PDFs")
print("=" * 70)
env = load_dotenv_manually(_ENV_PATH)


def get_cfg(name: str) -> str | None:
    return os.environ.get(name) or env.get(name)


base_url = get_cfg("SIGRID_API_BASE_URL")
function_key = get_cfg("SIGRID_API_FUNCTION_KEY")
database = get_cfg("SIGRID_API_DATABASE") or "ruesma"
database_rep = get_cfg("SIGRID_API_DATABASE_REP") or "ruesma_rep"
timeout_s = float(get_cfg("SIGRID_API_TIMEOUT_S") or "30")

print(f"  database     = {database!r}")
print(f"  database_rep = {database_rep!r}")

if not base_url or not function_key:
    print("❌ Faltan SIGRID_API_BASE_URL o SIGRID_API_FUNCTION_KEY en .env")
    sys.exit(2)
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
download_ide: int | None = None

args_iter = iter(sys.argv[1:])
for arg in args_iter:
    if arg == "--download":
        flag_download = True
    elif arg == "--download-ide":
        try:
            download_ide = int(next(args_iter))
        except (StopIteration, ValueError):
            print("❌ --download-ide requiere un entero")
            sys.exit(3)
    else:
        positional_args.append(arg)

cif_input = positional_args[0] if len(positional_args) > 0 else "B86359866"
obra_input = positional_args[1] if len(positional_args) > 1 else "0695"

cif = normalize_cif(cif_input)
obra = normalize_obra_code(obra_input)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def run_query(
    label: str, sql: str, parameters: list,
    max_rows: int = 500, target_database: str | None = None,
) -> dict | None:
    db = target_database or database
    url = f"{base_url.rstrip('/')}/api/sql/read"
    payload = {
        "database": db, "sql": sql, "parameters": parameters,
        "timeout_seconds": int(timeout_s), "max_rows": max_rows,
    }
    headers = {"x-functions-key": function_key, "Content-Type": "application/json"}
    print(f"\n  📡 {label}  [db={db}]")
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
        print("    ❌ JSON inválido")
        return None
    if not body.get("ok"):
        print(f"    ❌ ok=false: {json.dumps(body, ensure_ascii=False)[:200]}")
        return None
    rc = body.get("row_count", 0)
    print(f"    ✅ {rc} fila(s)")
    return body


def rows_to_dicts(body: dict | None) -> list[dict]:
    if not body or not body.get("rows"):
        return []
    cols = body["columns"]
    return [dict(zip(cols, row)) for row in body["rows"]]


def download_gra_from_rep(gra_rep_ide: int, out_dir: Path, fallback_name: str) -> bool:
    """Descarga un documento de ruesma_rep.gra vía /api/documents/read."""
    url = f"{base_url.rstrip('/')}/api/documents/read"
    payload = {
        "database": database_rep, "schema": "dbo", "table": "gra",
        "id_column": "ide", "id_value": gra_rep_ide,
        "blob_column": "ima",
        "filename_columns": ["nomori", "nom"],
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
    binary = r.content
    if not binary:
        print(f"    ❌ Respuesta vacía")
        return False
    fname = sanitize_filename(r.headers.get("X-Document-Filename", fallback_name))
    out_path = out_dir / fname
    if out_path.exists():
        out_path = out_dir / f"{out_path.stem}_{gra_rep_ide}{out_path.suffix}"
    out_path.write_bytes(binary)
    ct = r.headers.get("Content-Type", "?")
    print(f"    ✅ {out_path.name}  ({len(binary):,} bytes, {ct})")
    return True


# ═══════════════════════════════════════════════════════════════════════════
# MODO --download-ide: descarga directa de un gra de ruesma_rep
# ═══════════════════════════════════════════════════════════════════════════
if download_ide is not None:
    download_dir = Path.home() / "Downloads" / "sigrid_docs" / f"gra_{download_ide}"
    download_dir.mkdir(parents=True, exist_ok=True)
    body = run_query(
        "Obtener nombre",
        "SELECT nom, nomori FROM gra WHERE ide = ?",
        [download_ide], max_rows=1, target_database=database_rep,
    )
    rows = rows_to_dicts(body)
    fallback = (rows[0].get("nomori") or rows[0].get("nom") or f"gra_{download_ide}") if rows else f"gra_{download_ide}"
    print(f"\n  ⬇️  Descargando ruesma_rep.gra.ide={download_ide}")
    download_gra_from_rep(download_ide, download_dir, fallback)
    sys.exit(0)


# ═══════════════════════════════════════════════════════════════════════════
# MODO NORMAL: CIF + obra
# ═══════════════════════════════════════════════════════════════════════════
if not cif or not obra:
    print("❌ CIF u obra inválidos.")
    sys.exit(3)

print(f"\nParámetros:  cif={cif!r}  obra={obra!r}  --download={flag_download}")
print("-" * 70)


# ───────────────────────────────────────────────────────────────────────────
# PASO 1: CABECERA DEL CONTRATO
#   prv.cif → prv.ide = ctr.entide
#   ctr.obride → obr.ide = con.ide (filtrar por con.cod y con.emp=1)
#   ctr.ide = con.ide → con.cod, con.res
# ───────────────────────────────────────────────────────────────────────────
print(f"\n{'━' * 70}")
print(f"  PASO 1: Cabecera del contrato")
print(f"{'━' * 70}")

body = run_query("Localizar contrato por CIF + obra", """\
SELECT
    ctr.ide             AS contrato_ide,
    con_ctr.cod         AS codigo_contrato,
    con_ctr.res         AS nombre_contrato,
    con_ctr.emp         AS empresa,
    ctr.fecdoc          AS fecha_contrato,
    ctr.totbas          AS importe_sin_iva,
    ctr.entcif          AS cif_proveedor,
    ctr.entres          AS nombre_proveedor,
    ctr.entref          AS referencia_proveedor,
    con_obr.cod         AS codigo_obra,
    con_obr.res         AS nombre_obra
FROM ctr
JOIN con AS con_ctr ON ctr.ide    = con_ctr.ide
JOIN con AS con_obr ON ctr.obride = con_obr.ide
JOIN prv            ON ctr.entide = prv.ide
WHERE prv.cif     = ?
  AND con_obr.cod = ?
  AND con_ctr.emp = 1
""", [cif, obra], max_rows=20)

contratos = rows_to_dicts(body)
if not contratos:
    print(f"\n  ⚠️  No hay contrato para CIF={cif} obra={obra} empresa=1")
    sys.exit(0)

for c in contratos:
    print(f"\n  ✅ Contrato encontrado:")
    print(f"     ctr.ide           = {c['contrato_ide']}")
    print(f"     código contrato   = {c['codigo_contrato']!r}")
    print(f"     nombre            = {c['nombre_contrato']!r}")
    print(f"     importe sin IVA   = {c['importe_sin_iva']:,.2f}")
    print(f"     proveedor         = {c['cif_proveedor']}  ({c['nombre_proveedor']!r})")
    print(f"     referencia prov.  = {c.get('referencia_proveedor')!r}")
    print(f"     obra              = {c['codigo_obra']!r}  ({c['nombre_obra']!r})")

ctr_ide = contratos[0]["contrato_ide"]
ctr_cod = contratos[0]["codigo_contrato"]


# ───────────────────────────────────────────────────────────────────────────
# PASO 2: LÍNEAS DEL CONTRATO + PARTIDA
#   ctr.ide → ctrpro.docide
#   ctrpro.paride → obrparpar.ide → obrparpar.cod (código partida)
# ───────────────────────────────────────────────────────────────────────────
print(f"\n{'━' * 70}")
print(f"  PASO 2: Líneas del contrato + partida")
print(f"{'━' * 70}")

body = run_query("Líneas del contrato con partida", """\
SELECT
    ctrpro.ide          AS linea_ide,
    ctrpro.pos          AS linea_pos,
    ctrpro.numlin       AS numero_linea,
    con_pro.cod         AS codigo_producto,
    ctrpro.res          AS descripcion,
    ctrpro.unimed       AS unidad_medida,
    ctrpro.can          AS cantidad,
    ctrpro.canser       AS cantidad_servida,
    ctrpro.pre          AS precio_unitario,
    ctrpro.tot          AS importe_linea,
    ctrpro.dto          AS descuentos,
    obrparpar.cod       AS codigo_partida,
    obrparpar.res       AS descripcion_partida
FROM ctrpro
LEFT JOIN pro              ON ctrpro.proide = pro.ide
LEFT JOIN con AS con_pro   ON pro.ide       = con_pro.ide
LEFT JOIN obrparpar        ON ctrpro.paride = obrparpar.ide
WHERE ctrpro.docide = ?
ORDER BY ctrpro.pos
""", [ctr_ide], max_rows=1000)

lineas = rows_to_dicts(body)
print(f"\n  📋 {len(lineas)} línea(s)")

# Mostrar primeras 10
for i, ln in enumerate(lineas[:10], start=1):
    print(f"\n    Línea {i}:")
    print(f"      producto   = {ln.get('codigo_producto')!r}")
    print(f"      descripción= {ln.get('descripcion')!r}")
    print(f"      cantidad   = {ln.get('cantidad')}  servida={ln.get('cantidad_servida')}")
    print(f"      precio     = {ln.get('precio_unitario')}  importe={ln.get('importe_linea')}")
    print(f"      partida    = {ln.get('codigo_partida')!r}  ({ln.get('descripcion_partida')!r})")

if len(lineas) > 10:
    print(f"\n    ... y {len(lineas) - 10} líneas más")

total_lineas = sum((ln.get("importe_linea") or 0) for ln in lineas)
print(f"\n  Σ importe líneas = {total_lineas:,.2f}")


# ───────────────────────────────────────────────────────────────────────────
# PASO 3: DOCUMENTOS (PDFs del contrato)
#   ctr.ide → rcg.con = ctr.ide → rcg.gra = ruesma.gra.ide
#   ruesma.gra.cod → ruesma_rep.gra.cod → ruesma_rep.gra.ide (descarga)
# ───────────────────────────────────────────────────────────────────────────
print(f"\n{'━' * 70}")
print(f"  PASO 3: Documentos (PDFs)")
print(f"{'━' * 70}")

# 3a: rcg.con = ctr.ide → ruesma.gra.ide y gra.cod
body = run_query("RCG → ruesma.gra", """\
SELECT
    rcg.gra         AS gra_ruesma_ide,
    rcg.pos         AS rcg_pos,
    gra.cod         AS gra_cod,
    gra.nom         AS gra_nom,
    gra.nomori      AS gra_nomori,
    gra.fec         AS gra_fec,
    gra.usu         AS gra_usu
FROM rcg
JOIN gra ON rcg.gra = gra.ide
WHERE rcg.con = ?
ORDER BY rcg.pos
""", [ctr_ide])

docs_ruesma = rows_to_dicts(body)
print(f"\n  Vínculos en RCG: {len(docs_ruesma)}")
for d in docs_ruesma:
    print(f"    ruesma.gra.ide={d['gra_ruesma_ide']}  "
          f"cod={d['gra_cod']!r}  nom={d.get('gra_nomori') or d.get('gra_nom')!r}")

if not docs_ruesma:
    print(f"\n  ⚠️  No se encontraron documentos vinculados al contrato.")
    print()
    print("=" * 70)
    sys.exit(0)

# 3b: Para cada gra.cod de ruesma, buscar el registro en ruesma_rep.gra
#     y filtrar solo PDFs (nomori o nom termina en .pdf)
gra_cods = [d["gra_cod"] for d in docs_ruesma if d.get("gra_cod")]

docs_rep: list[dict] = []
for gra_cod in gra_cods:
    body = run_query(
        f"ruesma_rep.gra por cod={gra_cod!r}",
        """\
SELECT
    ide         AS gra_rep_ide,
    cod         AS gra_cod,
    nom         AS gra_nom,
    nomori      AS gra_nomori,
    fec         AS gra_fec,
    usu         AS gra_usu,
    DATALENGTH(ima) AS ima_bytes
FROM gra
WHERE cod = ?
""", [gra_cod], max_rows=5, target_database=database_rep,
    )
    for r in rows_to_dicts(body):
        docs_rep.append(r)

print(f"\n  Documentos en ruesma_rep: {len(docs_rep)}")

# Filtrar solo PDFs
pdf_docs: list[dict] = []
other_docs: list[dict] = []
for d in docs_rep:
    name = (d.get("gra_nomori") or d.get("gra_nom") or "").lower()
    if name.endswith(".pdf"):
        pdf_docs.append(d)
    else:
        other_docs.append(d)

print(f"    PDFs:     {len(pdf_docs)}")
print(f"    Otros:    {len(other_docs)}")

for d in pdf_docs:
    size = d.get("ima_bytes") or 0
    sz = f"{size/1024:.0f} KB" if size < 1024*1024 else f"{size/(1024*1024):.1f} MB"
    print(f"\n    📄 PDF: ruesma_rep.gra.ide={d['gra_rep_ide']}")
    print(f"       nom    = {d.get('gra_nomori') or d.get('gra_nom')!r}")
    print(f"       cod    = {d['gra_cod']!r}")
    print(f"       fec    = {d.get('gra_fec')}  usu={d.get('gra_usu')!r}")
    print(f"       tamaño = {sz}")

for d in other_docs:
    print(f"    📎 Otro: ruesma_rep.gra.ide={d['gra_rep_ide']}  "
          f"nom={d.get('gra_nomori') or d.get('gra_nom')!r}")


# ───────────────────────────────────────────────────────────────────────────
# PASO 4: DESCARGA (solo con --download)
# ───────────────────────────────────────────────────────────────────────────
docs_descargables = [d for d in pdf_docs if (d.get("ima_bytes") or 0) > 0]

if docs_descargables and flag_download:
    download_dir = Path.home() / "Downloads" / "sigrid_docs" / sanitize_filename(ctr_cod)
    download_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'━' * 70}")
    print(f"  PASO 4: Descargando {len(docs_descargables)} PDF(s)")
    print(f"  📁 {download_dir}")
    print(f"{'━' * 70}")

    ok = 0
    for idx, d in enumerate(docs_descargables, start=1):
        rep_ide = d["gra_rep_ide"]
        name = d.get("gra_nomori") or d.get("gra_nom") or f"contrato_{rep_ide}.pdf"
        print(f"\n  [{idx}/{len(docs_descargables)}] ruesma_rep.gra.ide={rep_ide}")
        if download_gra_from_rep(rep_ide, download_dir, name):
            ok += 1

    print(f"\n  📊 {ok}/{len(docs_descargables)} descargados")
    print(f"  📁 {download_dir}")
    if ok > 0:
        print(f"\n  explorer \"{download_dir}\"")

elif docs_descargables and not flag_download:
    print(f"\n  ℹ️  {len(docs_descargables)} PDF(s) descargables. Usa --download:")
    print(f"    python scripts/diagnose_sigrid_contrato_gra.py {cif} {obra} --download")

elif not pdf_docs:
    print(f"\n  ⚠️  No se encontraron PDFs. Los documentos del contrato pueden ser .docx u otros.")


# ═══════════════════════════════════════════════════════════════════════════
# RESUMEN
# ═══════════════════════════════════════════════════════════════════════════
print()
print("=" * 70)
print(" RESUMEN")
print("=" * 70)
print(f"  Contrato:    {ctr_cod}  (ide={ctr_ide})")
print(f"  Líneas:      {len(lineas)}")
print(f"  Documentos:  {len(docs_ruesma)} en RCG → {len(docs_rep)} en ruesma_rep")
print(f"    PDFs:      {len(pdf_docs)}")
print(f"    Otros:     {len(other_docs)}")
print()
print("  Cadena de relaciones:")
print("    prv.cif → prv.ide = ctr.entide")
print("    ctr.obride → obr.ide = con.ide (con.cod = código obra, emp=1)")
print("    ctr.ide = con.ide → con.cod (código contrato)")
print("    ctr.ide → ctrpro.docide (líneas)")
print("    ctrpro.paride → obrparpar.ide → obrparpar.cod (partida)")
print("    ctr.ide → rcg.con → rcg.gra → ruesma.gra.ide")
print("    ruesma.gra.cod → ruesma_rep.gra.cod → ruesma_rep.gra.ide")
print("    → /api/documents/read (descarga PDF)")
print("=" * 70)