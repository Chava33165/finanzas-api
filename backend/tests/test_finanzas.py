from datetime import date, datetime

from app import finanzas as f
from app.finanzas import TZ, Mov, Tarjeta, Caja


def ts(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=TZ).timestamp()


# ---------------------------------------------------------------- reparto

def test_reparto_2500_es_800_800_900():
    r = f.repartir(250000, 0)
    assert (r.gastos, r.fondo, r.ahorro) == (80000, 80000, 90000)
    assert r.retirar == 80000 and r.sobrante_a_ahorro == 0


def test_reparto_proporcional_cuadra_exacto():
    r = f.repartir(250500, 0)  # nomina real: $2,505
    assert r.gastos + r.fondo + r.ahorro == 250500
    assert r.gastos == 80200 and r.fondo == 80200  # 32 % redondeado a pesos


def test_sobrante_no_se_retira_y_va_al_ahorro():
    r = f.repartir(250000, 20000)
    assert r.retirar == 60000
    assert r.sobrante_a_ahorro == 20000
    assert r.retirar + r.fondo + r.ahorro + r.sobrante_a_ahorro == 250000


def test_sobrante_mayor_que_gastos():
    r = f.repartir(250000, 100000)
    assert r.retirar == 0 and r.sobrante_a_ahorro == 80000


# ---------------------------------------------------------------- ciclos de tarjeta

def test_ciclo_nu_corte_18_paga_28_mismo_mes():
    c = f.ciclo(18, 28, date(2026, 10, 20))
    assert c.ultimo_corte == date(2026, 10, 18)
    assert c.fecha_limite == date(2026, 10, 28)
    assert c.proximo_corte == date(2026, 11, 18)


def test_ciclo_plata_corte_20_paga_20_mes_siguiente():
    c = f.ciclo(20, 20, date(2026, 10, 25))
    assert c.ultimo_corte == date(2026, 10, 20)
    assert c.fecha_limite == date(2026, 11, 20)


def test_el_dia_de_corte_el_estado_ya_salio():
    # Nu, 18 oct: ya trae $1,760.31 a pagar el 28 oct y lo nuevo es del ciclo que cierra el 18 nov.
    c = f.ciclo(18, 28, date(2026, 10, 18))
    assert c.ultimo_corte == date(2026, 10, 18)
    assert c.fecha_limite == date(2026, 10, 28)
    assert c.proximo_corte == date(2026, 11, 18)


def test_plata_antes_de_su_corte():
    c = f.ciclo(20, 20, date(2026, 10, 18))
    assert c.ultimo_corte == date(2026, 9, 20)
    assert c.proximo_corte == date(2026, 10, 20)


def test_mes_corto_ajusta_el_dia():
    c = f.ciclo(31, 10, date(2027, 3, 5))
    assert c.ultimo_corte == date(2027, 2, 28)
    assert c.fecha_limite == date(2027, 3, 10)


def test_inicio_de_semana_es_jueves():
    assert f.inicio_semana(date(2026, 10, 18)) == date(2026, 10, 15)  # domingo -> jueves 15
    assert f.inicio_semana(date(2026, 10, 15)) == date(2026, 10, 15)


# ---------------------------------------------------------------- estado

NU = Tarjeta("nu", "Nu", 18, 28, limite_banco=3000000, tope_personal=300000)
PLATA = Tarjeta("plata", "Plata", 20, 20, limite_banco=1000000, tope_personal=200000)
CAJA = Caja(1, "Ahorro", es_destino=True)
HOY = date(2026, 10, 20)  # martes; semana desde el jueves 15


def estado(movs, tarjetas=(NU, PLATA), cajas=(CAJA,)):
    return f.estado(list(movs), list(tarjetas), list(cajas), HOY)


def pago(t, monto=250000, sobrante=0):
    r = f.repartir(monto, sobrante)
    return Mov(1, t, "pago_recibido", monto, reparto=r, caja=1)


def test_efectivo_de_la_semana():
    e = estado([
        pago(ts(2026, 10, 15)),
        Mov(2, ts(2026, 10, 16), "gasto", 30000, metodo="efectivo", categoria="Comida"),
        Mov(3, ts(2026, 10, 17), "gasto", 7000, metodo="efectivo", categoria="Transporte"),
    ])
    assert e["semana"]["limite"] == 80000
    assert e["semana"]["gastado"] == 37000
    assert e["efectivo"] == 43000
    assert e["fondo"] == 80000
    assert e["cajas"][0]["saldo"] == 90000
    assert "Te quedan $430 de $800" in e["mensaje"]


def test_tarjeta_a_pagar_del_corte_y_ciclo_actual():
    e = estado([
        pago(ts(2026, 10, 1)),
        Mov(2, ts(2026, 10, 10), "gasto", 120000, metodo="nu", categoria="Comida"),
        Mov(3, ts(2026, 10, 19), "gasto", 50000, metodo="nu", categoria="Salidas"),
    ])
    nu = next(t for t in e["tarjetas"] if t["id"] == "nu")
    assert nu["a_pagar"] == 120000  # lo del corte del 18
    assert nu["fecha_limite"] == "2026-10-28"
    assert nu["dias_para_pagar"] == 8
    assert nu["ciclo_actual"] == 50000
    assert nu["deuda"] == 170000
    assert nu["disponible_banco"] == 3000000 - 170000


def test_pagar_reduce_fondo_y_lo_del_corte():
    e = estado([
        pago(ts(2026, 10, 1)),
        Mov(2, ts(2026, 10, 10), "gasto", 50000, metodo="nu", categoria="Comida"),
        Mov(3, ts(2026, 10, 19), "pago_tarjeta", 50000, tarjeta="nu"),
    ])
    nu = next(t for t in e["tarjetas"] if t["id"] == "nu")
    assert nu["a_pagar"] == 0 and nu["deuda"] == 0
    assert e["fondo"] == 30000


def test_alerta_si_el_fondo_no_alcanza():
    e = estado([
        Mov(1, ts(2026, 10, 1), "inicial", 400000, metodo="nu"),  # deuda vieja de $4,000
        pago(ts(2026, 10, 15)),
    ])
    assert any("fondo no alcanza" in a["texto"] for a in e["alertas"])


def test_aviso_de_tope_personal():
    e = estado([Mov(1, ts(2026, 10, 19), "gasto", 250000, metodo="nu", categoria="Otros")])
    nu = next(t for t in e["tarjetas"] if t["id"] == "nu")
    assert nu["uso_tope"] == 250000 / 300000
    assert any("tope" in a["texto"] for a in e["alertas"])


def test_cajas_transferencia_y_retiro():
    viaje = Caja(2, "Viaje")
    e = estado([
        pago(ts(2026, 10, 15)),
        Mov(2, ts(2026, 10, 16), "transferencia_caja", 40000, caja=1, caja_destino=2),
        Mov(3, ts(2026, 10, 17), "retiro_caja", 10000, caja=2),
    ], cajas=(CAJA, viaje))
    saldos = {c["id"]: c["saldo"] for c in e["cajas"]}
    assert saldos == {1: 50000, 2: 30000}


def test_saldo_inicial_y_sobrante_al_ahorro():
    e = estado([
        Mov(1, ts(2026, 10, 1), "inicial", 20000, metodo="efectivo"),
        pago(ts(2026, 10, 15), sobrante=20000),
    ])
    assert e["efectivo"] == 80000  # 200 que tenia + 600 que retiro
    assert e["cajas"][0]["saldo"] == 90000 + 20000


def test_racha_de_ahorro_por_semanas():
    e = estado([pago(ts(2026, 10, 1)), pago(ts(2026, 10, 8)), pago(ts(2026, 10, 15))])
    assert e["racha_ahorro"] == 3


def test_gasto_por_categoria_semana():
    e = estado([
        Mov(1, ts(2026, 10, 16), "gasto", 10000, metodo="efectivo", categoria="Comida"),
        Mov(2, ts(2026, 10, 17), "gasto", 5000, metodo="nu", categoria="Comida"),
        Mov(3, ts(2026, 10, 10), "gasto", 9999, metodo="efectivo", categoria="Comida"),  # semana pasada
    ])
    assert e["categorias_semana"] == {"Comida": 15000}


# ---------------------------------------------------------------- compras a meses, ajustes, extras

def test_compra_a_meses_se_carga_en_cada_ciclo_hasta_el_ultimo_mes():
    # Nu, hoy 20 oct: la mensualidad de este ciclo (cierra 18 nov, se paga 28 nov) ya iba en lo capturado;
    # falta la del ciclo que cierra el 18 dic (se paga el 28 dic = ultimo pago "diciembre").
    p = f.Plan(1, "nu", "Celular", 19983, "2026-12", desde=date(2026, 11, 18))
    cargos = f.cargos_plan(p, NU)
    assert [(c.dia_inicio, c.fecha_limite) for c in cargos] == [(date(2026, 11, 18), date(2026, 12, 28))]
    e = f.estado([], [NU, PLATA], [CAJA], HOY, planes=[p])
    nu = next(t for t in e["tarjetas"] if t["id"] == "nu")
    assert nu["deuda"] == 0  # todavia no empieza ese ciclo
    assert nu["meses"][0]["pendiente"] == 19983
    assert nu["meses"][0]["pagos"] == ["2026-12-28"]
    assert nu["disponible_banco"] == 3000000 - 19983
    # ya en ese ciclo, la mensualidad aparece en lo que llevas
    e = f.estado([], [NU], [CAJA], date(2026, 11, 20), planes=[p])
    assert e["tarjetas"][0]["ciclo_actual"] == 19983


def test_ajuste_a_lo_que_dice_la_app():
    e = estado([
        Mov(1, ts(2026, 10, 10), "gasto", 100000, metodo="nu", categoria="Otros"),
        Mov(2, ts(2026, 10, 17), "ajuste_tarjeta", -20000, metodo="nu"),
    ])
    nu = next(t for t in e["tarjetas"] if t["id"] == "nu")
    assert nu["a_pagar"] == 80000


def test_ingreso_extra_a_caja_no_infla_el_efectivo():
    e = estado([
        Mov(1, ts(2026, 10, 16), "ingreso_extra", 1442784, metodo="caja", caja=1, nota="Fondo Plexus"),
        Mov(2, ts(2026, 10, 16), "ingreso_extra", 50000, metodo="fondo"),
    ])
    assert e["cajas"][0]["saldo"] == 1442784 and e["efectivo"] == 0 and e["fondo"] == 50000


def test_ultimo_mes_de_una_compra_a_msi():
    # Nu, 20 oct: 3 MSI -> pagos 28 nov, 28 dic, 28 ene (la 1a mensualidad cae en el ciclo actual)
    assert f.ultimo_mes_msi(NU, HOY, 3) == "2027-01"
    # Plata, 18 oct (antes de su corte del 20): pagos 20 oct?? no: el ciclo actual cierra el 20 oct y vence el 20 nov
    assert f.ultimo_mes_msi(PLATA, date(2026, 10, 18), 3) == "2027-01"


def test_compra_msi_cuenta_en_categorias_pero_la_deuda_es_por_mensualidad():
    p = f.Plan(7, "nu", "Audífonos", 26667, f.ultimo_mes_msi(NU, HOY, 3), desde=date(2026, 10, 18))
    e = f.estado([Mov(1, ts(2026, 10, 19), "compra_meses", 80000, metodo="nu", categoria="Personal", nota="Audífonos")],
                 [NU], [CAJA], HOY, planes=[p])
    nu = e["tarjetas"][0]
    assert nu["ciclo_actual"] == 26667  # solo la mensualidad de este ciclo
    assert nu["meses"][0]["restante"] == 3 * 26667
    assert nu["meses_restante"] == 3 * 26667
    assert e["categorias_semana"] == {"Personal": 80000}


def test_reparto_personalizado_semana_de_transicion():
    # 22 oct: todo a pagar tarjetas (1760.31 Nu + 644.69 Plata), 100 para el raite, 0 ahorro
    r = f.repartir_manual(10000, 240500, 0, sobrante_efectivo=0)
    assert (r.gastos, r.fondo, r.ahorro, r.retirar, r.sobrante_a_ahorro) == (10000, 240500, 0, 10000, 0)
    r = f.repartir_manual(10000, 240500, 0, sobrante_efectivo=3000)
    assert r.retirar == 7000 and r.sobrante_a_ahorro == 3000


def test_calendario_de_pagos_por_mes():
    # Nu, 20 oct: corte cerrado (pagar 28 oct), ciclo en curso (28 nov) y meses futuros (28 dic, 28 ene)
    celular = f.Plan(1, "nu", "Celular", 19983, "2026-12", desde=date(2026, 11, 18))
    audif = f.Plan(2, "nu", "Audífonos", 26667, f.ultimo_mes_msi(NU, HOY, 3), desde=date(2026, 10, 18))
    e = f.estado([
        Mov(1, ts(2026, 10, 10), "gasto", 176031, metodo="nu", categoria="Otros"),
        Mov(2, ts(2026, 10, 19), "gasto", 50000, metodo="nu", categoria="Comida"),
    ], [NU], [CAJA], HOY, planes=[celular, audif])
    cal = e["tarjetas"][0]["calendario"]
    assert [m["fecha_limite"] for m in cal] == ["2026-10-28", "2026-11-28", "2026-12-28", "2027-01-28"]
    assert [m["estado"] for m in cal] == ["cerrado", "en_curso", "futuro", "futuro"]
    assert cal[0]["monto"] == 176031
    assert cal[1]["monto"] == 50000 + 26667 and cal[1]["monto_meses"] == 26667
    assert cal[2]["monto"] == 19983 + 26667
    assert [x["descripcion"] for x in cal[2]["detalle"]] == ["Celular", "Audífonos"]
    assert cal[2]["detalle"][0]["ultimo"] is True
    assert cal[3]["monto"] == 26667


# ---------------------------------------------------------------- ¿cuánto gasto? (dia / semana / mes)

def _gastos():
    return [
        Mov(1, ts(2026, 10, 19, 9), "gasto", 12000, metodo="efectivo", categoria="Comida", nota="tacos"),
        Mov(2, ts(2026, 10, 19, 14), "gasto", 1200, metodo="efectivo", categoria="Transporte"),
        Mov(3, ts(2026, 10, 19, 18), "compra_meses", 80000, metodo="nu", categoria="Personal", nota="Audífonos"),
        Mov(4, ts(2026, 10, 16), "gasto", 5000, metodo="nu", categoria="Comida"),
        Mov(5, ts(2026, 10, 14), "gasto", 9000, metodo="efectivo", categoria="Salidas"),  # semana pasada (mié)
        Mov(6, ts(2026, 9, 30), "gasto", 30000, metodo="efectivo", categoria="Otros"),
        Mov(7, ts(2026, 10, 19), "pago_tarjeta", 50000, tarjeta="nu"),  # no es gasto
    ]


def test_periodo_dia():
    p = f.gastos_periodo(_gastos(), "dia", date(2026, 10, 19), hoy=date(2026, 10, 19))
    assert (p["inicio"], p["fin"]) == ("2026-10-19", "2026-10-19")
    assert p["total"] == 12000 + 1200 + 80000  # la compra a MSI cuenta completa
    assert p["por_categoria"] == {"Personal": 80000, "Comida": 12000, "Transporte": 1200}
    assert p["por_metodo"] == {"nu": 80000, "efectivo": 13200}
    assert [g["id"] for g in p["lista"]] == [3, 2, 1]  # lo mas nuevo primero
    assert p["siguiente"] is None and p["anterior"] == "2026-10-18"
    assert p["anterior_total"] == 0


def test_periodo_semana_de_jueves_a_miercoles():
    p = f.gastos_periodo(_gastos(), "semana", date(2026, 10, 19), hoy=date(2026, 10, 19))
    assert (p["inicio"], p["fin"]) == ("2026-10-15", "2026-10-21")
    assert p["total"] == 93200 + 5000
    assert len(p["barras"]) == 7 and p["barras"][1] == {"fecha": "2026-10-16", "total": 5000}
    assert p["anterior_total"] == 9000
    assert p["promedio_diario"] == round(98200 / 5)  # dias transcurridos: jue 15 .. lun 19
    assert p["siguiente"] is None and p["anterior"] == "2026-10-08"


def test_periodo_mes_y_navegacion():
    p = f.gastos_periodo(_gastos(), "mes", date(2026, 9, 10), hoy=date(2026, 10, 19))
    assert (p["inicio"], p["fin"]) == ("2026-09-01", "2026-09-30")
    assert len(p["barras"]) == 30 and p["total"] == 30000
    assert p["siguiente"] == "2026-10-01" and p["anterior"] == "2026-08-01"
    assert p["promedio_diario"] == 1000
    oct_ = f.gastos_periodo(_gastos(), "mes", date(2026, 10, 1), hoy=date(2026, 10, 19))
    assert len(oct_["barras"]) == 31 and oct_["anterior_total"] == 30000 and oct_["siguiente"] is None
