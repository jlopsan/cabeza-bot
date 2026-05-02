# Proyecto: Coches con cabeza — Bot de análisis de coches usados

## Instrucciones de salida
Actúa como un cavernícola. Usa oraciones cortas (3-6 palabras). Elimina rellenos, preámbulos y cortesías. Solo información esencial. Habla directo. No expliques. Pero la calidad del codigo tiene que seguir intacta.

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

## Hoja de ruta: 8 semanas

### Semana 0 — Identidad, landing, vídeo manifiesto ✅ HECHO
### Semana 1 — `/analizar <url>` ✅ HECHO (v4 en producción)
### Semana 2 — `/ideal` Recomendador ✅ HECHO
### Semana 3 — `/comparar` Comparador
### Semana 4 — `/tasar` Tasar coche con precio real de mercado
### Semana 5 — `/alertas` Alertas de chollos
### Semana 6 — `/importar_alemania`
### Semana 7 — Web pública con endpoints del bot
### Semana 8 — Planes de pago

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

## TAREA ACTUAL: Sistema freemium (Semana 2, prioridad máxima)

El bot tiene que estar en producción con límites de uso y opción de pago
antes del próximo vídeo. La gente que vea el vídeo debe poder probarlo
gratis un número limitado de veces y luego pagar para seguir.

### Modelo de negocio

```
Plan FREE:   3 análisis totales (no por día — para siempre)
Plan PAID:   20 análisis por 4.90€ (pago único, se suman al contador)
Plan PRO:    Ilimitado por 9.90€/mes (suscripción mensual)
```

### Tablas SQLite nuevas en `database.py`

Añadir a `init_db()` sin tocar las tablas existentes:

```sql
CREATE TABLE IF NOT EXISTS usuarios (
    user_id         INTEGER PRIMARY KEY,
    username        TEXT    DEFAULT '',
    first_name      TEXT    DEFAULT '',
    plan            TEXT    DEFAULT 'free',  -- 'free' | 'paid' | 'pro'
    analisis_free   INTEGER DEFAULT 0,       -- usados del plan free (max 3)
    analisis_mes    INTEGER DEFAULT 0,       -- usados este mes (pro)
    analisis_pack   INTEGER DEFAULT 0,       -- usados del pack paid (max 20)
    mes_actual      TEXT    DEFAULT '',      -- 'YYYY-MM' para reset mensual pro
    created_at      TEXT,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS pagos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    stripe_id   TEXT,                        -- checkout session ID de Stripe
    concepto    TEXT,                        -- 'pack_20' | 'pro_mes'
    importe     REAL,
    estado      TEXT    DEFAULT 'pendiente', -- 'pendiente' | 'completado'
    created_at  TEXT
);
```

### Funciones nuevas en `database.py`

```python
def get_o_crear_usuario(user_id: int, username: str = "",
                        first_name: str = "") -> dict:
    """Devuelve el usuario o lo crea con plan free."""

def puede_analizar(user_id: int) -> tuple[bool, int, str]:
    """
    Devuelve (puede_analizar, analisis_restantes, plan).
    Gestiona reset mensual para plan pro.
    Lógica:
      - free: puede si analisis_free < FREE_ANALISIS_MAX (3)
      - paid: puede si analisis_pack < PAID_ANALISIS_MAX (20)
      - pro:  puede siempre (reset analisis_mes cada mes nuevo)
    """

def registrar_analisis(user_id: int):
    """Incrementa el contador correcto según el plan del usuario."""

def activar_plan(user_id: int, concepto: str, stripe_id: str = ""):
    """
    Activa el plan tras pago confirmado por Stripe.
    concepto='pack_20' → plan='paid', analisis_pack=0
    concepto='pro_mes' → plan='pro', analisis_mes=0, mes_actual=hoy
    Guarda el pago en tabla pagos con estado='completado'.
    Si ya era 'paid' y compra otro pack, suma los análisis restantes.
    """
```

### Check de límite en `main.py` — cmd_analizar

Al principio de `cmd_analizar`, ANTES de cualquier scraping:

```python
user = update.effective_user
get_o_crear_usuario(user.id, user.username or "", user.first_name or "")
puede, restantes, plan = puede_analizar(user.id)

if not puede:
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("💳 20 análisis — 4.90€", callback_data="pagar_pack"),
        InlineKeyboardButton("🚀 Ilimitado — 9.90€/mes", callback_data="pagar_pro"),
    ]])
    await update.message.reply_text(
        "⚡ <b>Has agotado tus 3 análisis gratuitos.</b>\n\n"
        "El bot cruza el precio con el mercado real, investiga el "
        "historial del modelo, detecta red flags y calcula la "
        "etiqueta DGT. Todo en segundos.\n\n"
        "Para seguir analizando:\n"
        "• <b>4.90€</b> — 20 análisis (pago único)\n"
        "• <b>9.90€/mes</b> — Ilimitados\n\n"
        "Los ingresos financian el desarrollo del proyecto.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return

# Aviso cuando queda 1 análisis free
if plan == "free" and restantes == 1:
    await update.message.reply_text(
        "ℹ️ Este es tu último análisis gratuito. "
        "Después puedes continuar por 4.90€ (20 análisis).",
    )

# ... resto del código existente de cmd_analizar sin tocar ...

# AL FINAL del análisis, cuando haya terminado con éxito:
registrar_analisis(user.id)
```

### Handler de pago en `main.py`

```python
async def callback_pago(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    import stripe
    stripe.api_key = STRIPE_API_KEY

    es_pack  = query.data == "pagar_pack"
    price_id = STRIPE_PRICE_PACK if es_pack else STRIPE_PRICE_PRO
    mode     = "payment" if es_pack else "subscription"

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode=mode,
            success_url="https://juanlopera.es?pago=ok",
            cancel_url="https://juanlopera.es",
            metadata={"telegram_user_id": str(query.from_user.id)},
            locale="es",
        )
        await query.message.reply_text(
            f"🔗 <b>Completa el pago aquí:</b>\n\n{session.url}\n\n"
            "Cuando pagues el bot se activa automáticamente.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"[PAGO] Error Stripe: {e}")
        await query.message.reply_text(
            "⚠️ Error generando el enlace. "
            "Escríbeme a juanloperasanchez@gmail.com y lo resuelvo."
        )

# Registrar en main():
app.add_handler(CallbackQueryHandler(
    callback_pago, pattern=r"^pagar_(pack|pro)$"
))
```

### Variables nuevas en `config.py`

```python
# ─── STRIPE ──────────────────────────────────────────────────────────────────
STRIPE_API_KEY     = os.getenv("STRIPE_API_KEY", "")
STRIPE_PRICE_PACK  = os.getenv("STRIPE_PRICE_PACK", "")
STRIPE_PRICE_PRO   = os.getenv("STRIPE_PRICE_PRO", "")
STRIPE_WEBHOOK_SEC = os.getenv("STRIPE_WEBHOOK_SEC", "")

# ─── TIER LIMITS ─────────────────────────────────────────────────────────────
FREE_ANALISIS_MAX = int(os.getenv("FREE_ANALISIS_MAX", "3"))
PAID_ANALISIS_MAX = int(os.getenv("PAID_ANALISIS_MAX", "20"))
```

### `webhook.py` — nuevo archivo, servidor mínimo para Stripe

```python
"""
webhook.py — Servidor FastAPI mínimo para webhooks de Stripe.
NO tiene páginas de usuario. Solo recibe eventos de Stripe.

Arrancar en producción:
    uvicorn webhook:app --host 0.0.0.0 --port 8080

En local con Stripe CLI:
    stripe listen --forward-to localhost:8080/stripe/webhook
"""
from fastapi import FastAPI, Request, HTTPException
import stripe, logging
from config import STRIPE_API_KEY, STRIPE_WEBHOOK_SEC
from database import activar_plan

stripe.api_key = STRIPE_API_KEY
app    = FastAPI()
logger = logging.getLogger(__name__)

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig     = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig, STRIPE_WEBHOOK_SEC
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Firma inválida")
    except Exception as e:
        logger.error(f"[STRIPE] Error webhook: {e}")
        raise HTTPException(status_code=400)

    tipo = event["type"]
    logger.info(f"[STRIPE] Evento: {tipo}")

    # Pago único completado (pack 20 análisis)
    if tipo == "checkout.session.completed":
        session  = event["data"]["object"]
        user_id  = int(session.get("metadata", {}).get("telegram_user_id", 0))
        mode     = session.get("mode", "payment")
        concepto = "pack_20" if mode == "payment" else "pro_mes"
        if user_id:
            activar_plan(user_id, concepto, session.get("id", ""))
            logger.info(f"[STRIPE] user {user_id} → {concepto}")

    # Renovación mensual de suscripción pro
    elif tipo == "invoice.payment_succeeded":
        invoice = event["data"]["object"]
        sub_id  = invoice.get("subscription")
        if sub_id:
            try:
                sub     = stripe.Subscription.retrieve(sub_id)
                user_id = int(sub.metadata.get("telegram_user_id", 0))
                if user_id:
                    activar_plan(user_id, "pro_mes", invoice.get("id", ""))
                    logger.info(f"[STRIPE] user {user_id} renovado pro")
            except Exception as e:
                logger.error(f"[STRIPE] Error renovación: {e}")

    return {"ok": True}
```

### Orden de implementación en esta sesión

1. `database.py`: tablas `usuarios` + `pagos` + 4 funciones.
2. `config.py`: variables Stripe + TIER_LIMITS.
3. `main.py`: check de límite en `cmd_analizar` + `callback_pago` + handler.
4. `webhook.py`: archivo nuevo.
5. Instalar dependencias: `pip install stripe fastapi uvicorn`.
6. Probar flujo en local con `stripe listen --forward-to localhost:8080/stripe/webhook`.

**NO implementar en esta sesión:**
- Portal de cliente Stripe.
- UI web de gestión de cuenta.
- Códigos descuento.
- /tasar, /ideal, /km_check (van en semanas siguientes).

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

Tienes 3 análisis gratuitos para empezar.

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
    → registrar_analisis(user_id)  ← NUEVO al final si éxito
```

---

## Requisitos del entorno

Variables en `.env`:
```
TELEGRAM_TOKEN=...
SAMBANOVA_API_KEY=...
TAVILY_API_KEY=...
STRIPE_API_KEY=sk_test_...     (sk_live_... en producción)
STRIPE_PRICE_PACK=price_...
STRIPE_PRICE_PRO=price_...
STRIPE_WEBHOOK_SEC=whsec_...
```

Variables opcionales (con defaults en código):
```
AI_TIMEOUT_S=30
ANALISIS_CACHE_TTL_S=1800
HISTORICO_RETENCION_DIAS=180
ENABLE_VISION=false
ENABLE_COCHES_NET=true
FREE_ANALISIS_MAX=3
PAID_ANALISIS_MAX=20
```

Arrancar en producción Linux:
```bash
nohup xvfb-run python main.py > bot.log 2>&1 &
nohup xvfb-run python worker.py > worker.log 2>&1 &
nohup uvicorn webhook:app --host 0.0.0.0 --port 8080 > webhook.log 2>&1 &
```

---

## Limitaciones conocidas

- Coches.net: scraping HTML/SPA frágil. Falla controlado si cambia el HTML.
- Vision LLM: desactivado (`ENABLE_VISION=false`). Modelo decommissioned.
- TIER_LIMITS: pendiente de implementar (tarea actual).
- webhook.py: requiere puerto 8080 accesible externamente o nginx proxy.
- Sin portal de cliente Stripe todavía (gestión de suscripciones manual).

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

### 2026-04-28 — Sesión 5 (freemium — PENDIENTE)
- [ ] Tablas `usuarios` + `pagos` en database.py
- [ ] 4 funciones: get_o_crear_usuario, puede_analizar, registrar_analisis, activar_plan
- [ ] Variables Stripe en config.py
- [ ] Check de límite en cmd_analizar + registrar_analisis al final
- [ ] callback_pago + handler registrado en main.py
- [ ] webhook.py nuevo
- [ ] pip install stripe fastapi uvicorn
- [ ] Test con Stripe CLI modo test

---

## Roadmap futuro /analizar (diferido, NO implementar ahora)

- `/comparar url1 url2` — dos anuncios enfrentados.
- Botón "Buscar otro como este" tras veredicto.
- Histórico de tendencia del modelo (30/90 días).
- Coste total a 5 años (TCO).
- Monitor de precio del anuncio.
- Verificación matrícula → DGT (fase 9+).