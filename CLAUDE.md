# Proyecto: Coches con cabeza — Bot de análisis de coches usados

## Instrucciones de salida
Actúa como un cavernícola. Usa oraciones cortas (3-6 palabras). Elimina rellenos, preámbulos y cortesías. Solo información esencial. Habla directo. No expliques. Pero la calidad del codigo tiene que seguir intacta.

## OpenSpec activo

Este proyecto usa **OpenSpec** (`openspec/`). Antes de implementar features nuevas:

1. Leer `openspec/project.md` y los specs relevantes en `openspec/specs/<capability>/spec.md`.
2. Usar `/opsx:propose "<idea>"` para crear proposal + design + tasks.
3. Iterar la proposal con el usuario hasta acuerdo.
4. Usar `/opsx:apply` para implementar siguiendo los tasks.
5. Usar `/opsx:archive` cuando esté en producción para fusionar deltas en specs.

Capabilities sembradas: `telegram-bot-shell`, `analizar-anuncio`, `recomendador-ideal`, `scraping-multifuente`, `freemium-creditos`, `worker-misiones`, `dataset-historico`.

Consultar specs ANTES de tocar código en zonas estables (especialmente `analizar-anuncio` y `freemium-creditos`).

## Contexto general

Bot de Telegram (+ futura web pública) que analiza anuncios de coches
usados en el mercado español (Wallapop en fase 1, más fuentes después)
y devuelve veredictos objetivos sobre si un anuncio merece la pena:
precio vs mercado comparable, banderas rojas, qué preguntar antes de
ir a verlo, fiabilidad del modelo, recomendación de alternativas.

El bot original era solo importación DE→ES (arbitraje AutoScout24 →
Wallapop). Se mantiene como FEATURE del producto nuevo, no como
producto principal.

**IMPORTANTE**: El bot es 100% Telegram. No hay interfaz web todavía.
La web (juanlopera.es) es solo la landing de captura de emails.
El webhook de Stripe también se recibe vía servidor HTTP mínimo,
no vía web de usuario.

## Producto y posicionamiento

- **Marca**: Juan Lopera · Coches con cabeza
- **Web**: juanlopera.es (solo landing, sin app)
- **Canal**: @juanlopera.es en TikTok/Instagram/YouTube
- **Target primario**: particulares comprando coche usado en España
- **Target secundario**: Juan Lopera (yo) generando contenido semanal
  a partir de features construidas — el bot aparece como herramienta
  en los vídeos, no como producto que se vende directamente
- **Diferencial vs competencia** (El Box de Autonoción, Coches.net, etc):
  ellos asesoran sobre coche NUEVO con ficha técnica estática.
  Nosotros analizamos anuncios REALES del mercado usado con scraping
  en tiempo real.

## Stack actual

- Python 3.11+
- python-telegram-bot (bot UI — única interfaz de usuario)
- playwright (scraping AutoScout24 + Coches.net headed)
- httpx (scraping Wallapop API)
- openai SDK apuntando a SambaNova (Llama 4 Maverick)
- SQLite (persistencia)
- APScheduler (worker periódico)
- stripe (pagos — pendiente de integrar)
- fastapi + uvicorn (solo para webhook de Stripe — sin UI)

## Arquitectura actual de archivos

- `main.py`: entry point + ConversationHandler de Telegram
- `scraper.py`: scraping DE (AutoScout24 + Playwright) + ES (Wallapop API + Coches.net)
- `ai.py`: parseo NL, análisis IA de anuncios, validación precios
- `calculator.py`: landing price + IEDMT + beneficio
- `database.py`: SQLite — misiones, historico_precios, usuarios, pagos
- `worker.py`: daemon que revisa misiones cada N minutos + _ciclo_health diario
- `config.py`: variables de entorno y constantes
- `dgt.py`: etiqueta DGT + ZBE determinista
- `red_flags.py`: 5 reglas deterministas de detección de fraude
- `webhook.py`: servidor FastAPI mínimo SOLO para recibir webhooks de Stripe
- `permisos.py`: decorator `@requiere_acceso(comando)` y mapa `COSTE_COMANDO`

## Hoja de ruta: 8 semanas

### Semana 0 — Identidad, landing, vídeo manifiesto ✅ HECHO
### Semana 1 — `/analizar <url>` ✅ HECHO (v4 en producción)
### Semana 2 — `/ideal` Recomendador ✅ HECHO
### Semana 3 — Sistema freemium con Stripe ✅ HECHO
### Semana 4 — `/comparar` Comparador ✅ HECHO
### Semana 5 — `/tasar` Tasar coche con precio real de mercado ✅ HECHO
### Semana 6 — `/alertas` Alertas de chollos
### Semana 7 — `/importar_alemania` (puerto del /buscar antiguo)
### Semana 8 — Web pública con endpoints del bot
### Semana 9-10 — Telegram Stars como método de pago secundario (opcional)

## Reglas innegociables del desarrollo

1. **El bot es Telegram.** No construir UI web hasta semana 8.
   El webhook.py es infraestructura de pagos, no UI de usuario.
2. **El dataset histórico se construye en cada scrapeo.**
   Cada llamada a scraper persiste en `historico_precios`.
3. Cada sesión termina con **algo funcionando al 100%**,
   nunca con tres cosas a medias.
4. **No se rompe lo existente.** /analizar v4, /buscar, worker:
   tienen que funcionar igual al final de la sesión.
5. Tests manuales con casos reales antes de dar una feature por hecha.
6. Refactor solo si es necesario. No arreglar lo que funciona.

---

## SISTEMA FREEMIUM — Plan A (implementado, Semana 3) ✅

### Modelo de negocio HOY (2 features: /analizar + /ideal)

```
Plan FREE:        3 acciones de por vida (una sola vez por usuario nuevo, SIN reset, combinadas entre todos los comandos)
PACK CHICO:      30 acciones — 4.90€  (pago único, sin caducidad, se acumulan)
PACK GRANDE:    100 acciones — 9.90€  (pago único, sin caducidad, se acumulan)
```

**No hay PRO/suscripción todavía.** El código de `pro_mes` (database.py) y la env
`STRIPE_PRICE_PRO` están dormidos a propósito para activarlos cuando toque sin
reescribir webhook ni paywall.

**Por qué estos números:**
- 3 de por vida (no 3/día): el free diario permitía uso indefinido gratis (el
  power user nunca pagaba). 3 totales fuerza la decisión de compra tras probar
  el producto. Prueba real → conversión, no free eterno.
- 30 en pack chico: cubre el ciclo completo de compra (20-40 anuncios en 2-4 semanas).
- 100 en pack grande: power user — el que va a comprar coche en serio o el que
  cubre a familia/amigos. Margen para escalar a PRO cuando exista.
- Sin caducidad: quien compra coche cada 3 años no quiere que le caduquen créditos.
- `/ideal` gratis en free: es la killer feature. Se vira, sale en vídeos, crea efecto WOW.
  Cuando haya señal de demanda premium, se sube a paid sin tocar BD ni decorator.
- Precios 4.90€ y 9.90€: psicología de precio, funcionan.

### Plan a futuro (cómo evoluciona el modelo)

```
Hoy (2 features):           FREE + 2 PACKs
Cuando haya 4-5 features:   FREE + 2 PACKs + PRO 9.90€/mes
                            (los del PACK 100 son conversión natural a PRO al mismo precio)
Cuando haya 6+ features:    Considerar tiers o features premium puntuales
```

Por qué se introduce PRO solo con 4-5 features: hoy un usuario gasta el pack 100
en 2-3 meses → PRO mensual no aporta. Con más features, el uso recurrente justifica
suscripción y el pack 100 actúa como antesala (mismo precio que PRO).

### Diseño técnico (implementado)

**BD: créditos unificados** — tabla `usuarios`:
```sql
creditos_disponibles  INTEGER DEFAULT 3   -- se descuenta con cada acción
ultimo_reset_diario   TEXT    DEFAULT ''  -- LEGACY: columna inerte, ya no se lee ni escribe
```
No hay reset. Los 3 créditos free se dan una vez al crear el usuario y no se
renuevan. `ultimo_reset_diario` se conserva por compatibilidad con BDs antiguas.

**Mapa de costes en `permisos.py`:**
```python
COSTE_COMANDO = {
    "/analizar": 1,
    "/ideal":    1,
    "/comparar": 1,   # semana 4
    "/tasar":    1,   # semana 5
    "/alertas":  1,   # semana 6 — puede subir a 5 sin tocar BD
}
```
Hoy todo cuesta 1. Para añadir un comando nuevo: una línea en este dict
+ `@requiere_acceso("/nuevo")` en el handler. Nada más.

**Flujo de créditos:**
- `free`: 3 créditos de por vida (dados al crear el usuario). Bloquea si
  `creditos_disponibles < coste`. No se renuevan nunca.
- `paid`: descuenta de `creditos_disponibles` (sin caducidad).
  Cuando llega a 0 → sigue `paid` bloqueado hasta recargar pack.
  Si recarga otro pack → se acumulan (actuales + 30 ó actuales + 100).
- `pro`: siempre pasa, no descuenta nada (dormido — para cuando se lance suscripción).

**Funciones clave en `database.py`:**
- `puede_usar(user_id, coste)` → `(bool, restantes)`
- `registrar_uso(user_id, coste)` — descuenta según tier
- `registrar_analisis(user_id)` — alias de `registrar_uso(user_id, 1)` (compatibilidad)
- `puede_analizar(user_id)` — alias de `puede_usar(user_id, 1)` (compatibilidad)
- `activar_plan(user_id, concepto, stripe_id, ...)` — idempotente via stripe_id
  - `concepto='pack_30'`  → tier='paid', acumula 30 créditos
  - `concepto='pack_100'` → tier='paid', acumula 100 créditos
  - `concepto='pro_mes'`  → tier='pro' (dormido, listo para activar)
- `desactivar_pro(user_id)` — webhook cancelación → free con 3 créditos
- `pago_ya_procesado(stripe_id)` → bool (idempotencia Stripe)

**Variables de entorno (`config.py`):**
```
STRIPE_API_KEY=sk_test_...           (sk_live_... en producción)
STRIPE_PRICE_PACK_30=price_...       (producto "Pack 30 acciones", pago único, 4.90€)
STRIPE_PRICE_PACK_100=price_...      (producto "Pack 100 acciones", pago único, 9.90€)
STRIPE_PRICE_PRO=price_...           (DORMIDO — futuro PRO mensual 9.90€)
STRIPE_WEBHOOK_SEC=whsec_...         (del stripe CLI o del dashboard)
FREE_CREDITOS=3                      (default 3, de por vida; acepta FREE_CREDITOS_DIA legacy)
PAID_CREDITOS_PACK_30=30             (default 30, configurable por env)
PAID_CREDITOS_PACK_100=100           (default 100, configurable por env)
```

### Cómo se ata el pago al usuario de Telegram

1. User pulsa botón paywall (`pagar_pack_30` ó `pagar_pack_100`) → `callback_pago`
   llama `stripe.checkout.Session.create` con
   `metadata={"telegram_user_id": "123456", "concepto": "pack_30"}`.
2. Stripe devuelve URL única → bot la manda al chat (expira en 30 min).
3. User paga en el navegador con tarjeta (test: `4242 4242 4242 4242`).
4. Stripe envía `checkout.session.completed` al webhook.
5. `webhook.py` lee `metadata.concepto`, comprueba idempotencia, llama `activar_plan()`.
6. Bot envía mensaje de confirmación al user vía `api.telegram.org/sendMessage`.

Para futuro PRO: cuando se active, `subscription_data.metadata` propagará el
`telegram_user_id` para que las renovaciones (`invoice.payment_succeeded`) también
traigan el user_id.

### Eventos Stripe suscritos (configurar en Dashboard)
- `checkout.session.completed`            (HOY: activa pack_30 o pack_100)
- `invoice.payment_succeeded`              (FUTURO: renovación PRO)
- `customer.subscription.deleted`          (FUTURO: cancelación PRO)

### Para probar sin dinero real
```bash
# Terminal 1: bot
python main.py

# Terminal 2: webhook
uvicorn webhook:app --host 127.0.0.1 --port 8080

# Terminal 3: túnel Stripe CLI (imprime el whsec_xxx)
stripe listen --forward-to localhost:8080/stripe/webhook

# Simular pago sin tocar tarjeta:
stripe trigger checkout.session.completed
```
Tarjeta de test: `4242 4242 4242 4242`, cualquier fecha futura, cualquier CVV.

### Para añadir un comando futuro con acceso premium
1. Añadir entrada en `COSTE_COMANDO` en `permisos.py`.
2. Decorar el handler: `@requiere_acceso("/nuevo_comando")`.
3. Si se quiere bloquear para free: añadir lógica en el decorator
   (comprobando tier además de créditos). Hoy no es necesario.

---

## Contexto narrativo del bot para vídeos

El bot aparece en los vídeos del canal con contexto de build-in-public.
La integración en vídeos sigue este patrón:

> "Llevo X días construyendo un bot para analizar coches usados en España.
> Esto es lo que lleva construido hasta hoy — os lo enseño funcionando en directo."

El mensaje `/start` del bot debe reflejar este tono:

```
Hola 👋

Soy el bot de Juan Lopera — Coches con cabeza.

Analizo anuncios de coches usados en España en tiempo real:
precio vs mercado, red flags, etiqueta DGT, historial del modelo.

Estoy en construcción pública. Cada semana una función nueva.

Tienes 3 acciones gratuitas para empezar.

/analizar <url> — Analiza cualquier anuncio de Wallapop o Coches.net
/ayuda — Qué puedo hacer
```

---

## Capa de inteligencia con lenguaje natural (transversal)

1. **Fallback conversacional en todos los comandos.**
2. **Respuestas humanizadas.** El bot habla como Juan, no como un CSV.
3. **Tono**: directo, con datos, sin condescender. Incrédulo ante lo absurdo.
4. **Preguntas en vez de errores.** Si falta info, pregunta.
5. **Multi-turn.** Mantiene contexto entre mensajes del mismo usuario.

---

## Arquitectura del flujo /analizar v4 (existente, NO tocar)

```
URL → extractor regex (wallapop|coches.net)
    → caché 30 min por URL
    → obtener_anuncio_por_url    [Wallapop API | Coches.net Playwright]
    → buscar_comparables_todas   [Wallapop + Coches.net — paralelo]
    → guardar_historico_batch    (precio>0, año>1990)
    → estadística (mediana, percentil, score confianza)
    → generar_veredicto_analizar
        ├ _identificar_version  (1 llamada IA corta)
        ├ investigar_coche      (4 Tavily paralelos, caché 24h)
        ├ analizar_fotos        (DESACTIVADO — ENABLE_VISION=false)
        ├ red_flags             (determinista — red_flags.py)
        ├ etiqueta DGT + ZBE    (determinista — dgt.py)
        ├ precio anómalo <40%   (bloque 🚨)
        ├ alternativa motor     (determinista)
        └ veredicto IA grande   (1 llamada con todo el contexto)
    → render HTML Telegram
        ├ html.escape() en todos los campos
        └ _enviar_largo() si >4000 chars
    → botón preguntas + checklist (1 llamada IA si user pulsa Sí)
    → registrar_uso(user_id, "/analizar")  ← AHORA lo hace el decorator
```

---

## Requisitos del entorno

Variables en `.env`:
```
TELEGRAM_TOKEN=...
SAMBANOVA_API_KEY=...
TAVILY_API_KEY=...
STRIPE_API_KEY=sk_test_...        (sk_live_... en producción)
STRIPE_PRICE_PACK_30=price_...    (Pack 30 — 4.90€)
STRIPE_PRICE_PACK_100=price_...   (Pack 100 — 9.90€)
STRIPE_PRICE_PRO=price_...        (DORMIDO — futuro PRO mensual)
STRIPE_WEBHOOK_SEC=whsec_...
```

Variables opcionales (con defaults en código):
```
AI_TIMEOUT_S=30
ANALISIS_CACHE_TTL_S=1800
HISTORICO_RETENCION_DIAS=180
ENABLE_VISION=false
ENABLE_COCHES_NET=true
FREE_CREDITOS=3
PAID_CREDITOS_PACK_30=30
PAID_CREDITOS_PACK_100=100
WALLAPOP_RETRY_MAX=3
COCHES_NET_RETRY_MAX=2
WALLAPOP_APPVERSION=817730
WALLAPOP_MPID=6568109859988379704
WALLAPOP_DEVICEID=e17cd452-9a0a-466e-a628-6328966ced0d
```

Arrancar en producción Linux:
```bash
nohup xvfb-run python main.py > bot.log 2>&1 &
nohup xvfb-run python worker.py > worker.log 2>&1 &
nohup uvicorn webhook:app --host 0.0.0.0 --port 8080 > webhook.log 2>&1 &
```

Webhook expuesto a Internet:
- Puerto 8080 abierto en firewall Hetzner.
- O nginx reverse proxy con HTTPS en `webhook.juanlopera.es`.
- En Stripe Dashboard → Webhooks → añadir endpoint con esa URL.
- Eventos a suscribir:
  - `checkout.session.completed`
  - `invoice.payment_succeeded`
  - `customer.subscription.deleted`

---

## Limitaciones conocidas

- Coches.net: scraping HTML/SPA frágil. Falla controlado si cambia el HTML.
- Vision LLM: desactivado (`ENABLE_VISION=false`). Modelo decommissioned.
- webhook.py: requiere puerto 8080 accesible externamente o nginx proxy.
- Sin portal de cliente Stripe todavía (gestión de suscripciones manual
  por email los primeros meses).
- Anti-abuso multicuenta: usuarios creando Telegrams nuevos para
  resetear los 3 free. Sin mitigación todavía. Aceptar pérdida.
- Apple Tax: NO usar Telegram Payments + Stripe in-app para iOS.
  Stripe Checkout en navegador SÍ es válido.

---

## Log de desarrollo

### 2026-04-21 — Sesión 1 (estrategia)
- Sprint 8 semanas definido.
- Identidad: Juan Lopera · Coches con cabeza.
- Landing + logos + handles + Notion.

### 2026-04-21 — Sesión 2 (/analizar v1)
- `models.py`: `Anuncio`, `EstadisticaMercado`.
- `database.py`: `historico_precios` + `guardar_historico_batch()`.
- `scraper.py`: `obtener_item()`, `buscar_items()`, `_item_a_anuncio()`.
- `ai.py`: `generar_veredicto_analizar()`.
- `main.py`: `cmd_analizar` completo.

### 2026-04-25 — Sesión 3 (/analizar v2-v3)
- `ScraperCochesNet` con Playwright headless=False + Chrome UA.
- `buscar_comparables_todas()` paralelo + deduplicación.
- Veredicto experto: `_identificar_version()` + Tavily (caché 24h).
- `dgt.py` + `red_flags.py`.
- Preguntas + checklist vía botón inline.
- Fix Chrome UA (precio correcto en Coches.net).
- Vision desactivado.
- `_ciclo_health()` diario en worker.

### 2026-04-27 — Sesión 4 (robustez v4)
- `asyncio.wait_for` + `AI_TIMEOUT_S`.
- `_enviar_largo()` para >4000 chars.
- `html.escape()` en todos los campos.
- URL cleaner, filtros defensivos scraper.
- Filtro histórico (precio>0, año>1990).
- `_limpiar_texto()` en ai.py.
- Captura `MissingX/display` en Coches.net.
- `purgar_historico_antiguo(180)`.
- try/except global en `cmd_analizar`.
- Bloque 🚨 PRECIO ANORMALMENTE BAJO.
- Bloque 💸 OPCIÓN MÁS BARATA.
- Caché 30 min por URL.
- Score confianza 🟢/🟡/🔴.

### 2026-05-04 — Sesión 5 (freemium — A IMPLEMENTAR)
- Modelo revisado: 3 análisis cada 3h (ventana deslizante) en vez de
  3 totales para siempre.
- [ ] Tablas `usuarios` + `pagos` en database.py (con stripe_customer_id
      y stripe_subscription_id).
- [ ] 5 funciones: get_o_crear_usuario, puede_usar_comando,
      registrar_uso, activar_plan, desactivar_pro, pago_ya_procesado.
- [ ] `permisos.py` nuevo con PLAN_COMANDOS y decorator
      `@requiere_acceso(comando)`.
- [ ] Variables Stripe + FREE_VENTANA_HORAS en config.py.
- [ ] Aplicar decorator a `cmd_analizar` y `cmd_ideal` en main.py.
- [ ] `callback_pago` con metadata telegram_user_id + handler registrado.
- [ ] `webhook.py` nuevo con:
      - Idempotencia (event_id en tabla pagos).
      - Notificación al user vía Telegram tras activar plan.
      - Manejo de checkout.session.completed, invoice.payment_succeeded,
        customer.subscription.deleted.
- [ ] pip install stripe fastapi uvicorn
- [ ] Crear productos en Stripe Dashboard (modo test).
- [ ] Test con Stripe CLI modo test (5 escenarios).
- [ ] Configurar webhook en Stripe Dashboard apuntando a producción.

### 2026-07-11 — Sesión /tasar (Semana 5) ✅ HECHO
- OpenSpec change `tasar-coche` (proposal + design + specs + tasks).
- `/tasar`: valoración por precio de mercado. Sin URL, sin precio.
  Salida = valor + banda de negociación (±8%), no min/max del mercado.
- Reusa `buscar_comparables_todas` + `parsear_datos_anuncio_manual`.
  Helper compartido `_calcular_stats_precios` (refactor de `/analizar`, sin regresión).
- `_tasar_desde_precios`: recorte iterativo por ratio a la mediana
  (quita gama alta/outliers). Genérico, sin parsear motor.
- Afinado por motor: `_extraer_cv` + `_detectar_combustible` + `_filtrar_por_motor`
  (cascada cv/comb/pool). Si pides CV, NUNCA cae al pool base
  (bug cazado: GTI daba precio de Golf base).
- Estado propio `esperando_datos_tasar`, handler group=2, limpieza cruzada con `/analizar`.
- Cobro en éxito (`registrar=False` + `registrar_uso` en render OK). Admin no paga.
- Fix previo detectado en /analizar manual: no descontaba crédito → corregido.
- Tests live: Golf 2018 sin motor 13.890€ · 150cv diésel 15.990€ ·
  GTI 245 22.637€ · Golf R 300 24.740€. Regresión `/analizar` OK.

### 2026-07-15 — Freemium: free de por vida (antes 3/día)
- Cambio de modelo: FREE pasa de 3 acciones/día con reset a **3 de por vida**,
  una sola vez por usuario nuevo, SIN reset. Fuerza decisión de compra tras probar.
- `database.py`: `puede_usar` free ya no regenera (borrado `_reset_diario_necesario`
  y `minutos_hasta_reset`). `registrar_uso` paid→0 se queda `paid` bloqueado
  (antes volvía a free). `ultimo_reset_diario` queda como columna legacy inerte.
- `config.py`: `FREE_CREDITOS_DIA` → `FREE_CREDITOS` (env acepta el nombre viejo
  por compat). Borrados alias legacy `FREE_ANALISIS_MAX`/`FREE_VENTANA_HORAS`.
- Copy actualizado: `/start`, `/plan`, paywall, webhook cancelación, broadcast.
- Test: nuevo=3 → gasta 3 → bloqueado → día simulado NO regenera → pack=100 OK.
- Descubierto de paso: `eventos_comando` + `/log_cmd` (group=-1) + `/stats` YA
  registran uso histórico desde mayo. No hace falta `uso_log` (era duplicado).

---

## Roadmap futuro /analizar (diferido, NO implementar ahora)

- `/comparar url1 url2` — dos anuncios enfrentados.
- Botón "Buscar otro como este" tras veredicto.
- Histórico de tendencia del modelo (30/90 días).
- Coste total a 5 años (TCO).
- Monitor de precio del anuncio.
- Verificación matrícula → DGT (fase 9+).
- Telegram Stars como pago secundario (semana 9-10): añadir botón
  "Pagar con Stars" al paywall, usar `sendInvoice(currency="XTR")`.
  user_id viene nativo en `successful_payment`, sin webhook externo.