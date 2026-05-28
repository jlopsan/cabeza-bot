# recomendador-ideal

## Purpose

Define el comando `/ideal` que recibe necesidades en lenguaje natural (uso, presupuesto, combustible, plazas, etc.), genera candidatos de modelos viables, busca anuncios reales del mercado y devuelve un Top 3 con veredicto. Sustituye al recomendador v1 (`cmd_ideal_v1_disabled`). Archivos: [ideal_pipeline.py](../../../ideal_pipeline.py), [ideal_schema.py](../../../ideal_schema.py), [main.py](../../../main.py).

## Requirements

### Requirement: Parseo NL de necesidades del usuario
El comando `/ideal` MUST aceptar texto libre del usuario describiendo sus necesidades y SHALL extraer slots estructurados (`SlotsIdeal`) vía `alimentar_slots` usando el LLM, sin obligar a un formulario.

#### Scenario: Texto libre completo en un mensaje
- **WHEN** el usuario envía "necesito un coche para ir al trabajo, 8000€, diesel, 5 plazas"
- **THEN** `alimentar_slots` devuelve slots con uso, presupuesto, combustible y plazas rellenos

#### Scenario: Texto libre incompleto
- **WHEN** el usuario solo dice "busco un familiar barato"
- **THEN** el bot pregunta por los slots críticos pendientes (presupuesto, combustible) sin abrumar

### Requirement: Pipeline de 4 fases (brainstorm → enrichment → validation → render)
El pipeline `ejecutar_pipeline` MUST seguir el orden: `fase_brainstorm` genera candidatos de modelos con el LLM, `fase_enriquecimiento` añade contexto técnico, `fase_validacion_mercado` busca anuncios reales en Wallapop+Coches.net, `fase_render` produce el HTML del Top 3.

#### Scenario: Pipeline completo con slots válidos
- **WHEN** se ejecuta el pipeline con `SlotsIdeal` válidos
- **THEN** las 4 fases corren en orden y el resultado es una lista de candidatos con anuncios reales más HTML de veredicto

### Requirement: Filtrado por encaje con anuncio real
El validador `_anuncio_encaja` MUST comprobar marca/modelo (vía `_modelo_coincide`), familia de combustible (vía `_familia_combustible`), rango de año y km máximo según presupuesto antes de aceptar un anuncio como candidato.

#### Scenario: Anuncio con modelo correcto pero combustible incorrecto
- **WHEN** el candidato pide gasolina y el anuncio es diesel
- **THEN** `_anuncio_encaja` devuelve False y el anuncio se descarta

### Requirement: Segunda ronda si Top 3 incompleto
El pipeline SHALL ejecutar `fase_segunda_ronda` ampliando el espacio de búsqueda cuando la primera ronda no consigue 3 candidatos con anuncios reales.

#### Scenario: Primera ronda devuelve solo 1 candidato con anuncio
- **WHEN** tras la validación de mercado solo hay 1 candidato con anuncio real
- **THEN** el pipeline ejecuta `fase_segunda_ronda` con criterios más laxos

### Requirement: Callbacks de Top 3 (aceptar / mas / ninguno)
El bot SHALL ofrecer tres botones inline tras mostrar el Top 3: aceptar (analiza la opción elegida), más (segunda ronda con alternativas), ninguno (cierra el flujo y guarda feedback).

#### Scenario: Usuario pulsa "aceptar" en una opción
- **WHEN** el usuario pulsa botón aceptar de la opción N
- **THEN** el bot lanza `/analizar` sobre la URL de esa opción reutilizando el pipeline de análisis
