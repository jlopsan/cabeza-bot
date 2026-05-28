## Context

`/comparar` está implementado en Python (`comparar_pipeline.py` + handler en `main.py`) y funciona end-to-end con tres formatos de entrada (URLs, modelos NL, mezcla). El código maduró antes de adoptar OpenSpec, por lo que el contrato de la feature vivía solo en docstrings y en la cabeza del autor.

Esta proposal NO reescribe el código. Documenta lo que ya hace para evitar regresiones futuras, formaliza los derivados deterministas (TCO) como contrato (no como detalle de implementación), y deja por escrito las decisiones controvertidas (cobrar solo al entregar veredicto, timeout duro).

Constraints:
- Pipeline IA lento (~30-90s) — necesita timeout duro y mensajes "estoy pensando" intermedios.
- Tavily 5 queries por lado + 1 LLM grande por lado → coste no trivial; no se puede repetir por reintentos automáticos.
- Sesiones en memoria, sin persistencia: si el proceso reinicia, el slot-filling en curso se pierde. Aceptable hoy.

## Goals / Non-Goals

**Goals**:
- Dejar `comparar-coches` como capability sólida documentada en OpenSpec.
- Garantizar que el cobro de créditos solo ocurre cuando el usuario recibe valor (veredicto).
- Formalizar el TCO 3 años como cálculo Python (determinista) para que la IA NO alucine números.
- Asegurar persistencia en `historico_precios` de ambos lados (alimenta el dataset propio).

**Non-Goals**:
- NO añadir soporte para 3+ coches simultáneos.
- NO añadir comparativa de versiones (motor diésel vs gasolina del MISMO modelo) en esta iteración.
- NO añadir caché del veredicto por pareja A/B (cada llamada es fresca; coste asumido).
- NO añadir botones inline de seguimiento (preguntas para el vendedor, alternativas) — eso queda para iteración futura.

## Decisions

### Decisión 1: Slot-filling multi-turno con sesión en memoria (no DB)
Las sesiones `_SESIONES: dict[int, dict]` viven solo en RAM del proceso bot, con TTL 30 min.

**Por qué**: el slot-filling rara vez dura más de 1-2 mensajes; persistir a SQLite añade complejidad sin ROI. Si el proceso cae, el usuario relanza `/comparar` y perdió ~30 segundos.

**Alternativas**: persistir slots en `usuarios.sesion_json` (descartado: el bot reinicia rara vez y la pérdida es trivial).

### Decisión 2: Cobrar 1 crédito solo si llegamos al veredicto
Decorator con `registrar=False` + `registrar_uso(user_id, 1)` manual al final del flujo exitoso.

**Por qué**: el pipeline es lento e impredecible. Si fallamos (timeout, scraping vacío, IA caída), no es justo cobrar al usuario su único crédito del día (modelo free 3/día).

**Alternativas**: cobrar al inicio y devolver crédito en error (descartado: complica idempotencia, riesgo de fugas si el crash es entre descontar y devolver).

### Decisión 3: TCO 3 años calculado en Python, NO en IA
`_procesar_lado` calcula `coste_combustible_anual`, `perdida_3a`, `valor_residual`, `tco_3a` con fórmulas explícitas usando `KM_AÑO_REF=15000` y `PRECIO_LITRO=1.55€` (España aprox 2026). Los resultados se inyectan en el prompt para que la IA solo narre.

**Por qué**: los LLMs alucinan números. El TCO debe ser auditable y reproducible.

**Alternativas**: pedir al LLM que calcule (descartado: errores aritméticos sistemáticos). Variables `PRECIO_LITRO` configurables por env (no necesario hoy, pero trivial añadir luego en `config.py`).

### Decisión 4: Timeout duro 180s con `asyncio.wait_for`
Todo `ejecutar_pipeline` cae bajo un único `wait_for(timeout=180)`.

**Por qué**: el peor caso real son ~90s (Tavily lento + Coches.net lento + LLM grande). 180s es 2× el peor caso normal; por encima de eso algo está roto y conviene cortar.

**Alternativas**: timeout por fase (Tavily 30s + scraping 60s + LLM 60s). Descartado: más complejo, no aporta precisión real porque el bottleneck varía sesión a sesión.

### Decisión 5: Detección de "mismo coche en ambos lados" tras alimentar slots, NO antes
La detección ocurre después de que el parser haya rellenado marca/modelo/generación/versión en ambos lados.

**Por qué**: si el usuario escribe "Golf vs Golf" tras una sola palabra, los slots aún no están enriquecidos. La detección se hace en el estado consolidado para evitar falsos positivos / negativos.

**Alternativas**: validación temprana por string match (descartado: "Golf 7" vs "Golf MK7" son el mismo coche y un string match ingenuo falla).

## Risks / Trade-offs

- [Pipeline tarda más que timeout en mercados con poco stock] → Aceptamos timeout y mensaje al usuario. Mitigación futura: relajar `n=40` o saltar enriquecimiento Tavily si stats ya son sólidas.
- [Sesiones en memoria se pierden al reiniciar bot] → Aceptamos. Bot rara vez reinicia. Si pasa a problema, persistir en SQLite es trivial (~30 min de trabajo).
- [Coste por llamada: 2× Tavily (5 queries) + 2× LLM enriquecimiento + 1× LLM veredicto] → ~$0.05-0.10 por `/comparar`. Cubierto por margen del pack 4.90€/9.90€. Monitorear con `logger.info` actual.
- [`PRECIO_LITRO=1.55€` hardcoded queda obsoleto] → Bajo riesgo (precio se mueve ±10% al año). Si en 12 meses crece >20%, mover a `config.py`.
- [IA usa "A"/"B" en vez de `nombre_display`] → El prompt en `generar_veredicto_comparar` debe forzar `nombre_display` verbatim. Validar manualmente en testing.

## Migration Plan

No aplica — no hay migración de datos. Las tareas son verificación + ajustes menores sobre código existente.

## Open Questions

- ¿Mover `PRECIO_LITRO` y `KM_AÑO_REF` a `config.py` ahora o cuando duelan? **Recomendación**: ahora, son una línea y dejan margen.
- ¿Cachear veredictos `(modelo_a, modelo_b)` durante 24h para evitar pagar 2× si dos usuarios comparan el mismo par? **Recomendación**: NO en esta iteración. Esperar señal real de duplicados antes de añadir complejidad.
