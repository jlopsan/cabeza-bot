## Context

`/analizar` ya tiene todo el motor de precios: `buscar_comparables_todas` (Wallapop + Coches.net en paralelo, dedup) y el cálculo de estadística de mercado en `_pipeline_analisis` (main.py:797-824). El change `analizar-sin-url` añadió `parsear_datos_anuncio_manual` (ai.py) que extrae marca/modelo/año/km/precio de texto libre.

`/tasar` es un subconjunto de ese flujo: mismos comparables, misma estadística, pero **sin `anuncio.precio` de partida** y **sin la mitad cara del pipeline** (veredicto IA de anuncio, red flags, DGT, preguntas/checklist). La entrada `"/tasar": 1` ya existe en `COSTE_COMANDO`.

## Goals / Non-Goals

**Goals:**
- Responder "¿cuánto vale este coche?" con datos reales de mercado.
- Rango accionable: qué pedir (P75) y qué ofertar (P25), no solo un número.
- Reutilizar el motor existente sin duplicar lógica de scraping ni de estadística.
- Persistir comparables en `historico_precios` (regla innegociable del dataset).

**Non-Goals:**
- No juzga un anuncio concreto (eso es `/analizar`).
- No red flags, no DGT, no veredicto IA largo, no fotos.
- No requiere precio de entrada del usuario.
- No toca el flujo de `/analizar` ni sus handlers de estado.

## Decisions

**D1 — Estado propio, no reutilizar `esperando_datos_manuales`.**
El flujo manual de `/analizar` usa `ctx.user_data["esperando_datos_manuales"]`. `/tasar` usa una clave distinta `ctx.user_data["esperando_datos_tasar"]`. Por qué: el handler de captura de `/analizar` construye un `Anuncio` con precio y llama `_pipeline_analisis` (análisis completo). Si compartieran clave, un `/tasar` acabaría lanzando el pipeline caro y cobrando lo mismo pero haciendo otra cosa. Separar estados mantiene ambos flujos independientes y evita colisiones multi-turn.
*Alternativa descartada*: un solo estado con un flag de "modo". Añade ramas condicionales dentro del handler compartido y acopla dos features que deben poder cambiar por separado.

**D6 — Afinado por motor (CV/combustible), cascada anti-engaño.**
El modelo solo es poco preciso: un Golf 2018 va de 12k (110cv) a 25k (GTI/R). Si el usuario da el motor, se filtra a comparables de ese motor.
- **Extracción** (`_extraer_cv`, `_detectar_combustible`): regex sobre el texto libre + la descripción parseada. CV directo o kW→CV (×1.36). Combustible por keywords (TDI/diésel, TSI/gasolina, GTI→gasolina, e-golf→eléctrico…). Sin tocar la función LLM compartida con `/analizar` (menos riesgo).
- **Matching** contra `titulo`/`motor`/`modelo` de cada comparable (Wallapop rellena CV y combustible en el título).
- **Cascada** (`_filtrar_por_motor` → modo `cv`|`comb`|`pool`):
  - `cv`: CV ±20 (ampliable ×2). **Si pides CV, jamás se cae al pool base**: mejor 1 comparable del motor correcto (GTI 22.6k) que 11 del equivocado (base 12.9k). Muestra pequeña permitida (n≥1), sin recorte, confianza baja.
  - `comb`: solo combustible, si ≥3.
  - `pool`: sin motor → recorte estadístico (D3).
Bug encontrado en test: la primera versión caía de "CV sin 3 matches" a "todo el combustible", dando a un GTI el precio de un Golf base. Corregido: CV es señal fuerte, no se diluye.
*Alternativa descartada*: preguntar el motor siempre (fricción; el usuario eligió sin fricción). Con esto es opcional pero se usa si se da.

**D2 — Precio opcional al parsear; km opcional al tasar.**
`parsear_datos_anuncio_manual` ya devuelve `None` en campos ausentes. Para `/tasar` los campos críticos son **marca, modelo, año**. `km` es deseable pero opcional: si falta, se tasa el modelo/año sin filtro de km (rango más ancho, confianza menor). `precio` se ignora aunque el usuario lo dé.
*Por qué*: pedir km obligatorio rompe el caso "¿cuánto vale un Golf de 2018?" que es válido y común.

**D3 — Valor robusto + banda de negociación, NO P25/P75 crudos.**
Primera versión usaba P25/P50/P75 de todos los comparables. Falló en pruebas reales: un "Golf 2018" mezcla base (110cv, ~12-16k) con GTI/R (245-310cv, ~22-25k) y algún outlier (35k). El P25/P75 crudo daba oferta 13k / pide 24k (ratio 1.85x) — no es una banda de negociación, es la mezcla de coches distintos.

Corrección (`_tasar_desde_precios`):
- **Recorte iterativo por ratio a la mediana**: se mantienen los precios en `[med·0.55, med·1.40]`, se recalcula la mediana y se repite (máx 3 iteraciones) hasta estabilizar. Genérico: quita gama alta y outliers sin parsear motor/CV del texto, así vale para cualquier modelo (Golf, BMW, etc.) sin listas de acabados frágiles.
- **Valor de mercado** = mediana del conjunto recortado.
- **Oferta** = valor · 0.92; **Pide** = valor · 1.08 (banda de negociación fija ±8%, no los extremos del mercado).
- Se informa cuántos comparables se excluyeron.

Resultado Golf 2018: valor 13.890€, oferta 12.779€, pide 15.001€ (ratio 1.17x). `statistics.median` solo, sin dependencias nuevas.
*Alternativa descartada*: detectar GTI/R por palabra clave/CV en el título. Frágil (R-Line NO es gama alta, títulos inconsistentes) y no genérico entre marcas. El recorte estadístico logra lo mismo de forma robusta.

**D4 — Umbral mínimo de comparables reutilizado.**
Igual que `/analizar`: si hay <3 comparables con precio>0, no se tasa — se avisa "modelo poco común, sin datos suficientes". Confianza 🟢/🟡/🔴 por nº de comparables (mismos cortes que el score de confianza actual).

**D5 — Texto de recomendación: 1 llamada IA corta, con fallback determinista.**
`generar_texto_tasacion` produce el consejo humanizado (tono Juan). Si la IA falla o timeout, se renderiza un texto determinista con los números. Nunca bloquea la tasación por culpa de la IA. Reusa `_llamar_ia` con `asyncio.wait_for(AI_TIMEOUT_S)` como el resto.

**D6 — Cobro en éxito, patrón `/analizar`.**
Decorator `@requiere_acceso("/tasar", registrar=False)` (comprueba crédito, no descuenta). `registrar_uso(user_id, 1)` tras render exitoso, saltando admins. Coherente con el fix de `analizar-sin-url` y evita cobrar cuando el scraping no da comparables suficientes.

## Risks / Trade-offs

- **[Modelo poco común → 0-2 comparables]** → mensaje claro "sin datos suficientes", no se cobra crédito (cobro va tras éxito). Igual que `/analizar`.
- **[km opcional ensancha el rango y baja precisión]** → confianza 🔴/🟡 lo comunica; el texto avisa "sin km, estimación amplia".
- **[Usuario espera precio exacto]** → el copy enfatiza rango, no cifra única; P25/P75 educan sobre la horquilla real del mercado.
- **[Colisión de estado si el usuario lanza `/tasar` y luego `/analizar` sin responder]** → cada comando setea su clave y limpia la del otro al entrar; `/cancelar` limpia ambas.
- **[Doble cobro]** → mitigado por `registrar=False` + un único `registrar_uso` en la rama de éxito (mismo patrón verificado en `analizar-sin-url`).

## Migration Plan

Feature aditiva. Nada que migrar. Rollback = quitar el `CommandHandler("tasar", ...)` y el handler de captura; el resto del bot no depende de ellos. `COSTE_COMANDO["/tasar"]` puede quedarse (inerte sin handler).

## Open Questions

- ¿El texto de recomendación debe mencionar `/analizar` como siguiente paso ("si ves uno concreto, pásamelo con /analizar")? — Propuesto: sí, cierre con CTA. Decisión menor, se ajusta en apply.
