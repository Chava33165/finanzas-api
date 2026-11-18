"""
Traduce lo que manda la automatizacion "Transaccion" de Atajos (Apple Pay en Wallet) a un gasto:
el monto llega como texto con moneda, la tarjeta con su nombre de Wallet y el comercio tal cual.
"""
import re
import unicodedata

# Palabras del comercio -> categoria (en minusculas y sin acentos). La primera que coincida gana.
REGLAS = [
    ("Transporte", ["uber", "didi", "cabify", "pemex", "gasolin", "shell", "mobil", "bp ", "oxxo gas", "metro",
                    "mi macro", "mibici", "caseta", "estacionamiento"]),
    ("Comida", ["oxxo", "7-eleven", "7 eleven", "seven", "starbucks", "mcdonald", "burger", "kfc", "domino",
                "little caesars", "subway", "taco", "tortas", "restaurant", "cafe", "rappi", "uber eats",
                "didi food", "soriana", "walmart", "bodega aurrera", "chedraui", "la comer", "costco",
                "sams", "heb", "superama", "farmacia guadalajara"]),
    ("Salidas", ["cinepolis", "cinemex", "bar ", "antro", "boliche", "ticketmaster", "steam", "playstation",
                 "xbox", "nintendo", "spotify", "netflix", "disney", "hbo", "max "]),
    ("Escuela", ["office depot", "officemax", "papeler", "libreria", "gandhi", "ceti", "copias"]),
    ("Personal", ["amazon", "mercado libre", "mercadolibre", "liverpool", "palacio", "coppel", "zara", "h&m",
                  "nike", "adidas", "farmacia", "benavides", "del ahorro", "barber", "apple"]),
]


def _simple(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return f" {s.lower()} "


def monto(texto: str) -> float | None:
    """"$1,234.50", "MX$120", "1.234,50", "45,5" -> pesos."""
    limpio = re.sub(r"[^\d.,]", "", str(texto or ""))
    if not re.search(r"\d", limpio):
        return None
    if "," in limpio and "." in limpio:
        decimal = "," if limpio.rfind(",") > limpio.rfind(".") else "."
    elif "," in limpio:
        decimal = "," if len(limpio) - limpio.rfind(",") - 1 in (1, 2) else None
    elif "." in limpio:
        decimal = "." if len(limpio) - limpio.rfind(".") - 1 in (1, 2) else None
    else:
        decimal = None
    entero, _, dec = limpio.rpartition(decimal) if decimal else (limpio, "", "")
    entero = re.sub(r"[.,]", "", entero)
    return float(f"{entero or 0}.{dec or 0}")


# Tarjetas de debito: gastar con ellas es gastar el dinero de la semana, igual que efectivo.
# (BBVA = donde cae la nomina de Chava.)
DEBITO = ["bbva", "debito"]


def tarjeta(nombre_wallet: str, ids: list[str]) -> str | None:
    """Id de la tarjeta de credito (palabra completa) segun su nombre en Wallet; "efectivo" si es de debito."""
    n = _simple(nombre_wallet)
    if any(re.search(rf"\b{d}\b", n) for d in DEBITO):
        return "efectivo"
    return next((i for i in sorted(ids, key=len, reverse=True) if re.search(rf"\b{re.escape(i)}\b", n)), None)


def categoria(comercio: str) -> str:
    c = _simple(comercio)
    return next((cat for cat, palabras in REGLAS if any(p in c for p in palabras)), "Otros")
