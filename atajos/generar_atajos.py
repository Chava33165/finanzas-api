"""
Genera los Atajos de Apple (.shortcut, plist binario SIN firmar) para la API de dinero.

Uso (el token sale del entorno, nunca se imprime):
    docker run --rm --env-file ~/Servicios/task-hub/.env -v ~/Servicios/task-hub/atajos:/a -v <salida>:/out \
        python:3.12-slim python /a/generar_atajos.py /out

iOS/macOS solo importan atajos FIRMADOS. En la Mac:
    shortcuts sign -m anyone -i Gaste.shortcut -o "Gasté.shortcut"
y luego abre el firmado (se sincroniza al iPhone por iCloud).
"""
import os
import plistlib
import sys
import uuid
from pathlib import Path

BASE = os.environ.get("TASKHUB_URL", "https://tasks.tu-dominio.com/api/v1")
TOKEN = os.environ.get("BEARER_TOKEN", "")
TARJETAS = ["Nu", "Plata"]
MESES = ["3", "6", "9", "12"]
CATEGORIAS = ["Comida", "Transporte", "Escuela", "Salidas", "Personal", "Otros"]


def uid() -> str:
    return str(uuid.uuid4()).upper()


def salida(u: str, nombre: str) -> dict:
    return {"OutputUUID": u, "Type": "ActionOutput", "OutputName": nombre}


def variable(u: str, nombre: str) -> dict:
    return {"Value": salida(u, nombre), "WFSerializationType": "WFTextTokenAttachment"}


def texto(s: str, *vars_: tuple[str, str]) -> dict:
    """Texto con variables: cada ￼ en `s` se reemplaza por la siguiente variable (uuid, nombre)."""
    rangos, i = {}, 0
    for pos, ch in enumerate(s):
        if ch == "￼":
            rangos[f"{{{pos}, 1}}"] = salida(*vars_[i])
            i += 1
    return {"Value": {"string": s, "attachmentsByRange": rangos}, "WFSerializationType": "WFTextTokenString"}


def nombrada(nombre: str) -> dict:
    return {"Type": "Variable", "VariableName": nombre}


def texto_var(nombre: str) -> dict:
    """Texto que es solo una variable con nombre (Set Variable)."""
    return {"Value": {"string": "\ufffc", "attachmentsByRange": {"{0, 1}": nombrada(nombre)}},
            "WFSerializationType": "WFTextTokenString"}


def si(grupo: str, u: str, nombre: str, igual_a: str) -> dict:
    """Inicio de un "Si <salida> es <igual_a>"."""
    return accion("conditional", GroupingIdentifier=grupo, WFControlFlowMode=0, WFCondition=4,
                  WFConditionalActionString=igual_a,
                  WFInput={"Type": "Variable", "Variable": variable(u, nombre)})


def fin_si(grupo: str) -> dict:
    return accion("conditional", GroupingIdentifier=grupo, WFControlFlowMode=2)


def poner(nombre: str, u: str, salida_nombre: str) -> dict:
    return accion("setvariable", WFVariableName=nombre, WFInput=variable(u, salida_nombre))


def diccionario(items: list[tuple[str, int, dict]]) -> dict:
    """items: (clave, WFItemType [0 texto, 3 numero], valor como texto())."""
    return {"Value": {"WFDictionaryFieldValueItems": [
        {"WFItemType": tipo, "WFKey": texto(k), "WFValue": v} for k, tipo, v in items]},
        "WFSerializationType": "WFDictionaryFieldValue"}


def accion(ident: str, **params) -> dict:
    return {"WFWorkflowActionIdentifier": f"is.workflow.actions.{ident}", "WFWorkflowActionParameters": params}


def atajo(acciones: list[dict], preguntas: list[dict], color: int, glifo: int) -> bytes:
    return plistlib.dumps({
        "WFWorkflowClientVersion": "2302.0.4",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": color, "WFWorkflowIconGlyphNumber": glifo},
        "WFWorkflowTypes": ["NCWidget", "WatchKit"],
        "WFWorkflowInputContentItemClasses": ["WFStringContentItem"],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowHasShortcutInputVariables": False,
        "WFWorkflowImportQuestions": preguntas,
        "WFWorkflowActions": acciones,
    }, fmt=plistlib.FMT_BINARY)


def pregunta_token() -> dict:
    return {"ActionIndex": 0, "Category": "Parameter", "ParameterKey": "WFTextActionText",
            "Text": "Tu token de task-hub (BEARER_TOKEN). Si ya viene puesto, solo continúa.", "DefaultValue": TOKEN}


def gaste() -> bytes:
    """¿Cuánto? → ¿Efectivo o tarjeta? → (tarjeta) ¿Cuál? → ¿Contado o a meses? → (meses) ¿A cuántos?"""
    t, a, ef, ce, lf, f_, lt, tj, lc, cm, lm, mm, l2, c, n, d, v = (uid() for _ in range(17))
    g_tarjeta, g_meses = uid(), uid()
    acciones = [
        accion("gettext", UUID=t, WFTextActionText=TOKEN),
        accion("ask", UUID=a, WFAskActionPrompt="¿Cuánto gastaste?", WFInputType="Number"),
        # valores por defecto: efectivo, de contado
        accion("gettext", UUID=ef, WFTextActionText="efectivo"),
        poner("Metodo", ef, "Text"),
        accion("gettext", UUID=ce, WFTextActionText="0"),
        poner("Meses", ce, "Text"),
        accion("list", UUID=lf, WFItems=["Efectivo", "Tarjeta"]),
        accion("choosefromlist", UUID=f_, WFChooseFromListActionPrompt="¿Efectivo o tarjeta?", WFInput=variable(lf, "List")),
        si(g_tarjeta, f_, "Chosen Item", "Tarjeta"),
        accion("list", UUID=lt, WFItems=TARJETAS),
        accion("choosefromlist", UUID=tj, WFChooseFromListActionPrompt="¿Con qué tarjeta?", WFInput=variable(lt, "List")),
        poner("Metodo", tj, "Chosen Item"),
        accion("list", UUID=lc, WFItems=["De contado", "A meses"]),
        accion("choosefromlist", UUID=cm, WFChooseFromListActionPrompt="¿Fue a meses sin intereses?", WFInput=variable(lc, "List")),
        si(g_meses, cm, "Chosen Item", "A meses"),
        accion("list", UUID=lm, WFItems=MESES),
        accion("choosefromlist", UUID=mm, WFChooseFromListActionPrompt="¿A cuántos meses?", WFInput=variable(lm, "List")),
        poner("Meses", mm, "Chosen Item"),
        fin_si(g_meses),
        fin_si(g_tarjeta),
        accion("list", UUID=l2, WFItems=CATEGORIAS),
        accion("choosefromlist", UUID=c, WFChooseFromListActionPrompt="¿De qué fue?", WFInput=variable(l2, "List")),
        accion("ask", UUID=n, WFAskActionPrompt="¿Qué compraste? (opcional)", WFInputType="Text",
               WFAllowsMultilineText=False),
        accion("downloadurl", UUID=d, WFURL=f"{BASE}/gastos", WFHTTPMethod="POST", WFHTTPBodyType="JSON",
               ShowHeaders=True,
               WFHTTPHeaders=diccionario([("Authorization", 0, texto("Bearer ￼", (t, "Text")))]),
               WFJSONValues=diccionario([
                   ("monto", 3, texto("￼", (a, "Provided Input"))),
                   ("metodo", 0, texto_var("Metodo")),
                   ("meses", 3, texto_var("Meses")),
                   ("categoria", 0, texto("￼", (c, "Chosen Item"))),
                   ("nota", 0, texto("￼", (n, "Provided Input"))),
               ])),
        accion("getvalueforkey", UUID=v, WFDictionaryKey="mensaje", WFGetDictionaryValueType="Value",
               WFInput=variable(d, "Contents of URL")),
        accion("notification", WFNotificationActionTitle="Gasto guardado",
               WFNotificationActionBody=texto("￼", (v, "Dictionary Value")), WFNotificationActionSound=True),
    ]
    return atajo(acciones, [pregunta_token()], color=4282601983, glifo=59511)


def cuanto_queda() -> bytes:
    t, d, v = uid(), uid(), uid()
    acciones = [
        accion("gettext", UUID=t, WFTextActionText=TOKEN),
        accion("downloadurl", UUID=d, WFURL=f"{BASE}/resumen", WFHTTPMethod="GET", ShowHeaders=True,
               WFHTTPHeaders=diccionario([("Authorization", 0, texto("Bearer ￼", (t, "Text")))])),
        accion("getvalueforkey", UUID=v, WFDictionaryKey="mensaje", WFGetDictionaryValueType="Value",
               WFInput=variable(d, "Contents of URL")),
        accion("showresult", Text=texto("￼", (v, "Dictionary Value"))),
    ]
    return atajo(acciones, [pregunta_token()], color=463140863, glifo=59446)


def entrada(propiedad: str) -> dict:
    """Propiedad de la "Entrada del atajo" (lo que pasa la automatizacion Transaccion: Amount, Merchant…)."""
    return {"Type": "ExtensionInput",
            "Aggrandizements": [{"Type": "WFPropertyVariableAggrandizement", "PropertyName": propiedad}]}


def texto_entrada(propiedad: str) -> dict:
    return {"Value": {"string": "\ufffc", "attachmentsByRange": {"{0, 1}": entrada(propiedad)}},
            "WFSerializationType": "WFTextTokenString"}


def apple_pay() -> bytes:
    """Lo corre la automatizacion "Transaccion" de Wallet; manda monto, comercio y tarjeta tal cual."""
    t, d, v = uid(), uid(), uid()
    acciones = [
        accion("gettext", UUID=t, WFTextActionText=TOKEN),
        accion("downloadurl", UUID=d, WFURL=f"{BASE}/gastos/applepay", WFHTTPMethod="POST", WFHTTPBodyType="JSON",
               ShowHeaders=True,
               WFHTTPHeaders=diccionario([("Authorization", 0, texto("Bearer \ufffc", (t, "Text")))]),
               WFJSONValues=diccionario([
                   ("monto", 0, texto_entrada("Amount")),
                   ("comercio", 0, texto_entrada("Merchant")),
                   ("tarjeta", 0, texto_entrada("Card or Pass")),
               ])),
        accion("getvalueforkey", UUID=v, WFDictionaryKey="mensaje", WFGetDictionaryValueType="Value",
               WFInput=variable(d, "Contents of URL")),
        accion("notification", WFNotificationActionTitle="Apple Pay registrado",
               WFNotificationActionBody=texto("\ufffc", (v, "Dictionary Value")), WFNotificationActionSound=False),
    ]
    datos = plistlib.loads(atajo(acciones, [pregunta_token()], color=4271458815, glifo=59750))
    datos["WFWorkflowHasShortcutInputVariables"] = True
    datos["WFWorkflowInputContentItemClasses"] = []  # acepta lo que mande la automatizacion
    return plistlib.dumps(datos, fmt=plistlib.FMT_BINARY)


def cuanto_debo() -> bytes:
    t, d, v = uid(), uid(), uid()
    acciones = [
        accion("gettext", UUID=t, WFTextActionText=TOKEN),
        accion("downloadurl", UUID=d, WFURL=f"{BASE}/deuda", WFHTTPMethod="GET", ShowHeaders=True,
               WFHTTPHeaders=diccionario([("Authorization", 0, texto("Bearer \ufffc", (t, "Text")))])),
        accion("getvalueforkey", UUID=v, WFDictionaryKey="mensaje", WFGetDictionaryValueType="Value",
               WFInput=variable(d, "Contents of URL")),
        accion("showresult", Text=texto("\ufffc", (v, "Dictionary Value"))),
    ]
    return atajo(acciones, [pregunta_token()], color=4292093695, glifo=59811)


def contar_efectivo() -> bytes:
    """Cuentas lo que traes y el sistema queda igual: lo que falte cuenta como gastos sin anotar."""
    t, a, d, v = uid(), uid(), uid(), uid()
    acciones = [
        accion("gettext", UUID=t, WFTextActionText=TOKEN),
        accion("ask", UUID=a, WFAskActionPrompt="¿Cuánto tienes para gastar? (cartera + lo de gastos en tu débito)",
               WFInputType="Number"),
        accion("downloadurl", UUID=d, WFURL=f"{BASE}/calibrar", WFHTTPMethod="POST", WFHTTPBodyType="JSON",
               ShowHeaders=True,
               WFHTTPHeaders=diccionario([("Authorization", 0, texto("Bearer \ufffc", (t, "Text")))]),
               WFJSONValues=diccionario([("efectivo", 3, texto("\ufffc", (a, "Provided Input")))])),
        accion("getvalueforkey", UUID=v, WFDictionaryKey="mensaje", WFGetDictionaryValueType="Value",
               WFInput=variable(d, "Contents of URL")),
        accion("showresult", Text=texto("\ufffc", (v, "Dictionary Value"))),
    ]
    return atajo(acciones, [pregunta_token()], color=4251333119, glifo=59484)


def como_voy_hoy() -> bytes:
    t, d, v = uid(), uid(), uid()
    acciones = [
        accion("gettext", UUID=t, WFTextActionText=TOKEN),
        accion("downloadurl", UUID=d, WFURL=f"{BASE}/hoy", WFHTTPMethod="GET", ShowHeaders=True,
               WFHTTPHeaders=diccionario([("Authorization", 0, texto("Bearer \ufffc", (t, "Text")))])),
        accion("getvalueforkey", UUID=v, WFDictionaryKey="mensaje", WFGetDictionaryValueType="Value",
               WFInput=variable(d, "Contents of URL")),
        accion("showresult", Text=texto("\ufffc", (v, "Dictionary Value"))),
    ]
    return atajo(acciones, [pregunta_token()], color=463140863, glifo=59779)


def me_equivoque() -> bytes:
    """Borra el ultimo gasto registrado (de las ultimas 24 h), despues de confirmar."""
    t, d, v = uid(), uid(), uid()
    acciones = [
        accion("gettext", UUID=t, WFTextActionText=TOKEN),
        accion("alert", WFAlertActionTitle="¿Borrar tu último gasto?",
               WFAlertActionMessage="Se quita el último gasto que registraste hoy.", WFAlertActionCancelButtonShown=True),
        accion("downloadurl", UUID=d, WFURL=f"{BASE}/gastos/ultimo", WFHTTPMethod="DELETE", ShowHeaders=True,
               WFHTTPHeaders=diccionario([("Authorization", 0, texto("Bearer \ufffc", (t, "Text")))])),
        accion("getvalueforkey", UUID=v, WFDictionaryKey="mensaje", WFGetDictionaryValueType="Value",
               WFInput=variable(d, "Contents of URL")),
        accion("notification", WFNotificationActionTitle="Gasto borrado",
               WFNotificationActionBody=texto("\ufffc", (v, "Dictionary Value")), WFNotificationActionSound=True),
    ]
    return atajo(acciones, [pregunta_token()], color=4282601983, glifo=59722)


def me_entro_dinero() -> bytes:
    """Dinero que no es la nomina (tu papa, una venta…): ¿cuánto? → ¿de dónde? → ¿a dónde va? (o abonar a una tarjeta)."""
    t, a, n, l, c, d, v = (uid() for _ in range(7))
    acciones = [
        accion("gettext", UUID=t, WFTextActionText=TOKEN),
        accion("ask", UUID=a, WFAskActionPrompt="¿Cuánto te entró?", WFInputType="Number"),
        accion("ask", UUID=n, WFAskActionPrompt="¿De dónde salió? (mi papá, una venta…)", WFInputType="Text",
               WFAllowsMultilineText=False),
        accion("list", UUID=l, WFItems=["Efectivo", "Ahorro", "Fondo de tarjetas"] + [f"Abonar a {x}" for x in TARJETAS]),
        accion("choosefromlist", UUID=c, WFChooseFromListActionPrompt="¿A dónde va?", WFInput=variable(l, "List")),
        accion("downloadurl", UUID=d, WFURL=f"{BASE}/entradas", WFHTTPMethod="POST", WFHTTPBodyType="JSON",
               ShowHeaders=True,
               WFHTTPHeaders=diccionario([("Authorization", 0, texto("Bearer \ufffc", (t, "Text")))]),
               WFJSONValues=diccionario([
                   ("monto", 3, texto("\ufffc", (a, "Provided Input"))),
                   ("nota", 0, texto("\ufffc", (n, "Provided Input"))),
                   ("destino", 0, texto("\ufffc", (c, "Chosen Item"))),
               ])),
        accion("getvalueforkey", UUID=v, WFDictionaryKey="mensaje", WFGetDictionaryValueType="Value",
               WFInput=variable(d, "Contents of URL")),
        accion("notification", WFNotificationActionTitle="Entrada guardada",
               WFNotificationActionBody=texto("\ufffc", (v, "Dictionary Value")), WFNotificationActionSound=True),
    ]
    return atajo(acciones, [pregunta_token()], color=431817727, glifo=59765)


def pague_tarjeta() -> bytes:
    """¿Cuál tarjeta? → ¿cuánto? → ¿con qué dinero? (fondo, efectivo o me lo dieron → ¿quién?)."""
    t, lt, tj, a, vacio, lf, f_, quien, d, v = (uid() for _ in range(10))
    g = uid()
    acciones = [
        accion("gettext", UUID=t, WFTextActionText=TOKEN),
        accion("list", UUID=lt, WFItems=TARJETAS),
        accion("choosefromlist", UUID=tj, WFChooseFromListActionPrompt="¿Qué tarjeta pagaste?", WFInput=variable(lt, "List")),
        accion("ask", UUID=a, WFAskActionPrompt="¿Cuánto pagaste?", WFInputType="Number"),
        accion("gettext", UUID=vacio, WFTextActionText=""),
        poner("Nota", vacio, "Text"),
        accion("list", UUID=lf, WFItems=["Fondo de tarjetas", "Mi efectivo", "Me lo dieron"]),
        accion("choosefromlist", UUID=f_, WFChooseFromListActionPrompt="¿Con qué dinero?", WFInput=variable(lf, "List")),
        si(g, f_, "Chosen Item", "Me lo dieron"),
        accion("ask", UUID=quien, WFAskActionPrompt="¿Quién te lo dio o de dónde salió?", WFInputType="Text",
               WFAllowsMultilineText=False),
        poner("Nota", quien, "Provided Input"),
        fin_si(g),
        accion("downloadurl", UUID=d, WFURL=f"{BASE}/pagos-tarjeta", WFHTTPMethod="POST", WFHTTPBodyType="JSON",
               ShowHeaders=True,
               WFHTTPHeaders=diccionario([("Authorization", 0, texto("Bearer \ufffc", (t, "Text")))]),
               WFJSONValues=diccionario([
                   ("tarjeta", 0, texto("\ufffc", (tj, "Chosen Item"))),
                   ("monto", 3, texto("\ufffc", (a, "Provided Input"))),
                   ("desde", 0, texto("\ufffc", (f_, "Chosen Item"))),
                   ("nota", 0, texto_var("Nota")),
               ])),
        accion("getvalueforkey", UUID=v, WFDictionaryKey="mensaje", WFGetDictionaryValueType="Value",
               WFInput=variable(d, "Contents of URL")),
        accion("notification", WFNotificationActionTitle="Pago de tarjeta",
               WFNotificationActionBody=texto("\ufffc", (v, "Dictionary Value")), WFNotificationActionSound=True),
    ]
    return atajo(acciones, [pregunta_token()], color=4271458815, glifo=59750)


def proximos_pagos() -> bytes:
    t, d, v = uid(), uid(), uid()
    acciones = [
        accion("gettext", UUID=t, WFTextActionText=TOKEN),
        accion("downloadurl", UUID=d, WFURL=f"{BASE}/proximos", WFHTTPMethod="GET", ShowHeaders=True,
               WFHTTPHeaders=diccionario([("Authorization", 0, texto("Bearer \ufffc", (t, "Text")))])),
        accion("getvalueforkey", UUID=v, WFDictionaryKey="mensaje", WFGetDictionaryValueType="Value",
               WFInput=variable(d, "Contents of URL")),
        accion("showresult", Text=texto("\ufffc", (v, "Dictionary Value"))),
    ]
    return atajo(acciones, [pregunta_token()], color=463140863, glifo=59446)


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("falta BEARER_TOKEN en el entorno")
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    pedidos = sys.argv[2:]  # opcional: solo estos archivos
    todos = {"Gaste.shortcut": gaste, "Cuanto-me-queda.shortcut": cuanto_queda,
             "Gasto-ApplePay.shortcut": apple_pay, "Cuanto-debo.shortcut": cuanto_debo,
             "Contar-efectivo.shortcut": contar_efectivo, "Como-voy-hoy.shortcut": como_voy_hoy,
             "Me-equivoque.shortcut": me_equivoque, "Me-entro-dinero.shortcut": me_entro_dinero,
             "Pague-tarjeta.shortcut": pague_tarjeta, "Proximos-pagos.shortcut": proximos_pagos}
    for nombre, hacer in todos.items():
        if pedidos and nombre not in pedidos:
            continue
        datos = hacer()
        (out / nombre).write_bytes(datos)
        plistlib.loads(datos)  # se vuelve a leer: el archivo es un plist valido
        print(f"{nombre}: {len(datos)} bytes")
