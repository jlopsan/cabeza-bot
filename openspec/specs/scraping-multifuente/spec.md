# scraping-multifuente

## Purpose

Define la capa de scraping que obtiene anuncios individuales y búsquedas paralelas desde Wallapop (API JSON vía httpx), Coches.net (Playwright headed con Chrome UA) y AutoScout24 (Playwright headed, herencia DE→ES). Toda salida normaliza a `Anuncio` (models.py) y persiste en `historico_precios`. Archivos: [scraper.py](../../../scraper.py), [models.py](../../../models.py), [database.py](../../../database.py).

## Requirements

### Requirement: Wallapop vía API JSON
El scraper de Wallapop MUST usar httpx contra el endpoint público de items/search de Wallapop, parsear el JSON y normalizar a `Anuncio`. NO debe usar Playwright para Wallapop.

#### Scenario: Búsqueda con filtros válidos
- **WHEN** se llama a `buscar_items` con marca/modelo/precio_max/km_max
- **THEN** el scraper construye la query, hace la petición httpx y devuelve lista de `Anuncio` normalizados

### Requirement: Coches.net vía Playwright headed con Chrome UA
El scraper de Coches.net MUST lanzar Playwright con `headless=False` y User-Agent Chrome real para que el sitio devuelva precios correctos. SHALL caer de forma controlada si el HTML del sitio cambia, devolviendo lista vacía en vez de excepción.

#### Scenario: Selector HTML cambia
- **WHEN** Coches.net cambia el selector del precio
- **THEN** el scraper captura el error, lo registra y devuelve `[]` para que el pipeline siga con solo Wallapop

#### Scenario: MissingX/display detectado
- **WHEN** el HTML de un item contiene marcador `MissingX/display`
- **THEN** ese item se filtra y no aparece en los resultados

### Requirement: Búsqueda paralela multifuente con deduplicación
La función `buscar_comparables_todas` MUST lanzar Wallapop y Coches.net en paralelo (asyncio.gather) y deduplicar resultados por URL/ID antes de devolverlos.

#### Scenario: Mismo anuncio en ambas fuentes
- **WHEN** un anuncio aparece duplicado en Wallapop y Coches.net
- **THEN** se devuelve una sola entrada (la que se vio primero)

### Requirement: Persistencia en historico_precios obligatoria
Cada llamada a búsqueda de comparables MUST persistir en `historico_precios` vía `guardar_historico_batch` los anuncios con `precio > 0` y `año > 1990`. Los demás se descartan silenciosamente.

#### Scenario: Anuncio con precio 0
- **WHEN** un resultado tiene `precio = 0`
- **THEN** no se persiste pero no rompe el batch del resto

### Requirement: URL cleaner antes de extraer ID
El scraper MUST limpiar la URL (quitar query params de tracking, fragmentos) antes de extraer el ID para que la caché y la deduplicación funcionen correctamente.

#### Scenario: URL con parámetros UTM
- **WHEN** la URL trae `?utm_source=...`
- **THEN** el cleaner devuelve la URL canónica sin esos params
