## ADDED Requirements

### Requirement: Coste de misión sniper según tier, con cobro en éxito
El coste de crear una misión sniper MUST depender del tier: `free` descuenta `COSTE_SNIPER_FREE` (default 1) crédito, `paid` descuenta `COSTE_SNIPER_PAID` (default 5), `pro` no descuenta (dormido). El handler MUST usar `@requiere_acceso("/sniper", registrar=False)` (con `COSTE_COMANDO["/sniper"]=1` como gate mínimo del decorator) y calcular el coste real por tier dentro del flujo, descontándolo manualmente SOLO cuando la misión queda creada y sembrada (patrón cobro-en-éxito de `/tasar`). Las alertas del worker MUST NOT consumir créditos: se paga por misión, no por resultado. Pausar, reanudar, borrar, editar umbral y renovar una misión expirada MUST ser gratis. Ambos costes MUST ser configurables por env.

#### Scenario: Creación abortada a mitad de flujo
- **WHEN** un usuario inicia `/sniper` pero cancela antes de confirmar la misión
- **THEN** no se descuenta ningún crédito

#### Scenario: Paid crea misión con éxito
- **WHEN** un usuario paid confirma una misión y queda activa y sembrada
- **THEN** se descuentan 5 créditos en ese momento y las alertas posteriores no descuentan nada

### Requirement: Free puede usar el sniper una sola vez
Un usuario `free` MUST poder crear como máximo UNA misión sniper en toda su vida (aunque la borre después), descontando 1 crédito. El sniper es una función de las tres acciones gratuitas: gasta 1 de los 3 créditos free y deja los otros 2 para el resto de comandos. Tras esa única creación, `/sniper` MUST bloquear al usuario free con el paywall del sniper, con independencia de los créditos que le queden. El uso histórico se determina contando eventos `mision_creada` del usuario (append-only, robusto ante borrado de misiones).

#### Scenario: Free crea su primera misión
- **WHEN** un usuario free con 3 créditos crea su primera misión sniper
- **THEN** se descuenta 1 crédito (le quedan 2 para otros comandos) y la misión queda activa

#### Scenario: Free intenta una segunda misión
- **WHEN** un usuario free que ya creó una misión alguna vez (aunque la haya borrado) intenta `/sniper` de nuevo
- **THEN** se bloquea con el paywall del sniper aunque le queden créditos, porque el sniper free es de un solo uso

### Requirement: Límite de misiones activas por tier
El flujo de creación MUST comprobar contra BD el nº de misiones en estado `ACTIVA` del usuario y bloquear si alcanza el límite de su tier: `free` 1, `paid` 3, `pro` ilimitado (dormido), admin sin límite. Los límites MUST ser configurables por env sin tocar BD ni decorator. Para `free`, el tope de un solo uso histórico es más restrictivo y prevalece.

#### Scenario: Paid con misiones pausadas
- **WHEN** un usuario paid tiene 3 activas y 2 pausadas e intenta crear otra
- **THEN** se bloquea (cuentan las activas); si pausa una, puede crear

### Requirement: Paywall específico del sniper
Cuando el bloqueo ocurre en el flujo `/sniper` (sin créditos o sin hueco de misión), el paywall MUST usar mensaje propio orientado a importadores ("herramienta de trabajo — un solo coche paga el pack 100 veces") en lugar del genérico, manteniendo los botones de pago existentes, y MUST registrar el evento `paywall_visto` con `meta='sniper'`.

#### Scenario: Sin créditos al crear misión
- **WHEN** un usuario con 2 créditos intenta crear una misión de coste 5
- **THEN** recibe el paywall específico del sniper con los botones de pack y se registra `paywall_visto`
