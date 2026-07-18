## ADDED Requirements

### Requirement: AutoScout24 — búsqueda de detección parametrizada
`ScraperAutoScout24` MUST exponer una búsqueda de detección barata para el sniper: URL construida con los filtros nativos de la misión (marca/modelo/años/km/precio/combustible/caja/potencia), ordenada por fecha de publicación descendente, limitada a 1-2 páginas, que devuelve solo los datos del listado (fase 1: id, título, precio, km, año, link, foto) sin visitar detalles. La URL normalizada de esta búsqueda MUST ser determinista (misma misión → misma URL) para que el worker agrupe scrapeos.

#### Scenario: Detección para una misión
- **WHEN** el worker pide detección para "BMW 320d 2019-2021, <25.000€, <100.000 km"
- **THEN** el scraper construye la URL con esos filtros nativos ordenada por más reciente y devuelve los anuncios del listado sin navegar a detalles

#### Scenario: Misma misión, misma URL
- **WHEN** dos misiones tienen filtros equivalentes
- **THEN** sus URLs de detección normalizadas son idénticas

### Requirement: AutoScout24 — extracción ampliada de detalles para candidatos
La fase de detalles MUST extraer, además de lo actual (CO₂, caja, combustible, carrocería, descripción): potencia, nº de propietarios (Fahrzeughalter), tipo de vendedor (Händler o particular) y si el precio es Netto (MwSt. ausweisbar). Los campos ausentes MUST quedar vacíos/0 sin romper el flujo, y el scraper MUST NOT llamar a IA para completarlos.

#### Scenario: Anuncio de Händler con precio neto
- **WHEN** el detalle expone "MwSt. ausweisbar" y vendedor profesional
- **THEN** el anuncio sale con `es_netto=True` y `vendedor='haendler'`

#### Scenario: Detalle sin CO₂
- **WHEN** la página de detalle no expone CO₂
- **THEN** el campo queda en 0 y la estimación queda en manos del consumidor (heurística determinista de la cuenta), sin llamada IA

### Requirement: AutoScout24 — robustez anti-bloqueo y fallo distinguible
El scraper MUST aplicar jitter entre requests, rotación de user-agent y backoff en errores (patrones ya existentes), y MUST distinguir en su resultado dos situaciones para el circuit breaker del worker: **fallo** (excepción, timeout, HTML sin la estructura esperada) vs **vacío legítimo** (HTML válido con 0 resultados). Ante cambio de HTML MUST degradar con log claro y resultado de fallo, nunca excepción sin capturar.

#### Scenario: AutoScout24 cambia el layout
- **WHEN** el selector de tarjetas no aparece pero la página cargó
- **THEN** el scraper devuelve señal de fallo (no lista vacía silenciosa), loguea el motivo, y el worker puede activar el breaker

#### Scenario: Búsqueda sin resultados reales
- **WHEN** la búsqueda carga bien y el mercado no tiene anuncios que cumplan filtros
- **THEN** el resultado es vacío legítimo y no dispara el breaker

### Requirement: Persistencia de anuncios DE en histórico
Cada scrapeo de detección DE MUST persistir sus anuncios en `historico_precios` con `fuente='autoscout24'` (mismos filtros de calidad: precio>0, año>1990). El dataset DE vs ES es un activo del producto.

#### Scenario: Pasada de detección con 20 anuncios
- **WHEN** la fase 1 devuelve 20 anuncios válidos
- **THEN** se persisten en `historico_precios` con su fuente DE en el mismo batch pattern que las fuentes ES
