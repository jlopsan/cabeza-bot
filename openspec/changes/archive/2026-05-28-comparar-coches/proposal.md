## Why

El comando `/comparar` ya tiene handler + pipeline funcional ([main.py:2195](../../../main.py#L2195), [comparar_pipeline.py](../../../comparar_pipeline.py)) pero NO existe spec en OpenSpec. Sin spec, futuros cambios sobre `/comparar` improvisan: la IA no sabe qué es el contrato del comando ni qué casos cubre. Esta proposal documenta y formaliza la capability `comparar-coches` como spec versionada, y aprovecha para cerrar tres huecos pendientes detectados al revisar el código.

## What Changes

- **NEW capability `comparar-coches`** — spec con los requisitos del comando `/comparar` tal como existe hoy: dos URLs, dos modelos NL, o URL+modelo mezclados; slot-filling multi-turno; pipeline paralelo A/B con stats, DGT, Tavily enrich y veredicto IA; sesión TTL 30 min.
- Añadir requisito explícito de **detección de "mismo coche en ambos lados"** con reset del lado B (ya en código, sin spec).
- Añadir requisito de **TCO 3 años determinista** (combustible + mantenimiento + pérdida) calculado en `_procesar_lado` y entregado a la IA para evitar alucinación numérica.
- Añadir requisito de **registro de uso del crédito solo si llegamos al veredicto** (el handler usa `registrar=False` y descuenta al final). Esto es decisión de producto: si el flujo falla, no cobramos.
- Añadir requisito de **timeout duro 180s** sobre el pipeline.
- Añadir requisito de **persistencia en histórico** de los comparables de ambos lados, reusando `guardar_historico_batch` (alinea con [[dataset-historico]]).

## Capabilities

### New Capabilities
- `comparar-coches`: Comando `/comparar` que enfrenta dos coches a nivel modelo (URL, modelo NL, o mezcla) y devuelve veredicto con ganador en mercado, DGT, fiabilidad y TCO.

### Modified Capabilities
_(ninguna — `/comparar` no toca los requisitos de capabilities existentes; consume `scraping-multifuente`, `dataset-historico`, `freemium-creditos` sin cambiarlos)_

## Impact

- **Código existente**: `comparar_pipeline.py` (todo el pipeline) y `main.py:2195-2307` (handler + flujo). Cambios mínimos esperados — el grueso es documentación.
- **Dependencias de specs**: consume `scraping-multifuente` (buscar_comparables_todas), `dataset-historico` (guardar_historico_batch), `freemium-creditos` (decorator + registrar_uso), `analizar-anuncio` (obtener_anuncio_por_url para absorber URLs).
- **IA**: dos endpoints en `ai.py` (`parsear_comparar_input`, `enriquecer_modelo`, `generar_veredicto_comparar`) quedan referenciados en el spec.
- **Tests manuales** post-implementación: 3 escenarios — dos URLs, dos modelos NL, URL+modelo. Verificar timeout y mismo-coche.
- **Sin breaking changes**: la capability se añade nueva, no modifica nada que ya estuviera en `openspec/specs/`.
