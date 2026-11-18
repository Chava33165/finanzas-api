# Atajos de Apple para tu dinero

`task-hub` ya no maneja tareas: ahora es la API de tus gastos. Los atajos le hablan por tu
Cloudflare Tunnel de siempre, así que funcionan desde cualquier lugar, sin Tailscale.

- **URL base:** `https://tasks.tu-dominio.com/api/v1`.
- **Token:** el valor de `BEARER_TOKEN` en `~/Servicios/task-hub/.env`. Para verlo: `grep BEARER_TOKEN ~/Servicios/task-hub/.env`. No lo compartas: con él se pueden registrar gastos a tu nombre.

---

## Forma rápida: instalar los archivos ya hechos

Los atajos ya vienen armados en `~/Documentos/Personal/Atajos/` (se ven por Samba en Documentos → Personal → Atajos): `Gaste.shortcut` y
`Cuanto-me-queda.shortcut`, con la URL y tu token ya puestos.

Apple solo deja abrir atajos **firmados**, y eso solo se puede hacer en una Mac:

1. En la Mac, abre **Terminal** en la carpeta donde están (Samba → Documentos → Personal → Atajos) y corre:
   ```
   shortcuts sign -m anyone -i Gaste.shortcut -o "Gasté.shortcut"
   shortcuts sign -m anyone -i Cuanto-me-queda.shortcut -o "¿Cuánto me queda?.shortcut"
   ```
2. Da doble clic a cada archivo firmado → **Agregar atajo**. Te pregunta el token: ya viene puesto, solo continúa.
3. Por iCloud aparecen solos en tu iPhone y en tu iPad.

Para volver a generarlos (p. ej. si cambias el token):
`docker run --rm --user 1000:1000 --env-file ~/Servicios/task-hub/.env -v ~/Servicios/task-hub/atajos:/a:ro -v ~/Documentos/Personal/Atajos:/out python:3.12-slim python /a/generar_atajos.py /out`

En "¿Con qué pagaste?" ya vienen las opciones a meses (`Nu · 3 MSI`, `Plata · 6 MSI`…), así no te pregunta
por meses cuando pagas en efectivo.

---

## Hacerlo a mano

## 1. Atajo "Gasté"

En la app **Atajos**, toca **+** y llámalo **Gasté**. Agrega estas acciones en orden:

1. **Pedir entrada.**
   - Tipo: *Número*.
   - Pregunta: "¿Cuánto gastaste?".
2. **Elegir del menú.**
   - Pregunta: "¿Con qué pagaste?".
   - Opciones: `Efectivo`, `Nu` y `Plata`.
   - Dentro de cada opción pon una acción **Texto** con `efectivo`, `nu` o `plata` (en minúsculas).
3. **¿A meses?** Agrega **Elegir del menú** con la pregunta "¿Contado o a meses?" y las opciones `Contado`, `3 MSI`, `6 MSI`, `9 MSI` y `12 MSI`.
   - Dentro de cada opción pon una acción **Número** con `0`, `3`, `6`, `9` o `12`.
   - Si pagaste en efectivo, elige *Contado*.
4. **Elegir de la lista**, a partir de una acción **Lista** con estos elementos: `Comida`, `Transporte`, `Escuela`, `Salidas`, `Personal`, `Otros`.
5. **Pedir entrada.**
   - Tipo: *Texto*.
   - Pregunta: "¿Qué compraste? (opcional)". Con meses, esto es el nombre que verás en la tarjeta, ej. "Audífonos".
   - Activa *Permitir vacío*.
6. **Obtener contenido de URL.**
   - URL: `https://tasks.tu-dominio.com/api/v1/gastos`.
   - Método: **POST**.
   - Encabezados: `Authorization` = `Bearer <tu-token>`.
   - Cuerpo de la solicitud: **JSON**, con estos campos:
     - `monto` (Número): la *Entrada proporcionada* del paso 1;
     - `metodo` (Texto): el *Resultado del menú* del paso 2;
     - `meses` (Número): el *Resultado del menú* del paso 3;
     - `categoria` (Texto): el *Elemento elegido* del paso 4;
     - `nota` (Texto): la *Entrada proporcionada* del paso 5.
7. **Obtener valor del diccionario**, con la clave `mensaje`.
8. **Mostrar notificación** con el *Valor del diccionario*.

El atajo te responde algo así:
- en efectivo: **"Te quedan $430 de $802 esta semana."**;
- con tarjeta de contado: **"Nu: llevas $2,800 este ciclo de tu tope de $3,000."**;
- a meses: **"Nu a 3 MSI: $266.67 al mes, último pago el 28/01/2027. Debes $999.83 a meses en Nu. Este ciclo llevas $2,773.50 de tu tope de $3,000."**

Con meses, a la tarjeta solo se le carga la mensualidad en cada corte; la compra completa sí cuenta en "¿En qué se va?".

> Tip: ponlo en la pantalla de inicio o en el botón de acción. También puedes decir **"Oye Siri, gasté"**.

## 2. Atajo "¿Cuánto me queda?"

1. **Obtener contenido de URL.**
   - URL: `https://tasks.tu-dominio.com/api/v1/resumen`.
   - Método: **GET**.
   - Encabezado: `Authorization` = `Bearer <tu-token>`.
2. **Obtener valor del diccionario**, con la clave `mensaje`.
3. **Mostrar resultado.**

## Si algo falla

| Respuesta | Qué significa |
|---|---|
| `401` | El token está mal o le falta la palabra `Bearer ` (con espacio) antes. |
| `422` | El monto está vacío o es negativo, o el método o la categoría no son de la lista. |
| No responde | Revisa en la Pi: `cd ~/Servicios/task-hub && docker compose ps` (deben estar `backend` healthy y `cloudflared` Up). |

Todo lo demás (el reparto de cada jueves, pagar tarjetas, las cajas de ahorro y la nómina) se hace en la pestaña **Dinero** del dashboard.

## 3. Apple Pay automático (automatización "Transacción")

Registra solo cada pago con Apple Pay: monto, comercio y tarjeta. La categoría la adivina por el
comercio (OXXO → Comida, Uber → Transporte…).

- **Nu o Plata** → gasto de esa tarjeta (cuenta contra su tope).
- **BBVA (débito, donde cae la nómina)** → cuenta como **efectivo**: baja tu límite de la semana.

1. Firma e importa `Gasto-ApplePay.shortcut` (igual que los otros):
   `shortcuts sign -m anyone -i Gasto-ApplePay.shortcut -o "Gasto Apple Pay.shortcut"`
2. En el **iPhone**: Atajos → **Automatización** → **+** → **Transacción**.
3. En **Tarjetas** elige **Nu, Plata y BBVA**. Deja marcadas todas las categorías.
4. Marca **Ejecutar inmediatamente** (y desactiva "Notificar al ejecutar" si no quieres doble aviso).
5. Siguiente → **Gasto Apple Pay**.

Si la notificación dice "No reconozco la tarjeta", renombra la tarjeta en Wallet para que su nombre
contenga Nu, Plata o BBVA.

**Si al probar llega vacío** (el atajo no recibe monto o comercio), ábrelo y en la acción "Obtener
contenido de URL" vuelve a elegir en cada campo: *Entrada del atajo* → **Importe** / **Comercio** /
**Tarjeta o pase**.

## 4. "¿Cuánto debo?"

Firma e importa `Cuanto-debo.shortcut`. Te muestra, por tarjeta, lo que tienes que pagar y antes de
qué día, lo que llevas en el ciclo, lo que debes a meses y si tu fondo alcanza.

## 5. Uso diario: "Cuento mi efectivo", "¿Cómo voy hoy?" y "Me equivoqué"

Ya vienen firmados en iCloud Drive → **Atajos Dinero**. Ábrelos en el iPhone (app Archivos) → **Agregar atajo**.

| Atajo | Qué hace | Endpoint |
|---|---|---|
| **Cuento mi efectivo** | Cuentas lo que traes (cartera + lo de gastos en tu débito) y el sistema queda igual. Lo que falte cuenta como "Gastos sin anotar" de la semana; el día de pago, de la semana que terminó. | `POST /calibrar` `{"efectivo": N}` |
| **¿Cómo voy hoy?** | Lo que llevas hoy y cuánto puedes gastar por día hasta el jueves. | `GET /hoy` |
| **Me equivoqué** | Borra el último gasto que registraste (solo de las últimas 24 h), después de confirmar. | `DELETE /gastos/ultimo` |

Tip: pon **Cuento mi efectivo** como automatización cada miércoles en la noche (Atajos → Automatización →
Hora del día), así el jueves tu reparto sale con el sobrante real.

Para regenerarlos: el mismo comando de arriba con los nombres `Contar-efectivo.shortcut Como-voy-hoy.shortcut
Me-equivoque.shortcut`, y firmarlos en la Mac con `shortcuts sign -m anyone -i <archivo> -o <nombre>.shortcut`.

## 6. "Me entró dinero", "Pagué mi tarjeta" y "Próximos pagos"

Ya vienen firmados en iCloud Drive → **Atajos Dinero**. Ábrelos en el iPhone (app Archivos) → **Agregar atajo**.

| Atajo | Qué hace | Endpoint |
|---|---|---|
| **Me entró dinero** | Dinero que no es tu nómina (tu papá, una venta…). Te pregunta cuánto, de dónde salió y a dónde va: efectivo, ahorro, fondo de tarjetas o **abonar directo** a Nu o Plata. | `POST /entradas` `{"monto", "nota", "destino"}` |
| **Pagué mi tarjeta** | Qué tarjeta, cuánto y con qué dinero: **fondo de tarjetas**, **mi efectivo** (baja lo que te queda de la semana) o **me lo dieron** (entra y sale directo a la tarjeta, sin tocar tu efectivo ni tu fondo). | `POST /pagos-tarjeta` `{"tarjeta", "monto", "desde", "nota"}` |
| **Próximos pagos** | Lo que viene en los próximos 45 días: pagos de tarjeta (lo del corte, lo que va del ciclo y mensualidades a meses), cortes y tu pago del jueves, y si tu fondo alcanza. | `GET /proximos` |

Los atajos mandan las opciones tal como se ven ("Nu", "Abonar a Plata", "Mi efectivo") y la API las entiende.
Si algo no se puede (p. ej. el fondo no alcanza), la notificación te dice por qué con un ⚠️.

Para regenerarlos: el mismo comando de arriba con los nombres `Me-entro-dinero.shortcut Pague-tarjeta.shortcut
Proximos-pagos.shortcut`, y firmarlos en la Mac con `shortcuts sign -m anyone -i <archivo> -o <nombre>.shortcut`.

## Calibrar todo (en la app)

Dinero → **¿No cuadra algo? Calibrar mis saldos** (o el aviso "Tus datos se quedaron atrás", que sale si llevas
3 días sin registrar nada o una semana sin calibrar). Cada saldo viene con lo que dice el sistema: cambias lo que
no cuadre (efectivo, fondo, cajas, y lo que dice la app de cada tarjeta) y la diferencia entra como ajuste. El
historial no se borra. `POST /calibrar` acepta `efectivo`, `fondo`, `cajas: {id: saldo}`,
`tarjetas: {id: {corte, ciclo}}` y `faltante_como_gasto`.
