# freemium-creditos

## Purpose

Define el sistema unificado de créditos: FREE 3 de por vida (una vez por usuario, sin reset), PACK 30 (4.90€), PACK 100 (9.90€), ambos sin caducidad y acumulables. Tier PRO (suscripción mensual) está dormido pero listo. El decorator `@requiere_acceso(comando)` aplica la regla en cada handler. Pago vía Stripe Checkout con idempotencia por `stripe_id`. Archivos: [permisos.py](../../../permisos.py), [database.py](../../../database.py), [webhook.py](../../../webhook.py), [config.py](../../../config.py).

## Requirements

### Requirement: Tres tiers de usuario
La columna `tier` en `usuarios` MUST aceptar exactamente tres valores: `free`, `paid`, `pro`. El comportamiento por tier:
- `free`: `FREE_CREDITOS` créditos de por vida (dados una vez al crear el usuario, SIN reset). Bloquea si `creditos_disponibles < coste`.
- `paid`: créditos acumulados sin caducidad. Cuando llega a 0 → sigue `paid` bloqueado hasta recargar pack.
- `pro`: siempre pasa, no descuenta (DORMIDO hasta lanzar PRO).

#### Scenario: Usuario free agota sus créditos
- **WHEN** un usuario free con `creditos_disponibles=0` llama un comando
- **THEN** el decorator bloquea y envía paywall con botón de Pack 100

#### Scenario: Usuario paid agota pack
- **WHEN** un usuario paid descuenta su último crédito
- **THEN** queda en `creditos_disponibles=0` con tier `paid`; el siguiente intento recibe el paywall de recarga de pack

### Requirement: Créditos free de por vida sin reset
El método `puede_usar(user_id, coste)` para tier `free` MUST evaluar `creditos_disponibles >= coste` SIN regenerar créditos. Los `FREE_CREDITOS` (default 3) se otorgan una sola vez en `get_o_crear_usuario` y no se renuevan por tiempo. No existe reset diario ni ventana temporal.

#### Scenario: Usuario free agotado vuelve otro día
- **WHEN** un usuario que agotó sus 3 créditos envía un comando días después
- **THEN** `puede_usar` devuelve `(False, 0)` — no se regeneran créditos; solo un pack lo desbloquea

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
