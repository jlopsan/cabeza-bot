## 1. config.py — Nuevas variables de entorno

- [x] 1.1 Añadir `WALLAPOP_APPVERSION`, `WALLAPOP_MPID`, `WALLAPOP_DEVICEID` como vars opcionales en `config.py` con los valores actuales hardcoded como defaults
- [x] 1.2 Añadir `WALLAPOP_RETRY_MAX = int(os.getenv("WALLAPOP_RETRY_MAX", "3"))` en `config.py`
- [x] 1.3 Añadir `COCHES_NET_RETRY_MAX = int(os.getenv("COCHES_NET_RETRY_MAX", "2"))` en `config.py`
- [x] 1.4 Documentar las 5 nuevas vars en el bloque de variables opcionales del `CLAUDE.md`

## 2. Wallapop — Retry con backoff en `_fetch`

- [x] 2.1 Reemplazar el `try/except` simple de `ScraperWallapop._fetch` por un loop de `WALLAPOP_RETRY_MAX` intentos con delays `[0, 2, 5]` segundos
- [x] 2.2 En `_fetch`: reintentar en `httpx.ConnectError`, `httpx.ReadTimeout`, `httpx.TimeoutException` y respuestas 5xx
- [x] 2.3 En `_fetch`: si el status es 429, esperar 10s antes del reintento (independiente del delay estándar)
- [x] 2.4 Loguear cada intento fallido como `[Wallapop] Intento {n}/{max} fallido: {error}` y el éxito con el número de intento

## 3. Wallapop — Headers configurables vía .env

- [x] 3.1 En `ScraperWallapop._HEADERS`, sustituir los valores hardcoded de `x-appversion`, `mpid` y `x-deviceid` por referencias a las nuevas constantes de `config.py` (`WALLAPOP_APPVERSION`, `WALLAPOP_MPID`, `WALLAPOP_DEVICEID`)
- [x] 3.2 Verificar que los defaults siguen siendo los valores actuales y que las peticiones existentes funcionan igual

## 4. Wallapop — Estrategias en cascada en `obtener_item`

- [x] 4.1 Refactorizar `ScraperWallapop.obtener_item` para estructurar el flujo en 4 estrategias con early-return. Preservar la lógica actual de S1 (keywords del slug + match de web_slug)
- [x] 4.2 Implementar S2: si `target` es None tras S1, hacer `_fetch` con `params["keywords"] = item_id` (el ID numérico como keyword) y buscar el item en los resultados
- [x] 4.3 Implementar S3: si S2 también falla, hacer `GET https://api.wallapop.com/api/v3/items/{item_id}` y verificar si el response tiene `price > 0`. Si 200 con datos válidos, llamar a `_item_a_anuncio` con esos datos
- [x] 4.4 Implementar S4: si S3 falla (404 u otro), hacer `GET https://es.wallapop.com/item/{slug}` con httpx y extraer el JSON de `<script id="__NEXT_DATA__" type="application/json">`. Navegar la estructura del JSON para encontrar `price`, `title`, y atributos del coche
- [x] 4.5 Añadir logs `[Wallapop S{n}]` en cada transición de estrategia (intento, fallo, éxito)
- [x] 4.6 Log de ERROR al final si las 4 estrategias fallan, listando motivo de cada una
- [x] 4.7 Verificar con 3 URLs reales de Wallapop que el flujo funciona correctamente (anuncio activo, anuncio con keywords genéricas, anuncio con slug largo)

## 5. Coches.net — httpx con prewarming para anuncio individual

- [x] 5.1 En `ScraperCochesNet.obtener_anuncio`, añadir como S1 (antes del bloque Playwright actual): hacer GET a `https://www.coches.net/` con httpx para obtener cookies. Si la respuesta es < 10KB, marcar `homepage_bloqueada=True` y saltar a S2
- [x] 5.2 Si `homepage_bloqueada` es False, usar las cookies adquiridas para hacer GET del anuncio con httpx
- [x] 5.3 Reutilizar el extractor JSON-LD existente sobre el HTML de httpx. Si `ld_precio >= 1000`, construir y devolver el `Anuncio` sin abrir Playwright
- [x] 5.4 Si httpx no devuelve JSON-LD válido, continuar con S2 (Playwright) sin romper el flujo existente

## 6. Coches.net — Playwright stealth mejorado

- [x] 6.1 Reemplazar el `add_init_script` actual (solo `webdriver=undefined`) por el bloque completo: `webdriver=undefined`, `plugins=[1,2,3,4,5]`, `languages=['es-ES','es']`, `window.chrome={runtime:{}}`
- [x] 6.2 Antes de navegar al anuncio, visitar `https://www.coches.net/` en el mismo contexto para adquirir cookies. Esperar `load` con timeout 10s (no `networkidle` — demasiado lento)
- [x] 6.3 Implementar retry de Playwright: si tras la navegación el HTML es < 15KB y `ld_precio == 0`, esperar 5s y reintentar la navegación al anuncio hasta `COCHES_NET_RETRY_MAX` veces
- [x] 6.4 Añadir logs `[coches.net S1]` (httpx) y `[coches.net S2]` (Playwright) con resultado (OK / bloqueado / fallo)

## 7. Coches.net — comparables httpx primero

- [x] 7.1 En `ScraperCochesNet.buscar_comparables`, cambiar la lógica para intentar primero `_buscar_httpx`. Si devuelve `len > 0`, retornar directamente esos resultados sin adquirir el semáforo ni lanzar Playwright
- [x] 7.2 Solo si `_buscar_httpx` devuelve `[]`, adquirir `_COCHES_NET_SEM` y llamar a `_buscar_playwright` (comportamiento actual)
- [x] 7.3 Log `[coches.net comparables] httpx OK {n}` o `[coches.net comparables] httpx vacío → Playwright`

## 8. Verificación integral

- [x] 8.1 Test manual con 2 URLs reales de Wallapop (una URL normal, una con slug muy genérico o sin año en título)
- [x] 8.2 Test manual con 2 URLs reales de Coches.net (una de detalle de anuncio, una búsqueda de comparables)
- [ ] 8.3 Confirmar que `/analizar <url_wallapop>` y `/analizar <url_cochesnet>` funcionan end-to-end en el bot
- [ ] 8.4 Confirmar que `/buscar` (DE→ES) y `/ideal` siguen funcionando (no regresiones)
- [ ] 8.5 Revisar logs para confirmar que los prefijos `[Wallapop S{n}]` y `[coches.net S{n}]` aparecen correctamente
