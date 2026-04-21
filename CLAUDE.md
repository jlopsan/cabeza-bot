# Proyecto: Coches con cabeza — Bot de análisis de coches usados

## Contexto general

Bot de Telegram (+ futura web pública) que analiza anuncios de coches
usados en el mercado español (Wallapop en fase 1, más fuentes después)
y devuelve veredictos objetivos sobre si un anuncio merece la pena:
precio vs mercado comparable, banderas rojas, qué preguntar antes de
ir a verlo, fiabilidad del modelo, recomendación de alternativas.

El bot original era solo importación DE→ES (arbitraje AutoScout24 →
Wallapop). Se mantiene como FEATURE del producto nuevo, no como
producto principal.

## Producto y posicionamiento

- **Marca**: Juan Lopera · Coches con cabeza
- **Web**: juanlopera.es
- **Target primario**: particulares comprando coche usado en España
- **Target secundario**: Juan Lopera (yo) generando contenido semanal
  para TikTok/Reels/Shorts a partir de features construidas
- **Diferencial vs competencia** (El Box de Autonoción, Coches.net, etc):
  ellos asesoran sobre coche NUEVO con ficha técnica estática.
  Nosotros analizamos anuncios REALES del mercado usado con scraping
  en tiempo real.

## Stack actual

- Python 3.11+
- python-telegram-bot (bot UI)
- playwright (scraping AutoScout24)
- httpx (scraping Wallapop API)
- openai SDK apuntando a Groq (Llama 3.3 70B)
- SQLite (persistencia)
- APScheduler (worker periódico)

## Arquitectura actual

- `main.py`: entry point + ConversationHandler de Telegram
- `scraper.py`: scraping DE (AutoScout24 + Playwright) + ES (Wallapop API)
- `ai.py`: parseo NL, análisis IA de anuncios, validación precios
- `calculator.py`: landing price + IEDMT + beneficio
- `database.py`: SQLite para misiones de monitoreo
- `worker.py`: daemon que revisa misiones cada N minutos
- `config.py`: variables de entorno

## Hoja de ruta: 8 semanas, 8 features, 9 vídeos

### Semana 0 — Identidad, landing, vídeo manifiesto (YA HECHO)

### Semana 1 — `/analizar <url>` ← SEMANA ACTUAL
Usuario pega link de Wallapop. Bot extrae datos, busca 20-30
comparables, calcula estadística, devuelve veredicto IA.

### Semana 2 — Detector de km sospechosos
Estadística de km esperados por año/modelo, integrado en /analizar.

### Semana 3 — Evaluador de fiabilidad (parte 1)
Base de conocimiento con 20 modelos comunes + averías típicas.

### Semana 4 — Evaluador de fiabilidad (parte 2)
Ampliar a 40 modelos + coste mantenimiento anual medio.

### Semana 5 — `/ideal` Recomendador (parte 1)
Perfil de usuario en lenguaje natural → scraping Wallapop → anuncios reales.

### Semana 6 — `/ideal` Recomendador (parte 2)
Sistema de scoring ponderado + top 3 con explicaciones IA.

### Semana 7 — Tasador inverso + alertas de chollos
`/tasar <modelo> <año> <km>` + `/alerta` persistente.

### Semana 8 — Web pública + cierre temporada
FastAPI con endpoints del bot, integración con landing.

## Reglas innegociables del desarrollo

1. **Solo Wallapop + IA (Groq) durante las 8 semanas.** NADA de Coches.net,
   Autocasión, histórico externo, fiabilidad externa hasta semana 9.
2. **El dataset histórico se construye desde el día 1.** Cada scrapeo
   persiste en tabla `historico_precios`. Es el foso del producto.
3. Cada semana termina con **una feature funcionando al 100%**,
   nunca con tres cosas a medias.
4. **No se rompe lo existente**: el bot actual de importación DE→ES
   tiene que seguir funcionando al final de cada sesión.
5. Tests manuales con casos reales antes de dar una feature por hecha.
6. Refactor solo si es necesario para la feature en curso. No "arreglar"
   cosas que ya funcionan.

## Capa de inteligencia con lenguaje natural (transversal)

Toda interacción del bot debe soportar lenguaje natural, no solo
comandos estrictos. Esto es un PRINCIPIO TRANSVERSAL del producto,
no una feature aislada.

Guías:

1. **Cada comando tiene un fallback conversacional**. Si el usuario
   escribe `/analizar https://...` funciona. Si escribe
   *"oye, échale un vistazo a este anuncio: https://..."* también
   funciona. Un router conversacional detecta la intención y
   dispara el comando correcto.

2. **Respuestas en lenguaje natural, no robóticas**. Todas las salidas
   del bot pasan por un último paso de IA que las humaniza manteniendo
   los datos intactos. Preferimos que el bot "hable como Juan" a que
   devuelva tablas frías.

3. **Tono del bot**: directo, un poco incrédulo ante lo absurdo, con
   datos siempre por delante, sin condescender al usuario. Ni gurú ni
   payaso. Cuando detecte algo raro, que lo diga claro. Cuando no sepa,
   que lo admita.

4. **Preguntas clarificadoras cuando falta info**, no errores. Si el
   usuario dice *"quiero un coche por 6.000€"*, el bot pregunta:
   *"¿ciudad? ¿para qué uso?"*. No le devuelve un error.

5. **Multi-turn donde tenga sentido**. El bot mantiene contexto de
   la conversación del usuario en memoria (ya hay `ConversationHandler`
   en main.py). Si el usuario analiza un anuncio y luego dice *"y si
   busco otro con menos km?"*, el bot entiende el contexto.

## Modelo mental de arquitectura futura

Hacia donde vamos (no construir ya, pero tener presente):

- `models.py` con dataclasses `Anuncio`, `Perfil`, `Analisis`, `Veredicto`
- `scrapers/` con wallapop + cochesnet + autocasion (fase 2)
- `enrichers/` con fiabilidad, consumo, etiqueta_dgt, zbe (futuro)
- `scorer.py` con scoring ponderado por perfil (futuro)
- `advisor.py` con IA generando recomendación final (futuro)
- `historico.py` con BD propia de snapshots de mercado (desde ya)
- `router.py` con detección de intención por IA (desde ya, básico)

Cuando refactorices, piensa en no cerrar puertas a esta arquitectura,
pero NO la implementes completa todavía.

## Convenciones de código

- Docstrings en español (como el código actual).
- Logs con el logger de cada módulo, con prefijo entre corchetes
  identificador del contexto: `[DE]`, `[ES]`, `[AI]`, `[BOT]`, `[WORKER]`,
  `[HIST]`, `[ROUTER]`.
- Funciones async donde haya I/O.
- Type hints en código nuevo (dataclasses, return types de funciones
  públicas). En código existente sin hints, no añadirlos salvo que la
  función se toque.
- Manejo de errores: el bot NUNCA crashea por error de scraper o IA.
  Siempre try/except con fallback razonable y log descriptivo.
- Commits atómicos y mensajes claros en inglés con prefijo semántico:
  `feat:`, `fix:`, `refactor:`, `docs:`, `test:`.

## Claves API y secretos

Todas en `.env`:
- `TELEGRAM_TOKEN`
- `GROQ_API_KEY`

## Cómo probar

El bot se arranca con `python main.py`. El worker con `python worker.py`.
Para testing local, probar comandos directamente en el chat de Telegram
con la lista de 5 anuncios Wallapop reales seleccionados como casos de
prueba (ver archivo `casos_prueba.md` en la raíz).

## Sobre Coches.net (aviso explícito)

Coches.net está en el roadmap pero NO durante las semanas 1-8. Razones:

- Es scraping HTML (no API), más frágil y más lento.
- Nuestro valor está en filtrar ruido. Wallapop tiene mucho ruido;
  Coches.net poco.
- Los vídeos se basan en casos reales de Wallapop, que es donde está
  la audiencia del canal.
- Añadirlo fragmenta el foco y rompe el ritmo de 1 feature/semana.

Cuando alguien pregunte "¿y Coches.net?" en comentarios, la respuesta
es: "llegará en la fase 2, primero validamos lo que ya tenemos".

## Log de desarrollo

### 2026-04-21 — Sesión 1 (estrategia)
- Definido sprint de 8 semanas.
- Identidad cerrada: Juan Lopera · Coches con cabeza.
- Landing + logos + handles + panel Notion hechos.
- Roadmap completo en ROADMAP.md y Notion.

### 2026-04-21 — Sesión 2 (feature /analizar)
- `models.py` creado con dataclasses `Anuncio` y `EstadisticaMercado`.
- `database.py`: tabla `historico_precios` + `guardar_historico_batch()`.
- `scraper.py`: `obtener_anuncio_wallapop(url)`, `buscar_comparables_wallapop()`,
  métodos `obtener_item()`, `buscar_items()`, `_item_a_anuncio()` en `ScraperWallapop`.
- `ai.py`: `generar_veredicto_analizar(anuncio, stats)` con texto humanizado.
- `main.py`: handler `cmd_analizar` con flujo completo (extraer → comparables →
  estadística → veredicto IA → guardar histórico). Registrado en `/start`.
- Pendiente para sesión 3: `router.py` (lenguaje natural), tests manuales con URLs reales.

### Próximas sesiones
- [ ] Sesión 3 (domingo): tests manuales /analizar + router NL básico + vídeo semana 1