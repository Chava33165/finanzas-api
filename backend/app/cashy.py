"""
Cashy: asesora de dinero de Chava. Es una sesion de Claude (siempre la misma, via `resume`) que corre
el worker de KORRA (~/Servicios/asistente/worker, `POST /jobs`) con su plan Pro.

- Solo aconseja: corre sin herramientas de archivos ni shell, y no tiene ningun endpoint para escribir.
- En cada mensaje recibe una foto de los datos de dinero (solo lectura), asi siempre opina con lo actual.
- Es asincrona: el dashboard tiene 10 s de timeout y Claude tarda ~10-60 s. POST guarda el mensaje y una
  respuesta "pensando"; un hilo consulta al worker y la completa; el dashboard pregunta GET /cashy.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime

import httpx

log = logging.getLogger("task-hub")

WORKER_URL = os.environ.get("WORKER_URL", "http://worker:8000")
CWD = "JARVIS/cashy"
MODELOS = {"sonnet", "haiku", "opus"}
MODELO_DEFAULT = "sonnet"
MAX_TURNS = 4
SIN_HERRAMIENTAS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "NotebookEdit", "Task", "Agent", "Skill",
                    "WebFetch"]
INTERVALO_S = 3.0
LIMITE_S = 240

PERSONA = """Eres **Cashy**, la asesora de dinero personal de Chava (Salvador, estudiante del CETI y técnico de
pruebas en Plexus, en Guadalajara). Hablas en español de México, directa, cálida y con un toque de humor,
como una amiga que sabe mucho de finanzas. Respuestas cortas (máx. ~150 palabras salvo que te pida detalle),
con números concretos en pesos y una recomendación clara.

Su sistema (respétalo siempre):
- Cada jueves le llega su nómina (~$2,505) y la reparte 33 % gastos (efectivo de la semana, es un LÍMITE),
  33 % fondo de tarjetas y 34 % ahorro. Lo que sobra de efectivo no se vuelve a retirar: se va al ahorro.
- El fondo de tarjetas SOLO paga tarjetas. El ahorro NUNCA paga tarjetas. El saldo del fondo no es dinero
  para gastar. Paga siempre lo del corte completo antes de la fecha límite para no generar intereses.
- Tarjetas: Nu (corte 18, pago 28) y Plata (corte 20, pago 20 del mes siguiente), con topes personales.
  La BBVA es su débito (donde cae la nómina): gastar con ella cuenta como efectivo.
- El fondo de ahorro de Plexus y el aguinaldo son "por cobrar": no son dinero disponible todavía.

Cuando te pregunte si puede gastar algo: revisa su efectivo de la semana, cuántos días faltan para el jueves,
lo que debe y si el fondo alcanza, y dile sí / no / "sí, pero…" con el porqué en números.

IMPORTANTE: solo aconsejas. NUNCA digas que registraste, pagaste o moviste algo: no puedes. Si quiere hacer
algo, dile cómo: el Atajo "Gasté", el botón "+ Gasto", "Recibí mi pago", "Pagar con fondo" o "Mi app dice…"
en la pestaña Dinero. No inventes datos: usa solo el bloque [DATOS] de cada mensaje (montos en pesos)."""


def prompt(datos: dict, mensaje: str, nueva: bool) -> str:
    bloque = json.dumps(datos, ensure_ascii=False, separators=(",", ":"), default=str)
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M")
    partes = [PERSONA] if nueva else []
    partes += [f"[DATOS AL {ahora} — solo lectura]\n{bloque}", f"Chava: {mensaje}"]
    return "\n\n".join(partes)


class Cashy:
    def __init__(self, conn, lock, cliente: httpx.Client | None = None):
        self.conn, self.lock = conn, lock
        self.cliente = cliente or httpx.Client(base_url=WORKER_URL, timeout=15)
        self.lanzar = lambda fn: threading.Thread(target=fn, daemon=True).start()
        self.dormir = time.sleep

    # ---- ajustes
    def _ajuste(self, k: str) -> str | None:
        r = self.conn.execute("SELECT v FROM ajustes WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    def _poner(self, k: str, v: str | None):
        if v is None:
            self.conn.execute("DELETE FROM ajustes WHERE k=?", (k,))
        else:
            self.conn.execute("INSERT OR REPLACE INTO ajustes VALUES (?, ?)", (k, v))
        self.conn.commit()

    def modelo(self) -> str:
        return self._ajuste("cashy_modelo") or MODELO_DEFAULT

    def poner_modelo(self, modelo: str):
        if modelo not in MODELOS:
            raise ValueError(f"modelo debe ser uno de {', '.join(sorted(MODELOS))}")
        self._poner("cashy_modelo", modelo)

    # ---- conversacion
    def historia(self, limit: int = 50) -> dict:
        filas = self.conn.execute("SELECT id, ts, rol, texto, estado FROM cashy_msgs ORDER BY id DESC LIMIT ?",
                                  (limit,)).fetchall()
        msgs = [dict(r) for r in reversed(filas)]
        return {"mensajes": msgs, "pensando": any(m["estado"] == "pensando" for m in msgs),
                "modelo": self.modelo(), "sesion": bool(self._ajuste("cashy_sesion"))}

    def nueva_conversacion(self):
        self.conn.execute("DELETE FROM cashy_msgs")
        self._poner("cashy_sesion", None)

    def preguntar(self, mensaje: str, datos: dict) -> int:
        """Guarda la pregunta, encola el trabajo en el worker y regresa el id de la respuesta pendiente.
        Llamar con self.lock tomado."""
        if self.conn.execute("SELECT 1 FROM cashy_msgs WHERE estado='pensando'").fetchone():
            raise RuntimeError("Cashy sigue pensando tu pregunta anterior")
        ahora = time.time()
        self.conn.execute("INSERT INTO cashy_msgs(ts, rol, texto, estado) VALUES (?, 'chava', ?, 'ok')", (ahora, mensaje))
        rid = self.conn.execute("INSERT INTO cashy_msgs(ts, rol, texto, estado) VALUES (?, 'cashy', '', 'pensando')",
                                (ahora,)).lastrowid
        self.conn.commit()
        sesion = self._ajuste("cashy_sesion")
        body = {"prompt": prompt(datos, mensaje, nueva=not sesion), "cwd": CWD, "model": self.modelo(),
                "max_turns": MAX_TURNS, "disallowed_tools": SIN_HERRAMIENTAS}
        if sesion:
            body["resume"] = sesion
        try:
            r = self.cliente.post("/jobs", json=body)
            if r.status_code == 404 and sesion:  # la sesion ya no existe en el worker: empieza otra
                self._poner("cashy_sesion", None)
                body.pop("resume")
                body["prompt"] = prompt(datos, mensaje, nueva=True)
                r = self.cliente.post("/jobs", json=body)
            r.raise_for_status()
            job = r.json()["id"]
        except Exception as e:
            log.warning("cashy: el worker no respondió (%s)", e)
            self._terminar(rid, "error", "No pude hablar con Claude (¿está prendido el asistente KORRA?). Intenta otra vez.")
            return rid
        self.conn.execute("UPDATE cashy_msgs SET job_id=? WHERE id=?", (job, rid))
        self.conn.commit()
        self.lanzar(lambda: self._esperar(rid, job))
        return rid

    def _terminar(self, rid: int, estado: str, texto: str):
        self.conn.execute("UPDATE cashy_msgs SET estado=?, texto=?, ts=? WHERE id=?", (estado, texto, time.time(), rid))
        self.conn.commit()

    def _esperar(self, rid: int, job: str):
        inicio = time.monotonic()
        while time.monotonic() - inicio < LIMITE_S:
            self.dormir(INTERVALO_S)
            try:
                j = self.cliente.get(f"/jobs/{job}").json()
            except Exception:
                continue
            if j.get("status") in ("queued", "running"):
                continue
            with self.lock:
                if j.get("status") == "done" and j.get("result"):
                    if j.get("session_id"):
                        self._poner("cashy_sesion", j["session_id"])
                    self._terminar(rid, "ok", j["result"].strip())
                else:
                    self._terminar(rid, "error", "Se me trabó la respuesta 😵 ¿me lo preguntas otra vez?")
            return
        with self.lock:
            self._terminar(rid, "error", "Tardé demasiado en pensar (puede que Claude esté ocupado con otro trabajo de KORRA). Intenta otra vez.")

    def recuperar(self):
        """Al arrancar: las respuestas que quedaron "pensando" ya no tienen quien las espere."""
        self.conn.execute("UPDATE cashy_msgs SET estado='error', texto='Se reinició el servidor mientras pensaba. Pregúntame otra vez.' "
                          "WHERE estado='pensando'")
        self.conn.commit()
