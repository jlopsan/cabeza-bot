## Context

`/analizar <url>` es el comando estrella del bot. Cualquier fallo en el scraping = usuario molesto + crédito consumido sin resultado. Hoy los scrapers fallan de dos formas distintas:

**Wallapop**: `obtener_anuncio_wallapop` busca por keywords extraídas del slug URL y espera encontrar el item cuyo `web_slug` termina en `-{item_id_numérico}`. Si la búsqueda devuelve ≤50 resultados y el item está en posición >50, o si los keywords del slug son demasiado genéricos (ej. `seat-ibiza-123456`), el loop no lo encuentra. El fallback actual intenta los primeros 5 hash_ids con el endpoint `/api/v3/items/{hash}`, pero esos 5 son ítems aleatorios que probablemente no son el que buscamos.

**Coches.net**: El endpoint SSR de listado devuelve HTML completo, pero el de detalle de anuncio individual es una SPA que renderiza con React. En IPs de servidor Cloudflare activa un JS challenge que headless Playwright no supera porque sus fingerprints son detectables. El código ya tiene `headless=False` + xvfb como workaround, pero el anti-bot de Coches.net también detecta la ausencia de cookies de sesión previas, plugins nulos y canvas fingerprint atípico.

**Constraint**: No podemos usar proxies residenciales de pago (coste). No podemos cambiar la firma pública de las funciones (retrocompatibilidad con `main.py`).

## Goals / Non-Goals

**Goals:**
- Tasa de éxito Wallapop item individual: de ~70% actual a >97%
- Tasa de éxito Coches.net item individual: de ~55% actual a >90%
- Mejorar tasa de éxito en comparables de Coches.net (evitar Playwright cuando sea posible)
- Cero cambios en la interfaz pública (`obtener_anuncio_por_url`, `buscar_comparables_todas`)
- Reintentos transparentes para el usuario (un solo mensaje de "Extrayendo datos…")

**Non-Goals:**
- Eliminar dependencia de Playwright (es necesario para coches.net detail y para AutoScout24)
- Implementar proxies rotativos de pago
- Soportar nuevas fuentes (Milanuncios, Vibbo, etc.) — eso es semana 4+
- Mejorar scrapers DE (AutoScout24, mobile.de) — están estables

## Decisions

### D1: Wallapop — 4 estrategias en cascada

**Estrategia A** (actual): search por keywords del slug → match `web_slug.endswith(f"-{item_id}")`.  
**Estrategia B** (nueva): search con el item_id numérico como keyword directa. Wallapop indexa el ID en su buscador interno; si el anuncio existe, aparecerá en top 5.  
**Estrategia C** (nueva): endpoint REST directo `GET /api/v3/items/{item_id_numerico}` — Wallapop expone el item por su ID público sin necesidad de hash. Documentado de forma informal en diversas integraciones. Si devuelve 200 con campo `price`, usarlo directamente.  
**Estrategia D** (último recurso): httpx sobre `https://es.wallapop.com/item/{slug}` + extracción de `<script id="__NEXT_DATA__" type="application/json">` que Next.js incrusta con todos los datos del anuncio (precio, título, fotos, atributos) en un JSON.

Alternativa descartada: Playwright en Wallapop. El sitio es Next.js SSR + CSR pero el `__NEXT_DATA__` llega con el HTML inicial, no requiere JS execution. httpx es suficiente.

### D2: Wallapop — retry con backoff en `_fetch`

3 intentos. Delays: 0s, 2s, 5s (backoff lineal, no exponencial — 8s en el 3er intento sería demasiado para UX de Telegram). Solo reintenta en errores de red (ConnectionError, Timeout, 5xx). En 429 (rate limit), espera 10s antes del reintento.

Alternativa descartada: backoff exponencial. 2→4→8s da 14s de espera máxima, demasiado para UX de bot.

### D3: Wallapop — headers configurables vía .env

`WALLAPOP_APPVERSION`, `WALLAPOP_MPID`, `WALLAPOP_DEVICEID` como vars opcionales en `config.py`. Si están vacías, se usan los valores hardcoded actuales. Esto permite actualizar credenciales sin re-deploy (recarga del proceso).

### D4: Coches.net — httpx con prewarming como estrategia 1

Antes de lanzar Playwright, intentar httpx con:
- Session de httpx que primero visita `https://www.coches.net/` para adquirir cookies (Set-Cookie: adadvisor, etc.)
- UA Chrome Windows real
- Headers Accept/Accept-Language/Referer coherentes
- Seguir redirects

Si la respuesta tiene JSON-LD con precio ≥ 1000, devolver directamente. Si el HTML es < 15KB (bloqueo Cloudflare), ir a Playwright.

**Por qué esto funciona**: Cloudflare en modo "under attack" activa el JS challenge. En modo normal (la mayoría del tiempo), solo valida cookies de sesión y UA. Un httpx con cookies previas del dominio a menudo pasa.

Alternativa descartada: `cloudscraper`. Es una dependencia extra que resuelve el JS challenge de Cloudflare v2, pero Coches.net usa Cloudflare v3 + Turnstile en algunos endpoints. No garantiza éxito y añade complejidad.

### D5: Coches.net Playwright — stealth mejorado

Inyectar en `add_init_script` un bloque más completo que el `webdriver=undefined` actual:
```js
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
Object.defineProperty(navigator,'languages',{get:()=>['es-ES','es']});
window.chrome={runtime:{}};
```
Pre-warm: visitar `https://www.coches.net/` antes de navegar al anuncio (mismo contexto, mismas cookies). Esperar `networkidle` en la homepage antes de saltar al anuncio.

Alternativa descartada: `playwright-stealth` (librería). No está disponible en Python de forma estable; el port `pyppeteer-stealth` está abandonado.

### D6: Coches.net comparables — httpx primero, Playwright si falla

`buscar_comparables` actualmente va directo a Playwright (porque httpx fue "bloqueado por Cloudflare"). Pero `_buscar_httpx` ya existe y funciona en IPs no bloqueadas. Cambiar la lógica a: intentar httpx primero con timeout 15s. Si devuelve ≥ 1 resultado, usar esos. Solo si devuelve 0 (bloqueo probable), lanzar Playwright.

Ventaja: en la mayoría de ejecuciones (especialmente con IPs frescas o en horarios valle), httpx es suficiente y evita el overhead de 3-5s de Playwright.

### D7: Logging de diagnóstico por estrategia

Cada intento loguea `[Wallapop S{n}]` o `[coches.net S{n}]` con el motivo de fallo para facilitar monitoreo en producción. El log de éxito indica qué estrategia fue la que funcionó.

## Risks / Trade-offs

- **D3 Wallapop `__NEXT_DATA__`**: Wallapop puede migrar a otra arquitectura o cambiar la clave del JSON. → Mitigación: envolver en try/except; si falla, ya no hay más estrategias (devolver None con log claro).
- **D4 Coches.net httpx prewarming**: La homepage de coches.net puede devolver challenge JS si la IP está "tainted". → Mitigación: si homepage < 10KB, saltar directo a Playwright sin intentar el anuncio con httpx.
- **D5 Coches.net Playwright prewarming**: Pre-visitar la homepage añade ~2-3s al tiempo de respuesta. → Aceptable: el usuario ya espera 15-30s para el análisis completo; 3s extras no son perceptibles.
- **D2 Wallapop retry**: 3 intentos con delays → hasta ~7s extras en el peor caso. → Aceptable dado que el análisis completo tarda 20-40s.
- **cloudscraper descartado**: Si Cloudflare en Coches.net se vuelve más agresivo, httpx prewarming también fallará. Habría que reconsiderar en esa situación.

## Migration Plan

1. Desplegar scraper.py + config.py actualizados.
2. Verificar con 3 URLs reales de Wallapop y 3 de Coches.net antes de dar por bueno.
3. No hay cambios de BD ni de API Telegram — rollback = revertir scraper.py.

## Open Questions

- ¿El endpoint `GET /api/v3/items/{item_id_numerico}` de Wallapop devuelve 200 con el item o 404? → Verificar durante implementación con una URL real. Si no funciona, saltarlo.
- ¿La SPA de coches.net embebe JSON-LD en el HTML inicial (pre-render SSR) o solo post-JS? → Si es post-JS, el httpx-prewarming no funcionará para el detalle. → Plan B: buscar datos en el `<script type="application/json">` que Next.js/Nuxt suele injectar server-side.
