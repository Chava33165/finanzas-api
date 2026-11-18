"""
Reglas del sistema de dinero de Chava, sin HTTP ni SQL (solo datos -> datos).

Todo el dinero va en CENTAVOS enteros (nunca float) para que las sumas cuadren exacto.

El sistema (ver ATAJOS.md / la pestaña Dinero del dashboard):
  - Cada pago recibido se reparte 33 % gastos / 33 % fondo de tarjetas / 34 % ahorro (se puede
    cambiar; el ahorro se queda con el redondeo).
  - Los gastos son un LIMITE en efectivo: lo que sobro no se vuelve a retirar; esa parte de
    la asignacion se va al ahorro.
  - El fondo solo paga tarjetas; el ahorro nunca paga tarjetas.
  - La semana va de jueves (dia de pago) a miercoles.
"""
import calendar
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("TZ", "America/Mexico_City"))
PROPORCION = (33, 33, 34)  # gastos, fondo, ahorro (default; la real vive en ajustes)
DIA_PAGO = 3  # jueves (lunes = 0)
CATEGORIAS = ("Comida", "Transporte", "Escuela", "Salidas", "Personal", "Otros")
AVISO_TOPE = 0.8
AVISO_DIAS_PAGO = 3


@dataclass(frozen=True)
class Reparto:
    gastos: int
    fondo: int
    ahorro: int
    sobrante_a_ahorro: int
    retirar: int


@dataclass(frozen=True)
class Ciclo:
    ultimo_corte: date
    fecha_limite: date
    proximo_corte: date


@dataclass
class Tarjeta:
    id: str
    nombre: str
    dia_corte: int
    dia_pago: int
    limite_banco: int = 0
    tope_personal: int = 0
    estado: str = "activa"  # activa | pausada | bloqueada
    banco: str = ""
    ultimos4: str = ""
    nota: str = ""
    color: str = ""  # "#RRGGBB" para la app


@dataclass
class Caja:
    id: int
    nombre: str
    meta_nombre: str = ""
    meta_monto: int = 0
    rendimiento_anual: float = 0.0
    es_destino: bool = False


@dataclass
class Plan:
    """Compra a meses: una mensualidad por estado de cuenta hasta el que se paga en `ultimo_mes`."""
    id: int
    tarjeta: str
    descripcion: str
    mensualidad: int
    ultimo_mes: str  # "AAAA-MM": mes de la fecha limite del ultimo pago
    desde: date  # inicio (dia de corte) del primer ciclo que este sistema le carga


@dataclass(frozen=True)
class CargoPlan:
    dia_inicio: date  # el cargo entra al ciclo que empieza este dia
    fecha_limite: date  # y se paga con el estado que vence este dia


@dataclass
class Mov:
    id: int
    ts: float
    # inicial | pago_recibido | gasto | pago_tarjeta | ajuste_tarjeta (monto con signo) | ingreso_extra
    # | deposito_caja | retiro_caja | transferencia_caja | cargo_meses (generado de un Plan, no se guarda)
    # | compra_meses (la compra completa a MSI: cuenta en categorias; a la tarjeta la cargan sus mensualidades)
    # | calibracion (monto con signo: la diferencia entre lo calculado y lo real; no es gasto ni ingreso)
    tipo: str
    monto: int
    metodo: str = ""  # gasto: efectivo | <tarjeta> | extra (pagado con un ingreso extra: no toca la semana).  inicial / ingreso_extra: efectivo | fondo | caja | <tarjeta>
    categoria: str = ""
    nota: str = ""
    tarjeta: str = ""
    caja: int | None = None
    caja_destino: int | None = None
    reparto: Reparto | None = None
    extra: dict = field(default_factory=dict)

    @property
    def dia(self) -> date:
        return datetime.fromtimestamp(self.ts, TZ).date()


def _a_pesos(c: float) -> int:
    """Redondea al peso entero mas cercano (en centavos)."""
    return int(round(c / 100)) * 100


def repartir(ingreso: int, sobrante_efectivo: int, proporcion: tuple[int, int, int] | None = None) -> Reparto:
    proporcion = proporcion or PROPORCION
    gastos = _a_pesos(ingreso * proporcion[0] / 100)
    fondo = _a_pesos(ingreso * proporcion[1] / 100)
    ahorro = ingreso - gastos - fondo  # absorbe el redondeo: la suma cuadra exacta
    sobrante = min(max(sobrante_efectivo, 0), gastos)
    return Reparto(gastos, fondo, ahorro, sobrante, gastos - sobrante)


def repartir_manual(gastos: int, fondo: int, ahorro: int, sobrante_efectivo: int) -> Reparto:
    """Semana con reparto distinto (p. ej. para ponerse al corriente con tarjetas). Mismas reglas de
    efectivo: lo que sobro no se retira y esa parte va al ahorro."""
    sobrante = min(max(sobrante_efectivo, 0), gastos)
    return Reparto(gastos, fondo, ahorro, sobrante, gastos - sobrante)


def _dia_en(y: int, m: int, dia: int) -> date:
    return date(y, m, min(dia, calendar.monthrange(y, m)[1]))


def _mes(y: int, m: int, delta: int) -> tuple[int, int]:
    i = y * 12 + (m - 1) + delta
    return i // 12, i % 12 + 1


def _limite(corte: date, dia_pago: int) -> date:
    """El estado que sale en `corte` vence el primer dia `dia_pago` despues del corte."""
    limite = _dia_en(corte.year, corte.month, dia_pago)
    return limite if limite > corte else _dia_en(*_mes(corte.year, corte.month, 1), dia_pago)


def ciclo(dia_corte: int, dia_pago: int, hoy: date) -> Ciclo:
    """El corte ocurre al empezar su dia (asi lo hace Nu: el 18 el estado ya salio). Un ciclo va
    de un corte (incluido) al siguiente (excluido): lo comprado el dia de corte ya es del ciclo nuevo."""
    corte = _dia_en(hoy.year, hoy.month, dia_corte)
    if corte > hoy:
        corte = _dia_en(*_mes(hoy.year, hoy.month, -1), dia_corte)
    return Ciclo(corte, _limite(corte, dia_pago), _dia_en(*_mes(corte.year, corte.month, 1), dia_corte))


def ultimo_mes_msi(t: Tarjeta, hoy: date, meses: int) -> str:
    """Mes del ultimo pago de una compra a `meses` MSI hecha hoy: la 1a mensualidad cae en el ciclo actual."""
    corte = ciclo(t.dia_corte, t.dia_pago, hoy).proximo_corte
    for _ in range(meses - 1):
        corte = _dia_en(*_mes(corte.year, corte.month, 1), t.dia_corte)
    limite = _limite(corte, t.dia_pago)
    return f"{limite.year}-{limite.month:02d}"


def cargos_plan(p: Plan, t: Tarjeta) -> list[CargoPlan]:
    out = []
    inicio = p.desde
    ultimo = tuple(int(x) for x in p.ultimo_mes.split("-"))
    while len(out) < 120:
        corte = _dia_en(*_mes(inicio.year, inicio.month, 1), t.dia_corte)
        limite = _limite(corte, t.dia_pago)
        if (limite.year, limite.month) > ultimo:
            break
        out.append(CargoPlan(inicio, limite))
        inicio = corte
    return out


def inicio_semana(d: date) -> date:
    return d - timedelta(days=(d.weekday() - DIA_PAGO) % 7)


def pesos(c: int) -> str:
    """Centavos -> "$1,234" (o "$12.50" si trae centavos)."""
    v = c / 100
    return f"${v:,.0f}" if c % 100 == 0 else f"${v:,.2f}"


# ---------------------------------------------------------------- estado

def estado(movs: list[Mov], tarjetas: list[Tarjeta], cajas: list[Caja], hoy: date, planes=()) -> dict:
    movs = list(movs)
    por_id = {t.id: t for t in tarjetas}
    meses: dict[str, list[dict]] = defaultdict(list)
    # mensualidades por estado de cuenta: (tarjeta, fecha limite) -> [{descripcion, mensualidad, ultimo}]
    por_estado: dict[tuple[str, date], list[dict]] = defaultdict(list)
    for p in planes:
        if p.tarjeta not in por_id:
            continue
        cargos = cargos_plan(p, por_id[p.tarjeta])
        for i, c in enumerate(cargos):
            por_estado[(p.tarjeta, c.fecha_limite)].append(
                {"descripcion": p.descripcion, "mensualidad": p.mensualidad, "ultimo": i == len(cargos) - 1,
                 "faltan": len(cargos) - i})
            if c.dia_inicio <= hoy:  # ya empezo su ciclo: es un cargo real de la tarjeta
                movs.append(Mov(-p.id, datetime(c.dia_inicio.year, c.dia_inicio.month, c.dia_inicio.day, 12, tzinfo=TZ).timestamp(),
                                "cargo_meses", p.mensualidad, metodo=p.tarjeta, nota=p.descripcion))
        meses[p.tarjeta].append({
            "id": p.id, "descripcion": p.descripcion, "mensualidad": p.mensualidad, "ultimo_mes": p.ultimo_mes,
            "pendiente": sum(p.mensualidad for c in cargos if c.dia_inicio > hoy),  # aun no se carga a la tarjeta
            "restante": sum(p.mensualidad for c in cargos if c.fecha_limite >= hoy),  # lo que falta pagar de esta compra
            "pagos": [c.fecha_limite.isoformat() for c in cargos if c.fecha_limite >= hoy],
        })
    movs = sorted(movs, key=lambda m: m.ts)
    efectivo = fondo = 0
    caja_saldo: dict[int, int] = defaultdict(int)
    destino = next((c.id for c in cajas if c.es_destino), cajas[0].id if cajas else None)
    sem = inicio_semana(hoy)
    gastado_sem = 0
    limite_sem = None
    cat_sem: dict[str, int] = defaultdict(int)
    cat_mes: dict[str, int] = defaultdict(int)
    semanas_gasto: dict[date, int] = defaultdict(int)
    semanas_ahorro: set[date] = set()
    ahorro_semanal: dict[int, dict[date, int]] = defaultdict(lambda: defaultdict(int))

    for m in movs:
        if m.tipo == "inicial":
            if m.metodo == "efectivo":
                efectivo += m.monto
            elif m.metodo == "fondo":
                fondo += m.monto
            elif m.metodo == "caja" and m.caja is not None:
                caja_saldo[m.caja] += m.monto
        elif m.tipo == "ingreso_extra":
            if m.metodo == "efectivo":
                efectivo += m.monto
            elif m.metodo == "fondo":
                fondo += m.monto
            elif m.caja is not None:
                caja_saldo[m.caja] += m.monto
                ahorro_semanal[m.caja][inicio_semana(m.dia)] += m.monto
        elif m.tipo == "calibracion":
            if m.metodo == "efectivo":
                efectivo += m.monto
            elif m.metodo == "fondo":
                fondo += m.monto
            elif m.caja is not None:
                caja_saldo[m.caja] += m.monto
        elif m.tipo == "pago_recibido" and m.reparto:
            r = m.reparto
            efectivo += r.retirar
            fondo += r.fondo
            c = m.caja if m.caja is not None else destino
            if c is not None:
                caja_saldo[c] += r.ahorro + r.sobrante_a_ahorro
                ahorro_semanal[c][inicio_semana(m.dia)] += r.ahorro + r.sobrante_a_ahorro
            if r.ahorro > 0:
                semanas_ahorro.add(inicio_semana(m.dia))
            if m.dia >= sem:
                limite_sem = r.gastos
        elif m.tipo in ("gasto", "compra_meses"):
            if m.tipo == "gasto" and m.metodo == "efectivo":
                efectivo -= m.monto
                if m.dia >= sem:
                    gastado_sem += m.monto
            if m.dia >= sem:
                cat_sem[m.categoria or "Otros"] += m.monto
            if (m.dia.year, m.dia.month) == (hoy.year, hoy.month):
                cat_mes[m.categoria or "Otros"] += m.monto
            semanas_gasto[inicio_semana(m.dia)] += m.monto
        elif m.tipo == "pago_tarjeta":
            if m.metodo == "efectivo":  # lo pago con su efectivo / debito, no con el fondo
                efectivo -= m.monto
            else:
                fondo -= m.monto
        elif m.tipo == "deposito_caja" and m.caja is not None:
            caja_saldo[m.caja] += m.monto
            ahorro_semanal[m.caja][inicio_semana(m.dia)] += m.monto
        elif m.tipo == "retiro_caja" and m.caja is not None:
            caja_saldo[m.caja] -= m.monto
        elif m.tipo == "transferencia_caja" and m.caja is not None and m.caja_destino is not None:
            caja_saldo[m.caja] -= m.monto
            caja_saldo[m.caja_destino] += m.monto

    # Si esta semana aun no llega el pago, el limite es el de la ultima asignacion.
    if limite_sem is None:
        ultimo = next((m for m in reversed(movs) if m.tipo == "pago_recibido" and m.reparto), None)
        limite_sem = ultimo.reparto.gastos if ultimo else 0

    alertas: list[dict] = []
    tarjetas_out = [_tarjeta(t, movs, hoy, alertas, meses[t.id],
                             {lim: v for (tid, lim), v in por_estado.items() if tid == t.id}) for t in tarjetas]
    a_pagar_total = sum(t["a_pagar"] for t in tarjetas_out)
    deuda_total = sum(t["deuda"] for t in tarjetas_out)
    if a_pagar_total > fondo:
        alertas.append({"nivel": "alto", "texto":
            f"El fondo no alcanza: debes pagar {pesos(a_pagar_total)} y el fondo tiene {pesos(fondo)}. "
            "No lo cubras con tu ahorro; baja el gasto con tarjeta."})
    if limite_sem and gastado_sem > limite_sem:
        alertas.append({"nivel": "alto", "texto": f"Te pasaste {pesos(gastado_sem - limite_sem)} de tu límite de la semana."})

    cajas_out = []
    for c in cajas:
        saldo = caja_saldo[c.id]
        recientes = [v for s, v in ahorro_semanal[c.id].items() if s >= sem - timedelta(weeks=4)]
        ritmo = sum(recientes) // 4 if recientes else 0
        faltan = max(c.meta_monto - saldo, 0)
        semanas = -(-faltan // ritmo) if c.meta_monto and ritmo > 0 and faltan else None
        cajas_out.append({
            "id": c.id, "nombre": c.nombre, "saldo": saldo, "meta_nombre": c.meta_nombre,
            "meta_monto": c.meta_monto, "rendimiento_anual": c.rendimiento_anual, "es_destino": c.es_destino,
            "progreso": min(saldo / c.meta_monto, 1) if c.meta_monto else None,
            "ritmo_semanal": ritmo,
            "meta_fecha": (hoy + timedelta(weeks=semanas)).isoformat() if semanas else None,
        })

    racha = 0
    s = sem if sem in semanas_ahorro else sem - timedelta(weeks=1)
    while s in semanas_ahorro:
        racha += 1
        s -= timedelta(weeks=1)

    fin_sem = sem + timedelta(days=7)
    ahorro_total = sum(c["saldo"] for c in cajas_out)
    quedan = efectivo
    # Lo que puedes gastar al dia hasta el proximo pago (hoy incluido), en pesos cerrados.
    por_dia = max(quedan, 0) // max((fin_sem - hoy).days, 1) // 100 * 100
    mensaje = (f"Te quedan {pesos(quedan)} de {pesos(limite_sem)} esta semana "
               f"({pesos(por_dia)} por día hasta el jueves).") if limite_sem else f"Tienes {pesos(quedan)} en efectivo."
    return {
        "hoy": hoy.isoformat(),
        "mensaje": mensaje,
        "semana": {
            "inicio": sem.isoformat(),
            "proximo_pago": fin_sem.isoformat(),
            "dias_para_pago": (fin_sem - hoy).days,
            "limite": limite_sem,
            "gastado": gastado_sem,
            "por_dia": por_dia,
        },
        "efectivo": efectivo,
        "fondo": fondo,
        "ahorro_total": ahorro_total,
        "deuda_total": deuda_total,
        "a_pagar_total": a_pagar_total,
        "patrimonio": efectivo + fondo + ahorro_total - deuda_total,
        "tarjetas": tarjetas_out,
        "cajas": cajas_out,
        "racha_ahorro": racha,
        "categorias_semana": dict(cat_sem),
        "categorias_mes": dict(cat_mes),
        "semanas": [{"inicio": (sem - timedelta(weeks=i)).isoformat(),
                     "gastado": semanas_gasto.get(sem - timedelta(weeks=i), 0)} for i in range(7, -1, -1)],
        "alertas": alertas,
    }


def _calendario(t: Tarjeta, c: Ciclo, a_pagar: int, ciclo_actual: int, mensualidades: dict[date, list[dict]]) -> list[dict]:
    """Los proximos estados de cuenta, mes por mes: el ya cerrado, el que va en curso y los futuros
    (de esos solo se conocen las mensualidades a meses)."""
    out = [{"fecha_limite": c.fecha_limite.isoformat(), "corte": c.ultimo_corte.isoformat(), "estado": "cerrado",
            "monto": a_pagar, "monto_meses": sum(x["mensualidad"] for x in mensualidades.get(c.fecha_limite, [])),
            "detalle": mensualidades.get(c.fecha_limite, [])}]
    corte = c.proximo_corte
    ultimo = max(mensualidades, default=c.fecha_limite)
    while len(out) < 13:
        limite = _limite(corte, t.dia_pago)
        if len(out) > 1 and limite > ultimo:
            break
        detalle = mensualidades.get(limite, [])
        meses = sum(x["mensualidad"] for x in detalle)
        en_curso = len(out) == 1
        out.append({"fecha_limite": limite.isoformat(), "corte": corte.isoformat(),
                    "estado": "en_curso" if en_curso else "futuro",
                    "monto": ciclo_actual if en_curso else meses, "monto_meses": meses, "detalle": detalle})
        corte = _dia_en(*_mes(corte.year, corte.month, 1), t.dia_corte)
    return out


def _tarjeta(t: Tarjeta, movs: list[Mov], hoy: date, alertas: list[dict], meses: list[dict],
             mensualidades: dict[date, list[dict]] | None = None) -> dict:
    c = ciclo(t.dia_corte, t.dia_pago, hoy)
    deuda = al_corte = pagos_despues = ciclo_actual = 0
    for m in movs:
        cargo = m.metodo == t.id and m.tipo in ("gasto", "inicial", "ajuste_tarjeta", "cargo_meses")
        pago = m.tipo == "pago_tarjeta" and m.tarjeta == t.id
        if not (cargo or pago):
            continue
        v = m.monto if cargo else -m.monto
        deuda += v
        if m.dia < c.ultimo_corte:
            al_corte += v
        elif pago:
            pagos_despues += m.monto
        else:
            ciclo_actual += m.monto
    a_pagar = max(al_corte - pagos_despues, 0)
    # Anticipo: lo pagado de mas sobre el estado cerrado baja lo que se debe del ciclo en curso. El
    # tope personal se mide contra lo gastado en el ciclo (antes del anticipo): es un limite de gasto.
    anticipo = max(pagos_despues - max(al_corte, 0), 0)
    gastado_ciclo = ciclo_actual
    ciclo_actual = max(gastado_ciclo - anticipo, 0)
    dias = (c.fecha_limite - hoy).days
    uso = gastado_ciclo / t.tope_personal if t.tope_personal else None
    if a_pagar and dias < 0:
        alertas.append({"nivel": "alto", "texto": f"{t.nombre}: se pasó la fecha límite con {pesos(a_pagar)} sin pagar."})
    elif a_pagar and dias <= AVISO_DIAS_PAGO:
        alertas.append({"nivel": "medio", "texto": f"{t.nombre}: paga {pesos(a_pagar)} antes del {c.fecha_limite.day} (faltan {dias} días)."})
    if uso is not None and uso >= 1:
        alertas.append({"nivel": "alto", "texto": f"{t.nombre}: ya pasaste tu tope de {pesos(t.tope_personal)} este ciclo."})
    elif uso is not None and uso >= AVISO_TOPE:
        alertas.append({"nivel": "medio", "texto": f"{t.nombre}: llevas {uso:.0%} de tu tope de {pesos(t.tope_personal)}."})
    return {
        "id": t.id, "nombre": t.nombre, "banco": t.banco, "ultimos4": t.ultimos4, "estado": t.estado, "nota": t.nota,
        "color": t.color,
        "dia_corte": t.dia_corte, "dia_pago": t.dia_pago,
        "limite_banco": t.limite_banco, "tope_personal": t.tope_personal,
        "ultimo_corte": c.ultimo_corte.isoformat(), "proximo_corte": c.proximo_corte.isoformat(),
        "fecha_limite": c.fecha_limite.isoformat(), "dias_para_pagar": dias,
        "a_pagar": a_pagar, "ciclo_actual": ciclo_actual, "gastado_ciclo": gastado_ciclo, "anticipo": anticipo,
        "deuda": deuda,
        "disponible_banco": max(t.limite_banco - deuda - sum(p["pendiente"] for p in meses), 0) if t.limite_banco else None,
        "meses": meses,
        "calendario": _calendario(t, c, a_pagar, ciclo_actual, mensualidades or {}),
        "meses_pendiente": sum(p["pendiente"] for p in meses),
        "meses_restante": sum(p["restante"] for p in meses),
        "uso_tope": uso,
    }


# ---------------------------------------------------------------- ¿cuánto gasto? (dia / semana / mes)

ES_GASTO = ("gasto", "compra_meses")  # una compra a MSI cuenta completa el dia que se hizo


def _rango(vista: str, fecha: date) -> tuple[date, date]:
    if vista == "dia":
        return fecha, fecha
    if vista == "semana":
        ini = inicio_semana(fecha)
        return ini, ini + timedelta(days=6)
    ini = fecha.replace(day=1)
    return ini, _dia_en(ini.year, ini.month, 31)


def _mover(vista: str, inicio: date, pasos: int) -> date:
    if vista == "dia":
        return inicio + timedelta(days=pasos)
    if vista == "semana":
        return inicio + timedelta(weeks=pasos)
    return date(*_mes(inicio.year, inicio.month, pasos), 1)


def gastos_periodo(movs: list[Mov], vista: str, fecha: date, hoy: date) -> dict:
    """Cuanto se gasto en el dia, la semana (jueves-miercoles) o el mes de `fecha`."""
    inicio, fin = _rango(vista, fecha)
    a_ini, a_fin = _rango(vista, _mover(vista, inicio, -1))
    gastos = [m for m in movs if m.tipo in ES_GASTO]
    del_periodo = sorted((m for m in gastos if inicio <= m.dia <= fin), key=lambda m: m.ts, reverse=True)
    por_cat: dict[str, int] = defaultdict(int)
    por_met: dict[str, int] = defaultdict(int)
    por_dia: dict[date, int] = defaultdict(int)
    for m in del_periodo:
        por_cat[m.categoria or "Otros"] += m.monto
        por_met[m.metodo or "efectivo"] += m.monto
        por_dia[m.dia] += m.monto
    total = sum(por_cat.values())
    dias = [inicio + timedelta(days=i) for i in range((fin - inicio).days + 1)]
    transcurridos = max(1, sum(1 for d in dias if d <= hoy))
    siguiente = _mover(vista, inicio, 1)
    return {
        "vista": vista,
        "inicio": inicio.isoformat(),
        "fin": fin.isoformat(),
        "total": total,
        "anterior_total": sum(m.monto for m in gastos if a_ini <= m.dia <= a_fin),
        "promedio_diario": round(total / transcurridos),
        "por_categoria": dict(sorted(por_cat.items(), key=lambda kv: -kv[1])),
        "por_metodo": dict(sorted(por_met.items(), key=lambda kv: -kv[1])),
        "barras": [] if vista == "dia" else [{"fecha": d.isoformat(), "total": por_dia.get(d, 0)} for d in dias],
        "lista": [{"id": m.id, "ts": m.ts, "monto": m.monto, "metodo": m.metodo, "categoria": m.categoria,
                    "nota": m.nota, "tipo": m.tipo} for m in del_periodo],
        "anterior": _mover(vista, inicio, -1).isoformat(),
        "siguiente": siguiente.isoformat() if siguiente <= hoy else None,
    }


# ---------------------------------------------------------------- simulador de meses

def simular_meses(planes, tarjetas, compras: list[dict], hoy: date, capacidad: int, ahorro: int,
                  aporte_ahorro: int) -> dict:
    """¿Me alcanza para comprar a meses? Suma por mes (el de la fecha limite) las mensualidades que
    ya tienes y las de las compras que estas pensando, contra lo que tu reparto manda al fondo de
    tarjetas en un mes (`capacidad`). Lo que el fondo no alcance sale del ahorro, y se ve como
    quedaria mes a mes (sumando lo que el reparto le mete cada mes).

    compras (centavos): {descripcion, mensualidad, meses, tarjeta (id o None), primer_mes ("AAAA-MM"
    o None), del_ahorro (cuantas de las primeras mensualidades pagas directo del ahorro)}."""
    por_tarjeta = {t.id: t for t in tarjetas}
    mes_hoy = (hoy.year, hoy.month)
    ahorro_hoy = ahorro
    tabla: dict[tuple[int, int], dict] = {}

    def fila(mes):
        return tabla.setdefault(mes, {"actuales": 0, "nuevas": 0, "ahorro_directo": 0, "detalle": []})

    for p in planes:
        t = por_tarjeta.get(p.tarjeta)
        if not t:
            continue
        for c in cargos_plan(p, t):
            if c.fecha_limite >= hoy:
                f = fila((c.fecha_limite.year, c.fecha_limite.month))
                f["actuales"] += p.mensualidad
                f["detalle"].append({"descripcion": p.descripcion, "monto": p.mensualidad, "nueva": False})

    for c in compras:
        if c.get("primer_mes"):
            primero = tuple(int(x) for x in c["primer_mes"].split("-"))
        elif (t := por_tarjeta.get(c.get("tarjeta") or "")):
            # lo que compras hoy entra al estado del proximo corte y se paga en su fecha limite
            lim = _limite(ciclo(t.dia_corte, t.dia_pago, hoy).proximo_corte, t.dia_pago)
            primero = (lim.year, lim.month)
        else:
            primero = _mes(hoy.year, hoy.month, 1)
        for k in range(c["meses"]):
            mes = _mes(primero[0], primero[1], k)
            if mes < mes_hoy:
                continue
            f = fila(mes)
            directo = k < c.get("del_ahorro", 0)
            f["ahorro_directo" if directo else "nuevas"] += c["mensualidad"]
            f["detalle"].append({"descripcion": f"{c['descripcion']} {k + 1}/{c['meses']}", "monto": c["mensualidad"],
                                 "nueva": True, "del_ahorro": directo})

    meses = []
    if tabla:
        ultimo = max(tabla)
        mes, k = mes_hoy, 0
        while mes <= ultimo and k < 48:
            f = tabla.get(mes) or {"actuales": 0, "nuevas": 0, "ahorro_directo": 0, "detalle": []}
            total = f["actuales"] + f["nuevas"]
            faltante = max(0, total - capacidad)
            if k > 0:
                ahorro += aporte_ahorro
            ahorro -= faltante + f["ahorro_directo"]
            meses.append({"mes": f"{mes[0]}-{mes[1]:02d}", "actuales": f["actuales"], "nuevas": f["nuevas"],
                          "total": total, "capacidad": capacidad, "libre": capacidad - total,
                          "del_ahorro": faltante + f["ahorro_directo"], "ahorro_despues": ahorro,
                          "detalle": f["detalle"]})
            mes, k = _mes(mes[0], mes[1], 1), k + 1

    peor = max(meses, key=lambda m: m["total"], default=None)
    pct = round(peor["total"] * 100 / capacidad) if peor and capacidad > 0 else None
    pasados = [m["mes"] for m in meses if m["total"] > capacidad]
    if not compras:
        veredicto = ("info", "Agrega lo que quieres comprar y te digo cómo quedan tus meses.")
    elif capacidad <= 0:
        veredicto = ("info", "Todavía no tengo tus nóminas para saber cuánto entra a tu fondo cada mes.")
    elif min((m["ahorro_despues"] for m in meses), default=0) < 0:
        veredicto = ("alto", "No te alcanza: ni con tu ahorro cubres los meses en que el fondo no da.")
    elif pasados:
        veredicto = ("alto", f"Te pasas en {len(pasados)} {'mes' if len(pasados) == 1 else 'meses'}: el fondo no "
                             f"alcanza y lo que falta sale de tu ahorro ({pesos(peor['total'] - capacidad)} el peor mes).")
    elif pct >= 85:
        veredicto = ("medio", f"Muy justo: el peor mes se lleva el {pct}% de tu fondo y casi no te queda para usar la tarjeta en otra cosa.")
    elif pct >= 60:
        veredicto = ("medio", f"Aguanta: el peor mes se lleva el {pct}% de tu fondo. Cuida lo demás que pagues con tarjeta.")
    else:
        veredicto = ("bajo", f"Vas holgado: el peor mes se lleva el {pct}% de tu fondo.")
    return {"capacidad": capacidad, "ahorro_hoy": ahorro_hoy,
            "aporte_ahorro": aporte_ahorro, "meses": meses, "peor_mes": peor["mes"] if peor else None, "peor_pct": pct,
            "veredicto": {"nivel": veredicto[0], "texto": veredicto[1]}}
