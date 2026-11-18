"""
task-hub: API de dinero de Chava (antes tareas). La usan:
  - los Atajos de Apple del iPhone (por el Cloudflare Tunnel de task-hub, con BEARER_TOKEN);
  - la pestaña "Dinero" del homelab-dashboard (por la red interna taskhub-finanzas).

Hacia afuera todo el dinero va en PESOS (puede traer centavos); por dentro, en centavos.
Las reglas viven en finanzas.py; aqui solo se valida, se guarda y se traduce.
"""
import asyncio
import json
import logging
import os
import secrets
import tempfile
import time
import unicodedata
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import re

from pydantic import BaseModel, Field, field_validator, model_validator

from app import applepay
from app import cashy as cashy_mod
from app import db as basedatos
from app import finanzas as f
from app import nominas

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("task-hub")

BEARER_TOKEN = os.environ.get("BEARER_TOKEN", "")
NOMINAS_DIR = Path(os.environ.get("NOMINAS_DIR", "/nominas"))
MAX_MONTO = 100_000
AGUINALDO_DIAS = 30  # lo que da Plexus (dicho por Chava); la ley (LFT art. 87) pide minimo 15

conn = None  # se abre en lifespan (los tests usan otra base)
cashy: cashy_mod.Cashy | None = None


# ---------------------------------------------------------------- utilidades

def centavos(pesos: float) -> int:
    return int(round(pesos * 100))


# Campos de dinero en las respuestas: se convierten de centavos a pesos al salir.
DINERO = {
    "limite", "gastado", "efectivo", "fondo", "ahorro_total", "deuda_total", "a_pagar_total", "patrimonio",
    "limite_banco", "tope_personal", "a_pagar", "ciclo_actual", "deuda", "disponible_banco", "saldo",
    "meta_monto", "ritmo_semanal", "monto", "gastos", "ahorro", "sobrante_a_ahorro", "retirar",
    "percepciones", "deducciones", "neto", "isr", "imss", "fondo_ahorro", "cafeteria", "despensa",
    "actuales", "nuevas", "capacidad", "libre", "del_ahorro", "ahorro_despues", "ahorro_hoy", "aporte_ahorro",
    "fondo_tuyo", "fondo_empresa", "fondo_saldo_total", "sobrante_sugerido", "sobrante", "aporte_semanal",
    "iniciales", "mensualidad", "pendiente", "anterior_total", "promedio_diario", "por_categoria", "por_metodo",
    "total", "meses_pendiente", "restante", "meses_restante", "monto_meses", "estimado", "salario_diario",
    "categorias_semana", "categorias_mes", "por_dia", "liquidado", "gastado_ciclo", "anticipo", "antes", "ahora", "diferencia", "quedan",
}


def a_pesos(obj, dinero=False):
    if isinstance(obj, dict):
        return {k: a_pesos(v, dinero or k in DINERO) for k, v in obj.items()}
    if isinstance(obj, list):
        return [a_pesos(v, dinero) for v in obj]
    if dinero and isinstance(obj, int) and not isinstance(obj, bool):
        return obj / 100
    return obj


def ts_de(fecha: date | None) -> float:
    if fecha is None:
        return time.time()
    return datetime(fecha.year, fecha.month, fecha.day, 12, tzinfo=f.TZ).timestamp()


def hoy() -> date:
    return datetime.now(f.TZ).date()


def ajuste(k: str) -> str | None:
    r = conn.execute("SELECT v FROM ajustes WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def autorizado(authorization: str = Header(default="")):
    esperado = f"Bearer {BEARER_TOKEN}"
    if not BEARER_TOKEN or not secrets.compare_digest(authorization.encode(), esperado.encode()):
        raise HTTPException(401, "token inválido")


# ---------------------------------------------------------------- lectura -> dominio

def tarjetas() -> list[f.Tarjeta]:
    return [f.Tarjeta(id=r["id"], nombre=r["nombre"], dia_corte=r["dia_corte"], dia_pago=r["dia_pago"],
                      limite_banco=r["limite_banco"], tope_personal=r["tope_personal"], estado=r["estado"],
                      banco=r["banco"], ultimos4=r["ultimos4"], nota=r["nota"], color=r["color"] or "")
            for r in conn.execute("SELECT * FROM tarjetas ORDER BY orden, id")]


def cajas() -> list[f.Caja]:
    return [f.Caja(id=r["id"], nombre=r["nombre"], meta_nombre=r["meta_nombre"], meta_monto=r["meta_monto"],
                   rendimiento_anual=r["rendimiento_anual"], es_destino=bool(r["es_destino"]))
            for r in conn.execute("SELECT * FROM cajas WHERE archivada=0 ORDER BY id")]


def movimientos() -> list[f.Mov]:
    out = []
    for r in conn.execute("SELECT * FROM movimientos ORDER BY ts"):
        rep = f.Reparto(r["r_gastos"], r["r_fondo"], r["r_ahorro"], r["r_sobrante"], r["r_retirar"]) \
            if r["tipo"] == "pago_recibido" else None
        out.append(f.Mov(id=r["id"], ts=r["ts"], tipo=r["tipo"], monto=r["monto"], metodo=r["metodo"],
                         categoria=r["categoria"], nota=r["nota"], tarjeta=r["tarjeta"], caja=r["caja"],
                         caja_destino=r["caja_destino"], reparto=rep))
    return out


def planes() -> list[f.Plan]:
    return [f.Plan(r["id"], r["tarjeta"], r["descripcion"], r["mensualidad"], r["ultimo_mes"], date.fromisoformat(r["desde"]))
            for r in conn.execute("SELECT * FROM planes ORDER BY id")]


def estado() -> dict:
    return f.estado(movimientos(), tarjetas(), cajas(), hoy(), planes())


def iniciales() -> dict:
    """Los saldos iniciales capturados, para prellenar "Corregir saldos iniciales" (guardar reemplaza todo)."""
    out = {"efectivo": 0, "fondo": 0, "ahorro": 0, "deudas": {t.id: {"corte": 0, "ciclo": 0} for t in tarjetas()}}
    cortes = {t.id: f.ciclo(t.dia_corte, t.dia_pago, hoy()).ultimo_corte for t in tarjetas()}
    for r in conn.execute("SELECT ts, monto, metodo FROM movimientos WHERE tipo='inicial'"):
        if r["metodo"] in ("efectivo", "fondo"):
            out[r["metodo"]] += r["monto"]
        elif r["metodo"] == "caja":
            out["ahorro"] += r["monto"]
        elif r["metodo"] in out["deudas"]:
            dia = datetime.fromtimestamp(r["ts"], f.TZ).date()
            out["deudas"][r["metodo"]]["corte" if dia < cortes[r["metodo"]] else "ciclo"] += r["monto"]
    return out


def aguinaldo(ultima, primera) -> dict | None:
    """Estimado: Plexus da AGUINALDO_DIAS de salario (la ley pide minimo 15), proporcional a lo trabajado
    en el año; se paga antes del 20 dic."""
    if not ultima:
        return None
    conceptos = json.loads(ultima["conceptos"] or "{}")
    salario = max((c["monto"] for c in conceptos.get("percepciones", []) if c["clave"] == "2"), default=0)  # septimo dia = 1 dia
    if not salario:
        return None
    anio = hoy().year
    ingreso = max(date.fromisoformat(primera["periodo_ini"]), date(anio, 1, 1))
    dias = (date(anio, 12, 31) - ingreso).days + 1
    return {"dias": AGUINALDO_DIAS, "estimado": round(AGUINALDO_DIAS * salario * dias / 365), "salario_diario": salario, "desde": ingreso.isoformat(),
            "fecha_limite": f"{anio}-12-20", "cobrado": ajuste(f"aguinaldo_{anio}") is not None}


def insertar(**campos) -> int:
    cols = ", ".join(campos)
    marcas = ", ".join("?" for _ in campos)
    cur = conn.execute(f"INSERT INTO movimientos({cols}) VALUES ({marcas})", tuple(campos.values()))
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------- nominas

def importar_nominas() -> int:
    nuevas = 0
    conocidas = {r[0] for r in conn.execute("SELECT archivo FROM nominas")}
    for pdf in nominas.buscar_recibos(NOMINAS_DIR):
        if pdf.name in conocidas:
            continue
        try:
            n = nominas.leer_pdf(pdf)
        except Exception:
            log.exception("no pude leer el recibo %s", pdf)
            continue
        conceptos = json.dumps(n.pop("conceptos"), ensure_ascii=False)
        n.update(conceptos=conceptos, archivo=pdf.name, importada=time.time())
        conn.execute(f"INSERT OR IGNORE INTO nominas({', '.join(n)}) VALUES ({', '.join('?' for _ in n)})",
                     tuple(n.values()))
        nuevas += 1
    conn.commit()
    if nuevas:
        log.info("Importé %d recibos de nómina", nuevas)
    return nuevas


_firma_recibos: tuple | None = None


def importar_si_hay_nuevos():
    """Al pedir el resumen: si cambio la carpeta de recibos, importa ya (sin esperar al ciclo de cada hora)."""
    global _firma_recibos
    try:
        firma = tuple(sorted((p.name, p.stat().st_mtime) for p in NOMINAS_DIR.rglob("*.pdf")))
    except OSError:
        return
    if firma != _firma_recibos:
        importar_nominas()
        _firma_recibos = firma


def notas_nomina() -> dict[int, str]:
    reporte = NOMINAS_DIR / "reporte_nominas_plexus.md"
    try:
        return nominas.leer_notas(reporte.read_text())
    except OSError:
        return {}


def _pago_de_la_semana(dia_pago: date, uuid: str | None) -> bool:
    """Ya se registro el pago de ese jueves: ligado al recibo, o a mano (sin recibo) esa semana."""
    if uuid and conn.execute("SELECT 1 FROM movimientos WHERE nomina_uuid=?", (uuid,)).fetchone():
        return True
    desde, hasta = ts_de(dia_pago - timedelta(days=2)) - 43200, ts_de(dia_pago + timedelta(days=6)) + 43200
    return conn.execute("""SELECT 1 FROM movimientos WHERE tipo='pago_recibido' AND nomina_uuid IS NULL
                           AND ts >= ? AND ts < ?""", (desde, hasta)).fetchone() is not None


def pendientes() -> list[dict]:
    """Semanas que faltan para tener todo al dia: cada jueves de pago desde que arranco el sistema
    (hasta hoy) al que le falta el recibo de Plexus (KORRA lo acomoda en PLEXUS), el pago
    registrado, o los dos. De la mas vieja a la mas nueva."""
    inicio = ajuste("inicio")
    if not inicio:
        return []
    ini, h = date.fromisoformat(inicio), hoy()
    out = []
    dia = ini + timedelta(days=(f.DIA_PAGO - ini.weekday()) % 7)
    while dia <= h:
        # el recibo trae su fecha de pago (un dia festivo la puede mover): se busca cerca del jueves
        n = conn.execute("SELECT * FROM nominas WHERE fecha_pago BETWEEN ? AND ? ORDER BY fecha_pago LIMIT 1",
                         ((dia - timedelta(days=3)).isoformat(), (dia + timedelta(days=3)).isoformat())).fetchone()
        recibo, pago = n is not None, _pago_de_la_semana(dia, n["uuid"] if n else None)
        if not (recibo and pago):
            # sin recibo: el periodo es de lunes a domingo de la semana anterior al jueves
            p_ini = date.fromisoformat(n["periodo_ini"]) if n else dia - timedelta(days=10)
            out.append({"semana": n["semana"] if n else p_ini.isocalendar().week,
                        "periodo_ini": p_ini.isoformat(),
                        "periodo_fin": n["periodo_fin"] if n else (dia - timedelta(days=4)).isoformat(),
                        "fecha_pago": n["fecha_pago"] if n else dia.isoformat(),
                        "recibo": recibo, "pago": pago,
                        "uuid": n["uuid"] if n else None, "neto": n["neto"] if n else None})
        dia += timedelta(days=7)
    return out


def nomina_pendiente() -> dict | None:
    """El recibo mas viejo (desde que arranco el sistema) cuyo pago aun no se registra: para
    ponerse al dia en orden."""
    inicio = ajuste("inicio")
    if not inicio:
        return None
    for r in conn.execute("""SELECT * FROM nominas WHERE fecha_pago >= ? AND uuid NOT IN
                             (SELECT nomina_uuid FROM movimientos WHERE nomina_uuid IS NOT NULL)
                             ORDER BY fecha_pago""", (inicio,)).fetchall():
        dia = date.fromisoformat(r["fecha_pago"])
        if not _pago_de_la_semana(dia - timedelta(days=(dia.weekday() - f.DIA_PAGO) % 7), None):
            return {k: r[k] for k in ("uuid", "semana", "fecha_pago", "neto", "despensa")}
    return None


async def mantenimiento():
    """Cada hora: busca recibos nuevos y deja un respaldo diario de la base."""
    while True:
        try:
            with basedatos.lock:
                importar_nominas()
                if ajuste("ultimo_respaldo") != hoy().isoformat():
                    basedatos.respaldar(conn, basedatos.DB_PATH.parent / "respaldos", "diario")
                    conn.execute("INSERT OR REPLACE INTO ajustes VALUES ('ultimo_respaldo', ?)", (hoy().isoformat(),))
                    conn.commit()
        except Exception:
            log.exception("mantenimiento")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global conn, cashy
    conn = basedatos.conectar()
    cashy = cashy_mod.Cashy(conn, basedatos.lock)
    cashy.recuperar()
    tarea = asyncio.create_task(mantenimiento())
    yield
    tarea.cancel()


app = FastAPI(title="task-hub · dinero", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# Los Atajos solo leen "mensaje": los errores tambien lo traen, para que la notificacion diga que paso.
@app.exception_handler(HTTPException)
async def _error_http(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail, "mensaje": f"⚠️ {exc.detail}"}, status_code=exc.status_code,
                        headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def _error_validacion(request: Request, exc: RequestValidationError):
    errores = exc.errors()
    texto = errores[0].get("msg", "datos inválidos").removeprefix("Value error, ") if errores else "datos inválidos"
    return JSONResponse({"detail": jsonable_encoder(errores), "mensaje": f"⚠️ {texto}"}, status_code=422)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------- entradas

Monto = Field(gt=0, le=MAX_MONTO)


class GastoIn(BaseModel):
    monto: float = Monto
    metodo: str = "efectivo"
    categoria: str = "Otros"
    nota: str = Field(default="", max_length=80)
    fecha: date | None = None
    meses: int = Field(default=0, ge=0, le=48)  # 0 o 1 = de contado; 2+ = meses sin intereses (solo tarjeta)

    @model_validator(mode="before")
    @classmethod
    def _metodo_con_meses(cls, d):
        """El Atajo manda el metodo y los meses en una sola opcion de lista: "Nu · 3 MSI"."""
        if isinstance(d, dict):
            m = re.match(r"^\s*(\w+)\s*[·\-]?\s*(\d+)\s*msi\s*$", str(d.get("metodo", "")), re.I)
            if m:
                d = {**d, "metodo": m.group(1), "meses": int(m.group(2))}
            if d.get("meses") in ("", None):
                d = {**d, "meses": 0}
        return d

    @field_validator("metodo")
    @classmethod
    def _metodo(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("categoria")
    @classmethod
    def _categoria(cls, v: str) -> str:
        v = v.strip().capitalize()
        if v not in f.CATEGORIAS:
            raise ValueError(f"categoría debe ser una de {', '.join(f.CATEGORIAS)}")
        return v


class PagoRecibidoIn(BaseModel):
    monto: float = Monto
    sobrante: float | None = Field(default=None, ge=0, le=MAX_MONTO)
    caja: int | None = None
    nomina_uuid: str | None = Field(default=None, max_length=40)
    # Jueves de una semana pasada (para ponerse al dia): el pago cuenta para esa semana. Sin el: ahora.
    fecha: date | None = None
    # Reparto personalizado (los 3 o ninguno; deben sumar `monto`). Sin ellos: la proporcion guardada (33/33/34).
    gastos: float | None = Field(default=None, ge=0, le=MAX_MONTO)
    fondo: float | None = Field(default=None, ge=0, le=MAX_MONTO)
    ahorro: float | None = Field(default=None, ge=0, le=MAX_MONTO)


def proporcion() -> tuple[int, int, int]:
    """Porcentajes del reparto (gastos, fondo, ahorro). Se cambian con PUT /proporcion."""
    v = ajuste("proporcion")
    return tuple(int(x) for x in v.split(",")) if v else f.PROPORCION


def calcular_reparto(monto: float, sobrante: int, gastos, fondo, ahorro) -> f.Reparto:
    partes = (gastos, fondo, ahorro)
    if all(x is None for x in partes):
        return f.repartir(centavos(monto), sobrante, proporcion())
    if any(x is None for x in partes):
        raise HTTPException(422, "para un reparto personalizado pon gastos, fondo y ahorro")
    g, fo, a = (centavos(x) for x in partes)
    if g + fo + a != centavos(monto):
        raise HTTPException(422, f"el reparto suma {f.pesos(g + fo + a)} y te llegaron {f.pesos(centavos(monto))}")
    return f.repartir_manual(g, fo, a, sobrante)


class PagoTarjetaIn(BaseModel):
    tarjeta: str
    monto: float = Monto
    # De donde sale: el fondo para tarjetas (lo normal), su efectivo/debito, o dinero que le dieron
    # (regalo, venta…: entra y sale a la tarjeta en el mismo instante, sin pasar por el reparto).
    desde: Literal["fondo", "efectivo", "regalo"] = "fondo"
    nota: str = Field(default="", max_length=80)

    @field_validator("desde", mode="before")
    @classmethod
    def _desde(cls, v):
        # El atajo manda la opción como se ve: «Fondo de tarjetas», «Mi efectivo», «Me lo dieron».
        t = str(v or "fondo").strip().lower()
        return "fondo" if t.startswith("fondo") else "efectivo" if "efectivo" in t or "débito" in t else \
            "regalo" if t.startswith("me lo dieron") or t == "regalo" else t


class EntradaIn(BaseModel):
    """Dinero que le entro y no es la nomina (su papa le dio, vendio algo…). `destino`: efectivo, fondo,
    ahorro (la caja destino) o el id de una tarjeta para abonarle directo."""
    monto: float = Monto
    nota: str = Field(default="", max_length=80)
    destino: str = Field(default="efectivo", min_length=1, max_length=40)


class CajaIn(BaseModel):
    nombre: str = Field(min_length=1, max_length=40)
    meta_nombre: str = Field(default="", max_length=40)
    meta_monto: float = Field(default=0, ge=0, le=10_000_000)
    rendimiento_anual: float = Field(default=0, ge=0, le=100)
    es_destino: bool = False


class CajaMovIn(BaseModel):
    tipo: Literal["deposito", "retiro", "transferencia"]
    monto: float = Monto
    destino: int | None = None
    nota: str = Field(default="", max_length=80)


class TarjetaIn(BaseModel):
    nombre: str = Field(min_length=1, max_length=30)
    banco: str = Field(default="", max_length=30)
    ultimos4: str = Field(default="", pattern=r"^(\d{4})?$")
    dia_corte: int = Field(ge=1, le=31)
    dia_pago: int = Field(ge=1, le=31)
    limite_banco: float = Field(default=0, ge=0, le=10_000_000)
    tope_personal: float = Field(default=0, ge=0, le=10_000_000)
    estado: Literal["activa", "pausada", "bloqueada"] = "activa"
    nota: str = Field(default="", max_length=120)
    color: str = Field(default="", pattern=r"^(#[0-9A-Fa-f]{6})?$")


class DeudaInicialIn(BaseModel):
    corte: float = Field(default=0, ge=0, le=10_000_000)  # del estado ya cerrado (se paga en la proxima fecha limite)
    ciclo: float = Field(default=0, ge=0, le=10_000_000)  # lo que va del ciclo actual


class PlanIn(BaseModel):
    tarjeta: str
    descripcion: str = Field(min_length=1, max_length=40)
    mensualidad: float = Monto
    ultimo_mes: str = Field(pattern=r"^20\d\d-(0[1-9]|1[0-2])$")
    incluido: bool = True  # la mensualidad de este ciclo ya va en lo que llevas (lo normal en Nu)


class CompraSimIn(BaseModel):
    descripcion: str = Field(default="Compra", min_length=1, max_length=40)
    total: float = Monto  # precio total; la mensualidad es total / meses
    meses: int = Field(ge=1, le=48)
    tarjeta: str | None = None  # con tarjeta, la primera mensualidad cae en su proxima fecha limite
    primer_mes: str | None = Field(default=None, pattern=r"^20\d\d-(0[1-9]|1[0-2])$")
    del_ahorro: int = Field(default=0, ge=0, le=48)  # las primeras N mensualidades las pagas del ahorro


class SimuladorIn(BaseModel):
    compras: list[CompraSimIn] = Field(default_factory=list, max_length=10)


class AjusteIn(BaseModel):
    corte: float | None = Field(default=None, ge=0, le=10_000_000)  # lo que la app dice a pagar del corte
    ciclo: float | None = Field(default=None, ge=0, le=10_000_000)  # lo que la app dice que llevas en el ciclo


class ParteIn(BaseModel):
    destino: Literal["caja", "fondo", "efectivo", "tarjeta", "gasto"]
    monto: float = Monto
    caja: int | None = None
    tarjeta: str | None = None  # destino "tarjeta": se paga directo esa tarjeta
    # destino "gasto": algo que pagaste con este dinero (un regalo…). Cuenta en "¿En qué se va?",
    # no en tu limite de la semana.
    categoria: str = "Otros"
    nota: str = Field(default="", max_length=80)

    @field_validator("categoria")
    @classmethod
    def _categoria(cls, v: str) -> str:
        v = v.strip().capitalize()
        if v not in f.CATEGORIAS:
            raise ValueError(f"categoría debe ser una de {', '.join(f.CATEGORIAS)}")
        return v


class ProporcionIn(BaseModel):
    gastos: int = Field(ge=0, le=100)
    fondo: int = Field(ge=0, le=100)
    ahorro: int = Field(ge=0, le=100)


class ExtraIn(BaseModel):
    monto: float = Monto
    destino: Literal["caja", "fondo", "efectivo"] = "caja"
    caja: int | None = None
    concepto: Literal["fondo_plexus", "aguinaldo", "otro"] = "otro"
    nota: str = Field(default="", max_length=80)
    # Repartirlo en varios destinos (deben sumar `monto`). Sin partes, todo va a `destino`.
    partes: list[ParteIn] = Field(default=[], max_length=12)


class TarjetaRealIn(BaseModel):
    corte: float | None = Field(default=None, ge=0, le=10_000_000)  # a pagar del estado ya cerrado
    ciclo: float | None = Field(default=None, ge=0, le=10_000_000)  # lo que llevas del ciclo en curso


class CalibrarIn(BaseModel):
    """Lo que de verdad tienes hoy. Solo se toca lo que mandes."""
    efectivo: float | None = Field(default=None, ge=0, le=MAX_MONTO)
    fondo: float | None = Field(default=None, ge=0, le=MAX_MONTO)
    cajas: dict[int, float] = {}
    tarjetas: dict[str, TarjetaRealIn] = {}
    # El efectivo que falta casi siempre son gastos que no anotaste: cuentan para tu semana.
    faltante_como_gasto: bool = True


class InicialIn(BaseModel):
    efectivo: float = Field(default=0, ge=0, le=MAX_MONTO)
    fondo: float = Field(default=0, ge=0, le=MAX_MONTO)
    ahorro: float = Field(default=0, ge=0, le=10_000_000)
    deudas: dict[str, DeudaInicialIn] = {}


# ---------------------------------------------------------------- endpoints

r = APIRouter(prefix="/api/v1", dependencies=[Depends(autorizado)])


def _tarjeta_o_404(tid: str) -> f.Tarjeta:
    tid = tid.strip().lower()  # los atajos mandan el nombre como se ve («Nu», «Plata»)
    t = next((t for t in tarjetas() if t.id == tid), None)
    if not t:
        raise HTTPException(404, f"no tengo la tarjeta «{tid}»")
    return t


def _caja_o_404(cid: int) -> f.Caja:
    c = next((c for c in cajas() if c.id == cid), None)
    if not c:
        raise HTTPException(404, "no existe esa caja")
    return c


@r.get("/resumen")
def resumen():
    with basedatos.lock:
        importar_si_hay_nuevos()
        e = estado()
        e["iniciado"] = ajuste("inicio")
        e["ultima_calibracion"] = ajuste("ultima_calibracion")
        ult = conn.execute("SELECT max(ts) FROM movimientos WHERE tipo != 'inicial' AND origen != 'calibracion'").fetchone()[0]
        e["ultimo_movimiento"] = datetime.fromtimestamp(ult, f.TZ).date().isoformat() if ult else None
        e["nomina_pendiente"] = nomina_pendiente()
        e["pendientes"] = pendientes()
        ultima = conn.execute("SELECT * FROM nominas ORDER BY fecha_pago DESC LIMIT 1").fetchone()
        primera = conn.execute("SELECT * FROM nominas ORDER BY fecha_pago LIMIT 1").fetchone()
        # El fondo Plexus no es dinero en mano hasta que lo entregan (RH: ~noviembre). Al cobrarlo se
        # guarda la fecha del ultimo recibo: si no ha llegado un recibo mas nuevo, ya esta cobrado.
        cobrado = ajuste("fondo_plexus_cobrado")
        # Liquidacion: el recibo donde el acumulado bajo contra el anterior (Plexus entrego el fondo
        # y empezo uno nuevo). Se entrego el acumulado anterior. Cobrado = ya se registro esa entrega;
        # despues el fondo nuevo solo se va acumulando hasta la siguiente liquidacion.
        reinicio = liquidado = None
        totales = conn.execute("SELECT fecha_pago, fondo_saldo_total FROM nominas ORDER BY fecha_pago").fetchall()
        for antes, despues in zip(totales, totales[1:]):
            if antes[1] and despues[1] < antes[1]:
                reinicio, liquidado = despues[0], antes[1]
        desde = reinicio or (ultima["fecha_pago"] if ultima else None)
        e["fondo_plexus"] = ({k: ultima[k] for k in ("fondo_tuyo", "fondo_empresa", "fondo_saldo_total", "fecha_pago")}
                             | {"aporte_semanal": ultima["fondo_ahorro"] * 2, "liquidado": liquidado,
                                "liquidado_el": reinicio, "cobrado": bool(cobrado and cobrado >= desde)}) if ultima else None
        e["aguinaldo"] = aguinaldo(ultima, primera)
        e["iniciales"] = iniciales()
        e["categorias"] = list(f.CATEGORIAS)
        e["proporcion"] = list(proporcion())  # [gastos, fondo, ahorro] en %
        e["metodos"] = ["efectivo"] + [t["id"] for t in e["tarjetas"]]
        e["sobrante_sugerido"] = max(e["efectivo"], 0)
    return a_pesos(e)


@r.post("/gastos")
def gasto(g: GastoIn):
    with basedatos.lock:
        ids = {t.id: t for t in tarjetas()}
        if g.metodo != "efectivo" and g.metodo not in ids:
            raise HTTPException(422, f"método debe ser efectivo o {', '.join(ids)}")
        msi = g.meses if g.meses >= 2 else 0
        if msi and g.metodo == "efectivo":
            raise HTTPException(422, "los meses sin intereses solo son con tarjeta")
        monto = centavos(g.monto)
        with conn:
            mid = conn.execute(
                "INSERT INTO movimientos(ts, tipo, monto, metodo, categoria, nota, origen) VALUES (?,?,?,?,?,?, 'api')",
                (ts_de(g.fecha), "compra_meses" if msi else "gasto", monto, g.metodo, g.categoria, g.nota.strip())).lastrowid
            if msi:
                t = ids[g.metodo]
                desde = f.ciclo(t.dia_corte, t.dia_pago, hoy()).ultimo_corte  # 1a mensualidad: este ciclo
                conn.execute("INSERT INTO planes(tarjeta, descripcion, mensualidad, ultimo_mes, desde, creado, movimiento_id) "
                             "VALUES (?,?,?,?,?,?,?)",
                             (t.id, g.nota.strip() or g.categoria, round(monto / msi), f.ultimo_mes_msi(t, hoy(), msi),
                              desde.isoformat(), time.time(), mid))
        e = estado()
    if g.metodo == "efectivo":
        mensaje = e["mensaje"]
    else:
        t = next(t for t in e["tarjetas"] if t["id"] == g.metodo)
        if msi:
            plan = next(p for p in reversed(t["meses"]) if p["descripcion"] == (g.nota.strip() or g.categoria))
            mensaje = (f"{t['nombre']} a {msi} MSI: {f.pesos(round(monto / msi))} al mes, último pago el "
                       f"{date.fromisoformat(plan['pagos'][-1]).strftime('%d/%m/%Y')}. "
                       f"Debes {f.pesos(t['meses_restante'])} a meses en {t['nombre']}. Este ciclo llevas "
                       f"{f.pesos(t['ciclo_actual'])}")
        else:
            mensaje = f"{t['nombre']}: llevas {f.pesos(t['ciclo_actual'])} este ciclo"
        mensaje += f" de tu tope de {f.pesos(t['tope_personal'])}." if t["tope_personal"] else "."
        if t["estado"] != "activa":
            mensaje = f"⚠️ Ojo: marcaste {t['nombre']} como {t['estado']}. " + mensaje
    return {"id": mid, "mensaje": mensaje}


class ApplePayIn(BaseModel):
    monto: str = Field(max_length=40)  # tal cual lo da Wallet: "$1,234.50"
    comercio: str = Field(default="", max_length=120)
    tarjeta: str = Field(default="", max_length=80)  # nombre de la tarjeta en Wallet


@r.post("/gastos/applepay")
def gasto_applepay(x: ApplePayIn):
    """Automatizacion "Transaccion" de Atajos: cada pago con Apple Pay se registra solo."""
    with basedatos.lock:
        ids = [t.id for t in tarjetas()]
    tid = applepay.tarjeta(x.tarjeta, ids)
    if not tid:
        raise HTTPException(422, f"No reconozco la tarjeta «{x.tarjeta}». Renómbrala en Wallet para que diga Nu, Plata o BBVA.")
    pesos_ = applepay.monto(x.monto)
    if not pesos_ or pesos_ <= 0 or pesos_ > MAX_MONTO:
        raise HTTPException(422, f"No entendí el monto «{x.monto}».")
    comercio = " ".join(x.comercio.split())[:80]
    return gasto(GastoIn(monto=pesos_, metodo=tid, categoria=applepay.categoria(comercio), nota=comercio))


@r.get("/deuda")
def deuda():
    """Para el Atajo "¿Cuánto debo?": por tarjeta, lo del corte con su fecha, lo en curso y lo a meses."""
    with basedatos.lock:
        e = estado()
    meses = ["", "ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
    fecha = lambda iso: f"{int(iso[8:])} {meses[int(iso[5:7])]}"
    lineas = []
    for t in e["tarjetas"]:
        if not (t["deuda"] or t["meses_restante"]):
            lineas.append(f"{t['nombre']}: no debes nada ✓")
            continue
        partes = []
        if t["a_pagar"]:
            dias = t["dias_para_pagar"]
            cuando = "¡hoy!" if dias == 0 else (f"¡venció hace {-dias} días!" if dias < 0 else f"faltan {dias} días")
            partes.append(f"paga {f.pesos(t['a_pagar'])} antes del {fecha(t['fecha_limite'])} ({cuando})")
        else:
            partes.append("nada pendiente del corte")
        siguiente = next((m for m in t["calendario"] if m["estado"] == "en_curso"), None)
        if t["ciclo_actual"] and siguiente:
            partes.append(f"llevas {f.pesos(t['ciclo_actual'])} para el {fecha(siguiente['fecha_limite'])}")
        if t["meses_restante"]:
            partes.append(f"{f.pesos(t['meses_restante'])} a meses")
        lineas.append(f"{t['nombre']}: " + ", ".join(partes) + ".")
    falta = e["a_pagar_total"] - e["fondo"]
    lineas.append(f"Fondo: {f.pesos(e['fondo'])}" + (f", te faltan {f.pesos(falta)} para lo del corte." if falta > 0 else ", sí alcanza ✓"))
    return {"mensaje": "\n".join(lineas), "a_pagar": e["a_pagar_total"] / 100, "deuda_total": e["deuda_total"] / 100}


@r.get("/gastos/periodo")
def gastos_periodo(vista: Literal["dia", "semana", "mes"] = "dia", fecha: date | None = None):
    """¿Cuánto gasto? por día, semana (jueves a miércoles) o mes."""
    with basedatos.lock:
        movs = movimientos()
    return a_pesos(f.gastos_periodo(movs, vista, min(fecha or hoy(), hoy()), hoy()))


# ---------------------------------------------------------------- Cashy

class CashyIn(BaseModel):
    mensaje: str = Field(min_length=1, max_length=1000)


class CashyConfigIn(BaseModel):
    modelo: Literal["sonnet", "haiku", "opus"]


def datos_cashy() -> dict:
    """Foto de solo lectura de todo lo de dinero, en pesos, para cada pregunta a Cashy."""
    e = resumen()
    nominas_ = [{k: x[k] for k in ("semana", "fecha_pago", "percepciones", "deducciones", "neto", "fondo_ahorro",
                                   "despensa", "nota")} for x in listar_nominas()[:4]]
    return {"resumen": {k: v for k, v in e.items() if k not in ("categorias", "metodos", "iniciales")},
            "deuda": deuda()["mensaje"], "movimientos_recientes": listar_movimientos(limit=60, metodo="", categoria="", tipo=""),
            "nominas": nominas_, "hoy": hoy().isoformat(),
            "gasto_hoy": gastos_periodo("dia", None)["total"], "gasto_semana": gastos_periodo("semana", None)["total"],
            "gasto_mes": gastos_periodo("mes", None)["total"]}


@r.get("/cashy")
def cashy_historia(limit: int = Query(default=50, ge=1, le=200)):
    with basedatos.lock:
        return cashy.historia(limit)


@r.post("/cashy")
def cashy_preguntar(x: CashyIn):
    datos = datos_cashy()
    with basedatos.lock:
        try:
            cashy.preguntar(x.mensaje.strip(), datos)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return cashy.historia()


@r.delete("/cashy")
def cashy_nueva():
    with basedatos.lock:
        cashy.nueva_conversacion()
    return {"ok": True}


@r.get("/cashy/config")
def cashy_config():
    with basedatos.lock:
        return {"modelo": cashy.modelo(), "modelos": sorted(cashy_mod.MODELOS)}


@r.put("/cashy/config")
def cashy_poner_config(c: CashyConfigIn):
    with basedatos.lock:
        cashy.poner_modelo(c.modelo)
    return {"modelo": c.modelo}


@r.get("/reparto")
def reparto(monto: float = Query(gt=0, le=MAX_MONTO), sobrante: float | None = Query(default=None, ge=0),
            gastos: float | None = Query(default=None, ge=0), fondo: float | None = Query(default=None, ge=0),
            ahorro: float | None = Query(default=None, ge=0)):
    with basedatos.lock:
        s = centavos(sobrante) if sobrante is not None else max(estado()["efectivo"], 0)
    rep = calcular_reparto(monto, s, gastos, fondo, ahorro)
    return a_pesos({"monto": centavos(monto), "sobrante": s, **rep.__dict__})


@r.post("/pagos-recibidos")
def pago_recibido(p: PagoRecibidoIn):
    with basedatos.lock:
        if not ajuste("inicio"):
            raise HTTPException(409, "primero captura tus saldos iniciales")
        if p.caja is not None:
            _caja_o_404(p.caja)
        if p.nomina_uuid and conn.execute("SELECT 1 FROM movimientos WHERE nomina_uuid=?", (p.nomina_uuid,)).fetchone():
            raise HTTPException(409, "esa nómina ya está registrada")
        s = centavos(p.sobrante) if p.sobrante is not None else max(estado()["efectivo"], 0)
        rep = calcular_reparto(p.monto, s, p.gastos, p.fondo, p.ahorro)
        uuid = p.nomina_uuid
        if uuid is None and p.fecha is None:  # registrado a mano: si coincide con el recibo pendiente, es esa nomina
            pend = nomina_pendiente()
            uuid = pend["uuid"] if pend and pend["neto"] == centavos(p.monto) else None
        if p.fecha and p.fecha > hoy():
            raise HTTPException(422, "la fecha del pago no puede ser futura")
        mid = insertar(ts=ts_de(p.fecha) if p.fecha else time.time(), tipo="pago_recibido", monto=centavos(p.monto), caja=p.caja,
                       nomina_uuid=uuid, origen="api", r_gastos=rep.gastos, r_fondo=rep.fondo,
                       r_ahorro=rep.ahorro, r_sobrante=rep.sobrante_a_ahorro, r_retirar=rep.retirar)
    return a_pesos({"id": mid, **rep.__dict__}) | {
        "mensaje": f"Retira {f.pesos(rep.retirar)} para tus gastos, aparta {f.pesos(rep.fondo)} al fondo y "
                   f"{f.pesos(rep.ahorro + rep.sobrante_a_ahorro)} al ahorro."}


@r.post("/pagos-tarjeta")
def pago_tarjeta(p: PagoTarjetaIn):
    with basedatos.lock:
        t = _tarjeta_o_404(p.tarjeta)
        e = estado()
        monto = centavos(p.monto)
        deuda = next(x["deuda"] for x in e["tarjetas"] if x["id"] == t.id)
        if monto > deuda:
            raise HTTPException(409, f"{t.nombre} solo debe {f.pesos(deuda)}.")
        nota = p.nota.strip()
        if p.desde == "fondo":
            if monto > e["fondo"]:
                raise HTTPException(409, f"El fondo solo tiene {f.pesos(e['fondo'])}. Págalo con tu efectivo o con "
                                         "dinero que te dieron, o paga lo que alcance. El ahorro no paga tarjetas (regla 5).")
            mid = insertar(ts=time.time(), tipo="pago_tarjeta", monto=monto, tarjeta=t.id, nota=nota, origen="api")
            de = "desde el fondo"
        elif p.desde == "efectivo":
            if monto > e["efectivo"]:
                raise HTTPException(409, f"Tu efectivo es {f.pesos(max(e['efectivo'], 0))}: no alcanza para {f.pesos(monto)}.")
            mid = insertar(ts=time.time(), tipo="pago_tarjeta", monto=monto, metodo="efectivo", tarjeta=t.id,
                           nota=nota, origen="api")
            de = "con tu efectivo"
        else:  # regalo: entra al fondo y sale a la tarjeta en el mismo instante
            nota = nota or "Dinero que me dieron"
            ahora = time.time()
            insertar(ts=ahora, tipo="ingreso_extra", monto=monto, metodo="fondo", nota=nota, origen="otro")
            mid = insertar(ts=ahora, tipo="pago_tarjeta", monto=monto, tarjeta=t.id, nota=nota, origen="otro")
            de = f"con {nota.lower()}"
        resta = deuda - monto
    return {"id": mid, "mensaje": f"Pagaste {f.pesos(monto)} a {t.nombre} {de}. "
                                  + (f"Todavía debes {f.pesos(resta)}." if resta > 0 else "¡Quedó en ceros! 🎉")}


@r.post("/entradas")
def entrada(x: EntradaIn):
    """Forma corta de /ingresos-extra para la app y el atajo «Me entró dinero»."""
    destino = x.destino.strip().lower().removeprefix("abonar a ").strip()  # «Abonar a Nu» → nu
    destino = "fondo" if destino.startswith("fondo") else destino
    if destino in ("efectivo", "fondo"):
        extra = ExtraIn(monto=x.monto, destino=destino, nota=x.nota)
    elif destino in ("ahorro", "caja"):
        extra = ExtraIn(monto=x.monto, destino="caja", nota=x.nota)
    else:
        _tarjeta_o_404(destino)
        extra = ExtraIn(monto=x.monto, nota=x.nota, partes=[ParteIn(destino="tarjeta", monto=x.monto, tarjeta=destino)])
    return ingreso_extra(extra)


_DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
_MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def _cuando(d: date, hoy_: date) -> str:
    falta = (d - hoy_).days
    rel = {0: "hoy", 1: "mañana", -1: "ayer"}.get(falta, f"en {falta} días" if falta > 0 else f"hace {-falta} días")
    return f"{_DIAS[d.weekday()]} {d.day} {_MESES[d.month - 1]} ({rel})"


@r.get("/proximos")
def proximos(dias: int = Query(default=45, ge=1, le=120)):
    """Lo que viene: pagos de tarjeta (lo del corte y mensualidades a meses), cortes y tu pago del jueves."""
    with basedatos.lock:
        e = estado()
    h = hoy()
    fin = h + timedelta(days=dias)
    ev: list[dict] = []
    if pp := e.get("semana", {}).get("proximo_pago"):
        d = date.fromisoformat(pp)
        ev.append({"fecha": pp, "tipo": "nomina", "titulo": "Te pagan la semana", "monto": None, "tarjeta": None})
    for t in e["tarjetas"]:
        for c in t.get("calendario", []):
            d = date.fromisoformat(c["fecha_limite"])
            vencido = c["estado"] == "cerrado" and d < h  # lo del corte que ya se paso de fecha
            if c["monto"] > 0 and (h <= d or vencido) and d <= fin:
                titulo = (f"⚠️ Vencido: pagar {t['nombre']}" if vencido else f"Pagar {t['nombre']}" if c["estado"] == "cerrado"
                          else f"Pagar {t['nombre']} (lo que va de este ciclo)" if c["estado"] == "en_curso"
                          else f"Mensualidades de {t['nombre']}")
                ev.append({"fecha": c["fecha_limite"], "tipo": "pago_tarjeta", "titulo": titulo, "monto": c["monto"],
                           "tarjeta": t["id"], "estado": c["estado"],
                           "detalle": [x.get("descripcion", "") for x in c.get("detalle", [])]})
        corte = date.fromisoformat(t["proximo_corte"])
        if h <= corte <= fin:
            ev.append({"fecha": t["proximo_corte"], "tipo": "corte", "titulo": f"Corte de {t['nombre']}",
                       "monto": t["ciclo_actual"], "tarjeta": t["id"]})
    ev.sort(key=lambda x: (x["fecha"], x["tipo"] != "nomina"))
    for x in ev:
        x["dias"] = (date.fromisoformat(x["fecha"]) - h).days
    lineas = [f"• {_cuando(date.fromisoformat(x['fecha']), h)}: {x['titulo']}"
              + (f" — {f.pesos(x['monto'])}" if x["monto"] else "") for x in ev[:8]]
    urgente = sum(x["monto"] or 0 for x in ev if x["tipo"] == "pago_tarjeta" and x["dias"] <= 14)
    pie = f"\nFondo para tarjetas: {f.pesos(e['fondo'])}"
    if urgente > e["fondo"]:
        pie += f" (te faltan {f.pesos(urgente - e['fondo'])} para lo de las próximas 2 semanas)"
    mensaje = ("📅 Lo que viene:\n" + "\n".join(lineas) + pie) if lineas else "No tienes pagos en los próximos días 🙌"
    return a_pesos({"eventos": ev, "fondo": e["fondo"], "mensaje": mensaje})


@r.post("/saldos-iniciales")
def saldos_iniciales(i: InicialIn):
    """Con lo que tienes hoy arranca el sistema. Si ya se capturaron, se reemplazan (para corregir)."""
    with basedatos.lock:
        tarjetas_por_id = {t.id: t for t in tarjetas()}
        if not set(i.deudas) <= set(tarjetas_por_id):
            raise HTTPException(422, "deudas inválidas")
        destino = next((c for c in cajas() if c.es_destino), cajas()[0])
        ahora = time.time()
        filas = [("efectivo", i.efectivo, None, ahora), ("fondo", i.fondo, None, ahora),
                 ("caja", i.ahorro, destino.id, ahora)]
        for tid, d in i.deudas.items():
            # Lo del estado ya cerrado se fecha un dia antes del ultimo corte para que caiga en ese estado.
            corte = f.ciclo(tarjetas_por_id[tid].dia_corte, tarjetas_por_id[tid].dia_pago, hoy()).ultimo_corte
            filas += [(tid, d.corte, None, ts_de(corte - timedelta(days=1))), (tid, d.ciclo, None, ahora)]
        with conn:
            conn.execute("DELETE FROM movimientos WHERE tipo='inicial'")
            for metodo, v, caja, ts in filas:
                if v > 0:
                    conn.execute("INSERT INTO movimientos(ts, tipo, monto, metodo, caja, nota, origen) "
                                 "VALUES (?, 'inicial', ?, ?, ?, 'Saldo inicial', 'api')", (ts, centavos(v), metodo, caja))
            conn.execute("INSERT OR IGNORE INTO ajustes VALUES ('inicio', ?)", (hoy().isoformat(),))
    return {"ok": True}


def _ajustes_tarjeta(t: f.Tarjeta, actual: dict, corte: float | None, ciclo: float | None) -> list[tuple]:
    """(ts, diferencia, parte, antes, real) para que el corte y el ciclo cuadren con la app del banco.
    Lo del corte se fecha un dia antes del ultimo corte para que caiga en ese estado de cuenta."""
    c = f.ciclo(t.dia_corte, t.dia_pago, hoy())
    filas = []
    if corte is not None and centavos(corte) != actual["a_pagar"]:
        filas.append((ts_de(c.ultimo_corte - timedelta(days=1)), centavos(corte) - actual["a_pagar"], "corte",
                      actual["a_pagar"], centavos(corte)))
    if ciclo is not None and centavos(ciclo) != actual["ciclo_actual"]:
        filas.append((time.time(), centavos(ciclo) - actual["ciclo_actual"], "ciclo", actual["ciclo_actual"], centavos(ciclo)))
    return filas


@r.post("/tarjetas/{tid}/ajustar")
def ajustar_tarjeta(tid: str, a: AjusteIn):
    """'Mi app dice…': mete la diferencia como ajuste para que el corte y el ciclo cuadren con el banco."""
    with basedatos.lock:
        t = _tarjeta_o_404(tid)
        actual = next(x for x in estado()["tarjetas"] if x["id"] == tid)
        filas = _ajustes_tarjeta(t, actual, a.corte, a.ciclo)
        with conn:
            for ts, delta, parte, _, _ in filas:
                conn.execute("INSERT INTO movimientos(ts, tipo, monto, metodo, nota, origen) VALUES (?, 'ajuste_tarjeta', ?, ?, ?, 'api')",
                             (ts, delta, tid, f"Ajuste ({parte}) a lo que dice la app"))
    return {"ok": True, "mensaje": f"{t.nombre} quedó igual que tu app." if filas else f"{t.nombre} ya cuadraba."}


@r.put("/proporcion")
def poner_proporcion(x: ProporcionIn):
    """Cambia el reparto de los proximos pagos (los ya registrados guardan el suyo)."""
    if x.gastos + x.fondo + x.ahorro != 100:
        raise HTTPException(422, f"los porcentajes suman {x.gastos + x.fondo + x.ahorro} %, deben sumar 100")
    with basedatos.lock:
        conn.execute("INSERT OR REPLACE INTO ajustes VALUES ('proporcion', ?)", (f"{x.gastos},{x.fondo},{x.ahorro}",))
        conn.commit()
    return {"ok": True, "mensaje": f"Desde tu próximo pago: {x.gastos} % gastos, {x.fondo} % fondo y {x.ahorro} % ahorro."}


@r.post("/calibrar")
def calibrar(c: CalibrarIn):
    """"Esto es lo que de verdad tengo": deja cada saldo igual que la realidad sin borrar historia. La
    diferencia entra como movimiento de calibracion (el efectivo que falta, como gasto sin anotar)."""
    with basedatos.lock:
        if not ajuste("inicio"):
            raise HTTPException(409, "primero captura tus saldos iniciales")
        e = estado()
        por_caja = {x["id"]: x for x in e["cajas"]}
        por_tarjeta = {t.id: t for t in tarjetas()}
        if not set(c.cajas) <= set(por_caja):
            raise HTTPException(422, "esa caja no existe")
        if any(not 0 <= v <= 10_000_000 for v in c.cajas.values()):
            raise HTTPException(422, "el saldo de una caja no puede ser negativo")
        if not set(c.tarjetas) <= set(por_tarjeta):
            raise HTTPException(422, "esa tarjeta no existe")
        ahora = time.time()
        cambios, filas = [], []

        def saldo(que: str, antes: int, real: float, **mov):
            delta = centavos(real) - antes
            if not delta:
                return
            cambios.append({"que": que, "antes": antes, "ahora": centavos(real), "diferencia": delta})
            if mov.get("metodo") == "efectivo" and delta < 0 and c.faltante_como_gasto:
                # El dia de pago lo que falta es de la semana que acaba de terminar: no se come el limite nuevo.
                dia_pago = hoy() == f.inicio_semana(hoy())
                filas.append(dict(ts=ts_de(hoy() - timedelta(days=1)) if dia_pago else ahora, tipo="gasto",
                                  monto=-delta, metodo="efectivo", categoria="Otros", nota="Gastos sin anotar"))
            else:
                filas.append(dict(ts=ahora, tipo="calibracion", monto=delta, nota="Calibración", **mov))

        if c.efectivo is not None:
            saldo("Efectivo", e["efectivo"], c.efectivo, metodo="efectivo")
        if c.fondo is not None:
            saldo("Fondo de tarjetas", e["fondo"], c.fondo, metodo="fondo")
        for cid, v in c.cajas.items():
            saldo(por_caja[cid]["nombre"], por_caja[cid]["saldo"], v, metodo="caja", caja=cid)
        for tid, real in c.tarjetas.items():
            actual = next(x for x in e["tarjetas"] if x["id"] == tid)
            for ts, delta, parte, antes, ahora_ in _ajustes_tarjeta(por_tarjeta[tid], actual, real.corte, real.ciclo):
                que = f"{actual['nombre']}: {'a pagar del corte' if parte == 'corte' else 'lo del ciclo'}"
                cambios.append({"que": que, "antes": antes, "ahora": ahora_, "diferencia": delta})
                filas.append(dict(ts=ts, tipo="ajuste_tarjeta", monto=delta, metodo=tid, nota=f"Calibración ({parte})"))
        with conn:
            for fila in filas:
                fila["origen"] = "calibracion"
                conn.execute(f"INSERT INTO movimientos({', '.join(fila)}) VALUES ({', '.join('?' for _ in fila)})",
                             tuple(fila.values()))
            conn.execute("INSERT OR REPLACE INTO ajustes VALUES ('ultima_calibracion', ?)",
                         (datetime.now(f.TZ).isoformat(timespec="seconds"),))
        e = estado()
    if not cambios:
        mensaje = "Todo ya cuadraba ✓"
    else:
        lineas = [f"{x['que']}: {f.pesos(x['antes'])} → {f.pesos(x['ahora'])}" for x in cambios]
        mensaje = "Listo, quedó igual que la realidad.\n" + "\n".join(lineas) + "\n" + e["mensaje"]
    return a_pesos({"ok": True, "cambios": cambios}) | {"mensaje": mensaje}


@r.get("/hoy")
def hoy_resumen():
    """Para el Atajo "¿Cómo voy hoy?": lo gastado hoy y lo que te queda para la semana."""
    with basedatos.lock:
        movs = movimientos()
        e = estado()
    p = f.gastos_periodo(movs, "dia", hoy(), hoy())
    n = len(p["lista"])
    if n:
        top = next(iter(p["por_categoria"]))
        inicio = f"Hoy llevas {f.pesos(p['total'])} en {n} gasto{'s' if n > 1 else ''} (más en {top.lower()})."
    else:
        inicio = "Hoy no has gastado nada ✓"
    return {"mensaje": f"{inicio}\n{e['mensaje']}", "total": p["total"] / 100, "quedan": e["efectivo"] / 100,
            "por_dia": e["semana"]["por_dia"] / 100}


@r.delete("/gastos/ultimo")
def borrar_ultimo_gasto():
    """Para el Atajo "Me equivoqué": borra el ultimo gasto que registraste (solo de las ultimas 24 h)."""
    with basedatos.lock:
        fila = conn.execute("""SELECT id, monto, metodo, categoria, nota FROM movimientos
                               WHERE tipo IN ('gasto', 'compra_meses') AND origen != 'calibracion' AND ts > ?
                               ORDER BY id DESC LIMIT 1""", (time.time() - 86400,)).fetchone()
        if not fila:
            raise HTTPException(404, "No registraste gastos en las últimas 24 horas.")
        with conn:
            conn.execute("DELETE FROM planes WHERE movimiento_id=?", (fila["id"],))
            conn.execute("DELETE FROM movimientos WHERE id=?", (fila["id"],))
        e = estado()
    que = fila["nota"] or fila["categoria"]
    metodo = "efectivo" if fila["metodo"] == "efectivo" else fila["metodo"].capitalize()
    return {"ok": True, "mensaje": f"Borré {f.pesos(fila['monto'])} de {que} ({metodo}).\n{e['mensaje']}"}


@r.post("/planes")
def crear_plan(p: PlanIn):
    with basedatos.lock:
        t = _tarjeta_o_404(p.tarjeta)
        c = f.ciclo(t.dia_corte, t.dia_pago, hoy())
        desde = c.proximo_corte if p.incluido else c.ultimo_corte
        cur = conn.execute("INSERT INTO planes(tarjeta, descripcion, mensualidad, ultimo_mes, desde, creado) VALUES (?,?,?,?,?,?)",
                           (t.id, p.descripcion.strip(), centavos(p.mensualidad), p.ultimo_mes, desde.isoformat(), time.time()))
        conn.commit()
    return {"id": cur.lastrowid}


@r.delete("/planes/{pid}")
def borrar_plan(pid: int):
    with basedatos.lock:
        if not conn.execute("DELETE FROM planes WHERE id=?", (pid,)).rowcount:
            raise HTTPException(404, "no existe esa compra a meses")
        conn.commit()
    return {"ok": True}


@r.post("/simulador")
def simulador(x: SimuladorIn):
    """Simulador de meses: no guarda nada. Capacidad = lo que el reparto manda al fondo de tarjetas
    en un mes, con el neto promedio de las ultimas 8 nominas (semanas * 52 / 12)."""
    with basedatos.lock:
        for c in x.compras:
            if c.tarjeta:
                _tarjeta_o_404(c.tarjeta)
        netos = [r[0] for r in conn.execute("SELECT neto FROM nominas ORDER BY fecha_pago DESC LIMIT 8")]
        semanal = sum(netos) / len(netos) if netos else 0
        _, p_fondo, p_ahorro = proporcion()
        e = estado()
        compras = [{"descripcion": c.descripcion.strip(), "mensualidad": round(centavos(c.total) / c.meses),
                    "meses": c.meses, "tarjeta": c.tarjeta, "primer_mes": c.primer_mes,
                    "del_ahorro": min(c.del_ahorro, c.meses)} for c in x.compras]
        res = f.simular_meses(planes(), tarjetas(), compras, hoy(), round(semanal * 52 / 12 * p_fondo / 100),
                              e["ahorro_total"], round(semanal * 52 / 12 * p_ahorro / 100))
    return a_pesos(res) | {"neto_semanal": round(semanal) / 100, "semanas_promedio": len(netos)}


@r.post("/ingresos-extra")
def ingreso_extra(x: ExtraIn):
    """Dinero que no es la nomina semanal (fondo Plexus, aguinaldo, regalos…). No pasa por el reparto."""
    with basedatos.lock:
        nota = x.nota.strip() or {"fondo_plexus": "Fondo de ahorro Plexus", "aguinaldo": "Aguinaldo"}.get(x.concepto, "Ingreso extra")
        partes = x.partes or [ParteIn(destino=x.destino, monto=x.monto, caja=x.caja)]
        if sum(centavos(pt.monto) for pt in partes) != centavos(x.monto):
            raise HTTPException(422, f"las partes suman {f.pesos(sum(centavos(pt.monto) for pt in partes))} "
                                     f"y te llegaron {f.pesos(centavos(x.monto))}")
        e = estado()
        destino_caja = next((c.id for c in cajas() if c.es_destino), None)
        ahora = time.time()
        filas, resumen_partes = [], []
        for pt in partes:
            monto = centavos(pt.monto)
            if pt.destino == "tarjeta":
                t = next((t for t in e["tarjetas"] if t["id"] == pt.tarjeta), None)
                if not t:
                    raise HTTPException(422, f"no tengo la tarjeta «{pt.tarjeta}»")
                if monto > t["deuda"]:
                    raise HTTPException(409, f"{t['nombre']} solo debe {f.pesos(t['deuda'])}")
                # Pasa por el fondo (que es lo unico que paga tarjetas) y sale el mismo instante.
                filas += [dict(tipo="ingreso_extra", monto=monto, metodo="fondo", nota=nota, origen=x.concepto),
                          dict(tipo="pago_tarjeta", monto=monto, tarjeta=t["id"], nota=nota, origen=x.concepto)]
                resumen_partes.append(f"pagaste {f.pesos(monto)} a {t['nombre']}")
                continue
            if pt.destino == "gasto":
                filas.append(dict(tipo="gasto", monto=monto, metodo="extra", categoria=pt.categoria,
                                  nota=pt.nota.strip() or nota, origen=x.concepto))
                resumen_partes.append(f"{f.pesos(monto)} en {pt.nota.strip().lower() or pt.categoria.lower()}")
                continue
            caja = None
            if pt.destino == "caja":
                caja = _caja_o_404(pt.caja).id if pt.caja is not None else destino_caja
            filas.append(dict(tipo="ingreso_extra", monto=monto, metodo=pt.destino, caja=caja, nota=nota, origen=x.concepto))
            donde = {"fondo": "al fondo de tarjetas", "efectivo": "a efectivo"}.get(pt.destino) or \
                f"a {next(c.nombre for c in cajas() if c.id == caja)}"
            resumen_partes.append(f"{f.pesos(monto)} {donde}")
        with conn:
            for fila in filas:
                fila["ts"] = ahora
                mid = conn.execute(f"INSERT INTO movimientos({', '.join(fila)}) VALUES ({', '.join('?' for _ in fila)})",
                                   tuple(fila.values())).lastrowid
        if x.concepto == "fondo_plexus":
            ultima = conn.execute("SELECT max(fecha_pago) FROM nominas").fetchone()[0] or hoy().isoformat()
            conn.execute("INSERT OR REPLACE INTO ajustes VALUES ('fondo_plexus_cobrado', ?)", (ultima,))
        elif x.concepto == "aguinaldo":
            conn.execute("INSERT OR REPLACE INTO ajustes VALUES (?, ?)", (f"aguinaldo_{hoy().year}", str(mid)))
        conn.commit()
    detalle = f": {', '.join(resumen_partes)}" if len(partes) > 1 or partes[0].destino in ("tarjeta", "gasto") else ""
    return {"id": mid, "mensaje": f"Registré {f.pesos(centavos(x.monto))} de {nota.lower()}{detalle}."}


@r.put("/tarjetas/{tid}")
def editar_tarjeta(tid: str, t: TarjetaIn):
    with basedatos.lock:
        _tarjeta_o_404(tid)
        conn.execute("""UPDATE tarjetas SET nombre=?, banco=?, ultimos4=?, dia_corte=?, dia_pago=?, limite_banco=?,
                        tope_personal=?, estado=?, nota=?, color=? WHERE id=?""",
                     (t.nombre.strip(), t.banco.strip(), t.ultimos4, t.dia_corte, t.dia_pago,
                      centavos(t.limite_banco), centavos(t.tope_personal), t.estado, t.nota.strip(), t.color.upper(), tid))
        conn.commit()
    return {"ok": True}


@r.post("/tarjetas")
def crear_tarjeta(t: TarjetaIn):
    """Tarjeta de crédito nueva. Su id (el que usan los Atajos y Apple Pay) sale del nombre: "Nu" -> nu,
    "BBVA Azul" -> bbva-azul (si ya existe, -2, -3…)."""
    with basedatos.lock:
        base = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", t.nombre).encode("ascii", "ignore").decode().lower()).strip("-")[:20] or "tarjeta"
        if base in ("efectivo", "fondo", "caja", "extra"):
            base = f"t-{base}"
        tid, k = base, 2
        while conn.execute("SELECT 1 FROM tarjetas WHERE id=?", (tid,)).fetchone():
            tid, k = f"{base}-{k}", k + 1
        orden = conn.execute("SELECT coalesce(max(orden), 0) + 1 FROM tarjetas").fetchone()[0]
        conn.execute("""INSERT INTO tarjetas(id, nombre, banco, ultimos4, dia_corte, dia_pago, limite_banco, tope_personal,
                        estado, nota, color, orden) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (tid, t.nombre.strip(), t.banco.strip(), t.ultimos4, t.dia_corte, t.dia_pago, centavos(t.limite_banco),
                      centavos(t.tope_personal), t.estado, t.nota.strip(), t.color.upper(), orden))
        conn.commit()
    return {"id": tid, "mensaje": f"Agregué la tarjeta {t.nombre.strip()}. En gastos aparece como «{tid}»."}


def _guardar_caja(c: CajaIn, cid: int | None) -> int:
    if c.es_destino:
        conn.execute("UPDATE cajas SET es_destino=0")
    valores = (c.nombre.strip(), c.meta_nombre.strip(), centavos(c.meta_monto), c.rendimiento_anual, int(c.es_destino))
    if cid is None:
        cid = conn.execute("INSERT INTO cajas(nombre, meta_nombre, meta_monto, rendimiento_anual, es_destino) "
                           "VALUES (?,?,?,?,?)", valores).lastrowid
    else:
        conn.execute("UPDATE cajas SET nombre=?, meta_nombre=?, meta_monto=?, rendimiento_anual=?, es_destino=? "
                     "WHERE id=?", valores + (cid,))
    if not conn.execute("SELECT 1 FROM cajas WHERE es_destino=1 AND archivada=0").fetchone():
        conn.execute("UPDATE cajas SET es_destino=1 WHERE id=(SELECT min(id) FROM cajas WHERE archivada=0)")
    conn.commit()
    return cid


@r.post("/cajas")
def crear_caja(c: CajaIn):
    with basedatos.lock:
        if len(cajas()) >= 12:
            raise HTTPException(409, "máximo 12 cajas")
        return {"id": _guardar_caja(c, None)}


@r.put("/cajas/{cid}")
def editar_caja(cid: int, c: CajaIn):
    with basedatos.lock:
        _caja_o_404(cid)
        return {"id": _guardar_caja(c, cid)}


@r.delete("/cajas/{cid}")
def borrar_caja(cid: int):
    with basedatos.lock:
        _caja_o_404(cid)
        saldo = next(c["saldo"] for c in estado()["cajas"] if c["id"] == cid)
        if saldo != 0:
            raise HTTPException(409, f"la caja todavía tiene {f.pesos(saldo)}: muévelo a otra caja primero")
        if len(cajas()) == 1:
            raise HTTPException(409, "necesitas al menos una caja")
        conn.execute("UPDATE cajas SET archivada=1, es_destino=0 WHERE id=?", (cid,))
        conn.execute("UPDATE cajas SET es_destino=1 WHERE id=(SELECT min(id) FROM cajas WHERE archivada=0) "
                     "AND NOT EXISTS (SELECT 1 FROM cajas WHERE es_destino=1 AND archivada=0)")
        conn.commit()
    return {"ok": True}


@r.post("/cajas/{cid}/movimientos")
def mover_caja(cid: int, m: CajaMovIn):
    with basedatos.lock:
        _caja_o_404(cid)
        monto = centavos(m.monto)
        saldo = next(c["saldo"] for c in estado()["cajas"] if c["id"] == cid)
        if m.tipo in ("retiro", "transferencia") and monto > saldo:
            raise HTTPException(409, f"la caja solo tiene {f.pesos(saldo)}")
        if m.tipo == "transferencia":
            if m.destino is None or m.destino == cid:
                raise HTTPException(422, "elige otra caja de destino")
            _caja_o_404(m.destino)
        mid = insertar(ts=time.time(), tipo=f"{m.tipo}_caja", monto=monto, caja=cid,
                       caja_destino=m.destino if m.tipo == "transferencia" else None, nota=m.nota.strip(), origen="api")
    return {"id": mid}


@r.get("/movimientos")
def listar_movimientos(limit: int = Query(default=60, ge=1, le=500), metodo: str = "", categoria: str = "",
                       tipo: str = ""):
    filtros, params = [], []
    for col, v in (("metodo", metodo), ("categoria", categoria), ("tipo", tipo)):
        if v:
            filtros.append(f"{col}=?")
            params.append(v)
    where = f"WHERE {' AND '.join(filtros)}" if filtros else ""
    with basedatos.lock:
        filas = conn.execute(f"SELECT id, ts, tipo, monto, metodo, categoria, nota, tarjeta, caja, caja_destino, "
                             f"r_retirar AS retirar FROM movimientos {where} ORDER BY ts DESC LIMIT ?",
                             (*params, limit)).fetchall()
    return a_pesos([dict(x) for x in filas])


@r.delete("/movimientos/{mid}")
def borrar_movimiento(mid: int):
    with basedatos.lock:
        fila = conn.execute("SELECT tipo FROM movimientos WHERE id=?", (mid,)).fetchone()
        if not fila:
            raise HTTPException(404, "no existe ese movimiento")
        with conn:
            conn.execute("DELETE FROM planes WHERE movimiento_id=?", (mid,))
            conn.execute("DELETE FROM movimientos WHERE id=?", (mid,))
    return {"ok": True}


MAX_RECIBO = 5 << 20  # un recibo de Plexus pesa ~70 KB


def _nombre_libre(carpeta: Path, nombre: str) -> Path:
    """Nunca se pisa un archivo (misma regla que KORRA): si ya existe, ` (2)`, ` (3)`, ..."""
    destino, base, k = carpeta / nombre, nombre.removesuffix(".pdf"), 2
    while destino.exists():
        destino, k = carpeta / f"{base} ({k}).pdf", k + 1
    return destino


@r.post("/nominas/recibo")
async def subir_recibo(request: Request):
    """Recibo de Plexus (PDF) subido desde la app. Se acomoda igual que lo hace KORRA
    (PLEXUS/Semana_N/Plexus_nomina_semana_N_<inicio>_<fin>.pdf) y se importa en ese momento."""
    datos = await request.body()
    if len(datos) > MAX_RECIBO:
        raise HTTPException(413, "Ese archivo pesa demasiado para ser un recibo.")
    if not datos.startswith(b"%PDF-"):
        raise HTTPException(422, "Eso no es un PDF. Sube el recibo de Plexus en PDF.")
    if not NOMINAS_DIR.is_dir():
        raise HTTPException(503, "No encuentro la carpeta PLEXUS en la Pi.")
    # Primero a un archivo oculto en la misma carpeta (buscar_recibos no lo ve) y luego se mueve.
    fd, tmp = tempfile.mkstemp(dir=NOMINAS_DIR, prefix=".subida-", suffix=".pdf")
    tmp = Path(tmp)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(datos)
        try:
            n = await asyncio.to_thread(nominas.leer_pdf, tmp)
        except Exception:
            log.exception("recibo subido ilegible")
            raise HTTPException(422, "No pude leer ese PDF como recibo de nómina de Plexus.")
        with basedatos.lock:
            ya = conn.execute("SELECT semana, fecha_pago, neto FROM nominas WHERE uuid=?", (n["uuid"],)).fetchone()
            if ya:
                return a_pesos({"semana": ya["semana"], "fecha_pago": ya["fecha_pago"], "neto": ya["neto"], "nuevo": False}) | {
                    "mensaje": f"El recibo de la semana {ya['semana']} ya estaba."}
            carpeta = NOMINAS_DIR / f"Semana_{n['semana']}"
            carpeta.mkdir(exist_ok=True)
            destino = _nombre_libre(carpeta, f"Plexus_nomina_semana_{n['semana']}_{n['periodo_ini']}_{n['periodo_fin']}.pdf")
            os.chmod(tmp, 0o644)
            os.replace(tmp, destino)
            importar_nominas()
        log.info("recibo subido desde la app: %s", destino.name)
        return a_pesos({"semana": n["semana"], "fecha_pago": n["fecha_pago"], "neto": n["neto"], "nuevo": True,
                        "archivo": f"PLEXUS/{carpeta.name}/{destino.name}"}) | {
            "mensaje": f"Listo: semana {n['semana']}, {f.pesos(n['neto'])} netos."}
    finally:
        tmp.unlink(missing_ok=True)


@r.get("/nominas")
def listar_nominas():
    notas = notas_nomina()
    with basedatos.lock:
        filas = conn.execute("SELECT * FROM nominas ORDER BY fecha_pago DESC").fetchall()
    out = []
    for x in filas:
        d = dict(x)
        d["conceptos"] = json.loads(d["conceptos"] or "{}")
        d.pop("importada", None)
        d["nota"] = notas.get(d["semana"], "")
        out.append(a_pesos(d))
    return out


app.include_router(r)
