## Context

`/analizar` hoy es Command-only: `cmd_analizar` lee la URL del mensaje, llama `_core_analisis(url, ...)` y termina. No hay estado conversacional. Si `obtener_anuncio_por_url` devuelve `None` o precio==0, el bot responde con error y el handler muere.

El pipeline completo vive en `_core_analisis` a partir de la línea donde ya existe el objeto `Anuncio`. Toda la lógica de comparables, estadísticas, veredicto IA, DGT y red flags es agnóstica al origen del anuncio — trabaja solo con el dataclass.

El bot ya tiene patrón de estado conversacional via `ctx.user_data` (usado en `/ideal`) y ConversationHandlers registrados en `main`. No se necesita nueva infraestructura.

## Goals / Non-Goals

**Goals:**
- Cuando `/analizar` llega sin URL → bot pregunta los datos del coche en texto libre.
- Cuando scraping falla → bot ofrece fallback manual con botón inline.
- Texto libre parseado por LLM corto → `{marca, modelo, año, km, precio, descripcion}`.
- Si faltan campos críticos → bot re-pregunta solo los que faltan.
- Pipeline desde `buscar_comparables_todas` en adelante es idéntico al flujo URL.
- Coste: 1 crédito igual, descontado por el decorator existente.

**Non-Goals:**
- Portal de edición de datos antes del análisis (no hay UI de confirmación).
- Soporte para fuentes distintas de texto libre (no fotos, no formulario).
- Cambios en el sistema de créditos o tiers.
- Modificar el flujo cuando la URL sí funciona.

## Decisions

### D1: Estado vía `ctx.user_data`, no ConversationHandler nuevo

**Elegido**: flag `ctx.user_data["esperando_datos_manuales"] = True` + `MessageHandler` global que intercepta el siguiente mensaje del usuario.

**Alternativa descartada**: ConversationHandler envolviendo `cmd_analizar`. Requeriría refactorizar el registro en `main` y afectaría a los tests manuales del flujo URL, que funciona estable en producción.

**Rationale**: `ctx.user_data` ya es el patrón usado para el contexto QA del veredicto (`analisis_qa_ctx`). Es la mínima superficie de cambio.

### D2: Parsing por LLM corto (misma función que usa `/ideal`)

**Elegido**: nueva función `parsear_datos_anuncio_manual(texto: str) -> dict` en `ai.py`. Llama al LLM con un system prompt JSON-only y extrae `{marca, modelo, año, km, precio, descripcion}`. Igual a `_parse_filtros_ideal` pero orientada a anuncio único.

**Alternativa descartada**: regex determinista. Funciona para "Golf 2019 150000 9500" pero falla con "un Golf TDI de 2019 con 150k km por 9500 pavos". El LLM ya hace esto bien en `/ideal`.

**Alternativa descartada**: plantilla key:value que el usuario copia y rellena. Más fricción, peor UX. El texto libre es más natural y consistente con el tono del bot.

### D3: Fallback manual ofrecido con botón inline

**Elegido**: cuando `anuncio == None` en `_core_analisis`, editar el mensaje de error para incluir botón `InlineKeyboardButton("✏️ Introducir datos a mano", callback_data="manual:si")`. El callback activa el estado en `ctx.user_data` y pide los datos.

**Alternativa descartada**: pregunta automática sin botón. Puede confundir al usuario si el error fue transitorio y quiere reintentar la URL.

### D4: Refactorizar `_core_analisis` para aceptar `Anuncio` prebuilt

**Elegido**: extraer la segunda mitad de `_core_analisis` (desde `buscar_comparables_todas` en adelante) en función separada `_pipeline_analisis(anuncio, source_msg, ctx, url)` donde `url` puede ser `None`. `_core_analisis` construye el `Anuncio` desde scraping y llama `_pipeline_analisis`. El flujo manual construye el `Anuncio` a mano y llama directamente `_pipeline_analisis`.

**Rationale**: evita duplicar ~100 líneas de lógica. La función existente ya tiene una clara frontera natural en la línea donde termina el scraping y empieza el análisis.

### D5: Cabecera adaptada cuando no hay URL

Cuando `url is None`, la cabecera omite el enlace `<a href=...>Ver anuncio</a>` y muestra `📋 Datos introducidos manualmente` en su lugar. El resto del veredicto es idéntico.

## Risks / Trade-offs

- **Parseo LLM puede extraer datos incorrectos** → El bot muestra los datos extraídos ("¿Es esto correcto? VW Golf 2019 · 120.000 km · 8.500€") y permite reintento si el usuario responde "no". Mitiga la mayoría de errores.
- **El usuario introduce datos falsos** → Sin mitigación. El veredicto tendrá el precio y km del usuario, y los comparables del mercado real — la comparación seguirá siendo útil aunque el precio manual sea erróneo.
- **Estado `ctx.user_data` no expira** → Si el usuario activa el modo manual y luego no responde, el flag queda activo. Mitigación: el `MessageHandler` que captura los datos comprueba si el flag existe y lo limpia en cualquier caso (respuesta válida o /cancelar).
- **Refactor de `_core_analisis`** → Zona estable marcada como "NO tocar" en el spec. El refactor es mínimo (extraer segunda mitad a función nueva, sin cambiar lógica). Riesgo bajo si se hace con tests manuales antes y después.

## Open Questions

- ¿Mostrar confirmación de los datos extraídos antes de lanzar el análisis, o ir directo? (Recomendado: mostrar, como `cmd_analizar` muestra "✅ Anuncio encontrado: …" antes de buscar comparables.)
