# Orden de corte (regla #3 — una cosa al 100%)

Si no cabe todo, cerrar en este orden. El vídeo NO sale hasta que 1-3 estén probados con casos reales.
1. `/sniper` crea misión + worker vigila + alerta con dedup (mínimo publicable)
2. Cuenta de importación en la alerta (IEDMT estimado)
3. Comparación con mercado ES + margen neto
4. Freemium + límites por tier
5. Deep link + embudo instrumentado
6. Gestión de misiones (pausar/editar/borrar)

## 0. Preparación y decisiones abiertas

- [x] 0.1 Confirmado por Juan (2026-07-18): paid 5 créditos/misión + 3 activas; free UNA sola vez (1 crédito, 2 restantes para otros comandos) + 1 activa. IEDMT estimación con disclaimer (BOE fase 2). Sniper adelanta a S6 y cubre S7.
- [x] 0.2 Confirmado: sniper delante de `/alertas` (S6); `/sniper` cubre `/importar_alemania` (S7).
- [ ] 0.3 Verificar tramos IEDMT 2026 en `config.py` contra fuente oficial vigente (0/4,75/9,75/14,75 · cortes 120/159/199).
- [ ] 0.4 Rama de trabajo desde `main`; no tocar código vivo hasta que la regresión esté cubierta.

## 1. Config y feature flag

- [x] 1.1 `config.py`: `ENABLE_SNIPER` (env, default false en prod).
- [x] 1.2 `config.py`: costes fijos por env — AISLADOS del legacy: `SNIPER_COSTE_TRANSPORTE=1000`, `SNIPER_COSTE_COC_GESTION=400`, `SNIPER_COSTE_HOMOLOG_ITV=300`, `SNIPER_COSTE_TASAS_DGT=100`. `COSTE_TRANSPORTE`/`COSTE_GESTORIA_ITV` legacy intactos (los usa scanner.py).
- [x] 1.3 `config.py`: umbrales `SNIPER_UMBRAL_EUR=1500`, `SNIPER_UMBRAL_PCT=10`, `SNIPER_ALERTAS_PASADA=3`.
- [x] 1.4 `config.py`: ciclo — `SNIPER_INTERVAL_MINUTES`, `SNIPER_BUDGET_S=150`, `SNIPER_MAX_SCRAPES_HORA=60`, `SNIPER_CB_FALLOS=3`, `SNIPER_CB_PAUSA_MIN=30`, `SNIPER_MISION_DIAS=30`, `SNIPER_DETECCION_PAGINAS=2`.
- [x] 1.5 `config.py`: valoración — `VALORACION_TTL_H=12`, `VALORACION_KM_BANDA=20000`.
- [x] 1.6 `config.py`: heurística CO₂ determinista `CO2_TIPICO_POR_COMBUSTIBLE` + `CO2_TIPICO_DEFAULT` + nuevo-fiscal (`NUEVO_FISCAL_KM_MAX`/`_MESES_MAX`, `IVA_ES_PCT`).
- [x] 1.7 `config.py`/`permisos.py`: `COSTE_COMANDO["/sniper"] = 1` (gate mínimo), `COSTE_SNIPER_FREE=1`, `COSTE_SNIPER_PAID=5`, `MISIONES_MAX = {"free":1, "paid":3, "pro":999}` (env-configurable). Free = un solo uso de por vida (contar eventos `mision_creada`).

## 2. Migraciones BD (aditivas, sin romper prod)

- [x] 2.1 `misiones`: `ALTER ADD` `marca`, `modelo`, `umbral_margen_eur`, `umbral_margen_pct`, `expira_at`, `snapshot_sembrado`, `last_run_at`, `alertas_total`, `ultimo_error` (try/except por columna, patrón existente).
- [x] 2.2 `usuarios`: `ALTER ADD` `fuente_captacion`, `fuente_captacion_at`.
- [x] 2.3 Tabla `alertas_enviadas(id, mision_id, anuncio_id, huella, tipo, precio, margen_eur, margen_pct, url, ts)` con `UNIQUE(mision_id, anuncio_id)` + índices por `mision_id` y `huella`.
- [x] 2.4 Tabla `valoraciones_mercado(marca, modelo, año, km_banda, mediana, n_comparables, precios_json, actualizado_at)` con `UNIQUE(marca, modelo, año, km_banda)`.
- [x] 2.5 Tabla `eventos(id, user_id, evento, meta, ts)` + índice por `evento`.
- [x] 2.6 Tabla `estado_fuentes(fuente, fallos_seguidos, pausada_hasta, scrapes_hora_json)`.
- [x] 2.7 Migración de datos: `expirar_misiones_legacy()` invocada en el arranque del worker v2 (`ciclo_worker`), no en `init_db` (migración 100% aditiva).
- [x] 2.8 Funciones DB misiones v2: `crear_mision_sniper`, `obtener_mision`, `obtener_misiones_sniper_activas`, `contar_misiones_activas`, `renovar_mision`, `editar_umbral_mision`, `set_mision_run`, `marcar_snapshot_sembrado`, `incr_alertas_mision`, `expirar_misiones_vencidas` (reusa `pausar/activar/eliminar/obtener_misiones_usuario`).
- [x] 2.9 Funciones DB dedup/snapshot: `huella_anuncio`, `anuncio_ya_visto`, `huella_vista_reciente`, `registrar_visto` (snapshot y alerta), `sembrar_snapshot`.
- [x] 2.10 Funciones DB valoración: `get_valoracion`, `upsert_valoracion`, `valoracion_caducada`.
- [x] 2.11 Funciones DB eventos/fuentes: `registrar_evento_embudo`, `contar_eventos`, `get_estado_fuente`, `fuente_pausada`, `incr_fallo_fuente`, `reset_fuente`, `incr_scrape_hora`, `scrapes_ultima_hora`, `stats_sniper`.
- [x] 2.12 Funciones DB captación: `set_fuente_captacion` (first-touch, no sobrescribe).
- [x] 2.13 Incluir `alertas_enviadas` y `eventos` en `purgar_historico_antiguo` (180 días).

## 3. Cálculo de importación (calculator.py)

- [x] 3.1 `calcular_cuenta_importacion(precio_de, mediana_es, co2, combustible, año)` NUEVA (aislada, NO reescribe la legacy que usa scanner.py) → base IEDMT = `mediana_es`; desglose transporte + COC/gestión + homologación/ITV + tasas DGT + IEDMT.
- [x] 3.2 `calcular_margen_sniper(...)` → `{margen_eur, margen_pct, importacion, inversion, iedmt, tipo_iedmt_pct, co2_estimado, desglose}`.
- [x] 3.3 `estimar_co2_deterministico(combustible, año)` en calculator (sin IA) + `_normalizar_comb`.
- [x] 3.4 `es_nuevo_fiscal(km, año, meses)` + flag `co2_estimado` para la tarjeta. (Flag Netto lo aporta el scraper, grupo 4.)
- [x] 3.5 Verificado `calcular_tipo_iedmt`: tests de los 4 tramos (0/4,75/9,75/14,75) + caso CO₂=0 → estimación. Regresión legacy OK.

## 4. Scraper AutoScout24 (robustez + extracción)

- [x] 4.1 `ScraperAutoScout24.buscar_deteccion(...)` → fase 1 (listado) ordenado por reciente, N páginas (`SNIPER_DETECCION_PAGINAS`); devuelve `(anuncios, señal)` con `ok|vacio|fallo`.
- [x] 4.2 `url_deteccion_normalizada(...)` determinista (params ordenados) para agrupar por clave de scrapeo.
- [x] 4.3 `obtener_detalle_candidato(coche)` NUEVO: co2, `cv` (potencia PS), `propietarios`, `vendedor` (haendler/particular), `es_netto`. Ausentes → vacío/0. (No toca el `_fase2_detalles` legacy.)
- [x] 4.4 El path de misiones (`obtener_detalle_candidato`) NO llama a IA. `estimar_co2` queda solo en el path legacy.
- [x] 4.5 `_pagina_tiene_landmark` distingue `fallo` (HTML roto/timeout/sin landmark) de `vacio` (landmark o texto "keine Fahrzeuge" con 0 tarjetas).
- [x] 4.6 `_persistir_de_historico` persiste DE en `historico_precios` con `fuente='autoscout24'` (precio>0, año>1990) vía `Anuncio` + `guardar_historico_batch`.
- [x] 4.7 jitter en detección, rotación UA y `_PLAYWRIGHT_SEM` reusados; log claro en degradación. (Backoff explícito → lo aplica el breaker del worker, grupo 6.)

## 5. Pipeline sniper (sniper_pipeline.py — compartido bot+worker)

- [x] 5.1 `clave_scrapeo(mision)` → URL normalizada (delega en `ScraperAutoScout24.url_deteccion_normalizada`).
- [x] 5.2 `valoracion_fresca(...)` + `refrescar_valoracion(...)` → `buscar_comparables_todas` + mediana (statistics, sin importar main); upsert en `valoraciones_mercado`; persiste comparables en histórico. (Split get/refresh para el presupuesto del worker.)
- [x] 5.3 `evaluar_candidato(anuncio, valoracion)` → cuenta importación (calculator) → `{alerta, cuenta, n_comparables}` con umbral doble €/%.
- [x] 5.4 `render_tarjeta_alerta(...)` → formato del vídeo, `html.escape`, confianza 🟢/🟡/🔴, avisos IVA/Netto, `co2_estimado`, `boton_ver_anuncio`.
- [x] 5.5 `sembrar` + `filtrar_nuevos` (ID + huella, re-publicación descartada) + `marcar_visto`.
- [x] 5.6 `confianza(n)` → ≥8 🟢, 4-7 🟡, <4 🔴 + advertencia en tarjeta.

## 6. Worker (worker.py)

- [x] 6.1 `_ciclo_sniper` + `_pasada_sniper`: respeta `ENABLE_SNIPER` (dormido si false), circuit breaker (`fuente_pausada`), cap scrapes/hora, presupuesto `SNIPER_BUDGET_S`, orden por `last_run_at` ASC.
- [x] 6.2 Agrupa misiones activas por `sp.clave_scrapeo`; un `buscar_deteccion` por clave por pasada.
- [x] 6.3 Por clave: detección → por misión: siembra si no sembrado, si no `filtrar_nuevos` → pre-filtro margen → `obtener_detalle_candidato` solo candidatos → re-evaluar → alertar (máx `SNIPER_ALERTAS_PASADA`, resto visto).
- [x] 6.4 Refresco de valoración máx 1 por pasada (`refrescos`); candidatos sin valoración fresca NO se marcan vistos (se reevalúan la próxima pasada).
- [x] 6.5 `set_mision_run` (last_run_at/ultimo_error), `incr_alertas_mision`; `expirar_misiones_vencidas` al inicio de pasada.
- [x] 6.6 Circuit breaker: `fallo` → `incr_fallo_fuente`; a `SNIPER_CB_FALLOS` → pausa y corta pasada; `ok`/`vacio` → `reset_fuente`.
- [x] 6.7 `registrar_evento_embudo(user, 'alerta_enviada', ...)` por alerta.
- [x] 6.8 Eliminados `_ciclo_normal`, `procesar_mision` legacy, `_get_beneficio_coche`; `_ciclo_health` intacto; `gather` = sniper + health. `expirar_misiones_legacy()` al arranque (tarea 2.7).

## 7. Bot — flujo /sniper (main.py)

- [ ] 7.1 `cmd_sniper`: sin args → listado de misiones con botones; con args → flujo de creación NL.
- [ ] 7.2 Creación: `@requiere_acceso("/sniper", registrar=False)`; parseo NL (1 llamada IA), slot-filling multi-turn de marca/modelo, confirmación de slots.
- [ ] 7.3 Chequeo pre-creación (paywall específico si falla): free = un solo uso histórico (contar `mision_creada`); límite de misiones activas por tier; créditos suficientes para el coste del tier (paid necesita 5).
- [ ] 7.4 Al confirmar: valorar mercado ES en caliente, `crear_mision_sniper`, `registrar_uso(user_id, coste_por_tier)` (free 1 / paid 5), `registrar_evento_embudo(mision_creada)`, mensaje de vigilancia activa.
- [ ] 7.5 Callbacks de gestión: pausar/reanudar/borrar/editar-umbral/renovar (gratis); confirmación en borrar y renovar.
- [ ] 7.6 Registrar `CommandHandler("sniper")` + alias `CommandHandler("buscar")` (mismo entry) + `CallbackQueryHandler` de gestión.
- [ ] 7.7 Paywall específico del sniper en `permisos.py`/main con evento `paywall_visto` meta=sniper.

## 8. Deep links y embudo (main.py)

- [ ] 8.1 `start`: leer `ctx.args`; `set_fuente_captacion` first-touch; `registrar_evento_embudo(start, payload)`.
- [ ] 8.2 Bienvenida contextual si payload `v_sniper*`.
- [ ] 8.3 `cmd_stats_sniper` (solo admin): misiones por estado, alertas 24h/7d, estado breaker, conversión por `fuente_captacion`.

## 9. Limpieza del legacy (solo tras regresión verde)

- [ ] 9.1 `main.py`: eliminar `TIER_LIMITS`, `_tier_puede`, ConversationHandler `/buscar` interactivo, `/calcular`, y sus handlers comentados.
- [ ] 9.2 `calculator.py`: eliminar `calcular_sniper_score`, `formato_sniper_score`, `formato_tarjeta`; conservar `calcular_precio_maximo_de` como helper (sin handler).
- [ ] 9.3 `scraper.py`: eliminar `buscar_y_cruzar`, `buscar_coches_alemania` si ya no los usa nadie (verificar imports en worker y main).
- [ ] 9.4 Grep de referencias muertas (`calcular_sniper_score`, `precio_objetivo_es`, `buscar_y_cruzar`) → 0 usos vivos.

## 10. Tests manuales con casos reales (criterios de aceptación)

- [ ] 10.1 Crear misión real (BMW 320d 2019-2021, <25.000€, <100.000 km): primera pasada siembra snapshot SIN alertas.
- [ ] 10.2 Anuncio nuevo genera alerta en <10 min; el mismo anuncio no vuelve a alertar; re-publicación (ID nuevo, misma huella) no alerta.
- [ ] 10.3 Reinicio del worker no re-alerta ni pierde snapshot.
- [ ] 10.4 Cuenta a mano contra 2-3 anuncios reales: IEDMT del tramo correcto según CO₂, margen coherente con mediana ES.
- [ ] 10.5 Caso sin CO₂ → tarjeta "IEDMT estimado" sin romper.
- [ ] 10.6 Caso nuevo fiscal (<6.000 km) → aviso de IVA; caso Netto → flag.
- [ ] 10.7 AutoScout24 caído (simular fallo) → circuit breaker pausa 30 min, log claro, resto del bot vivo.
- [ ] 10.8 Deep link `?start=v_sniper_alemania` → `fuente_captacion` persistida + onboarding contextual.
- [ ] 10.9 Freemium: free 1 misión → segunda bloqueada con paywall sniper; cobro 5 créditos solo al crear; alertas no descuentan.
- [ ] 10.10 Presupuesto: 30 misiones simuladas → pasada respeta `SNIPER_BUDGET_S` y reparte por `last_run_at`.
- [ ] 10.11 REGRESIÓN: `/analizar` v4, `/ideal`, `/comparar`, `/tasar` y ciclo health intactos.

## 11. Despliegue

- [ ] 11.1 Deploy con `ENABLE_SNIPER=false`; verificar regresión en prod.
- [ ] 11.2 `ENABLE_SNIPER=true` con misión admin 48h; validar snapshot/dedup/latencia/cuenta.
- [ ] 11.3 Abrir a usuarios + publicar vídeo solo tras 1-3 probados.
- [ ] 11.4 Actualizar CLAUDE.md (roadmap S7 cubierta, tono/arquitectura) y `openspec/project.md`.
