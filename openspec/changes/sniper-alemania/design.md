## Context

El flujo legacy `/buscar` v3 existe completo pero oculto (handlers comentados en `main.py:2998-3002`). Auditoría del estado actual:

**Lo que se salva (funciona o casi):**
- `ScraperAutoScout24` (2 fases: listado → detalles) con selectores `data-*` estables, stealth context y semáforo Playwright global.
- `ScraperMobileDe` ya existe tras la interfaz `ScraperDE` (el doc lo pedía como fase 2 — ya está hecho, solo se desactiva para misiones v1).
- Worker con 3 ciclos (`normal` 15 min, `sniper` 3 min, `health` diario) que coexisten via `asyncio.gather`.
- Dedup básico por `oportunidades_enviadas(mision_id, coche_id)` — existe pero es pobre (sin datos, sin re-publicaciones).
- Tramos IEDMT en `config.py` correctos para 2026 (0/4,75/9,75/14,75).
- Tablas de normalización DE (colores, carrocerías, combustibles, extras) completas.

**Lo que está roto o mal diseñado (por qué se comentó el handler):**
- `procesar_mision` llama `parsear_modelo_nl` (IA) en CADA ciclo por CADA misión — coste y latencia absurdos a 3 min.
- `buscar_y_cruzar` scrapea comparables ES (Wallapop + Coches.net con Playwright) en cada pasada — inviable a 3 min con >2 misiones.
- Sin snapshot inicial: misión nueva alerta todo lo que supere umbral (spam de 5+ alertas al crearla).
- IEDMT sobre precio de compra DE — base legal incorrecta, infla o desinfla el margen.
- Sin circuit breaker: si AS24 bloquea, el ciclo martillea cada 3 min (empeora el bloqueo).
- Tier-gating propio (`TIER_LIMITS` free/pro/sniper en `main.py`) anterior al freemium — contradice `permisos.py`.
- `calcular_sniper_score`: 4 sub-scores con pesos arbitrarios y "frescura" inventada (longitud de descripción). Duplica lo que dice el margen con menos honestidad.
- CO₂ casi nunca presente en listado; fallback a `estimar_co2` (IA) dentro del scraper — IA en el hot loop del worker.

**Restricciones:** SQLite pelado (sin ORM), migraciones aditivas, no romper `/analizar` v4 ni `/ideal` ni `/comparar` ni `/tasar` ni el ciclo normal, tono cavernícola, público objetivo = importadores profesionales (la credibilidad del número ES el producto).

## Goals / Non-Goals

**Goals:**
- `/sniper` crea misiones fiables: snapshot inicial, dedup persistente, alerta en <10 min del anuncio nuevo con margen.
- Cuenta de importación honesta: margen neto con desglose, IEDMT etiquetado "estimado", aviso IVA nuevo fiscal, flag Netto.
- Ciclo de 3 min barato y robusto: cero IA, cero scraping ES, agrupación de scrapeos, presupuesto por pasada, circuit breaker.
- Todo estado en SQLite: reinicio del worker no re-alerta ni pierde nada.
- Monetización: misión = 5 créditos, límites por tier, paywall propio.
- Embudo medible: deep link → misión → alerta → pago.

**Non-Goals:**
- mobile.de como fuente de misiones (fase 2 — la interfaz ya lo permite).
- Botón "Analizar a fondo" (requiere `/analizar` con AutoScout24 — fase 2).
- Tablas BOE del IEDMT embebidas (fase 2 si los pro lo piden; v1 = estimación con disclaimer).
- Proxies rotatorios (se diseña el hueco — `PROXIES` ya existe en config — pero no se puebla).
- PRO mensual activo (los límites por tier lo dejan listo, sigue dormido).

## Decisions

### D1 — Comando `/sniper` con alias `/buscar`
El vídeo dice "sniper"; el comando dice lo mismo. `/buscar` se registra apuntando al mismo entry point (usuarios antiguos no se pierden). El flujo interactivo legacy de `/buscar` (búsqueda puntual con tarjetas) NO revive: `/sniper` es solo misiones. Alternativa considerada: revivir ambos flujos — rechazada (dos flujos a mantener, la búsqueda puntual ya la cubre `/ideal` + el propio ciclo de misión).

### D2 — Módulo nuevo `sniper_pipeline.py`
Bot (`main.py`) y worker necesitan la MISMA lógica de evaluación y render (crear misión evalúa candidatos iniciales; el ciclo evalúa nuevos). Se extrae a `sniper_pipeline.py` (patrón `ideal_pipeline.py`): construcción de clave de scrapeo, evaluación de candidato (filtros → valoración → cuenta → umbral), render de tarjeta de alerta, gestión de snapshot. `worker.py` y `main.py` lo importan; ninguno duplica. Alternativa: meter todo en `worker.py` — rechazada (main necesita evaluar al crear, y worker.py ya mezcla demasiado).

### D3 — Misiones v2 por columnas aditivas (sin tabla nueva)
`ALTER TABLE misiones ADD COLUMN`: `marca`, `modelo` (parseados con IA UNA vez al crear — el ciclo no llama IA nunca), `umbral_margen_eur` (default env 1500), `umbral_margen_pct` (default 10), `expira_at` (creación + `SNIPER_MISION_DIAS`, default 30), `snapshot_sembrado` (0/1), `last_run_at`, `alertas_total`, `ultimo_error`. `estado` reutiliza la columna existente con valores `ACTIVA/PAUSADA/EXPIRADA`. Alternativa: tabla `misiones_v2` — rechazada (migración de datos en prod sin necesidad; las columnas viejas no estorban).

### D4 — Detección de "nuevo" por snapshot de IDs, no por timestamp
Tabla nueva `alertas_enviadas(id, mision_id, anuncio_id, huella, tipo, precio, margen_eur, margen_pct, url, ts)` con `UNIQUE(mision_id, anuncio_id)`. `tipo` ∈ {`snapshot`, `alerta`}:
- Primera pasada de una misión: todos los anuncios visibles se insertan con `tipo='snapshot'`, cero alertas, `snapshot_sembrado=1`.
- Pasadas siguientes: anuncio cuyo `anuncio_id` no está en la tabla = candidato. Si supera umbral → alerta + fila `tipo='alerta'` con precio/margen (dedup + dataset del "caso real del sniper" en una sola tabla). Si no supera → fila `snapshot` (visto, no repetir evaluación).
- **Re-publicaciones**: `huella = sha1(marca|modelo|año|km//500|precio//100)`. Antes de alertar se comprueba también la huella: mismo coche con ID nuevo en <30 días → se registra como `snapshot` (visto), sin alerta.
`oportunidades_enviadas` queda legacy inerte (no se lee ni escribe). Timestamps de AS24 nunca se usan como fuente de verdad. Reinicio del worker: la tabla ES el estado — idempotencia gratis.

### D5 — Valoración ES desacoplada del ciclo: tabla `valoraciones_mercado`
`valoraciones_mercado(marca, modelo, año, km_banda, mediana, n_comparables, precios_json, actualizado_at)` con `UNIQUE(marca, modelo, año, km_banda)`; `km_banda = km // 20000`. TTL `VALORACION_TTL_H` (default 12).
- Al **crear** la misión se calcula la valoración en caliente (el usuario ya espera; reutiliza `buscar_comparables_todas` + `_calcular_stats_precios` de `/analizar` — cero lógica nueva de estadística) y se persiste.
- En el **ciclo sniper**, un candidato usa la valoración cacheada. Si caducó: se refresca como máximo UNA valoración por pasada (protege el presupuesto); los candidatos sin valoración fresca se quedan en cola de evaluación (fila `pendiente` no — simplemente no se marcan como vistos y se re-evalúan en la siguiente pasada con la valoración ya refrescada).
- Score de confianza en la tarjeta: `n_comparables` ≥8 🟢, 4-7 🟡, <4 🔴 con texto explícito ("margen con 3 comparables no es margen").
- Cada refresco persiste comparables en `historico_precios` (regla innegociable #2). Los anuncios DE del listado también, con `fuente='autoscout24'` — la columna `fuente` ya existe; el dataset DE vs ES queda construyéndose solo.
Alternativa: cache en memoria del worker — rechazada (se pierde al reiniciar, y el bot también la necesita al crear misión).

### D6 — IEDMT: base = valor de mercado ES, etiquetado "estimado"
La base imponible legal del IEDMT en usados es el valor de mercado del vehículo (las tablas de Hacienda son un puerto seguro para aproximarlo). La mediana de comparables ES es un proxy mejor y más defendible que el precio de compra DE (error actual del legacy). Fórmula v1:
```
iedmt      = mediana_es × tipo(co2)        ← etiquetado SIEMPRE "estimado"
importacion = transporte + coc_gestion + homologacion_itv + tasas_dgt + iedmt
margen_neto = mediana_es − precio_de − importacion
margen_pct  = margen_neto / (precio_de + importacion)
```
Costes fijos por env con defaults 2026: `COSTE_TRANSPORTE=1000`, `COSTE_COC_GESTION=400`, `COSTE_HOMOLOGACION_ITV=300`, `COSTE_TASAS_DGT=100` (sustituyen a los hardcoded 1200/350; el desglose actual COC/DGT no existe). Nada hardcodeado en la fórmula.
- **CO₂ ausente** → heurística determinista en `config.py` (combustible × año → g/km típico) y tarjeta con "IEDMT estimado (sin CO₂ del anuncio)". CERO llamadas IA en el worker: `estimar_co2` (IA) se elimina del path del scraper para misiones.
- **Nuevo fiscal** (<6 meses o <6.000 km): bloque de aviso "⚠️ IVA español 21% se sumaría" — se muestra, no se auto-suma (depende del régimen del comprador).
- **Netto (MwSt. ausweisbar)**: flag informativo en la tarjeta — para un pro con NIF-IVA el margen real es mayor; no se calcula automáticamente.
Alternativa: tablas BOE embebidas — fase 2; el disclaimer honesto vale más que una falsa precisión sin la orden anual completa.

### D7 — Se elimina el Sniper Score; la métrica es el margen
`calcular_sniper_score`/`formato_sniper_score` y `formato_tarjeta` legacy se retiran junto con el flujo interactivo muerto. La tarjeta nueva (en `sniper_pipeline.py`) sigue el formato del vídeo: precio DE · mercado ES · importación total (desglose colapsado si IEDMT fino, agregado si estimado grueso) · margen neto € y % · confianza · botón "Ver anuncio". `html.escape()` en todo campo scrapeado; `_enviar_largo()` si aplica. Una métrica, un umbral, cero pseudo-ciencia.

### D8 — Ciclo sniper: agrupación + presupuesto + round-robin
- **Clave de scrapeo** = URL AS24 normalizada (marca, modelo, filtros ordenados). Misiones con la misma clave comparten UN scrapeo por pasada (10 misiones "BMW 320d" = 1 request-set, no 10).
- **Presupuesto por pasada**: `SNIPER_BUDGET_S` (default 150 s). Se procesan claves ordenadas por `last_run_at` ASC (la más olvidada primero); al agotar presupuesto, el resto espera la siguiente pasada. Con 30 misiones el ciclo no se alarga: se reparte.
- **Cap global**: `SNIPER_MAX_SCRAPES_HORA` (default 60). Contador persistido en `estado_fuentes`. Superado → la pasada se salta con log.
- Scrapeo de detección = SOLO fase 1 (listado, ordenado por publicación reciente `sort=age&desc=1`, 1-2 páginas). La fase 2 (detalles: CO₂, Netto, Händler, propietarios) SOLO para candidatos nuevos que pasan el pre-filtro de margen — normalmente 0-2 por pasada.
- Jitter entre requests (ya existe), backoff en errores, rotación de UA (ya existe).

### D9 — Circuit breaker persistido en `estado_fuentes`
`estado_fuentes(fuente, fallos_seguidos, pausada_hasta, scrapes_hora_json)`. Fallo de scrapeo AS24 (excepción o 0 cards con HTML inesperado) incrementa `fallos_seguidos`; a `SNIPER_CB_FALLOS` (default 3) → `pausada_hasta = now + 30 min`, log WARNING claro, el ciclo sniper duerme sin morir. Éxito resetea. En SQLite: sobrevive reinicios y es visible en `/stats_sniper`. El ciclo normal y las fuentes ES no se ven afectados. "0 resultados con HTML válido" NO cuenta como fallo (mercado vacío ≠ bloqueo).

### D10 — Freemium: coste por tier, free de un solo uso, límite por tier
- **Coste por tier** (no plano): `free` = `COSTE_SNIPER_FREE` (1) y **una sola vez de por vida**; `paid` = `COSTE_SNIPER_PAID` (5) por misión; `pro` no descuenta. El sniper free es una de las 3 acciones gratuitas: gasta 1 crédito y deja 2 para el resto.
- `COSTE_COMANDO["/sniper"] = 1` actúa solo como gate mínimo del decorator (garantiza ≥1 crédito y crea usuario/admin-bypass). El coste real y las reglas se resuelven en el handler tras confirmar los slots, con `registrar=False` y descuento manual SOLO cuando la misión queda creada y sembrada (patrón cobro-en-éxito de `/tasar`). Se elige el handler y no el decorator porque el coste depende del tier y el decorator lo fija estático en decoración.
- **Free de un solo uso**: se cuenta `eventos` con `evento='mision_creada'` del usuario (append-only, robusto ante borrado de misiones). Free con ≥1 → paywall sniper aunque le queden créditos.
- **Límite de misiones ACTIVAS por tier** en `permisos.py`: `MISIONES_MAX = {"free": 1, "paid": 3, "pro": 999}` (env-configurable). Chequeo en el flujo de creación contra BD, no en el decorator. Para free, el tope de un solo uso histórico prevalece.
- Pausar/reanudar/borrar/editar umbral: gratis. Renovar una misión expirada: gratis con confirmación explícita (re-siembra snapshot; el coste fue por crear). Las alertas nunca consumen.
- Paywall propio del sniper (mensaje distinto al genérico): herramienta de trabajo, un coche paga el pack 100 veces.
- Admins pasan sin coste ni límite (patrón existente).

### D11 — Deep links + embudo en tabla `eventos`
- `start` lee `ctx.args` (payload de `t.me/bot?start=v_sniper_alemania`). Primera captura persiste `usuarios.fuente_captacion` + `fuente_captacion_at` (first-touch, no se sobrescribe). Payload `v_sniper*` → bienvenida contextual del sniper en vez de la genérica.
- Tabla `eventos(id, user_id, evento, meta, ts)` genérica. Eventos v1: `start` (meta=payload), `mision_creada`, `alerta_enviada`, `paywall_visto`, `pago_ok`. `eventos_comando` sigue igual (no se toca).
- `/stats_sniper` (admin): misiones activas/pausadas/expiradas, alertas 24 h/7 d, estado circuit breaker, conversión por `fuente_captacion`.

### D12 — Limpieza del legacy
Al final de la implementación (no antes de que `/sniper` funcione): se eliminan `TIER_LIMITS`, `_tier_puede`, `_check_access` del flujo sniper, el ConversationHandler de `/buscar` interactivo, `/calcular` (la calculadora inversa `calcular_precio_maximo_de` se conserva en `calculator.py` como helper — sin handler), `calcular_sniper_score`, `formato_sniper_score`, `formato_tarjeta` y `buscar_y_cruzar`. `_check_access`/`ALLOWED_USER_IDS` se conserva SOLO donde ya se usa en comandos vivos (no se toca `/analizar` y compañía). Regla #6: nada de esto se borra hasta que la regresión pase.

### D13 — Feature flag de despliegue
`ENABLE_SNIPER` (env, default true en dev / arrancar en false en prod). Con false: el handler responde "en construcción" y el ciclo sniper duerme. Permite desplegar código con el vídeo aún no publicado y hacer rollback sin revertir commits.

## Risks / Trade-offs

- **[Bloqueo/ToS AutoScout24]** Scraping cada 3 min escala mal con muchas misiones → agrupación por clave (D8), cap global de scrapes/hora, jitter, circuit breaker (D9), fase 1 de 1-2 páginas. A escala real: `PROXIES` ya existe como hueco, y mobile.de como fuente alternativa está detrás de la misma interfaz. Aceptado: con cientos de misiones habrá que replantear (proxies o feed de pago).
- **[Precisión IEDMT]** Sin tablas BOE es estimación → base = mediana ES (mejor proxy legal que precio DE), etiqueta "estimado" SIEMPRE, disclaimer en tarjeta. Para pros la credibilidad es el producto: mejor honesto que falso-preciso. Fase 2: tablas BOE.
- **[Promesa de latencia]** "Te aviso en minutos" del vídeo vs ciclo 3 min + presupuesto → la promesa pública es "<10 min". No prometer menos de lo que cumple el sistema con 30 misiones.
- **[Spam de alertas]** Umbral bajo = sniper silenciado y muerto → default 1500 € Y 10 % (ambos), editable por misión. Máx `SNIPER_ALERTAS_PASADA` (default 3) alertas por misión por pasada; el resto queda visto.
- **[SQLite bot+worker concurrentes]** Ya mitigado: WAL + busy_timeout 30 s. Las escrituras nuevas del worker son pequeñas y puntuales.
- **[Valoración ES obsoleta (TTL 12 h)]** Margen calculado con mercado de ayer → aceptable: el mercado ES se mueve en semanas; la tarjeta muestra `n` comparables y fecha de valoración si >6 h.
- **[Selectores AS24 cambian]** → patrón fallo-controlado existente (log claro, lista vacía) + el circuit breaker distingue "HTML roto" de "sin resultados"; `_ciclo_health` puede sondear AS24 igual que sondea ES (tarea opcional).
- **[Playwright en pasadas de 3 min]** Coste de arrancar Chromium por pasada → `_PLAYWRIGHT_SEM` ya limita concurrencia; con agrupación, una pasada típica = 1-3 lanzamientos. Si duele: browser persistente, fase 2.

## Migration Plan

1. Migraciones BD aditivas en `init_db` (columnas `misiones`/`usuarios`, tablas `alertas_enviadas`, `valoraciones_mercado`, `eventos`, `estado_fuentes`) — seguras en prod, `CREATE IF NOT EXISTS`/`ALTER` con try/except como el patrón existente.
2. Desplegar código con `ENABLE_SNIPER=false` → regresión de lo existente en prod.
3. Activar `ENABLE_SNIPER=true` con misión propia (admin) 48 h → validar snapshot, dedup, latencia, cuenta a mano contra 2-3 anuncios reales.
4. Abrir a usuarios + publicar vídeo (el vídeo NO sale hasta que misión+alerta+cuenta estén probadas con casos reales).
5. Rollback: `ENABLE_SNIPER=false`. Las tablas nuevas quedan (inertes, sin coste).
6. Purga: `alertas_enviadas` y `eventos` entran en el ciclo de purga a 180 días junto a `historico_precios`.

## Open Questions

Resueltas por Juan (2026-07-18):
- **Coste y límites** — CONFIRMADO con matiz: `paid` 5 créditos/misión + 3 activas; `free` **una sola vez** gastando 1 crédito (los otros 2 quedan para el resto de comandos) + 1 misión activa. Ver D10.
- **Prioridad en la hoja de ruta** — CONFIRMADO: el sniper adelanta a `/alertas` (S6).
- **`/importar_alemania` (S7)** — CONFIRMADO: este cambio la sustituye; S7 queda cubierta por `/sniper`.
- **Tablas BOE IEDMT** — CONFIRMADO fase 2: v1 estimación con disclaimer.

### Mejora a futuro (no ahora — coste, 2026-07-21)

**Fuente DE de pago (Carapis / agregador similar) — DIFERIDO por presupuesto.**
Investigado como alternativa al scraping propio para eliminar de raíz el cap de
`SNIPER_MAX_SCRAPES_HORA` y el bloqueo WAF de mobile.de:
- **Carapis** (carapis.com): API unificada, cubre AS24 + mobile.de en una sola
  integración, incluye estado TÜV/HU (útil para el semáforo de riesgo). Tarifa
  plana ($99/mes ≈ 10k llamadas, $299/mes ilimitado) — el modelo de precio que
  SÍ encaja con vigilancia continua 24/7. Trial 14 días sin tarjeta.
- **Apify** (scrapers por actor): pago por resultado ($4/1000 AS24, $0.75/1000
  mobile.de). Descartado para el ciclo del sniper: con solo 20 modelos vigilados
  cada 15 min, AS24 vía Apify saldría ≈200-230€/día — la economía de pago-por-
  resultado NO encaja con polling continuo. Podría valer para lookups puntuales
  (ej. un futuro `/importar_alemania` bajo demanda), no para el worker.
- **auto-api.com**: tiene endpoint `/changes` pensado justo para polling
  periódico (encaja conceptualmente mejor que los otros dos), pero precio
  oculto y requiere contacto comercial — descartado por ahora (Juan no quiere
  gestión de contacto con proveedores todavía).

**Decisión**: no se activa nada de pago hasta que haya suficientes usuarios de
pago del sniper para justificar el coste fijo. Cuando llegue ese momento:
empezar por el trial gratuito de Carapis (sin tarjeta, cero riesgo) para
validar calidad de datos antes de comprometer presupuesto. El scraping propio
(AS24 vivo y validado; mobile.de bloqueado por WAF) sigue siendo la única
fuente en producción — las optimizaciones de agrupación por marca+modelo y
presupuesto separado worker/escaneo (ver D8 actualizado) son las que mitigan
el cap mientras tanto, sin coste adicional.
