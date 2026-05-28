# analizar-anuncio

## Purpose

Define el flujo `/analizar <url>` v4 que analiza un anuncio individual de Wallapop o Coches.net y devuelve un veredicto experto: precio vs mercado, red flags, etiqueta DGT, alternativas, preguntas para el vendedor y checklist de inspección. Estado: producción estable, NO tocar lógica sin proposal previa. Archivos: [main.py](../../../main.py), [ai.py](../../../ai.py), [scraper.py](../../../scraper.py), [red_flags.py](../../../red_flags.py), [dgt.py](../../../dgt.py).

## Requirements

### Requirement: Aceptar URLs de Wallapop y Coches.net
El comando `/analizar` SHALL aceptar URLs de los dominios `wallapop.com` y `coches.net` y MUST rechazar otros dominios con un mensaje claro al usuario.

#### Scenario: URL de Wallapop válida
- **WHEN** el usuario envía `/analizar https://es.wallapop.com/item/...`
- **THEN** el bot extrae el ID del item, llama a `obtener_anuncio_por_url` con `source="wallapop"` y continúa el pipeline

#### Scenario: URL de Coches.net válida
- **WHEN** el usuario envía `/analizar https://www.coches.net/...`
- **THEN** el bot lanza el scraping Playwright con `ScraperCochesNet` y continúa el pipeline

#### Scenario: URL no soportada
- **WHEN** el usuario envía `/analizar https://otrosite.com/...`
- **THEN** el bot responde con mensaje corto indicando que solo soporta Wallapop y Coches.net

### Requirement: Caché de 30 minutos por URL
El bot MUST cachear el veredicto por URL durante `ANALISIS_CACHE_TTL_S` segundos (default 1800 = 30 min) para evitar re-scrapear y re-llamar al LLM cuando el mismo anuncio se consulta de nuevo.

#### Scenario: Reanalizar URL dentro de 30 min
- **WHEN** un usuario envía la misma URL dos veces en menos de 30 minutos
- **THEN** la segunda llamada devuelve el veredicto cacheado sin re-scrapear ni invocar LLM, descontando crédito según `permisos.py`

### Requirement: Comparables paralelos multifuente
El bot MUST buscar comparables en Wallapop y Coches.net en paralelo vía `buscar_comparables_todas`, deduplicar por ID/URL y persistir todos los resultados con precio>0 y año>1990 en `historico_precios`.

#### Scenario: Búsqueda de comparables con éxito
- **WHEN** se ejecuta `buscar_comparables_todas` para un anuncio
- **THEN** las dos fuentes se consultan en paralelo, los resultados se deduplican y los válidos se persisten en `historico_precios` vía `guardar_historico_batch`

### Requirement: Veredicto con red flags y etiqueta DGT
El bot MUST aplicar `red_flags.detectar` (5 reglas deterministas) y `dgt.etiqueta_zbe` (determinista) antes de generar el veredicto IA, e incluir ambos en el mensaje final.

#### Scenario: Anuncio con red flag detectado
- **WHEN** un anuncio dispara una regla de `red_flags`
- **THEN** el veredicto incluye la sección de banderas rojas con la regla disparada y el bot avisa explícitamente al usuario

### Requirement: Bloque 🚨 precio anormalmente bajo
El bot MUST mostrar un bloque destacado 🚨 cuando el precio del anuncio sea menor al 40% de la mediana de comparables, indicando posible estafa.

#### Scenario: Precio sospechosamente bajo
- **WHEN** `precio_anuncio < 0.40 * mediana_comparables`
- **THEN** el veredicto antepone el bloque 🚨 PRECIO ANORMALMENTE BAJO con explicación corta

### Requirement: Score de confianza visible
El bot MUST mostrar un score de confianza 🟢/🟡/🔴 basado en número y dispersión de comparables, para que el usuario pondere el veredicto.

#### Scenario: Pocos comparables encontrados
- **WHEN** se obtienen <5 comparables válidos
- **THEN** el score es 🔴 y el veredicto avisa que la muestra es insuficiente

### Requirement: Render HTML seguro
El bot MUST aplicar `html.escape()` a todos los campos provenientes de scraping o LLM antes de inyectarlos en el mensaje HTML de Telegram para prevenir inyección de etiquetas.

#### Scenario: Anuncio con caracteres HTML en título
- **WHEN** un anuncio contiene `<` o `&` en título o descripción
- **THEN** esos caracteres aparecen escapados en el mensaje final y Telegram lo renderiza sin error

### Requirement: Botón de preguntas y checklist tras el veredicto
El bot SHALL ofrecer un botón inline "¿Quieres preguntas para el vendedor?" tras el veredicto. Al pulsarlo se genera una llamada IA adicional con las preguntas y un checklist de inspección.

#### Scenario: Usuario pulsa el botón de preguntas
- **WHEN** el usuario pulsa el botón inline tras un veredicto
- **THEN** el bot ejecuta una llamada LLM corta y envía preguntas + checklist como mensaje adicional
