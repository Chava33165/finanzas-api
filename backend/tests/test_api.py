import os
import sqlite3
from datetime import date, timedelta

import pytest

os.environ["BEARER_TOKEN"] = "token-de-prueba"


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import db as basedatos
    from app import main

    viejo = tmp_path / "tasks.db"
    sqlite3.connect(viejo).executescript(  # la base del task-hub viejo, con su tabla de tareas
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT); INSERT INTO tasks(title) VALUES ('vieja');")
    monkeypatch.setattr(basedatos, "DB_PATH", viejo)
    monkeypatch.setattr(main, "NOMINAS_DIR", tmp_path / "sin-nominas")
    monkeypatch.setattr(basedatos, "conectar", lambda path=viejo, _c=basedatos.conectar: _c(path))
    with TestClient(main.app) as c:
        c.headers["Authorization"] = "Bearer token-de-prueba"
        yield c, viejo


def test_sin_token_no_entra(cliente):
    c, _ = cliente
    assert c.get("/api/v1/resumen", headers={"Authorization": "Bearer otro"}).status_code == 401
    assert c.get("/health").status_code == 200


def test_migracion_respeta_tareas_y_respalda(cliente):
    c, viejo = cliente
    assert sqlite3.connect(viejo).execute("SELECT title FROM tasks").fetchall() == [("vieja",)]
    assert list((viejo.parent / "respaldos").glob("tasks_v0_*.db"))


def test_flujo_completo(cliente):
    c, _ = cliente
    assert c.post("/api/v1/pagos-recibidos", json={"monto": 2500}).status_code == 409  # sin saldos iniciales
    assert c.post("/api/v1/saldos-iniciales", json={"efectivo": 999}).status_code == 200
    # corregir reemplaza lo anterior
    assert c.post("/api/v1/saldos-iniciales", json={"efectivo": 200, "deudas": {"nu": {"corte": 1000}}}).status_code == 200
    assert c.post("/api/v1/saldos-iniciales", json={"deudas": {"amex": {"corte": 1}}}).status_code == 422

    prev = c.get("/api/v1/reparto", params={"monto": 2500}).json()
    assert prev["retirar"] == 600 and prev["sobrante_a_ahorro"] == 200

    r = c.post("/api/v1/pagos-recibidos", json={"monto": 2500}).json()
    assert "Retira $600" in r["mensaje"]

    g = c.post("/api/v1/gastos", json={"monto": 120.5, "metodo": "efectivo", "categoria": "comida", "nota": "tacos"})
    assert g.status_code == 200 and "Te quedan $679.50 de $800" in g.json()["mensaje"]

    g = c.post("/api/v1/gastos", json={"monto": 300, "metodo": "nu", "categoria": "Salidas"}).json()
    assert g["mensaje"].startswith("Nu: llevas")

    e = c.get("/api/v1/resumen").json()
    assert e["fondo"] == 800 and e["cajas"][0]["saldo"] == 1100
    nu = next(t for t in e["tarjetas"] if t["id"] == "nu")
    assert nu["deuda"] == 1300
    assert nu["a_pagar"] == 1000 and nu["ciclo_actual"] == 300

    assert c.post("/api/v1/pagos-tarjeta", json={"tarjeta": "nu", "monto": 900}).status_code == 409  # fondo: 800
    assert c.post("/api/v1/pagos-tarjeta", json={"tarjeta": "nu", "monto": 800}).status_code == 200
    assert c.get("/api/v1/resumen").json()["fondo"] == 0


def test_validacion(cliente):
    c, _ = cliente
    assert c.post("/api/v1/gastos", json={"monto": -5}).status_code == 422
    assert c.post("/api/v1/gastos", json={"monto": 10, "metodo": "amex"}).status_code == 422
    assert c.post("/api/v1/gastos", json={"monto": 10, "categoria": "Casino"}).status_code == 422
    assert c.post("/api/v1/gastos", json={"monto": 10, "nota": "x" * 81}).status_code == 422
    assert c.put("/api/v1/tarjetas/nu", json={"nombre": "Nu", "dia_corte": 40, "dia_pago": 28}).status_code == 422


def test_cajas_y_tarjetas(cliente):
    c, _ = cliente
    c.post("/api/v1/saldos-iniciales", json={"ahorro": 1000})
    viaje = c.post("/api/v1/cajas", json={"nombre": "Viaje", "meta_nombre": "Cancún", "meta_monto": 8000}).json()["id"]
    assert c.post("/api/v1/cajas/1/movimientos", json={"tipo": "transferencia", "monto": 400, "destino": viaje}).status_code == 200
    assert c.post(f"/api/v1/cajas/{viaje}/movimientos", json={"tipo": "retiro", "monto": 999}).status_code == 409
    assert c.delete(f"/api/v1/cajas/{viaje}").status_code == 409  # todavia tiene dinero
    saldos = {x["id"]: x["saldo"] for x in c.get("/api/v1/resumen").json()["cajas"]}
    assert saldos == {1: 600, viaje: 400}

    ok = c.put("/api/v1/tarjetas/nu", json={"nombre": "Nu", "banco": "Nu México", "ultimos4": "1234", "dia_corte": 18,
                                           "dia_pago": 28, "limite_banco": 30000, "tope_personal": 3000})
    assert ok.status_code == 200
    nu = next(t for t in c.get("/api/v1/resumen").json()["tarjetas"] if t["id"] == "nu")
    assert (nu["limite_banco"], nu["tope_personal"], nu["ultimos4"]) == (30000, 3000, "1234")


def test_borrar_movimiento(cliente):
    c, _ = cliente
    mid = c.post("/api/v1/gastos", json={"monto": 50}).json()["id"]
    assert c.delete(f"/api/v1/movimientos/{mid}").status_code == 200
    assert c.get("/api/v1/movimientos").json() == []


def test_ajuste_meses_y_extras(cliente):
    c, _ = cliente
    c.post("/api/v1/saldos-iniciales", json={"deudas": {"nu": {"corte": 1500}}})
    assert c.post("/api/v1/tarjetas/nu/ajustar", json={"corte": 1760.31, "ciclo": 2506}).json()["mensaje"].endswith("igual que tu app.")
    nu = next(t for t in c.get("/api/v1/resumen").json()["tarjetas"] if t["id"] == "nu")
    assert (nu["a_pagar"], nu["ciclo_actual"]) == (1760.31, 2506)
    assert c.post("/api/v1/tarjetas/nu/ajustar", json={"corte": 1760.31}).json()["mensaje"].endswith("ya cuadraba.")

    assert c.post("/api/v1/planes", json={"tarjeta": "nu", "descripcion": "Celular", "mensualidad": 199.83,
                                          "ultimo_mes": "2099-13"}).status_code == 422
    pid = c.post("/api/v1/planes", json={"tarjeta": "nu", "descripcion": "Celular", "mensualidad": 199.83,
                                         "ultimo_mes": "2099-12"}).json()["id"]
    nu = next(t for t in c.get("/api/v1/resumen").json()["tarjetas"] if t["id"] == "nu")
    assert nu["meses"][0]["descripcion"] == "Celular" and nu["meses_pendiente"] > 0
    assert c.delete(f"/api/v1/planes/{pid}").status_code == 200

    r = c.post("/api/v1/ingresos-extra", json={"monto": 4000, "concepto": "aguinaldo"}).json()
    assert "aguinaldo" in r["mensaje"]
    e = c.get("/api/v1/resumen").json()
    assert e["cajas"][0]["saldo"] == 4000 and e["efectivo"] == 0


def test_gasto_a_meses_desde_el_atajo(cliente):
    c, _ = cliente
    r = c.post("/api/v1/gastos", json={"monto": 800, "metodo": "nu", "categoria": "Personal", "nota": "Audífonos", "meses": 3})
    assert r.status_code == 200, r.text
    assert "3 MSI" in r.json()["mensaje"] and "$266.67 al mes" in r.json()["mensaje"]
    nu = next(t for t in c.get("/api/v1/resumen").json()["tarjetas"] if t["id"] == "nu")
    assert nu["ciclo_actual"] == 266.67 and nu["meses"][0]["descripcion"] == "Audífonos"
    assert c.post("/api/v1/gastos", json={"monto": 800, "metodo": "efectivo", "meses": 3}).status_code == 422
    # borrar el gasto borra su compra a meses
    mid = r.json()["id"]
    assert c.delete(f"/api/v1/movimientos/{mid}").status_code == 200
    assert next(t for t in c.get("/api/v1/resumen").json()["tarjetas"] if t["id"] == "nu")["meses"] == []


def test_pago_con_reparto_personalizado(cliente):
    c, _ = cliente
    c.post("/api/v1/saldos-iniciales", json={"deudas": {"nu": {"corte": 1760.31}}})
    malo = {"monto": 2505, "sobrante": 0, "gastos": 100, "fondo": 2405, "ahorro": 100}
    assert c.post("/api/v1/pagos-recibidos", json=malo).status_code == 422  # no suma 2505
    prev = c.get("/api/v1/reparto", params={"monto": 2505, "sobrante": 0, "gastos": 100, "fondo": 2405, "ahorro": 0}).json()
    assert prev["fondo"] == 2405 and prev["retirar"] == 100
    ok = c.post("/api/v1/pagos-recibidos", json=malo | {"ahorro": 0})
    assert ok.status_code == 200, ok.text
    assert c.post("/api/v1/pagos-tarjeta", json={"tarjeta": "nu", "monto": 1760.31}).status_code == 200
    e = c.get("/api/v1/resumen").json()
    assert e["fondo"] == 644.69 and e["semana"]["limite"] == 100


def test_iniciales_para_prellenar(cliente):
    c, _ = cliente
    c.post("/api/v1/saldos-iniciales", json={"efectivo": 120, "deudas": {"nu": {"corte": 1760.31, "ciclo": 2506.83}}})
    ini = c.get("/api/v1/resumen").json()["iniciales"]
    assert ini["efectivo"] == 120 and ini["deudas"]["nu"] == {"corte": 1760.31, "ciclo": 2506.83}
    assert ini["deudas"]["plata"] == {"corte": 0, "ciclo": 0}


def test_atajo_manda_metodo_y_meses_juntos(cliente):
    c, _ = cliente
    r = c.post("/api/v1/gastos", json={"monto": 600, "metodo": "Nu · 3 MSI", "categoria": "Personal", "nota": "Tenis"})
    assert r.status_code == 200, r.text
    assert "3 MSI: $200 al mes" in r.json()["mensaje"]
    assert c.post("/api/v1/gastos", json={"monto": 50, "metodo": "Efectivo", "categoria": "Comida", "meses": ""}).status_code == 200


def test_apple_pay_y_cuanto_debo(cliente):
    c, _ = cliente
    c.post("/api/v1/saldos-iniciales", json={"deudas": {"nu": {"corte": 1760.31}}})
    r = c.post("/api/v1/gastos/applepay", json={"monto": "$1,250.00", "comercio": "OXXO GDL", "tarjeta": "Tarjeta de Crédito Nu"})
    assert r.status_code == 200, r.text
    assert r.json()["mensaje"].startswith("Nu: llevas $1,250")
    m = c.get("/api/v1/movimientos").json()[0]
    assert (m["monto"], m["metodo"], m["categoria"], m["nota"]) == (1250, "nu", "Comida", "OXXO GDL")
    b = c.post("/api/v1/gastos/applepay", json={"monto": "$10", "comercio": "Starbucks", "tarjeta": "BBVA"})
    assert b.status_code == 200 and c.get("/api/v1/movimientos").json()[0]["metodo"] == "efectivo"
    assert c.post("/api/v1/gastos/applepay", json={"monto": "$10", "tarjeta": "Amex"}).status_code == 422
    assert c.post("/api/v1/gastos/applepay", json={"monto": "gratis", "tarjeta": "Nu"}).status_code == 422
    d = c.get("/api/v1/deuda").json()
    assert "Nu: paga $1,760.31 antes del" in d["mensaje"] and "Plata: no debes nada" in d["mensaje"]
    assert "Fondo: $0, te faltan $1,760.31" in d["mensaje"]


def test_errores_traen_mensaje_para_el_atajo(cliente):
    c, _ = cliente
    r = c.post("/api/v1/gastos/applepay", json={"monto": "$10", "tarjeta": "Amex"})
    assert r.json()["mensaje"].startswith("⚠️ No reconozco la tarjeta")
    assert c.post("/api/v1/gastos", json={"monto": -1}).json()["mensaje"].startswith("⚠️")
    assert c.get("/api/v1/resumen", headers={"Authorization": "Bearer x"}).json()["mensaje"] == "⚠️ token inválido"


def test_calibrar_deja_todo_igual_que_la_realidad(cliente):
    c, _ = cliente
    assert c.post("/api/v1/calibrar", json={"efectivo": 10}).status_code == 409  # sin arrancar
    c.post("/api/v1/saldos-iniciales", json={"efectivo": 500, "fondo": 300, "ahorro": 1000, "deudas": {"nu": {"corte": 800, "ciclo": 200}}})
    caja = c.get("/api/v1/resumen").json()["cajas"][0]["id"]

    r = c.post("/api/v1/calibrar", json={"efectivo": 420, "fondo": 350, "cajas": {str(caja): 1200},
                                          "tarjetas": {"nu": {"corte": 750, "ciclo": 260}}}).json()
    assert len(r["cambios"]) == 5 and "Efectivo: $500 → $420" in r["mensaje"]
    e = c.get("/api/v1/resumen").json()
    nu = next(t for t in e["tarjetas"] if t["id"] == "nu")
    assert (e["efectivo"], e["fondo"], e["cajas"][0]["saldo"]) == (420, 350, 1200)
    assert (nu["a_pagar"], nu["ciclo_actual"]) == (750, 260)
    assert e["ultima_calibracion"]
    # los $80 que faltaban de efectivo son gastos sin anotar: cuentan en la semana (el dia de pago, en la que acaba)
    from app import main
    sin_anotar = 0 if main.hoy().weekday() == 3 else 80
    assert e["semana"]["gastado"] == sin_anotar
    # sin cambios no mete nada
    assert c.post("/api/v1/calibrar", json={"efectivo": 420}).json()["mensaje"] == "Todo ya cuadraba ✓"
    # sin contarlo como gasto: queda como calibracion pura
    c.post("/api/v1/calibrar", json={"efectivo": 400, "faltante_como_gasto": False})
    e = c.get("/api/v1/resumen").json()
    assert e["efectivo"] == 400 and e["semana"]["gastado"] == sin_anotar
    assert c.post("/api/v1/calibrar", json={"tarjetas": {"amex": {"corte": 1}}}).status_code == 422
    assert c.post("/api/v1/calibrar", json={"cajas": {"999": 1}}).status_code == 422


def test_hoy_y_borrar_ultimo_gasto(cliente):
    c, _ = cliente
    c.post("/api/v1/saldos-iniciales", json={"efectivo": 500})
    c.post("/api/v1/pagos-recibidos", json={"monto": 2500})
    assert "no has gastado" in c.get("/api/v1/hoy").json()["mensaje"]
    c.post("/api/v1/gastos", json={"monto": 50, "categoria": "Comida", "nota": "tacos"})
    c.post("/api/v1/gastos", json={"monto": 30, "categoria": "Transporte", "nota": "uber"})
    h = c.get("/api/v1/hoy").json()
    assert h["total"] == 80 and "2 gastos" in h["mensaje"] and "por día" in h["mensaje"]
    r = c.delete("/api/v1/gastos/ultimo").json()
    assert "Borré $30 de uber" in r["mensaje"]
    assert c.get("/api/v1/hoy").json()["total"] == 50


def test_ingreso_extra_repartido(cliente):
    c, _ = cliente
    c.post("/api/v1/saldos-iniciales", json={"deudas": {"nu": {"corte": 800}}})
    caja = c.get("/api/v1/resumen").json()["cajas"][0]["id"]
    partes = [{"destino": "tarjeta", "tarjeta": "nu", "monto": 800}, {"destino": "fondo", "monto": 500},
              {"destino": "caja", "caja": caja, "monto": 8700}]
    assert c.post("/api/v1/ingresos-extra", json={"monto": 9000, "concepto": "fondo_plexus", "partes": partes}).status_code == 422
    r = c.post("/api/v1/ingresos-extra", json={"monto": 10000, "concepto": "fondo_plexus", "partes": partes}).json()
    assert "pagaste $800 a Nu" in r["mensaje"]
    e = c.get("/api/v1/resumen").json()
    nu = next(t for t in e["tarjetas"] if t["id"] == "nu")
    assert nu["deuda"] == 0 and e["fondo"] == 500 and e["cajas"][0]["saldo"] == 8700
    demasiado = [{"destino": "tarjeta", "tarjeta": "nu", "monto": 1}]
    assert c.post("/api/v1/ingresos-extra", json={"monto": 1, "partes": demasiado}).status_code == 409


def test_pago_a_mano_se_liga_a_su_nomina(cliente):
    c, viejo = cliente
    from app import main
    c.post("/api/v1/saldos-iniciales", json={"efectivo": 100})
    main.conn.execute("INSERT INTO nominas(uuid, semana, fecha_pago, neto, fondo_ahorro, fondo_tuyo, fondo_empresa, fondo_saldo_total) VALUES ('U-42', 42, ?, 210150, 0, 0, 0, 0)",
                      (main.hoy().isoformat(),))
    main.conn.commit()
    assert c.get("/api/v1/resumen").json()["nomina_pendiente"]["uuid"] == "U-42"
    c.post("/api/v1/pagos-recibidos", json={"monto": 2101.5})
    assert c.get("/api/v1/resumen").json()["nomina_pendiente"] is None


def test_proporcion_33_33_34(cliente):
    c, _ = cliente
    c.post("/api/v1/saldos-iniciales", json={"efectivo": 0})
    assert c.put("/api/v1/proporcion", json={"gastos": 33, "fondo": 33, "ahorro": 33}).status_code == 422
    assert c.put("/api/v1/proporcion", json={"gastos": 33, "fondo": 33, "ahorro": 34}).status_code == 200
    assert c.get("/api/v1/resumen").json()["proporcion"] == [33, 33, 34]
    r = c.post("/api/v1/pagos-recibidos", json={"monto": 3000}).json()
    assert (r["gastos"], r["fondo"], r["ahorro"]) == (990, 990, 1020)


def test_gasto_de_dinero_extra_no_toca_la_semana(cliente):
    c, _ = cliente
    c.post("/api/v1/saldos-iniciales", json={"efectivo": 100})
    partes = [{"destino": "gasto", "categoria": "Personal", "nota": "Regalo a mis papás", "monto": 10000},
              {"destino": "caja", "monto": 5000}]
    r = c.post("/api/v1/ingresos-extra", json={"monto": 15000, "concepto": "fondo_plexus", "partes": partes}).json()
    assert "$10,000 en regalo a mis papás" in r["mensaje"]
    e = c.get("/api/v1/resumen").json()
    assert e["efectivo"] == 100 and e["semana"]["gastado"] == 0
    assert e["categorias_mes"]["Personal"] == 10000 and e["cajas"][0]["saldo"] == 5000


def test_anticipo_baja_lo_del_ciclo(cliente):
    c, _ = cliente
    c.post("/api/v1/saldos-iniciales", json={"fondo": 2000, "deudas": {"nu": {"ciclo": 4632.29}}})
    c.post("/api/v1/pagos-tarjeta", json={"tarjeta": "nu", "monto": 2000})
    nu = next(t for t in c.get("/api/v1/resumen").json()["tarjetas"] if t["id"] == "nu")
    assert nu["a_pagar"] == 0 and nu["anticipo"] == 2000
    assert nu["ciclo_actual"] == 2632.29 and nu["gastado_ciclo"] == 4632.29 and nu["deuda"] == 2632.29


def test_fondo_plexus_cobrado_sigue_cobrado_con_recibos_nuevos(cliente):
    c, _ = cliente
    from app import main
    c.post("/api/v1/saldos-iniciales", json={"efectivo": 0})
    def recibo(sem, fecha, total):
        main.conn.execute("INSERT INTO nominas(uuid, semana, fecha_pago, neto, fondo_ahorro, fondo_tuyo, fondo_empresa, "
                          "fondo_saldo_total) VALUES (?, ?, ?, 0, 0, 0, 0, ?)", (f"u{sem}", sem, fecha, total))
        main.conn.commit()
    recibo(42, "2026-10-22", 1519616)
    recibo(43, "2026-10-29", 106066)
    fp = c.get("/api/v1/resumen").json()["fondo_plexus"]
    assert fp["liquidado"] == 15196.16 and not fp["cobrado"]
    c.post("/api/v1/ingresos-extra", json={"monto": 15439, "concepto": "fondo_plexus"})
    assert c.get("/api/v1/resumen").json()["fondo_plexus"]["cobrado"]
    recibo(44, "2026-11-05", 212132)  # el fondo nuevo crece: sigue cobrado
    assert c.get("/api/v1/resumen").json()["fondo_plexus"]["cobrado"]


# ---------------------------------------------------------------- lo que falta por semana


def _recibo(main, sem, pago, uuid=None):
    p = date.fromisoformat(pago)
    main.conn.execute(
        "INSERT INTO nominas(uuid, semana, periodo_ini, periodo_fin, fecha_pago, neto, fondo_ahorro, fondo_tuyo, "
        "fondo_empresa, fondo_saldo_total) VALUES (?, ?, ?, ?, ?, 250000, 0, 0, 0, 0)",
        (uuid or f"U-{sem}", sem, (p - timedelta(days=10)).isoformat(), (p - timedelta(days=4)).isoformat(), pago))
    main.conn.commit()


def test_pendientes_dice_que_falta_cada_semana(cliente, monkeypatch):
    c, _ = cliente
    from app import main
    c.post("/api/v1/saldos-iniciales", json={"efectivo": 100})
    main.conn.execute("UPDATE ajustes SET v='2026-10-18' WHERE k='inicio'")
    main.conn.commit()
    monkeypatch.setattr(main, "hoy", lambda: date(2026, 11, 13))  # viernes
    _recibo(main, 42, "2026-10-22")
    _recibo(main, 43, "2026-10-29")
    _recibo(main, 45, "2026-11-12")
    # el 42 se registro ligado a su recibo; el 43 todavia no
    main.conn.execute("INSERT INTO movimientos(ts, tipo, monto, nomina_uuid, origen, r_gastos, r_fondo, r_ahorro, r_sobrante, r_retirar) VALUES (?, 'pago_recibido', 250000, 'U-42', 'api', 0, 0, 0, 0, 0)",
                      (main.ts_de(date(2026, 10, 22)),))
    main.conn.commit()

    pend = c.get("/api/v1/resumen").json()["pendientes"]
    assert [(p["semana"], p["recibo"], p["pago"]) for p in pend] == [(43, True, False), (44, False, False), (45, True, False)]
    s44 = pend[1]
    assert (s44["periodo_ini"], s44["periodo_fin"], s44["fecha_pago"], s44["neto"]) == ("2026-10-26", "2026-11-01", "2026-11-05", None)
    assert pend[0]["neto"] == 2500  # en pesos
    # para ponerse al dia en orden: primero el recibo mas viejo
    assert c.get("/api/v1/resumen").json()["nomina_pendiente"]["semana"] == 43


def test_pago_a_mano_sin_recibo_cuenta_para_su_semana(cliente, monkeypatch):
    c, _ = cliente
    from app import main
    c.post("/api/v1/saldos-iniciales", json={"efectivo": 100})
    main.conn.execute("UPDATE ajustes SET v='2026-11-02' WHERE k='inicio'")
    main.conn.execute("INSERT INTO movimientos(ts, tipo, monto, origen, r_gastos, r_fondo, r_ahorro, r_sobrante, r_retirar) VALUES (?, 'pago_recibido', 240000, 'api', 0, 0, 0, 0, 0)",
                      (main.ts_de(date(2026, 11, 6)),))
    main.conn.commit()
    monkeypatch.setattr(main, "hoy", lambda: date(2026, 11, 13))
    pend = c.get("/api/v1/resumen").json()["pendientes"]
    assert [(p["semana"], p["recibo"], p["pago"]) for p in pend] == [(44, False, True), (45, False, False)]
    # cuando llega el recibo de esa semana ya no se ofrece registrarlo otra vez
    _recibo(main, 44, "2026-11-05")
    r = c.get("/api/v1/resumen").json()
    assert [p["semana"] for p in r["pendientes"]] == [45]
    assert r["nomina_pendiente"] is None


def test_ponerse_al_dia_con_un_pago_de_otra_semana(cliente, monkeypatch):
    c, _ = cliente
    from app import main
    c.post("/api/v1/saldos-iniciales", json={"efectivo": 0})
    main.conn.execute("UPDATE ajustes SET v=\x272026-11-02\x27 WHERE k=\x27inicio\x27")
    main.conn.commit()
    monkeypatch.setattr(main, "hoy", lambda: date(2026, 11, 13))
    assert c.post("/api/v1/pagos-recibidos", json={"monto": 2400, "fecha": "2026-11-05"}).status_code == 200
    pend = c.get("/api/v1/resumen").json()["pendientes"]  # la 44 ya tiene pago; solo le falta el recibo
    assert [(p["semana"], p["recibo"], p["pago"]) for p in pend] == [(44, False, True), (45, False, False)]
    assert c.post("/api/v1/pagos-recibidos", json={"monto": 2400, "fecha": "2026-11-20"}).status_code == 422


# ---------------------------------------------------------------- simulador de meses

def test_simulador_de_meses(cliente, monkeypatch):
    c, _ = cliente
    from app import main
    c.post("/api/v1/saldos-iniciales", json={"efectivo": 0})
    monkeypatch.setattr(main, "hoy", lambda: date(2026, 11, 13))
    for k in range(8):  # $2,400 netos por semana: al fondo (33%) van 2400*52/12*0.33 = $3,432 al mes
        main.conn.execute("INSERT INTO nominas(uuid, semana, fecha_pago, neto, fondo_ahorro, fondo_tuyo, fondo_empresa, fondo_saldo_total) "
                          "VALUES (?, ?, ?, 240000, 0, 0, 0, 0)", (f"S{k}", 36 + k, (date(2026, 9, 3) + timedelta(days=7 * k)).isoformat()))
    main.conn.commit()
    monkeypatch.setattr(main, "proporcion", lambda: (33, 33, 34))

    vacio = c.post("/api/v1/simulador", json={"compras": []}).json()
    assert vacio["capacidad"] == 3432 and vacio["veredicto"]["nivel"] == "info"

    r = c.post("/api/v1/simulador", json={"compras": [
        {"descripcion": "Celular", "total": 3065.40 * 5, "meses": 5, "primer_mes": "2026-12", "del_ahorro": 1},
        {"descripcion": "Botas", "total": 2869, "meses": 6, "primer_mes": "2026-12"},
    ]}).json()
    por_mes = {m["mes"]: m for m in r["meses"]}
    assert list(por_mes)[0] == "2026-11" and list(por_mes)[-1] == "2027-05"
    dic, ene = por_mes["2026-12"], por_mes["2027-01"]
    assert dic["nuevas"] == 478.17 and dic["del_ahorro"] == 3065.40  # la 2/6 del celular, directo del ahorro
    assert ene["nuevas"] == round(3065.40 + 478.17, 2) and ene["del_ahorro"] == 111.57  # el fondo no alcanza por $111.57
    assert r["veredicto"]["nivel"] == "alto"

    holgado = c.post("/api/v1/simulador", json={"compras": [{"total": 1909.86, "meses": 3, "tarjeta": "nu"}]})
    assert holgado.status_code == 200 and holgado.json()["veredicto"]["nivel"] == "bajo"
    assert c.post("/api/v1/simulador", json={"compras": [{"total": 100, "meses": 3, "tarjeta": "no-existe"}]}).status_code == 404


# ---------------------------------------------------------------- subir recibo desde la app

def test_subir_recibo_lo_acomoda_como_korra(cliente, tmp_path, monkeypatch):
    c, _ = cliente
    from app import main, nominas
    plexus = tmp_path / "PLEXUS"
    plexus.mkdir()
    monkeypatch.setattr(main, "NOMINAS_DIR", plexus)
    leido = {"uuid": "UP-46", "semana": 46, "periodo_ini": "2026-11-09", "periodo_fin": "2026-11-15",
             "fecha_pago": "2026-11-19", "neto": 250550, "fondo_ahorro": 0, "fondo_tuyo": 0, "fondo_empresa": 0,
             "fondo_saldo_total": 0, "conceptos": {"percepciones": [], "deducciones": []}}
    monkeypatch.setattr(nominas, "leer_pdf", lambda ruta: dict(leido))
    pdf = {"content": b"%PDF-1.7 recibo", "headers": {"Content-Type": "application/pdf"}}

    r = c.post("/api/v1/nominas/recibo", **pdf)
    assert r.status_code == 200, r.text
    assert r.json()["nuevo"] and r.json()["neto"] == 2505.5 and r.json()["semana"] == 46
    guardado = plexus / "Semana_46" / "Plexus_nomina_semana_46_2026-11-09_2026-11-15.pdf"
    assert guardado.read_bytes() == b"%PDF-1.7 recibo"
    assert [n["semana"] for n in c.get("/api/v1/nominas").json()] == [46]
    assert not list(plexus.glob(".subida-*"))  # no deja temporales

    otra = c.post("/api/v1/nominas/recibo", **pdf).json()  # el mismo otra vez: no se duplica
    assert not otra["nuevo"] and "ya estaba" in otra["mensaje"]
    assert len(list((plexus / "Semana_46").iterdir())) == 1

    assert c.post("/api/v1/nominas/recibo", content=b"hola", headers={"Content-Type": "application/pdf"}).status_code == 422

    def roto(ruta):
        raise ValueError("no es de Plexus")
    monkeypatch.setattr(nominas, "leer_pdf", roto)
    assert c.post("/api/v1/nominas/recibo", content=b"%PDF-otro", headers={"Content-Type": "application/pdf"}).status_code == 422
    assert not list(plexus.glob(".subida-*"))


def test_tarjeta_nueva_con_color(cliente):
    c, _ = cliente
    nueva = {"nombre": "BBVA Azul", "banco": "BBVA", "ultimos4": "1234", "dia_corte": 5, "dia_pago": 25,
             "limite_banco": 20000, "tope_personal": 2000, "color": "#1a4fa0"}
    r = c.post("/api/v1/tarjetas", json=nueva)
    assert r.status_code == 200 and r.json()["id"] == "bbva-azul"
    assert c.post("/api/v1/tarjetas", json=nueva).json()["id"] == "bbva-azul-2"
    t = next(x for x in c.get("/api/v1/resumen").json()["tarjetas"] if x["id"] == "bbva-azul")
    assert (t["color"], t["limite_banco"], t["ultimos4"]) == ("#1A4FA0", 20000, "1234")
    assert c.post("/api/v1/tarjetas", json=nueva | {"color": "rojo"}).status_code == 422
    assert c.post("/api/v1/tarjetas", json=nueva | {"nombre": "Efectivo"}).json()["id"] == "t-efectivo"
    assert c.put("/api/v1/tarjetas/bbva-azul", json=nueva | {"color": "#00aa55"}).status_code == 200
    t = next(x for x in c.get("/api/v1/resumen").json()["tarjetas"] if x["id"] == "bbva-azul")
    assert t["color"] == "#00AA55"
    assert c.post("/api/v1/gastos", json={"monto": 100, "metodo": "bbva-azul"}).status_code == 200


def test_pagar_desde_efectivo_regalo_y_entradas(cliente):
    c, _ = cliente
    c.post("/api/v1/saldos-iniciales", json={"efectivo": 500, "deudas": {"nu": {"corte": 1000}}})
    e = c.get("/api/v1/resumen").json()
    assert e["fondo"] == 0
    # el fondo no alcanza → te sugiere las otras formas
    r = c.post("/api/v1/pagos-tarjeta", json={"tarjeta": "nu", "monto": 100})
    assert r.status_code == 409 and "efectivo" in r.json()["detail"].lower()
    # desde el efectivo: baja el efectivo y la deuda, el fondo sigue en 0
    r = c.post("/api/v1/pagos-tarjeta", json={"tarjeta": "nu", "monto": 200, "desde": "efectivo"})
    assert r.status_code == 200 and "Todavía debes $800" in r.json()["mensaje"]
    e = c.get("/api/v1/resumen").json()
    assert (e["efectivo"], e["fondo"], next(t for t in e["tarjetas"] if t["id"] == "nu")["deuda"]) == (300, 0, 800)
    assert c.post("/api/v1/pagos-tarjeta", json={"tarjeta": "nu", "monto": 400, "desde": "efectivo"}).status_code == 409
    # $300 que le dio su papa, directo a Nu: no toca efectivo ni fondo
    r = c.post("/api/v1/pagos-tarjeta", json={"tarjeta": "nu", "monto": 300, "desde": "regalo", "nota": "Mi papá"})
    assert r.status_code == 200 and "mi papá" in r.json()["mensaje"]
    e = c.get("/api/v1/resumen").json()
    assert (e["efectivo"], e["fondo"], next(t for t in e["tarjetas"] if t["id"] == "nu")["deuda"]) == (300, 0, 500)
    # /entradas: a efectivo, a una tarjeta, al ahorro; tarjeta inexistente → 404
    assert c.post("/api/v1/entradas", json={"monto": 50, "nota": "venta", "destino": "efectivo"}).status_code == 200
    r = c.post("/api/v1/entradas", json={"monto": 100, "nota": "regalo", "destino": "nu"})
    assert r.status_code == 200 and "Nu" in r.json()["mensaje"]
    ahorro_antes = c.get("/api/v1/resumen").json()["ahorro_total"]
    assert c.post("/api/v1/entradas", json={"monto": 25, "destino": "ahorro"}).status_code == 200
    e = c.get("/api/v1/resumen").json()
    assert (e["efectivo"], e["ahorro_total"] - ahorro_antes, next(t for t in e["tarjetas"] if t["id"] == "nu")["deuda"]) == (350, 25, 400)
    assert c.post("/api/v1/entradas", json={"monto": 10, "destino": "amex"}).status_code == 404
    # borrar el pago desde efectivo regresa el efectivo
    movs = c.get("/api/v1/movimientos").json()
    pago_ef = next(m for m in movs if m["tipo"] == "pago_tarjeta" and m["metodo"] == "efectivo")
    assert c.delete(f"/api/v1/movimientos/{pago_ef['id']}").status_code == 200
    assert c.get("/api/v1/resumen").json()["efectivo"] == 550


def test_proximos(cliente):
    c, _ = cliente
    c.post("/api/v1/saldos-iniciales", json={"efectivo": 100, "deudas": {"nu": {"corte": 1000}}})
    p = c.get("/api/v1/proximos").json()
    tipos = [x["tipo"] for x in p["eventos"]]
    assert "nomina" in tipos and "pago_tarjeta" in tipos
    assert all(x["dias"] >= 0 or x.get("estado") == "cerrado" for x in p["eventos"])
    assert p["mensaje"].startswith("📅 Lo que viene") and "pagar nu" in p["mensaje"].lower() and "Fondo para tarjetas" in p["mensaje"]


def test_atajos_mandan_lo_que_se_ve(cliente):
    c, _ = cliente
    c.post("/api/v1/saldos-iniciales", json={"efectivo": 500, "deudas": {"nu": {"corte": 1000}}})
    r = c.post("/api/v1/pagos-tarjeta", json={"tarjeta": "Nu", "monto": 100, "desde": "Mi efectivo"})
    assert r.status_code == 200 and "con tu efectivo" in r.json()["mensaje"]
    r = c.post("/api/v1/pagos-tarjeta", json={"tarjeta": "Nu", "monto": 50, "desde": "Me lo dieron", "nota": "Mi papá"})
    assert r.status_code == 200 and "mi papá" in r.json()["mensaje"]
    assert c.post("/api/v1/entradas", json={"monto": 30, "destino": "Abonar a Nu"}).status_code == 200
    assert c.post("/api/v1/entradas", json={"monto": 20, "destino": "Fondo de tarjetas"}).status_code == 200
    e = c.get("/api/v1/resumen").json()
    assert (e["efectivo"], e["fondo"], next(t for t in e["tarjetas"] if t["id"] == "nu")["deuda"]) == (400, 20, 820)
    assert c.post("/api/v1/pagos-tarjeta", json={"tarjeta": "nu", "monto": 1, "desde": "otra cosa"}).status_code == 422
