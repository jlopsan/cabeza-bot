# Coches con cabeza — Bot de análisis de coches usados

## Resumen

Bot de Telegram (sin UI web) que analiza anuncios de coches usados en el mercado español. Devuelve veredictos objetivos: precio vs mercado comparable, banderas rojas, qué preguntar antes de ver el coche, fiabilidad del modelo, alternativas. Wallapop y Coches.net en fase 1. AutoScout24 (DE→ES) como feature heredada.

Marca: **Juan Lopera · Coches con cabeza**. Web `juanlopera.es` es solo landing de captura de emails. El único canal de usuario es Telegram. FastAPI solo expone webhook de Stripe.

## Stack

- Python 3.11+
- `python-telegram-bot` — única interfaz de usuario
- `playwright` — scraping AutoScout24 + Coches.net (headed)
- `httpx` — scraping Wallapop API
- `openai` SDK apuntando a SambaNova (Llama 4 Maverick)
- SQLite + `APScheduler` (worker periódico)
- `stripe` (pagos), `fastapi` + `uvicorn` (solo webhook)

## Entry points

- `python main.py` — bot Telegram
- `python worker.py` — daemon de misiones (cada 15 min normal, 3 min sniper) + ciclo_health diario
- `uvicorn webhook:app --port 8080` — webhook Stripe

## Reglas innegociables

1. **Bot 100% Telegram.** No construir UI web hasta semana 8. `webhook.py` es infraestructura de pagos, no UI.
2. **Dataset histórico siempre.** Cada scrapeo persiste en `historico_precios` (precio>0, año>1990, retención 180 días).
3. **No romper lo existente.** `/analizar` v4, `/buscar` (DE→ES), `/ideal`, worker: deben seguir funcionando intactos tras cada cambio.
4. **Cada sesión cierra con algo funcionando al 100%.** Nunca tres cosas a medias.
5. **Tests manuales con casos reales** antes de marcar feature como completa.
6. **Refactor solo si es necesario.** No arreglar lo que funciona.

## Tono de output del bot

Cavernícola. Oraciones 3-6 palabras. Cero rellenos, preámbulos o cortesías. Información esencial. Directo. Sin explicaciones innecesarias. La calidad del código intacta.

## Roadmap (8 semanas)

- ✅ S0 Identidad + landing + vídeo manifiesto
- ✅ S1 `/analizar <url>` (v4 en producción)
- ✅ S2 `/ideal` recomendador
- ✅ S3 freemium con Stripe (3 free de por vida, pack 30 a 4.90€, pack 100 a 9.90€)
- ✅ S4 `/comparar`
- ✅ S5 `/tasar` (valor + banda de negociación, afinado por CV/combustible)
- ⬜ S6 `/alertas`
- ⬜ S7 `/importar_alemania` (puerto del /buscar antiguo)
- ⬜ S8 Web pública con endpoints
- ⬜ S9-10 Telegram Stars (opcional)

## Modelo de negocio actual

- **FREE**: 3 acciones de por vida (una vez por usuario nuevo, sin reset, combinadas entre comandos)
- **PACK CHICO**: 30 acciones — 4.90€ (pago único, sin caducidad, acumulables)
- **PACK GRANDE**: 100 acciones — 9.90€ (pago único, sin caducidad, acumulables)
- **PRO mensual**: dormido (código listo, se activa con 4-5 features)

Costes por comando en `permisos.py::COSTE_COMANDO`. Hoy todo cuesta 1.

## Archivos clave

| Módulo | Responsabilidad |
|---|---|
| `main.py` | Entry point Telegram + ConversationHandler |
| `ai.py` | LLM (SambaNova), parseo NL, veredictos |
| `scraper.py` | Wallapop API + Coches.net Playwright + AutoScout24 |
| `database.py` | SQLite: misiones, historico_precios, usuarios, pagos |
| `worker.py` | Daemon APScheduler |
| `permisos.py` | Decorator `@requiere_acceso` + créditos |
| `webhook.py` | FastAPI mínimo para Stripe |
| `red_flags.py` | 5 reglas deterministas de fraude |
| `dgt.py` | Etiqueta DGT + ZBE determinista |
| `calculator.py` | Landing price + IEDMT + beneficio |
| `ideal_pipeline.py`, `ideal_schema.py` | Pipeline /ideal |
| `comparar_pipeline.py` | Pipeline /comparar (en construcción) |
| `config.py` | Variables de entorno + constantes |

## Limitaciones conocidas

- Coches.net: scraping HTML/SPA frágil
- Vision LLM desactivado (`ENABLE_VISION=false`)
- Sin portal Stripe de cliente todavía
- Anti-abuso multicuenta: sin mitigación (aceptar pérdida)
- iOS: no usar Telegram Payments para evitar Apple Tax — Stripe Checkout en navegador sí
