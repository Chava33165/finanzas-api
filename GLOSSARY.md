# Dinero de Chava

El sistema con el que Chava reparte su nómina semanal, controla lo que gasta, paga sus tarjetas de crédito y ahorra.

## Ingresos

**Pago recibido**:
La nómina semanal de Plexus que llega cada jueves y se reparte.
_Avoid_: sueldo, depósito

**Nómina**:
El recibo semanal de Plexus (CFDI) con percepciones, deducciones y neto; un Pago recibido se liga a su Nómina.
_Avoid_: recibo de pago, comprobante

**Reparto**:
La división de un Pago recibido en Gastos, Fondo de tarjetas y Ahorro, por porcentajes (33/33/34); el Ahorro se queda con el redondeo.
_Avoid_: distribución, split

**Ingreso extra**:
Dinero que no es la nómina semanal (Fondo Plexus, aguinaldo, regalos); no pasa por el Reparto y puede repartirse a mano en varios destinos.
_Avoid_: bono, entrada

**Fondo Plexus**:
El fondo de ahorro de la empresa: Chava y Plexus aportan lo mismo cada semana y se entrega una vez al año.
_Avoid_: fondo de ahorro (se confunde con el Fondo de tarjetas)

**Liquidación del Fondo Plexus**:
La entrega anual del Fondo Plexus; se reconoce porque el acumulado del recibo vuelve a empezar.
_Avoid_: cobro del fondo

## Dinero disponible

**Efectivo**:
Lo que Chava tiene para gastar en la semana: la cartera más lo de gastos que queda en su cuenta de débito de nómina.
_Avoid_: cash, saldo

**Límite de la semana**:
La parte de Gastos del último Reparto; la semana va de jueves a miércoles.
_Avoid_: presupuesto

**Fondo de tarjetas**:
Dinero apartado solo para pagar tarjetas de crédito; el Ahorro nunca paga tarjetas.
_Avoid_: fondo (a secas)

**Caja**:
Un apartado de ahorro con nombre, meta y rendimiento; una de ellas es la Caja destino, donde cae el Ahorro de cada Reparto.
_Avoid_: cuenta de ahorro, cajita (salvo en su nombre)

## Gastos

**Gasto**:
Dinero que sale para algo que Chava compró o dio, con una categoría y un método (efectivo, una tarjeta, o dinero extra).

**Gasto de dinero extra**:
Un Gasto pagado con un Ingreso extra: cuenta en "¿En qué se va?" pero no en el Límite de la semana ni en el Efectivo.
_Avoid_: gasto extraordinario

**Gastos sin anotar**:
Efectivo que falta al Calibrar; se registra como Gasto de la semana en que se gastó.

## Tarjetas

**Corte**:
El día en que la tarjeta cierra su estado de cuenta.

**Ciclo**:
Lo que va de un Corte al siguiente.

**Pago para no generar intereses**:
Lo que falta pagar del estado de cuenta ya cerrado, antes de su fecha límite.
_Avoid_: pago mínimo (es otra cosa)

**Anticipo**:
Lo que se le paga a una tarjeta de más sobre el estado ya cerrado; baja lo que se debe del Ciclo en curso.
_Avoid_: abono, prepago

## Mantenimiento

**Calibración**:
Dejar un saldo igual que la realidad agregando la diferencia como movimiento, sin borrar el historial.
_Avoid_: corrección, reset, saldos iniciales
