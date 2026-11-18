# finanzas-api · mi sistema de dinero

> *La API que lleva mis finanzas personales: reparto de la nómina, tarjetas de crédito, meses sin intereses y ahorro.*
> *Personal finance API (FastAPI + SQLite) with 60+ pytest tests, payroll PDF parser and Apple Shortcuts integration.*

Cada jueves me pagan y el sistema reparte el dinero en **gastos de la semana, fondo para tarjetas y ahorro**.
Todo saldo se **deriva de los movimientos** (ver [docs/adr](docs/adr)): nada se guarda "a mano", así que borrar
un movimiento recalcula todo.

## Lo que hace

- **Tarjetas de crédito** con día de corte y de pago, compras a **meses sin intereses** y calendario de pagos.
- **Próximos pagos**: cortes, pagos de tarjeta, mensualidades y el pago de la semana.
- **Recibos de nómina en PDF**: los importa solos (`pdftotext` + parser) y detecta la semana que falta.
- **Cajas de ahorro** con metas y rendimiento, simulador de compras a meses y calibración de saldos.
- **Atajos de Apple**: "Gasté", "Me entró dinero", "Pagué mi tarjeta", "Próximos pagos" y una automatización
  de **Apple Pay** que registra cada compra sola. Se generan con [`atajos/generar_atajos.py`](atajos/generar_atajos.py).
- **Cashy**: asesora financiera con IA que ve los datos pero no mueve nada.

**Stack:** Python 3.12 · FastAPI · Pydantic · SQLite · pytest (60+ pruebas) · Docker · Apple Shortcuts (plist)

```bash
cd backend && pip install -r requirements-dev.txt && pytest
```

Más detalle de los atajos en [ATAJOS.md](ATAJOS.md) y del vocabulario en [GLOSSARY.md](GLOSSARY.md).

---
Hecho por **Salvador Pantoja** · construido con ayuda de IA (Claude) como herramienta, entendiendo cada pieza.
