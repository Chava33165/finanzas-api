"""
Lee los recibos de nomina de Plexus (PDF "representacion impresa de un CFDI") con pdftotext.

KORRA acomoda los PDF en ~/Documentos/Personal/PLEXUS/Semana_NN/ (o se suben desde la app:
POST /nominas/recibo los guarda ahi mismo, por eso la carpeta va montada con escritura). Montos en centavos. Notas del formato real (16 recibos, semanas 26-41):
  - pdftotext duplica algunos renglones (texto en negritas): se ignoran los repetidos seguidos.
  - "Total de Percepciones" NO incluye el vale de despensa ni el ajuste por redondeo: el
    "Total Pagado" (percepciones - deducciones) es lo que llega a la cuenta. Los vales de
    despensa llegan aparte, a su tarjeta de vales.
"""
import re
import subprocess
from pathlib import Path

MESES = {m: i for i, m in enumerate(
    ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"], start=1)}
MONTO = r"\$([\d,]+\.\d{2})"
RENGLON = re.compile(r"^\s*([A-Z]?\d+)\s{2,}(.+?)\s{2,}" + MONTO + r"(?:\s+([A-Z]?\d+)\s{2,}(.+?)\s{2,}" + MONTO + r")?\s*$")


def _c(texto: str) -> int:
    return int(round(float(texto.replace(",", "")) * 100))


def _fecha(texto: str) -> str:
    d, m, y = texto.split("/")
    return f"{y}-{MESES[m[:3].capitalize()]:02d}-{int(d):02d}"


def _buscar(patron: str, texto: str) -> re.Match:
    m = re.search(patron, texto)
    if not m:
        raise ValueError(f"no encontré {patron!r} en el recibo")
    return m


def parsear(texto: str) -> dict:
    lineas = []
    for linea in texto.splitlines():
        if not lineas or linea != lineas[-1]:
            lineas.append(linea)
    texto = "\n".join(lineas)

    periodo = _buscar(r"(\d{2}/\w{3}/\d{4})\s+Al:\s+(\d{2}/\w{3}/\d{4})", texto)
    fondo = _buscar(r"FONDO DE AHORRO\s+" + MONTO + r"\s+FONDO AHORRO CIA\s+" + MONTO + r"\s+SALDO TOTAL\s+" + MONTO, texto)
    comprobante = _buscar(r"Fecha Comprobante:\s+(\d{2}/\w{3}/\d{4})", texto)

    percepciones, deducciones = [], []
    bloque = texto.split("Percepciones", 1)[-1].split("Total de Percepciones", 1)[0]
    for linea in bloque.splitlines():
        r = RENGLON.match(linea)
        if not r:
            continue
        # Un renglon con 2 conceptos trae percepcion (izq) y deduccion (der); con 1 hay que ver de
        # que lado quedo: las deducciones empiezan despues de la columna ~90.
        if r.group(4):
            percepciones.append({"clave": r.group(1), "concepto": r.group(2).strip(), "monto": _c(r.group(3))})
            deducciones.append({"clave": r.group(4), "concepto": r.group(5).strip(), "monto": _c(r.group(6))})
        elif linea.index(r.group(1)) > 80:
            deducciones.append({"clave": r.group(1), "concepto": r.group(2).strip(), "monto": _c(r.group(3))})
        else:
            percepciones.append({"clave": r.group(1), "concepto": r.group(2).strip(), "monto": _c(r.group(3))})

    def suma(lista, clave):
        return sum(x["monto"] for x in lista if x["clave"] == clave)

    return {
        "semana": int(_buscar(r"Periodo de Pago\s+(\d+)", texto).group(1)),
        "periodo_ini": _fecha(periodo.group(1)),
        "periodo_fin": _fecha(periodo.group(2)),
        "fecha_pago": _fecha(comprobante.group(1)),
        "uuid": _buscar(r"UUID \)\s*:\s*([0-9A-F-]{36})", texto).group(1),
        "percepciones": _c(_buscar(r"Total de Percepciones:\s+" + MONTO, texto).group(1)),
        "deducciones": _c(_buscar(r"Total de Deducciones:\s+" + MONTO, texto).group(1)),
        "neto": _c(_buscar(r"Total Pagado:\s+" + MONTO, texto).group(1)),
        "isr": suma(deducciones, "51"),
        "imss": suma(deducciones, "52"),
        "fondo_ahorro": suma(deducciones, "62"),
        "cafeteria": suma(deducciones, "136"),
        "despensa": suma(percepciones, "13"),
        "fondo_tuyo": _c(fondo.group(1)),
        "fondo_empresa": _c(fondo.group(2)),
        "fondo_saldo_total": _c(fondo.group(3)),
        "conceptos": {"percepciones": percepciones, "deducciones": deducciones},
    }


def leer_pdf(ruta: Path) -> dict:
    out = subprocess.run(["pdftotext", "-layout", str(ruta), "-"], capture_output=True, timeout=30, check=True)
    return parsear(out.stdout.decode("utf-8", errors="replace"))


def buscar_recibos(carpeta: Path) -> list[Path]:
    return sorted(carpeta.glob("Semana_*/*.pdf")) if carpeta.is_dir() else []


def leer_notas(texto_reporte: str) -> dict[int, str]:
    """Notas por semana del reporte que escribe KORRA (faltas, vacaciones, ajustes)."""
    notas = {}
    for m in re.finditer(r"\*\*Semana (\d+)\*\*[^:]*:\s*(.+)", texto_reporte):
        notas[int(m.group(1))] = m.group(2).replace("**", "").strip()
    return notas
