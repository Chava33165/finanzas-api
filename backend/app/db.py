"""
SQLite (/data/tasks.db, el mismo archivo del task-hub viejo) con migraciones versionadas.

Para cambiar el esquema: agrega una funcion AL FINAL de MIGRACIONES (nunca edites las que ya
existen). Al arrancar se aplican las que falten (PRAGMA user_version) y antes se respalda el
archivo en /data/respaldos/. La tabla vieja `tasks` no se toca.
"""
import os
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("DATABASE_PATH", "/data/tasks.db"))
MAX_RESPALDOS = 14


def m001_finanzas(db: sqlite3.Connection):
    db.execute("""CREATE TABLE IF NOT EXISTS tarjetas (
        id TEXT PRIMARY KEY, nombre TEXT NOT NULL, banco TEXT DEFAULT '', ultimos4 TEXT DEFAULT '',
        dia_corte INTEGER NOT NULL, dia_pago INTEGER NOT NULL,
        limite_banco INTEGER DEFAULT 0, tope_personal INTEGER DEFAULT 0,
        estado TEXT DEFAULT 'activa', nota TEXT DEFAULT '', orden INTEGER DEFAULT 0)""")
    db.execute("INSERT OR IGNORE INTO tarjetas(id, nombre, banco, dia_corte, dia_pago, orden) VALUES "
               "('nu', 'Nu', 'Nu México', 18, 28, 0), ('plata', 'Plata', 'Plata', 20, 20, 1)")
    db.execute("""CREATE TABLE IF NOT EXISTS cajas (
        id INTEGER PRIMARY KEY, nombre TEXT NOT NULL, meta_nombre TEXT DEFAULT '', meta_monto INTEGER DEFAULT 0,
        rendimiento_anual REAL DEFAULT 0, es_destino INTEGER DEFAULT 0, archivada INTEGER DEFAULT 0)""")
    if not db.execute("SELECT 1 FROM cajas").fetchone():
        db.execute("INSERT INTO cajas(nombre, es_destino) VALUES ('Ahorro', 1)")
    db.execute("""CREATE TABLE IF NOT EXISTS movimientos (
        id INTEGER PRIMARY KEY, ts REAL NOT NULL, tipo TEXT NOT NULL, monto INTEGER NOT NULL,
        metodo TEXT DEFAULT '', categoria TEXT DEFAULT '', nota TEXT DEFAULT '', tarjeta TEXT DEFAULT '',
        caja INTEGER, caja_destino INTEGER, origen TEXT DEFAULT '', nomina_uuid TEXT,
        -- reparto (solo en pago_recibido), en centavos
        r_gastos INTEGER, r_fondo INTEGER, r_ahorro INTEGER, r_sobrante INTEGER, r_retirar INTEGER)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_mov_ts ON movimientos(ts)")
    db.execute("""CREATE TABLE IF NOT EXISTS nominas (
        uuid TEXT PRIMARY KEY, semana INTEGER, periodo_ini TEXT, periodo_fin TEXT, fecha_pago TEXT,
        percepciones INTEGER, deducciones INTEGER, neto INTEGER, isr INTEGER, imss INTEGER,
        fondo_ahorro INTEGER, cafeteria INTEGER, despensa INTEGER,
        fondo_tuyo INTEGER, fondo_empresa INTEGER, fondo_saldo_total INTEGER,
        conceptos TEXT, archivo TEXT, importada REAL)""")
    db.execute("CREATE TABLE IF NOT EXISTS ajustes (k TEXT PRIMARY KEY, v TEXT)")


def m002_meses(db: sqlite3.Connection):
    """Compras a meses: la mensualidad se carga sola en cada ciclo hasta el ultimo mes."""
    db.execute("""CREATE TABLE IF NOT EXISTS planes (
        id INTEGER PRIMARY KEY, tarjeta TEXT NOT NULL, descripcion TEXT NOT NULL, mensualidad INTEGER NOT NULL,
        ultimo_mes TEXT NOT NULL, desde TEXT NOT NULL, creado REAL)""")


def m003_msi_desde_gasto(db: sqlite3.Connection):
    """Una compra a MSI registrada como gasto queda ligada a su plan (borrar el gasto borra el plan)."""
    if "movimiento_id" not in {r[1] for r in db.execute("PRAGMA table_info(planes)")}:
        db.execute("ALTER TABLE planes ADD COLUMN movimiento_id INTEGER")


def m004_cashy(db: sqlite3.Connection):
    """Conversacion con Cashy (asesora de dinero; ver app/cashy.py)."""
    db.execute("""CREATE TABLE IF NOT EXISTS cashy_msgs (
        id INTEGER PRIMARY KEY, ts REAL NOT NULL, rol TEXT NOT NULL,  -- chava | cashy
        texto TEXT NOT NULL, estado TEXT NOT NULL DEFAULT 'ok',       -- ok | pensando | error
        job_id TEXT)""")


def m005_ligar_nominas(db: sqlite3.Connection):
    """Pagos registrados sin su recibo (a mano, o antes de que llegara el PDF): se ligan a la nomina
    con el mismo neto pagada a 3 dias o menos, para que no siga saliendo como pendiente."""
    sueltos = db.execute("SELECT id, ts, monto FROM movimientos WHERE tipo='pago_recibido' AND nomina_uuid IS NULL").fetchall()
    for mid, ts, monto in sueltos:
        dia = time.strftime("%Y-%m-%d", time.localtime(ts))
        n = db.execute("""SELECT uuid FROM nominas WHERE neto=? AND abs(julianday(fecha_pago) - julianday(?)) <= 3
                         AND uuid NOT IN (SELECT nomina_uuid FROM movimientos WHERE nomina_uuid IS NOT NULL)
                         ORDER BY abs(julianday(fecha_pago) - julianday(?)) LIMIT 1""", (monto, dia, dia)).fetchone()
        if n:
            db.execute("UPDATE movimientos SET nomina_uuid=? WHERE id=?", (n[0], mid))


def m006_tarjeta_color(db: sqlite3.Connection):
    """Color de la tarjeta en la app ("#RRGGBB"; vacío = el de su banco o uno neutro)."""
    db.execute("ALTER TABLE tarjetas ADD COLUMN color TEXT DEFAULT ''")


MIGRACIONES = [m001_finanzas, m002_meses, m003_msi_desde_gasto, m004_cashy, m005_ligar_nominas, m006_tarjeta_color]


def respaldar(db: sqlite3.Connection, carpeta: Path, etiqueta: str):
    """Copia consistente (API de backup de SQLite, sirve aunque haya escrituras en WAL)."""
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = sqlite3.connect(carpeta / f"tasks_{etiqueta}_{time.strftime('%Y%m%d_%H%M%S')}.db")
    with destino:
        db.backup(destino)
    destino.close()
    for viejo in sorted(carpeta.glob("tasks_*.db"), key=lambda p: p.stat().st_mtime)[:-MAX_RESPALDOS]:
        viejo.unlink()


def conectar(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    existia = path.exists() and path.stat().st_size > 0
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    pendientes = MIGRACIONES[version:]
    if pendientes and existia:
        respaldar(db, path.parent / "respaldos", f"v{version}")
    for i, mig in enumerate(pendientes, start=version + 1):
        with db:
            mig(db)
            db.execute(f"PRAGMA user_version = {i}")
    return db


# Un solo escritor a la vez: FastAPI corre los endpoints sync en varios hilos.
lock = threading.Lock()
