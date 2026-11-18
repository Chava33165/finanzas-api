import json
import os
import sqlite3

import httpx
import pytest

os.environ["BEARER_TOKEN"] = "token-de-prueba"


class WorkerFalso:
    """Imita POST /jobs y GET /jobs/{id} del worker de KORRA."""

    def __init__(self):
        self.jobs, self.pedidos, self.falla = {}, [], False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if self.falla:
            return httpx.Response(500)
        if req.method == "POST" and req.url.path == "/jobs":
            body = json.loads(req.content)
            self.pedidos.append(body)
            jid = f"j{len(self.pedidos)}"
            self.jobs[jid] = {"id": jid, "status": "done", "result": f"respuesta {len(self.pedidos)}",
                              "session_id": "sesion-123"}
            return httpx.Response(200, json={"id": jid})
        if req.method == "GET" and req.url.path.startswith("/jobs/"):
            return httpx.Response(200, json=self.jobs[req.url.path.rsplit("/", 1)[1]])
        return httpx.Response(404)


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import db as basedatos
    from app import main

    base = tmp_path / "tasks.db"
    monkeypatch.setattr(basedatos, "DB_PATH", base)
    monkeypatch.setattr(main, "NOMINAS_DIR", tmp_path / "sin-nominas")
    monkeypatch.setattr(basedatos, "conectar", lambda path=base, _c=basedatos.conectar: _c(path))
    worker = WorkerFalso()
    pendientes = []
    with TestClient(main.app) as c:
        main.cashy.cliente = httpx.Client(base_url="http://worker", transport=httpx.MockTransport(worker))
        main.cashy.lanzar = pendientes.append  # el hilo se corre a mano, ya sin el candado tomado
        main.cashy.dormir = lambda s: None
        c.headers["Authorization"] = "Bearer token-de-prueba"

        def correr():
            while pendientes:
                pendientes.pop(0)()

        yield c, worker, correr


def test_misma_sesion_y_sin_herramientas(cliente):
    c, worker, correr = cliente
    r = c.post("/api/v1/cashy", json={"mensaje": "¿cómo voy esta semana?"})
    assert r.status_code == 200 and r.json()["pensando"] is True
    assert c.post("/api/v1/cashy", json={"mensaje": "otra"}).status_code == 409  # aun piensa
    correr()
    h = c.get("/api/v1/cashy").json()
    assert [m["texto"] for m in h["mensajes"]] == ["¿cómo voy esta semana?", "respuesta 1"] and not h["pensando"]
    primero = worker.pedidos[0]
    assert "resume" not in primero and "Eres **Cashy**" in primero["prompt"] and "[DATOS AL" in primero["prompt"]
    assert "token-de-prueba" not in primero["prompt"]
    assert {"Bash", "Write", "Edit", "Read"} <= set(primero["disallowed_tools"]) and primero["model"] == "sonnet"

    c.put("/api/v1/cashy/config", json={"modelo": "haiku"})
    c.post("/api/v1/cashy", json={"mensaje": "¿puedo gastar 300?"})
    correr()
    segundo = worker.pedidos[1]
    assert segundo["resume"] == "sesion-123" and segundo["model"] == "haiku"
    assert "Eres **Cashy**" not in segundo["prompt"]  # la persona ya esta en la sesion


def test_error_del_worker_y_nueva_conversacion(cliente):
    c, worker, correr = cliente
    worker.falla = True
    h = c.post("/api/v1/cashy", json={"mensaje": "hola"}).json()
    assert h["mensajes"][-1]["estado"] == "error" and not h["pensando"]
    assert c.delete("/api/v1/cashy").json() == {"ok": True}
    assert c.get("/api/v1/cashy").json()["mensajes"] == []


def test_validacion(cliente):
    c, _, _ = cliente
    assert c.put("/api/v1/cashy/config", json={"modelo": "gpt"}).status_code == 422
    assert c.post("/api/v1/cashy", json={"mensaje": "x" * 1001}).status_code == 422
    assert c.get("/api/v1/cashy/config").json()["modelo"] == "sonnet"


def test_gastos_periodo_api(cliente):
    c, _, _ = cliente
    c.post("/api/v1/gastos", json={"monto": 120.5, "categoria": "Comida"})
    p = c.get("/api/v1/gastos/periodo", params={"vista": "dia"}).json()
    assert p["total"] == 120.5 and p["lista"][0]["monto"] == 120.5 and isinstance(p["lista"][0]["id"], int)
    assert c.get("/api/v1/gastos/periodo", params={"vista": "anio"}).status_code == 422
