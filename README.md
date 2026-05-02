# Coches con cabeza — Bot de análisis de coches usados

Bot de Telegram que analiza anuncios de coches usados en España.
Scrapea Wallapop y Coches.net en tiempo real. Devuelve veredicto
objetivo: precio vs mercado, red flags, etiqueta DGT, fiabilidad
del modelo y qué preguntar antes de ir a verlo.

Producto de **Juan Lopera · Coches con cabeza** ([juanlopera.es](https://juanlopera.es)).

---

## Qué hace

- `/analizar <url>` — Analiza anuncio de Wallapop o Coches.net.
- `/buscar` — Búsqueda con criterios (legacy, importación DE→ES).
- Worker de misiones — Avisa cuando aparece un chollo.

Salida del análisis:
- Precio vs mediana de comparables reales.
- Score de confianza 🟢/🟡/🔴.
- Red flags deterministas (5 reglas).
- Etiqueta DGT + restricciones ZBE.
- Investigación del modelo (Tavily — fiabilidad, problemas).
- Bloque 🚨 si precio anómalamente bajo.
- Alternativa motor más barata si aplica.
- Botón de preguntas + checklist de inspección.

---

## Modelo freemium

| Plan | Precio | Análisis |
|------|--------|----------|
| Free | 0€ | 3 totales |
| Pack | 4,90€ pago único | 20 |
| Pro  | 9,90€/mes | Ilimitados |

Pagos por Stripe. Webhook activa el plan automáticamente.

---

## Stack

- Python 3.11+
- python-telegram-bot
- playwright (Coches.net headed)
- httpx (Wallapop API)
- openai SDK → SambaNova (Llama 4 Maverick)
- Tavily (investigación)
- SQLite
- APScheduler (worker)
- stripe + fastapi + uvicorn (webhook pagos)

---

## Arquitectura

```
cabeza-bot/
├── main.py        Bot Telegram + ConversationHandler
├── scraper.py     Wallapop API + Coches.net Playwright
├── ai.py          Parseo NL + veredictos IA
├── calculator.py  Landing price + IEDMT (legacy DE→ES)
├── database.py    SQLite: misiones, historico_precios, usuarios, pagos
├── worker.py      Daemon misiones + health diario
├── config.py      Variables de entorno
├── dgt.py         Etiqueta DGT + ZBE (determinista)
├── red_flags.py   5 reglas detección fraude
├── webhook.py     FastAPI mínimo — webhooks Stripe
└── requirements.txt
```

---

## Arranque

### Variables `.env`

```
TELEGRAM_TOKEN=...
SAMBANOVA_API_KEY=...
TAVILY_API_KEY=...
STRIPE_API_KEY=sk_test_...
STRIPE_PRICE_PACK=price_...
STRIPE_PRICE_PRO=price_...
STRIPE_WEBHOOK_SEC=whsec_...
```

Opcionales (con defaults):
```
AI_TIMEOUT_S=30
ANALISIS_CACHE_TTL_S=1800
HISTORICO_RETENCION_DIAS=180
ENABLE_VISION=false
ENABLE_COCHES_NET=true
FREE_ANALISIS_MAX=3
PAID_ANALISIS_MAX=20
```

### Instalación

```bash
pip install -r requirements.txt
playwright install chromium
```

### Producción Linux

```bash
nohup xvfb-run python main.py > bot.log 2>&1 &
nohup xvfb-run python worker.py > worker.log 2>&1 &
nohup uvicorn webhook:app --host 0.0.0.0 --port 8080 > webhook.log 2>&1 &
```

### Local con Stripe CLI

```bash
stripe listen --forward-to localhost:8080/stripe/webhook
```

---

## Roadmap (8 semanas)

- ✅ Semana 0 — Identidad + landing + vídeo manifiesto.
- ✅ Semana 1 — `/analizar <url>` v4.
- 🔨 Semana 2 — Freemium + `/km_check`.
- Semana 3-4 — Evaluador de fiabilidad.
- Semana 5-6 — `/ideal` recomendador.
- Semana 7 — `/tasar` + alertas chollos.
- Semana 8 — Web pública con endpoints.

---

## Limitaciones conocidas

- Coches.net frágil ante cambios de HTML.
- Vision LLM desactivado (modelo decommissioned).
- Sin portal de cliente Stripe — gestión manual de suscripciones.
- Webhook requiere puerto 8080 accesible (o nginx proxy).
