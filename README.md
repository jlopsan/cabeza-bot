# 🎯 German Sniper Bot  v2

Bot de Telegram para importadores profesionales de coches.
Scrapa AutoScout24.de, cruza los resultados con precios de mercado reales
de Wallapop España y calcula el beneficio bruto de cada operación.

---

## Arranque rápido

```bash
pip install -r requirements.txt
playwright install chromium
export TELEGRAM_TOKEN="tu_token_aqui"

python main.py     # terminal 1 — bot Telegram
python worker.py   # terminal 2 — monitoreo en segundo plano
```

---

## Arquitectura

```
german_sniper/
├── main.py         Bot Telegram + ConversationHandler (máquina de estados)
├── scraper.py      AutoScout24.de (Playwright) + Wallapop API (httpx)
├── calculator.py   IEDMT + costes fijos + cálculo de beneficio (modo manual/auto)
├── database.py     SQLite: misiones + oportunidades ya enviadas
├── worker.py       Loop en segundo plano — notifica si beneficio ≥ 3.000€
├── config.py       Tokens, costes, tramos fiscales, parámetros de negocio
└── requirements.txt
```

---

## Dos modos de precio ES

| Modo | Cómo activarlo | Fuente del precio ES |
|------|---------------|----------------------|
| **Manual** | Escribe un número (ej: `32000`) | El usuario fija el precio |
| **Auto** | Escribe `auto` | Wallapop API — promedio de las 5 ofertas más baratas reales |

Ambos modos funcionan tanto en búsquedas en vivo como en misiones del worker.

---

## Fórmula de negocio

```
Landing Price  =  Precio_DE + IEDMT(CO₂%) + 1.200€ transporte + 350€ gestoría/ITV
Beneficio      =  Precio_ES  -  Landing Price
Notificar si   Beneficio ≥ 3.000€
```

### Tramos IEDMT (2024)

| CO₂ (g/km)  | Tipo   |
|-------------|--------|
| ≤ 120       | 0 %    |
| 121 – 159   | 4,75 % |
| 160 – 199   | 9,75 % |
| ≥ 200       | 14,75 %|

---

## Cruce DE ↔ ES (modo auto)

Cuando encuentra un coche con, p.ej., `Audi A5, 2019, 80.000 km`,
lanza una búsqueda en Wallapop con:
- Años: 2018 – 2020
- Km: 60.000 – 100.000

Aplica filtro anti-scam (descarta precios < 50% de la mediana o < 1.500€)
y promedia los 5 más baratos del conjunto limpio.

---

## Actualizar selectores si la web cambia

| Web | Archivo | Dónde |
|-----|---------|-------|
| AutoScout24.de | `scraper.py` | dict `SELECTORS_DE` (línea ~30) |
| Wallapop API   | `scraper.py` | clase `ScraperSpain`, constantes `_API_URL` + sección "Extraer precios" |
