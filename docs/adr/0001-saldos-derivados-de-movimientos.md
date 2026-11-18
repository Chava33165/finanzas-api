# Los saldos se calculan de los movimientos; calibrar agrega la diferencia

Efectivo, Fondo de tarjetas, Cajas y deuda de cada tarjeta no se guardan: se recalculan cada vez a partir de
la lista de movimientos. Cuando la realidad no cuadra (gastos sin anotar, cargos del banco), la Calibración
inserta un movimiento con la diferencia en vez de sobrescribir el saldo. Se eligió así para que el historial
diga qué pasó de verdad y cualquier error se pueda deshacer borrando un movimiento; el costo es que lo que de
verdad pasó (un regalo, un pago a una tarjeta) debe registrarse como tal y no esconderse en una calibración,
o las gráficas de gasto quedan incompletas.
