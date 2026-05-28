## MODIFIED Requirements

### Requirement: Wallapop vía API JSON
El scraper de Wallapop MUST usar httpx contra los endpoints de Wallapop. Para obtener un anuncio individual por URL, MUST ejecutar 4 estrategias en cascada parando en la primera que devuelva un `Anuncio` válido con `precio > 0`:
1. Búsqueda por keywords del slug URL + match de `web_slug.endswith(f"-{item_id}")` en los resultados (estrategia original).
2. Búsqueda con el item_id numérico como keyword directa en el mismo endpoint de search.
3. `GET /api/v3/items/{item_id_numerico}` — endpoint REST público de Wallapop por ID numérico.
4. httpx sobre `https://es.wallapop.com/item/{slug}` + extracción del JSON embebido en `<script id="__NEXT_DATA__">`.

El método `_fetch` MUST reintentar automáticamente hasta `WALLAPOP_RETRY_MAX` veces (default 3) con delays de 0s, 2s, 5s en errores de red o respuestas 5xx. En 429 MUST esperar 10s antes de reintentar.

Los headers de API (`x-appversion`, `mpid`, `x-deviceid`) MUST ser configurables vía variables de entorno (`WALLAPOP_APPVERSION`, `WALLAPOP_MPID`, `WALLAPOP_DEVICEID`). Si no están definidas, usar los valores hardcoded actuales como default.

NO debe usar Playwright para Wallapop.

#### Scenario: Item encontrado por keywords de slug (S1)
- **WHEN** se llama a `obtener_anuncio_wallapop` con una URL cuyo item aparece en los primeros 50 resultados de la búsqueda por keywords del slug
- **THEN** devuelve el `Anuncio` con todos los campos correctos sin intentar estrategias posteriores

#### Scenario: Item no encontrado por S1, encontrado por ID numérico como keyword (S2)
- **WHEN** la estrategia S1 no encuentra el item (no hay web_slug que termine en el item_id) pero el item existe y Wallapop lo indexa por su ID numérico
- **THEN** la estrategia S2 lo encuentra buscando el item_id como keyword y devuelve el `Anuncio`

#### Scenario: Item no encontrado por S1/S2, recuperado por endpoint REST (S3)
- **WHEN** las estrategias S1 y S2 no encuentran el item pero `GET /api/v3/items/{item_id}` devuelve 200
- **THEN** la estrategia S3 parsea la respuesta y devuelve el `Anuncio`

#### Scenario: Item recuperado por __NEXT_DATA__ (S4)
- **WHEN** S1, S2 y S3 fallan pero la página web de Wallapop tiene `<script id="__NEXT_DATA__">` con los datos del anuncio
- **THEN** la estrategia S4 extrae el JSON embebido y devuelve el `Anuncio`

#### Scenario: Todas las estrategias fallan (anuncio eliminado)
- **WHEN** el anuncio fue eliminado o desactivado y ninguna estrategia lo encuentra
- **THEN** devuelve `None` con log de nivel ERROR indicando las 4 estrategias intentadas y sus motivos de fallo

#### Scenario: Error de red transitorio
- **WHEN** la petición httpx falla con ConnectionError o Timeout
- **THEN** el retry automático reintenta hasta `WALLAPOP_RETRY_MAX` veces con delay progresivo antes de declarar fallo

#### Scenario: Rate limit (429)
- **WHEN** Wallapop devuelve 429
- **THEN** el retry espera 10 segundos antes del siguiente intento

### Requirement: Coches.net vía Playwright headed con Chrome UA
El scraper de Coches.net MUST ejecutar las siguientes estrategias en cascada para obtener un anuncio individual:

**Estrategia 1**: httpx con cookies precalentadas:
1. Visitar `https://www.coches.net/` con httpx para adquirir cookies de sesión.
2. Si la homepage devuelve < 10KB, saltar directamente a estrategia 2 (IP bloqueada).
3. Usar las cookies + UA Chrome Windows para hacer GET del anuncio.
4. Si el HTML contiene JSON-LD con `price ≥ 1000`, devolver el `Anuncio` sin abrir Playwright.

**Estrategia 2**: Playwright headed con perfil stealth mejorado:
1. Inyectar bloque de stealth completo: `webdriver=undefined`, `plugins=[1,2,3,4,5]`, `languages=['es-ES','es']`, `window.chrome={runtime:{}}`.
2. Pre-visitar `https://www.coches.net/` en el mismo contexto para adquirir cookies.
3. Navegar al anuncio y esperar renderizado de la SPA.
4. Si el HTML es < 15KB y no hay JSON-LD, reintentar hasta `COCHES_NET_RETRY_MAX` veces (default 2) con delay de 5s.

SHALL caer de forma controlada si el HTML del sitio cambia, devolviendo `None` en vez de excepción.

La búsqueda de comparables (`buscar_comparables`) MUST intentar httpx primero. Solo si httpx devuelve 0 resultados MUST intentar Playwright.

#### Scenario: Anuncio obtenido con httpx sin Playwright (S1 coches.net)
- **WHEN** la IP no está bloqueada por Cloudflare y la homepage devuelve HTML normal
- **THEN** el anuncio se extrae solo con httpx + JSON-LD, Playwright no se lanza

#### Scenario: IP bloqueada en homepage — salto directo a Playwright (S2 coches.net)
- **WHEN** la homepage de coches.net devuelve < 10KB (JS challenge activo)
- **THEN** el scraper salta la estrategia httpx y lanza Playwright directamente

#### Scenario: Playwright bloqueado en primer intento — retry (S2 retry)
- **WHEN** Playwright obtiene HTML < 15KB sin JSON-LD (anti-bot activo)
- **THEN** el scraper espera 5s y reintenta hasta `COCHES_NET_RETRY_MAX` veces antes de devolver `None`

#### Scenario: Comparables con httpx suficiente
- **WHEN** `buscar_comparables` se llama y httpx devuelve ≥ 1 resultado
- **THEN** devuelve esos resultados sin lanzar Playwright

#### Scenario: Comparables httpx vacío — fallback Playwright
- **WHEN** httpx devuelve 0 resultados (posible bloqueo)
- **THEN** `buscar_comparables` lanza Playwright con semáforo `_COCHES_NET_SEM`

#### Scenario: Selector HTML cambia
- **WHEN** Coches.net cambia el selector del precio y ningún selector CSS lo encuentra
- **THEN** el scraper captura el error, lo registra y devuelve `None` para anuncio individual o `[]` para comparables, sin excepciones no capturadas

#### Scenario: MissingX/display detectado
- **WHEN** el HTML de un item contiene marcador `MissingX/display`
- **THEN** ese item se filtra y no aparece en los resultados

## ADDED Requirements

### Requirement: Diagnóstico de estrategias por log
El sistema MUST emitir logs de nivel INFO/WARNING identificando la estrategia activa con el prefijo `[Wallapop S{n}]` o `[coches.net S{n}]`. Al terminar con éxito, MUST registrar qué estrategia funcionó. Al fallar completamente, MUST registrar todas las estrategias intentadas y el motivo de cada fallo.

#### Scenario: Éxito en estrategia no principal
- **WHEN** Wallapop necesita la estrategia S3 para encontrar el anuncio
- **THEN** el log registra `[Wallapop S3 OK]` para facilitar monitoreo de qué estrategias se usan más

#### Scenario: Fallo completo
- **WHEN** todas las estrategias fallan para una URL
- **THEN** el log de nivel ERROR incluye el resumen `[Wallapop: S1 fallo X, S2 fallo Y, S3 404, S4 no NEXT_DATA]`

### Requirement: Headers Wallapop configurables
Los valores `x-appversion`, `mpid` y `x-deviceid` que Wallapop API requiere MUST poder sobreescribirse sin modificar código, vía variables de entorno. Si las vars no están definidas, los valores hardcoded actuales son el default.

#### Scenario: Override de headers via env
- **WHEN** `WALLAPOP_APPVERSION=99999` está definida en `.env`
- **THEN** todas las peticiones a la API de Wallapop usan ese valor en el header `x-appversion`
