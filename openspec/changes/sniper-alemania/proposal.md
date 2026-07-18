## Why

El sniper DE→ES fue el origen del bot y es la feature con mayor disposición a pagar de todo el producto: los importadores profesionales son el primer público B2B real y hoy hay tráfico con conversión cero. El código legacy (`/buscar` v3) existe pero está oculto (handlers comentados en `main.py`) y no es fiable como herramienta de trabajo: sin snapshot inicial (una misión nueva spamea alertas), IEDMT calculado sobre el precio de compra DE (incorrecto — la base es el valor de mercado), una llamada IA de parseo por misión en CADA ciclo de 3 min, scraping de comparables ES en cada pasada, sin circuit breaker (AutoScout24 caído tumba el ciclo en bucle), sin límites por tier (usa un sistema de tiers legacy anterior al freemium), y un "Sniper Score" compuesto que duplica lo que ya dice el margen. Este cambio lo revive como `/sniper`: fiable, escalable y conectado a la arquitectura actual (freemium, worker, dataset).

## What Changes

- **Nuevo comando `/sniper`** (alias `/buscar`): crear misión de vigilancia con filtros (marca, modelo, años, km máx, precio máx; opcionales combustible, caja, potencia) vía capa NL existente + confirmación de slots. `/sniper` sin args → lista de misiones con botones pausar/reanudar/borrar/editar umbral.
- **Misiones v2**: marca/modelo parseados y persistidos al crear (cero IA en el ciclo), umbral de margen por misión, estados `activa/pausada/expirada`, expiración automática configurable (30 días), snapshot inicial sembrado sin alertas, contadores y logging por misión.
- **Alerta = cuenta de importación completa**: tarjeta con precio DE, mercado ES, coste de importación (transporte + COC/gestión + homologación/ITV + tasas DGT + IEDMT estimado) y **margen neto € y %**. Aviso de IVA si nuevo fiscal (<6 meses o <6.000 km). Flag Netto/Händler. Botón "Ver anuncio". Sin Sniper Score compuesto: la métrica es el margen neto (**se retira** de las alertas).
- **IEDMT corregido**: base = valor de mercado ES estimado (mediana de comparables), NO el precio de compra DE. Siempre etiquetado "estimado". Tramos 2026 verificados (0% ≤120 g/km · 4,75% 121-159 · 9,75% 160-199 · 14,75% ≥200). Costes fijos parametrizados por env.
- **Valoración ES cacheada**: tabla `valoraciones_mercado` por modelo+año+banda de km con TTL (default 12 h). El ciclo de 3 min NUNCA scrapea Wallapop/Coches.net; refresca la valoración solo si caducó. Cada refresco persiste en `historico_precios`.
- **Robustez AutoScout24**: búsqueda parametrizada por URL nativa ordenada por publicación reciente, jitter entre requests, backoff, **circuit breaker** (N fallos seguidos → pausa del ciclo sniper 30 min con log, sin afectar al ciclo normal), agrupación de misiones con filtros equivalentes en un solo scrapeo, cap global de requests/hora. Extracción ampliada: CO₂ g/km, potencia, Händler/particular, precio Netto/Brutto, nº propietarios.
- **Dedup fiable**: tabla `alertas_enviadas` (misión, anuncio, precio, margen, ts). Nunca dos alertas por el mismo anuncio; heurística de re-publicación (marca+modelo+año+km±500+precio). Reinicio del worker no re-alerta ni pierde snapshot: todo estado en SQLite.
- **Freemium por tier**: `paid` crea misión por 5 créditos, hasta 3 activas; `free` puede crear el sniper **una sola vez** de por vida (gasta 1 de los 3 créditos, deja 2 para el resto), 1 activa; `pro` ilimitado (dormido). Las alertas NO consumen créditos. Paywall propio del sniper ("herramienta de trabajo, un coche paga el pack 100 veces").
- **Deep links + embudo**: `/start` captura payload (`?start=v_sniper_alemania`) → columna `usuarios.fuente_captacion` + onboarding contextual. Tabla `eventos` (user_id, evento, meta, ts) para el embudo start→misión→alerta→pago. Comando admin `/stats_sniper`.
- **Se retira** el tier-gating legacy (`TIER_LIMITS`/`_tier_puede` en `main.py`) del flujo revivido: todo acceso pasa por `@requiere_acceso` + límites en BD.
- **Fase 2 (NO ahora)**: mobile.de como fuente de misiones (la interfaz `ScraperDE` ya lo permite), botón "Analizar a fondo" (requiere `/analizar` con soporte AutoScout24 — se omite el botón en v1).

## Capabilities

### New Capabilities
- `sniper-alemania`: misiones de vigilancia del mercado alemán (AutoScout24) con detección de chollo contra mercado español, cuenta de importación completa (landing price + IEDMT estimado), alertas con deduplicación y gestión de misiones desde Telegram.

### Modified Capabilities
- `worker-misiones`: el ciclo sniper pasa a misiones v2 — snapshot inicial sin alertas, round-robin con presupuesto de tiempo por pasada, circuit breaker por fuente, expiración automática, idempotencia total tras reinicio, logging por misión.
- `scraping-multifuente`: AutoScout24 gana requisitos de robustez (rate limit, backoff, circuit breaker, fallo controlado) y de extracción (CO₂, Netto/Brutto, Händler, propietarios); persistencia de anuncios DE en `historico_precios` con su fuente.
- `freemium-creditos`: coste de comando >1 crédito (misión = 5) + nuevo límite de misiones activas por tier, comprobado al crear misión.
- `telegram-bot-shell`: `/start` captura payload de deep link, persiste `fuente_captacion` y adapta el onboarding al contexto del vídeo.

## Impact

- `main.py`: nuevo flujo `/sniper` (ConversationHandler NL + gestión con botones), retirada de `TIER_LIMITS`/`_check_access` del flujo sniper, deep link en `start`, `/stats_sniper` admin. Los handlers legacy comentados de `/buscar` y `/calcular` se sustituyen (el código muerto se elimina al cerrar).
- `worker.py`: `_ciclo_sniper` reescrito (agrupación, presupuesto, circuit breaker, snapshot); `procesar_mision` sin llamadas IA.
- `scraper.py`: `ScraperAutoScout24` robustecido + campos nuevos; sin fuente nueva.
- `calculator.py`: `calcular_landing_price` con desglose nuevo y base IEDMT corregida; se retiran `calcular_sniper_score`/`formato_sniper_score` del flujo de alertas.
- `database.py`: migraciones aditivas — columnas en `misiones` y `usuarios`, tablas `alertas_enviadas`, `valoraciones_mercado`, `eventos`; purga a 180 días.
- `permisos.py`: `"/sniper": 5` en `COSTE_COMANDO` + chequeo de límite de misiones.
- `config.py`: costes fijos 2026 por env, umbral de margen default, intervalos y caps del sniper.
- `ai.py`: reutiliza parseo NL existente para slots de misión (1 llamada al crear, ninguna en ciclo).
- Sin dependencias nuevas. Regresión obligatoria: `/analizar`, `/ideal`, `/comparar`, `/tasar` y ciclo normal del worker intactos.
