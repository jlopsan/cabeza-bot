## Why

Cuando Wallapop o Coches.net bloquean el scraper, el usuario se queda sin nada — el bot muere en la primera fase. Pero el pipeline de análisis completo (comparables, veredicto IA, fiabilidad, red flags, DGT) funciona con cualquier `Anuncio` construido a mano: solo necesita marca, modelo, año, km y precio. Esta feature también cubre el caso de uso real donde el usuario quiere analizar un coche que vio en WhatsApp, en un concesionario, o que le ofrece un conocido — sin anuncio online.

## What Changes

- **Modo manual como fallback**: cuando `obtener_anuncio_por_url` falla o devuelve anuncio vacío, el bot ofrece botón "Introducir datos a mano" en vez de terminar con error.
- **Modo manual como feature de primera clase**: `/analizar` sin URL muestra prompt de datos en vez de pedir una URL.
- **Parsing de datos por IA**: el usuario escribe libremente ("Golf TDI 2018 150000km 9500€") y el bot extrae marca/modelo/año/km/precio vía LLM corto (igual que hace `/ideal` con los huecos).
- **Re-validación si faltan campos**: si el LLM no puede extraer marca, modelo, año, km o precio, el bot pregunta específicamente los que faltan.
- **Pipeline idéntico desde `buscar_comparables_todas`**: una vez construido el `Anuncio` manual, el análisis es exactamente igual al flujo URL — comparables, estadísticas, veredicto IA, preguntas/checklist.
- **Cabecera adaptada**: sin URL no hay enlace "Ver anuncio"; se muestra "📋 Datos introducidos manualmente" en la cabecera.
- **Coste**: 1 crédito igual que el análisis normal.

## Capabilities

### New Capabilities
- ninguna

### Modified Capabilities
- `analizar-anuncio`: nuevos escenarios — análisis sin URL y fallback manual cuando falla el scraping.

## Impact

- `main.py`: `cmd_analizar` — rama para `/analizar` sin URL; `_core_analisis` — rama de fallback cuando `anuncio == None`; nuevo handler de mensaje para capturar datos manuales (estado vía `ctx.user_data`).
- `ai.py`: nueva función `parsear_datos_anuncio_manual(texto)` — extrae `{marca, modelo, año, km, precio, descripcion}` del texto libre del usuario.
- `models.py`: sin cambios — `Anuncio` ya soporta campos opcionales vacíos.
- `permisos.py`: sin cambios — el crédito se descuenta igual con el decorator existente.
- Sin nuevas dependencias.
