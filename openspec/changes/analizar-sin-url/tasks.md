## 1. ai.py — Función de parseo de datos manuales

- [x] 1.1 Crear `parsear_datos_anuncio_manual(texto: str) -> dict` en `ai.py`: llama al LLM con system prompt JSON-only para extraer `{marca, modelo, año, km, precio, descripcion}` del texto libre del usuario
- [x] 1.2 Manejar el caso de campos ausentes: la función devuelve el dict con `None` en los campos que no se pudieron extraer (no lanza excepción)
- [ ] 1.3 Test manual: probar con "Golf TDI 2018 150000km 9500€", "toyota corolla 2020 85k 12500", "un seat ibiza del 2016 con 90 mil km por 7000 pavos"

## 2. main.py — Refactor de _core_analisis

- [x] 2.1 Extraer la segunda mitad de `_core_analisis` (desde `buscar_comparables_todas` hasta el final) a nueva función `_pipeline_analisis(anuncio, source_msg, ctx, url=None)` donde `url` puede ser `None`
- [x] 2.2 `_core_analisis` llama `_pipeline_analisis` después de construir el `Anuncio` desde scraping — verificar que el flujo URL funciona exactamente igual que antes
- [x] 2.3 En `_pipeline_analisis`, adaptar la cabecera: si `url is None` mostrar "📋 Datos introducidos manualmente" en vez del enlace "Ver anuncio"

## 3. main.py — Fallback manual cuando falla scraping

- [x] 3.1 En la rama `if not anuncio or anuncio.precio <= 0` de `_core_analisis`, añadir botón inline "✏️ Introducir datos a mano" (`callback_data="manual:si"`) al mensaje de error, además del texto de error actual
- [x] 3.2 Guardar en `ctx.user_data["manual_source_msg"] = source_msg` y `ctx.user_data["esperando_datos_manuales"] = True` cuando el usuario pulse el botón
- [x] 3.3 Registrar `callback_query_handler` para `callback_data="manual:si"` que activa el estado manual y envía el mensaje pidiendo datos

## 4. main.py — Flujo /analizar sin URL

- [x] 4.1 En `cmd_analizar`, cuando `url_match` es `None` (no hay URL en el mensaje), en vez de pedir la URL: activar `ctx.user_data["esperando_datos_manuales"] = True` y responder con el mensaje de prompt de datos con ejemplo
- [x] 4.2 El crédito ya se descuenta al entrar en `cmd_analizar` por el decorator `@requiere_acceso` — verificar que este comportamiento se mantiene (el crédito se consume aunque el usuario luego no responda con datos)

## 5. main.py — Handler de captura de datos manuales

- [x] 5.1 Crear `async def _capturar_datos_manuales(update, ctx)` — se activa cuando `ctx.user_data.get("esperando_datos_manuales")` es `True`
- [x] 5.2 Llamar a `parsear_datos_anuncio_manual(texto)` con el mensaje del usuario
- [x] 5.3 Si faltan campos críticos (marca, modelo, año, km o precio): responder pidiendo los que faltan específicamente, mantener estado activo
- [x] 5.4 Si todos los campos críticos presentes: mostrar confirmación ("✅ VW Golf 2019 · 150.000 km · 9.500€ — buscando comparables…"), construir objeto `Anuncio` con `item_id` sintético, limpiar `ctx.user_data["esperando_datos_manuales"]` y llamar `_pipeline_analisis`
- [x] 5.5 Registrar el handler en `main` como `MessageHandler(filters.TEXT & ~filters.COMMAND, _capturar_datos_manuales)` con prioridad correcta (antes del handler conversacional genérico si lo hay)

## 6. Pruebas manuales de regresión

- [ ] 6.1 Test: `/analizar <url wallapop válida>` — funciona igual que antes (flujo URL sin tocar)
- [ ] 6.2 Test: `/analizar <url coches.net válida>` — funciona igual que antes
- [ ] 6.3 Test: `/analizar` sin URL → bot pide datos → usuario responde → análisis completo
- [ ] 6.4 Test: `/analizar <url que falla>` → bot ofrece botón → usuario pulsa → bot pide datos → análisis completo
- [ ] 6.5 Test: datos incompletos ("Golf 2019") → bot pide km y precio → usuario completa → análisis
- [ ] 6.6 Test: verificar que `/cancelar` limpia el estado `esperando_datos_manuales`
