# freemium-creditos

## Purpose

Define el sistema unificado de créditos: FREE 3/día (reset medianoche UTC), PACK 30 (4.90€), PACK 100 (9.90€), ambos sin caducidad y acumulables. Tier PRO (suscripción mensual) está dormido pero listo. El decorator `@requiere_acceso(comando)` aplica la regla en cada handler. Pago vía Stripe Checkout con idempotencia por `stripe_id`. Archivos: [permisos.py](../../../permisos.py), [database.py](../../../database.py), [webhook.py](../../../webhook.py), [config.py](../../../config.py).

## Requirements

### Requirement: Tres tiers de usuario
La columna `tier` en `usuarios` MUST aceptar exactamente tres valores: `free`, `paid`, `pro`. El comportamiento por tier:
- `free`: 3 créditos/día con reset diario. Bloquea si `creditos_disponibles < coste`.
- `paid`: créditos acumulados sin caducidad. Cuando llega a 0 → vuelve a `free` con 3 créditos.
- `pro`: siempre pasa, no descuenta (DORMIDO hasta lanzar PRO).

#### Scenario: Usuario free agota créditos del día
- **WHEN** un usuario free con `creditos_disponibles=0` y reset de hoy llama un comando
- **THEN** el decorator bloquea y envía paywall con botones de Pack 30 y Pack 100

#### Scenario: Usuario paid agota pack
- **WHEN** un usuario paid descuenta su último crédito
- **THEN** su tier vuelve a `free` con 3 créditos y `ultimo_reset_diario` puesto a hoy

### Requirement: Reset diario automático a medianoche UTC
El método `puede_usar(user_id, coste)` para tier `free` MUST detectar si `ultimo_reset_diario` es anterior al día UTC actual y resetear `creditos_disponibles` a `FREE_CREDITOS_DIA` (default 3) antes de evaluar.

#### Scenario: Usuario free vuelve al día siguiente
- **WHEN** un usuario que agotó ayer envía un comando hoy
- **THEN** `puede_usar` resetea sus créditos a 3, descuenta 1 y deja `restantes=2`

### Requirement: Decorator @requiere_acceso aplica coste por comando
El decorator `requiere_acceso(comando)` MUST consultar `COSTE_COMANDO[comando]` (default 1 si falta) y:
1. Llamar `puede_usar(user_id, coste)`. Si False → enviar paywall y abortar.
2. Si True → ejecutar el handler.
3. Si `registrar=True` (default), llamar `registrar_uso` tras el handler para descontar.

#### Scenario: Comando con registrar=False
- **WHEN** un handler usa `@requiere_acceso("/ideal", registrar=False)`
- **THEN** el decorator chequea acceso pero NO descuenta (el handler descuenta manualmente al terminar el flujo)

### Requirement: Activación de pack por webhook Stripe
La función `activar_plan(user_id, concepto, stripe_id)` MUST:
- `concepto='pack_30'` → tier='paid', `creditos += PAID_CREDITOS_PACK_30` (default 30)
- `concepto='pack_100'` → tier='paid', `creditos += PAID_CREDITOS_PACK_100` (default 100)
- `concepto='pro_mes'` → tier='pro' (dormido, listo)

#### Scenario: Usuario compra Pack 30 con 5 créditos restantes
- **WHEN** un usuario paid con `creditos=5` compra otro Pack 30
- **THEN** sus créditos pasan a 35 (acumulan, no reemplazan)

### Requirement: Idempotencia de pagos por stripe_id
La función `pago_ya_procesado(stripe_id)` MUST devolver True si ese `stripe_id` ya está en la tabla `pagos`. El webhook MUST consultarla antes de activar plan y abortar si ya se procesó.

#### Scenario: Stripe reenvía el mismo evento
- **WHEN** Stripe entrega el mismo `checkout.session.completed` dos veces
- **THEN** el segundo evento detecta `pago_ya_procesado=True` y no duplica créditos

### Requirement: Metadata Telegram en checkout
La función `callback_pago` MUST crear la sesión de Stripe con `metadata={"telegram_user_id": str(user_id), "concepto": ...}` para que el webhook pueda atar el pago al usuario sin email.

#### Scenario: Webhook recibe checkout.session.completed
- **WHEN** el webhook recibe el evento
- **THEN** lee `metadata.telegram_user_id` y `metadata.concepto`, comprueba idempotencia, y llama `activar_plan(user_id, concepto, stripe_id)`

### Requirement: Notificación al usuario tras activar pack
El webhook MUST enviar un mensaje a Telegram al usuario vía `api.telegram.org/sendMessage` tras activar exitosamente un pack, confirmando los créditos añadidos.

#### Scenario: Pack 100 activado
- **WHEN** `activar_plan` completa con `concepto='pack_100'`
- **THEN** el webhook envía un mensaje al chat confirmando "100 créditos añadidos" con tono cavernícola
