from app import applepay as ap


def test_monto_en_formatos_de_wallet():
    assert ap.monto("$1,234.50") == 1234.50
    assert ap.monto("MX$120.00") == 120.0
    assert ap.monto("120") == 120.0
    assert ap.monto("$1.234,50") == 1234.50
    assert ap.monto("45,5") == 45.5
    assert ap.monto("sin numero") is None


def test_tarjeta_por_nombre_de_wallet():
    ids = ["nu", "plata"]
    assert ap.tarjeta("Plata Card", ids) == "plata"
    assert ap.tarjeta("Tarjeta de Crédito Nu", ids) == "nu"
    assert ap.tarjeta("Nu", ids) == "nu"
    assert ap.tarjeta("BBVA Débito", ids) == "efectivo"  # debito = dinero de la semana
    assert ap.tarjeta("Tarjeta Débito", ids) == "efectivo"
    assert ap.tarjeta("Amex", ids) is None


def test_categoria_por_comercio():
    assert ap.categoria("OXXO GDL 1234") == "Comida"
    assert ap.categoria("UBER *TRIP") == "Transporte"
    assert ap.categoria("Cinépolis Andares") == "Salidas"
    assert ap.categoria("Office Depot") == "Escuela"
    assert ap.categoria("Tienda Random SA") == "Otros"
