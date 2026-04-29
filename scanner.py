# scanner.py - Bot de ofertas generales (gancho gratuito)
#
# Escanea modelos populares en AutoScout24 + mobile.de cada hora,
# cruza con precios España, y publica las mejores ofertas en un canal
# de Telegram público. Sirve como demo gratuita del bot principal.
#
# Ejecutar: python scanner.py
#
import asyncio
import logging
import random

import httpx

from config import (
    TELEGRAM_TOKEN, SCANNER_CHANNEL_ID, SCANNER_INTERVAL_MINUTES,
    SCANNER_TOP_DEALS, SCANNER_MODELS, MIN_BENEFICIO,
)
from database import (
    init_db, scanner_ya_enviado, scanner_marcar_enviado,
)
from scraper import buscar_y_cruzar
from calculator import (
    calcular_landing_price, calcular_beneficio,
    calcular_sniper_score, formato_sniper_score, formato_tarjeta,
)

logging.basicConfig(
    format="%(asctime)s [SCANNER] %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


async def _send_canal(texto: str, foto_url: str | None = None):
    """Envía mensaje al canal público."""
    async with httpx.AsyncClient(timeout=15) as client:
        if foto_url:
            r = await client.post(f"{TELEGRAM_API}/sendPhoto", json={
                "chat_id": SCANNER_CHANNEL_ID, "photo": foto_url,
                "caption": texto, "parse_mode": "HTML",
            })
        else:
            r = await client.post(f"{TELEGRAM_API}/sendMessage", json={
                "chat_id": SCANNER_CHANNEL_ID, "text": texto,
                "parse_mode": "HTML", "disable_web_page_preview": False,
            })
        if not r.is_success:
            logger.error(f"Telegram error {r.status_code}: {r.text[:200]}")


async def escanear_modelo(marca: str, modelo: str, filtros: dict) -> list[dict]:
    """Busca un modelo y devuelve coches con beneficio calculado."""
    try:
        coches = await buscar_y_cruzar(marca, modelo, filtros)
    except Exception as e:
        logger.error(f"Error escaneando {marca} {modelo}: {e}")
        return []

    if not coches:
        return []

    # Calcular beneficio y score para cada coche
    resultado = []
    for c in coches:
        precio_es = c.get("precio_medio_es", 0)
        if not precio_es:
            continue
        calc = calcular_landing_price(c["precio"], c.get("co2", 0))
        benef = calcular_beneficio(calc["landing_price"], precio_es)
        if benef["beneficio"] < MIN_BENEFICIO:
            continue
        # No re-publicar coches que ya enviamos
        if scanner_ya_enviado(c["id"]):
            continue
        c["_beneficio"] = benef["beneficio"]
        c["_score"] = calcular_sniper_score(c)
        resultado.append(c)

    # Ordenar por sniper_score descendente
    resultado.sort(key=lambda c: c["_score"]["sniper_score"], reverse=True)
    return resultado


async def ciclo_scanner():
    """Ciclo principal del scanner: escanea modelos populares y publica en canal."""
    init_db()

    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN no configurado.")
        return
    if not SCANNER_CHANNEL_ID:
        logger.error("SCANNER_CHANNEL_ID no configurado. Añade a .env: SCANNER_CHANNEL_ID=@tu_canal")
        return

    logger.info(f"Scanner arrancado. Publicando cada {SCANNER_INTERVAL_MINUTES} min en {SCANNER_CHANNEL_ID}")
    logger.info(f"Modelos monitorizados: {len(SCANNER_MODELS)}")

    while True:
        todas_ofertas: list[dict] = []

        # Escanear una selección aleatoria (no todos cada vez, para no saturar)
        modelos_ciclo = random.sample(SCANNER_MODELS, min(4, len(SCANNER_MODELS)))

        for marca, modelo, filtros in modelos_ciclo:
            logger.info(f"Escaneando {marca} {modelo}...")
            ofertas = await escanear_modelo(marca, modelo, filtros)
            todas_ofertas.extend(ofertas)
            # Pausa entre modelos para no saturar portales
            await asyncio.sleep(random.uniform(10, 20))

        if not todas_ofertas:
            logger.info("Sin ofertas nuevas en este ciclo.")
            await asyncio.sleep(SCANNER_INTERVAL_MINUTES * 60)
            continue

        # Seleccionar top ofertas globales
        todas_ofertas.sort(key=lambda c: c["_score"]["sniper_score"], reverse=True)
        top = todas_ofertas[:SCANNER_TOP_DEALS]

        # Publicar cabecera
        cabecera = (
            f"🎯 <b>OFERTAS DE IMPORTACIÓN</b> — {len(top)} oportunidades\n"
            f"{'─' * 35}\n"
            f"Coches en Alemania con margen de beneficio\n"
            f"al importarlos a España.\n\n"
            f"🤖 Análisis automático cada {SCANNER_INTERVAL_MINUTES} min\n"
            f"🔔 ¿Quieres alertas personalizadas? → @GermanSniperBot"
        )
        await _send_canal(cabecera)
        await asyncio.sleep(1)

        # Publicar cada oferta
        for idx, coche in enumerate(top, 1):
            score = coche["_score"]
            tarjeta = formato_tarjeta(coche)
            score_txt = formato_sniper_score(score)

            texto = (
                f"<b>#{idx}</b> 📍<i>{coche.get('fuente', '?')}</i>\n"
                f"{tarjeta}\n\n"
                f"{score_txt}\n\n"
                f"💡 <i>¿Buscas este modelo concreto? Con @GermanSniperBot "
                f"puedes monitorizar alertas en tiempo real.</i>"
            )

            foto = coche.get("foto")
            if foto:
                try:
                    await _send_canal(texto, foto_url=foto)
                except Exception:
                    await _send_canal(texto)
            else:
                await _send_canal(texto)

            scanner_marcar_enviado(coche["id"])
            logger.info(f"Publicado: {coche['titulo'][:40]} (Score: {score['sniper_score']})")
            await asyncio.sleep(2)

        logger.info(f"Ciclo completado. Próximo en {SCANNER_INTERVAL_MINUTES} min.")
        await asyncio.sleep(SCANNER_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    asyncio.run(ciclo_scanner())
