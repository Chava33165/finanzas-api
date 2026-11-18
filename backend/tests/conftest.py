import pytest


@pytest.fixture(autouse=True)
def reparto_viejo(monkeypatch):
    """Las pruebas de siempre se escribieron con el reparto 32/32/36 (el de antes de nov 2026)."""
    from app import finanzas
    monkeypatch.setattr(finanzas, "PROPORCION", (32, 32, 36))
