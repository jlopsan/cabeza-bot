## MODIFIED Requirements

### Requirement: Ciclo sniper cada 3 minutos
`_ciclo_sniper` MUST ejecutarse cada `SNIPER_INTERVAL_MINUTES` (default 3) procesando las misiones v2 en estado `ACTIVA` con estas reglas:
1. **Cero IA y cero scraping ES en el ciclo**: usa marca/modelo persistidos en la misión y valoraciones cacheadas (`valoraciones_mercado`); como máximo refresca UNA valoración caducada por pasada.
2. **Agrupación por clave de scrapeo**: misiones con la misma URL de búsqueda AutoScout24 normalizada comparten UN scrapeo por pasada.
3. **Presupuesto por pasada**: `SNIPER_BUDGET_S` (default 150 s). Las claves se procesan ordenadas por `last_run_at` ASC; al agotar presupuesto, el resto espera la siguiente pasada (round-robin).
4. **Cap global**: máximo `SNIPER_MAX_SCRAPES_HORA` (default 60) scrapeos DE por hora, contador persistido.
5. **Detección barata**: solo fase 1 del scraper (listado ordenado por publicación reciente); la fase 2 (detalles) solo para candidatos nuevos que pasan el pre-filtro de margen.
6. Cada pasada actualiza `last_run_at` y `ultimo_error` de las misiones procesadas.

#### Scenario: Diez misiones del mismo modelo
- **WHEN** 10 misiones vigilan "BMW 320d" con los mismos filtros
- **THEN** la pasada lanza un único scrapeo de AutoScout24 y evalúa las 10 misiones contra el mismo resultado

#### Scenario: Treinta misiones y presupuesto agotado
- **WHEN** hay 30 claves de scrapeo y el presupuesto de 150 s se agota en la clave 12
- **THEN** las 18 restantes se procesan primero en la siguiente pasada (orden por `last_run_at` ASC) y ninguna pasada bloquea al resto de ciclos

#### Scenario: Match sniper
- **WHEN** una misión sniper detecta un anuncio nuevo que supera su umbral de margen
- **THEN** la notificación al usuario es inmediata, con la tarjeta de cuenta de importación completa

## REMOVED Requirements

### Requirement: Ciclo normal cada 15 minutos
**Reason**: El flujo `/buscar` interactivo que creaba misiones "normales" estaba oculto en producción (handler comentado) — no existen misiones normales reales. Las misiones v2 van todas al ciclo sniper con agrupación y presupuesto, que ya resuelve el coste que el ciclo de 15 min intentaba mitigar.
**Migration**: La migración de BD marca cualquier misión pre-v2 como `EXPIRADA`. `_ciclo_normal` y `procesar_mision` legacy se eliminan; el worker queda con ciclo sniper + health.

### Requirement: Cálculo de beneficio según precio objetivo
**Reason**: El margen ahora lo define la evaluación de `sniper-alemania` (valoración de mercado cacheada + cuenta de importación con IEDMT sobre valor ES + umbral doble €/% por misión). El `precio_objetivo_es` manual del legacy desaparece del flujo.
**Migration**: `_get_beneficio_coche` se elimina. La columna `precio_objetivo_es` queda inerte en BD (compatibilidad, no se lee).

## ADDED Requirements

### Requirement: Circuit breaker por fuente DE
El worker MUST mantener en la tabla `estado_fuentes` un contador de fallos consecutivos por fuente. Tras `SNIPER_CB_FALLOS` (default 3) fallos seguidos de AutoScout24, MUST pausar el ciclo sniper hasta `now + 30 min` con log WARNING claro, sin afectar al ciclo health ni al resto del bot. Un scrapeo exitoso MUST resetear el contador. "Cero resultados con HTML válido" MUST NOT contar como fallo. El estado MUST sobrevivir reinicios (persistido en SQLite).

#### Scenario: AutoScout24 bloquea tres pasadas seguidas
- **WHEN** el scraper falla 3 veces consecutivas
- **THEN** el ciclo sniper queda pausado 30 min, se loguea el motivo, y el ciclo health sigue corriendo

#### Scenario: Mercado sin resultados
- **WHEN** una búsqueda devuelve 0 anuncios pero el HTML es válido
- **THEN** no incrementa el contador de fallos

### Requirement: Idempotencia total tras reinicio del worker
Todo el estado del sniper (snapshot, alertas enviadas, valoraciones, breaker, contadores) MUST vivir en SQLite. Un reinicio del worker MUST NOT re-enviar alertas ya enviadas, MUST NOT re-sembrar snapshots ya sembrados y MUST NOT perder el estado del circuit breaker.

#### Scenario: Reinicio a mitad de pasada
- **WHEN** el worker muere tras alertar 2 de 3 candidatos y se reinicia
- **THEN** los 2 alertados no se repiten y el tercero se evalúa en la siguiente pasada

### Requirement: Expiración automática de misiones
Cada pasada MUST marcar como `EXPIRADA` las misiones con `expira_at < now` antes de procesarlas. Las misiones `EXPIRADA` o `PAUSADA` MUST NOT generar scraping.

#### Scenario: Misión zombi
- **WHEN** una misión lleva 30 días sin renovarse
- **THEN** pasa a `EXPIRADA` y deja de consumir presupuesto de scraping

### Requirement: Logging por misión
Cada pasada MUST dejar rastro consultable por misión: última pasada (`last_run_at`), anuncios vistos, alertas enviadas (contador) y último error (`ultimo_error`). Sin esto no se puede depurar "no me llegan alertas".

#### Scenario: Usuario reporta que no le llegan alertas
- **WHEN** el admin consulta la misión vía `/stats_sniper` o los logs
- **THEN** puede ver cuándo corrió por última vez, cuántos anuncios vio y qué error tuvo (si hubo)
