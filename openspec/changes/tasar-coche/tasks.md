## 1. main.py — Función de estadística de rango reutilizable

- [x] 1.1 Extraer el cálculo de estadística de `_pipeline_analisis` (main.py:797-824) a un helper `_calcular_stats_precios(precios: list[float]) -> dict|None` que devuelva `{n, mediana, media, desviacion, p25, p75}` o `None` si `len < 3`. Usar `statistics.quantiles(precios, n=4)` para p25/p75
- [x] 1.2 Refactorizar `_pipeline_analisis` para usar el helper sin cambiar su comportamiento observable (mediana, media, desviación, percentil-de-anuncio siguen igual). Verificar que `/analizar` no cambia

## 2. ai.py — Texto de recomendación de tasación

- [x] 2.1 Crear `async def generar_texto_tasacion(marca, modelo, año, km, stats: dict, con_km: bool) -> str` con system prompt en tono Juan (directo, con datos): explica valor de mercado (mediana), qué ofertar (p25) y qué pedir (p75). `_llamar_ia` ya trae timeout interno 30/60s
- [x] 2.2 Añadir fallback determinista `_texto_tasacion_fallback(stats, con_km)` que arma el texto solo con los números, sin IA. `generar_texto_tasacion` lo devuelve si la IA falla o hace timeout (nunca lanza excepción)

## 3. main.py — Comando /tasar y captura de datos

- [x] 3.1 Crear `cmd_tasar` decorado con `@requiere_acceso("/tasar", registrar=False)`: parsea la línea tras `/tasar`; si hay datos suficientes procesa directo, si no activa `ctx.user_data["esperando_datos_tasar"] = True` y envía el prompt. Limpia `esperando_datos_manuales` al entrar
- [x] 3.2 Crear `async def _capturar_datos_tasar(update, ctx)` — activo solo si `ctx.user_data.get("esperando_datos_tasar")`. Delega en `_intentar_tasar` que ignora el precio
- [x] 3.3 Validar campos críticos (marca, modelo, año). Si faltan: pedir los específicos y mantener estado. `km` opcional → `con_km=False`
- [x] 3.4 Crear `async def _pipeline_tasacion(...)`: `buscar_comparables_todas`, `guardar_historico_batch` (precio>0, año>1990), `_calcular_stats_precios`. Si `None` (<3): avisa y sale SIN cobrar. Si OK: `generar_texto_tasacion`, render con `html.escape`, `registrar_uso(user_id, 1)` si no admin
- [x] 3.5 Render: cabecera coche, mediana (valor mercado), P25 (oferta), P75 (venta), nº comparables, confianza 🟢/🟡/🔴, CTA a `/analizar`
- [x] 3.6 Limpiar `esperando_datos_tasar` antes del pipeline (en `_intentar_tasar`)

## 4. main.py — Registro de handlers y cancelar

- [x] 4.1 Registrar `CommandHandler("tasar", cmd_tasar)` en `main()`
- [x] 4.2 Registrar `MessageHandler(..., _capturar_datos_tasar)` en group=2 (independiente del manual en group=1; cada grupo corre 1 handler, cada uno actúa solo con su clave). Limpieza cruzada de estados en `cmd_tasar`, `cmd_analizar` sin-URL y `callback_manual`
- [x] 4.3 Añadir `ctx.user_data.pop("esperando_datos_tasar", None)` en `cancelar`

## 5. Actualizar /ayuda y /start

- [x] 5.1 Añadir `/tasar` al menú de `/start` ("/tasar — Cuánto vale un coche en el mercado"). No hay handler `/ayuda` separado; `/start` hace de ayuda

## 6. Pruebas manuales con casos reales

- [x] 6.1 Test (live): `golf 2018 120000km` → tras fix: valor 13.890€, oferta 12.779€, pide 15.001€ (excl 6 gama alta/outlier), conf 🟢, texto IA OK
- [x] 6.2 Test: `/tasar` sin datos → estado + prompt → captura → `_intentar_tasar` (smoke en bot vivo por el usuario: OK)
- [x] 6.3 Test (live): `golf 2018 120000km 9500€` → parsea marca/modelo/año/km, precio ignorado por el flujo
- [x] 6.4 Test (live): `bmw serie 1 2019` sin km → n=31 con `con_km=False` (km=0 desactiva filtro en scraper)
- [x] 6.5 Test (live): `Koenigsegg jesko` → 0 comparables → `_tasar_desde_precios` None → no tasa, no cobra
- [x] 6.6 Test: cobro solo en éxito + admin no paga — `registrar_uso` en rama de éxito, saltado para admin (verificado por código + test BD de descuento)
- [x] 6.7 Test regresión (live): helper da mediana/media/desviación idénticas al cálculo viejo → `/analizar` intacto
- [x] 6.8 Test: `/cancelar` limpia `esperando_datos_tasar` (smoke en bot vivo por el usuario: OK)

## 7. Fix de robustez de tasación (feedback usuario)

- [x] 7.1 Detectado en test real: P25/P75 crudos daban rango exagerado (oferta 13k / pide 24k, 1.85x) por mezclar base con GTI/R + outlier
- [x] 7.2 `_tasar_desde_precios` en main.py: recorte iterativo por ratio a la mediana `[0.55, 1.40]` (máx 3 pasadas), genérico sin parsear motor/CV
- [x] 7.3 Valor = mediana del grueso; oferta = valor·0.92; pide = valor·1.08 (banda de negociación ±8%, constantes `_TASAR_MARGEN`, `_TASAR_RATIO_LO/HI`)
- [x] 7.4 `generar_texto_tasacion` + fallback + render actualizados a claves `valor/oferta/pide/excluidos`; render avisa nº excluidos + "el acabado mueve el precio"
- [x] 7.5 Retest (live): Golf 2018 → valor 13.890€, banda 12.779–15.001€ (1.17x); edge homogéneo no recorta; specs/design/proposal reconciliados

## 8. Afinado por motor CV/combustible (feedback usuario)

- [x] 8.1 `_extraer_cv` (regex, kW→CV) y `_detectar_combustible` (keywords) en main.py, sin tocar la función LLM compartida con `/analizar`
- [x] 8.2 `_filtrar_por_motor` con cascada modo `cv`/`comb`/`pool`; `_intentar_tasar` extrae motor del texto + descripción y lo pasa al pipeline
- [x] 8.3 `_pipeline_tasacion`: filtra por motor; `cv`→min_n=1 sin recorte; `comb`/`pool`→min_n=3 con recorte. Render con nota de motor + muestra pequeña
- [x] 8.4 Prompt de `/tasar` invita a dar el motor; `generar_texto_tasacion` recibe el criterio de motor
- [x] 8.5 BUG corregido: CV con <3 matches ya NO cae al pool base (GTI daba precio de Golf base). CV es señal fuerte, muestra pequeña permitida
- [x] 8.6 Retest (live): 150cv→15.990€ · GTI 245→22.637€ (antes 12.890 ❌) · Golf R 300→24.740€ · solo diésel→15.990€ · sin motor→13.890€
