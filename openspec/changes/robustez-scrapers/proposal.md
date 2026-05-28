## Why

Los scrapers de Wallapop y Coches.net fallan con demasiada frecuencia en producción: Wallapop porque la búsqueda por keywords del slug no siempre encuentra el item exacto, y Coches.net porque Cloudflare/anti-bot bloquea Playwright headless y los selectores CSS son frágiles. El usuario recibe `😔 No pude extraer los datos` para URLs válidas. Hay que elevar la tasa de éxito a >98% con estrategias en cascada, reintentos con backoff y extracción robusta.

## What Changes

- **Wallapop item lookup**: Añadir 3 estrategias de fallback en cascada tras el intento actual (keywords del slug). Estrategia 2: buscar con el item_id numérico como keyword. Estrategia 3: llamar al endpoint REST de detalle de Wallapop con el ID numérico. Estrategia 4 (último recurso): httpx sobre la página web de Wallapop + extrae JSON embebido en `__NEXT_DATA__` o `window.__INITIAL_STATE__`.
- **Wallapop API headers**: Las cabeceras `x-appversion`, `mpid`, `x-deviceid` envejecen. Hacerlas configurables vía `.env` con valores por defecto actualizables sin deploys.
- **Wallapop retry**: Añadir retry con backoff exponencial (3 intentos, 2s → 4s → 8s) en `_fetch`.
- **Coches.net item fetch**: Añadir estrategia 1 (nueva, antes de Playwright): httpx con `cloudscraper`-style headers + cookies precalentadas del dominio. Si el HTML tiene JSON-LD válido, devolver directamente sin abrir Playwright.
- **Coches.net Playwright stealth**: Mejorar perfil anti-detección: visitar homepage primero para adquirir cookies, inyectar propiedades de fingerprint (plugins, mimeTypes, webGL vendor), scroll simulado antes de extraer.
- **Coches.net retry**: Reintentar automáticamente con backoff si el HTML recibido es < 15 KB y no hay JSON-LD (señal de bloqueo), hasta 2 reintentos.
- **Coches.net comparables**: El path `buscar_comparables` actual va directo a Playwright. Añadir pre-intento con httpx puro (SSR funciona en muchas IPs); si devuelve > 0 results, evitar Playwright completamente.
- **Diagnóstico mejorado**: Logs específicos por estrategia para saber cuál funcionó, qué se intentó y por qué se falló.

## Capabilities

### New Capabilities
*(ninguna nueva — solo robustez de la existente)*

### Modified Capabilities
- `scraping-multifuente`: Los requirements de fiabilidad cambian. Se añaden requisitos de cascada de estrategias, retry con backoff y extracción por múltiples métodos para item individual y para comparables.

## Impact

- **scraper.py**: Modificaciones en `ScraperWallapop.obtener_item`, `ScraperWallapop._fetch`, `ScraperCochesNet.obtener_anuncio`, `ScraperCochesNet.buscar_comparables`.
- **config.py**: Nuevas vars opcionales: `WALLAPOP_APPVERSION`, `WALLAPOP_MPID`, `WALLAPOP_DEVICEID`, `WALLAPOP_RETRY_MAX`, `COCHES_NET_RETRY_MAX`.
- **requirements / pip**: Añadir `cloudscraper` (biblioteca ligera, sin deps pesadas).
- Sin cambios en `main.py`, `ai.py`, `database.py`, `models.py`.
- Retrocompatible: mismas firmas públicas, mismos retornos.
