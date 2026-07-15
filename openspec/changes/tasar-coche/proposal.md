## Why

`/analizar` juzga un anuncio concreto: necesita un coche real (URL o datos con precio) y dice si ESE precio merece la pena. Pero muchos usuarios no tienen anuncio todavía — quieren saber cuánto vale su coche antes de venderlo, o cuánto ofrecer por uno que van a ver. Hoy el bot no responde a "¿cuánto vale un Golf 2018 con 120.000 km?". `/tasar` cubre ese hueco: valoración pura del modelo contra el mercado real, sin precio de partida y sin veredicto de fraude.

## What Changes

- **Nuevo comando `/tasar`**: el usuario da marca, modelo, año y km (texto libre, sin URL, sin precio). El bot devuelve una tasación de mercado.
- **Reutiliza el motor de precios existente**: `buscar_comparables_todas` (Wallapop + Coches.net) + `calcular_estadistica` sobre los comparables. Cero scraping nuevo.
- **Reutiliza el parsing manual**: `parsear_datos_anuncio_manual` extrae marca/modelo/año/km. El precio es opcional aquí (no se necesita para tasar).
- **Salida = valor + banda de negociación**: valor de mercado (mediana del grueso, tras recortar gama alta y outliers) con oferta (−8%) y precio de venta (+8%), nº de comparables usados y score de confianza 🟢/🟡/🔴. La banda es de negociación, no el mínimo/máximo del mercado (evita rangos exagerados por mezcla de acabados).
- **Sin red flags, sin DGT, sin veredicto IA de anuncio**: es valoración, no auditoría. Una llamada IA corta y opcional para el texto de recomendación ("pide X, oferta Y").
- **Coste**: 1 crédito vía `@requiere_acceso("/tasar")`. Ya existe la entrada en `COSTE_COMANDO`.
- **Dataset histórico**: los comparables scrapeados se persisten en `historico_precios` como todo scrapeo.
- **Multi-turn**: si faltan campos críticos (marca, modelo, año), el bot los pregunta; km es opcional (si falta, tasa el modelo/año completo con menos precisión).

## Capabilities

### New Capabilities
- `tasar-coche`: valoración de un coche por modelo/año/km contra comparables reales de mercado, devolviendo rango de precio (mediana + percentiles) sin juzgar un anuncio concreto.

### Modified Capabilities
- ninguna

## Impact

- `main.py`: nuevo `cmd_tasar` decorado con `@requiere_acceso("/tasar", registrar=False)` + cobro en éxito (patrón de `/analizar`); handler de captura de datos de tasación (estado en `ctx.user_data`, separado del de `/analizar`); registro del `CommandHandler("tasar", ...)` y del handler de mensaje.
- `ai.py`: reutiliza `parsear_datos_anuncio_manual`. Opcional: `generar_texto_tasacion(marca, modelo, año, km, stats)` — 1 llamada IA corta para el consejo de precio (o texto determinista si se prefiere sin IA).
- `scraper.py`: sin cambios — usa `buscar_comparables_todas` tal cual.
- `database.py`: sin cambios — `guardar_historico_batch` ya existe; los comparables se persisten igual que en `/analizar`.
- `permisos.py`: sin cambios — `"/tasar": 1` ya está en `COSTE_COMANDO`.
- Sin nuevas dependencias.
