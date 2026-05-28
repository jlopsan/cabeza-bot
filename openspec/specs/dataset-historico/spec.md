# dataset-historico

## Purpose

Define la tabla `historico_precios` y sus reglas de persistencia. Es el dataset propio del proyecto: cada scrapeo alimenta la tabla, y la mediana/percentil de comparables se calcula sobre ella. Sin este dataset el bot pierde su diferencial vs competencia (que solo trabaja con ficha técnica estática). Archivos: [database.py](../../../database.py).

## Requirements

### Requirement: Persistencia automática en cada búsqueda de comparables
Toda llamada a `buscar_comparables_todas` (o equivalente) MUST invocar `guardar_historico_batch` con los resultados antes de devolverlos. Ninguna ruta de scraping puede omitir esta persistencia.

#### Scenario: Búsqueda con resultados
- **WHEN** `buscar_comparables_todas` devuelve 12 anuncios válidos
- **THEN** los 12 se insertan en `historico_precios` con timestamp actual antes de devolver al caller

### Requirement: Filtros de calidad (precio>0, año>1990)
`guardar_historico_batch` MUST descartar silenciosamente cualquier anuncio con `precio <= 0` o `año <= 1990` para no contaminar el dataset con datos rotos del scraping.

#### Scenario: Batch mixto con anuncios inválidos
- **WHEN** el batch contiene 1 anuncio con `precio=0` y 1 con `año=1985`
- **THEN** esos dos se descartan y los demás se persisten

### Requirement: Retención de 180 días
La tabla `historico_precios` MUST purgarse vía `purgar_historico_antiguo(180)` ejecutado en el ciclo health diario (ver [[worker-misiones]]). Filas con `fecha < hoy - 180` se eliminan.

#### Scenario: Worker corre purga
- **WHEN** `_ciclo_health` ejecuta `purgar_historico_antiguo(180)`
- **THEN** todas las filas con fecha anterior a 180 días se borran físicamente

### Requirement: Base para mediana/percentil/score de confianza
El cálculo estadístico de `/analizar` (mediana de comparables, percentil del precio del anuncio, score 🟢/🟡/🔴) MUST consultar `historico_precios` además de los comparables nuevos para enriquecer la muestra cuando existan datos previos del mismo modelo.

#### Scenario: Modelo con histórico previo
- **WHEN** existen 30 anuncios de Toyota Yaris 2015 en `historico_precios` de las últimas semanas
- **THEN** la estadística del veredicto los incorpora junto con los comparables nuevos del scrapeo actual
