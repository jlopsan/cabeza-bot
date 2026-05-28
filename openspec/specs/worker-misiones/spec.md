# worker-misiones

## Purpose

Define el daemon que ejecuta misiones periódicas de búsqueda en background, separado del proceso del bot. Tres ciclos: normal (15 min, misiones de usuario), sniper (3 min, misiones premium con alerta), health (diario, purga de histórico y métricas). Notifica al usuario por Telegram cuando encuentra match. Archivos: [worker.py](../../../worker.py), [database.py](../../../database.py).

## Requirements

### Requirement: Ciclo normal cada 15 minutos
`_ciclo_normal` MUST ejecutarse cada 15 minutos y procesar todas las misiones activas con `es_sniper=False`, lanzando `procesar_mision` para cada una.

#### Scenario: Misión normal encuentra match nuevo
- **WHEN** `procesar_mision` encuentra un anuncio que cumple los filtros y no estaba en el histórico de la misión
- **THEN** el bot envía mensaje al usuario via `_send` con los datos del coche y registra el match para no reenviarlo

### Requirement: Ciclo sniper cada 3 minutos
`_ciclo_sniper` MUST ejecutarse cada 3 minutos para misiones con `es_sniper=True`, llamando `procesar_mision(mision, es_sniper=True)`.

#### Scenario: Match sniper
- **WHEN** una misión sniper detecta un anuncio nuevo
- **THEN** la notificación al usuario es inmediata y con prefijo destacado de alerta

### Requirement: Ciclo health diario
`_ciclo_health` MUST ejecutarse una vez al día y SHALL:
1. Purgar `historico_precios` mayor a `HISTORICO_RETENCION_DIAS` (default 180).
2. Reportar métricas básicas (cuántos anuncios, cuántas misiones activas) al log.

#### Scenario: Histórico antiguo
- **WHEN** existen filas en `historico_precios` con `fecha < hoy - 180 días`
- **THEN** el ciclo health las elimina vía `purgar_historico_antiguo(180)`

### Requirement: Cálculo de beneficio según precio objetivo
`_get_beneficio_coche(coche, precio_objetivo)` MUST devolver `precio_objetivo - precio_coche` cuando `precio_objetivo` esté definido, o `None` en caso contrario, para que `procesar_mision` solo notifique cuando hay beneficio positivo si la misión lo exige.

#### Scenario: Misión con precio objetivo y match negativo
- **WHEN** el coche cuesta más que el `precio_objetivo` de la misión
- **THEN** no se envía notificación al usuario aunque cumpla los demás filtros

### Requirement: Daemon independiente del bot
El worker MUST poder ejecutarse en proceso separado (`python worker.py`) sin compartir loop de evento ni objetos en memoria con `main.py`. Solo comparte la base SQLite.

#### Scenario: Bot cae pero worker sigue
- **WHEN** el proceso del bot muere
- **THEN** el worker sigue procesando ciclos y notifica al usuario cuando el bot vuelva (los mensajes encolan vía API HTTP de Telegram, no requieren proceso bot vivo)
