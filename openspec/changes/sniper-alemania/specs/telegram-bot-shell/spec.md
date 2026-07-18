## ADDED Requirements

### Requirement: Captura de deep link en /start
`/start` MUST leer el payload de deep link (`ctx.args`, p. ej. `t.me/bot?start=v_sniper_alemania`). En la primera captura MUST persistir `usuarios.fuente_captacion` y `fuente_captacion_at` (first-touch: no se sobrescribe en visitas posteriores) y MUST registrar el evento `start` con el payload en `meta`. Si el payload identifica una campaña del sniper (`v_sniper*`), la bienvenida MUST ser contextual al sniper en lugar de la genérica.

#### Scenario: Usuario llega por deep link del sniper
- **WHEN** un usuario nuevo abre el bot con `?start=v_sniper_alemania`
- **THEN** se guarda `fuente_captacion='v_sniper_alemania'`, se registra el evento `start`, y la bienvenida menciona el sniper directamente

#### Scenario: Usuario ya registrado vuelve con otro payload
- **WHEN** un usuario con `fuente_captacion` ya guardada abre el bot con un payload distinto
- **THEN** no se sobrescribe la fuente original (first-touch se conserva)

#### Scenario: Start sin payload
- **WHEN** un usuario envía `/start` sin argumentos
- **THEN** recibe la bienvenida genérica y no se altera su `fuente_captacion`
