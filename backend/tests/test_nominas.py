import os
from pathlib import Path

import pytest

from app import nominas

CARPETA = Path(os.environ.get("NOMINAS_DIR", "/nominas"))
RECIBOS = nominas.buscar_recibos(CARPETA)


@pytest.mark.skipif(not RECIBOS, reason="sin recibos reales montados")
def test_los_16_recibos_cuadran_con_el_reporte():
    leidos = [nominas.leer_pdf(p) for p in RECIBOS]
    assert len(leidos) == 16
    assert sorted(r["semana"] for r in leidos) == list(range(26, 42))
    assert len({r["uuid"] for r in leidos}) == 16
    assert sum(r["neto"] for r in leidos) == 3955600  # reporte_nominas_plexus.md: $39,556.00
    assert sum(r["percepciones"] for r in leidos) == 5549140
    assert sum(r["deducciones"] for r in leidos) == 1593540
    for r in leidos:
        assert r["percepciones"] - r["deducciones"] == r["neto"], r["semana"]


@pytest.mark.skipif(not RECIBOS, reason="sin recibos reales montados")
def test_semana_41():
    r = next(nominas.leer_pdf(p) for p in RECIBOS if "semana_41" in p.name)
    assert r["neto"] == 250500
    assert (r["isr"], r["imss"], r["fondo_ahorro"], r["cafeteria"]) == (33952, 9999, 46099, 14076)
    assert r["despensa"] == 35500
    assert r["fondo_saldo_total"] == 1442784 and r["fondo_tuyo"] == 721392
    assert r["fecha_pago"] == "2026-10-15" and r["periodo_ini"] == "2026-10-05"
    assert r["uuid"] == "CB23E1B6-B4C4-DE4E-B2B5-094BEEE6BDEB"
