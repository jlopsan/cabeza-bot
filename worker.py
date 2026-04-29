# worker.py - Proceso en segundo plano para monitoreo de misiones activas (v3)
#
# Cambios v3:
#   - Dos ciclos: normal (15 min) + sniper (3 min)
#   - Sniper Score en alertas
#   - Usa buscar_y_cruzar() multi-fuente
#
import asyncio
import json
import logging

import httpx

from config import (
    TELEGRAM_TOKEN, WORKER_INTERVAL_MINUTES, SNIPER_INTERVAL_MINUTES,
    TOP_RESULTS, MIN_BENEFICIO,
)
from database import (
    init_db, obtener_misiones_activas,
    ya_enviada, marcar_enviada,
    purgar_historico_antiguo,
)
from scraper import buscar_y_cruzar, buscar_coches_alemania, buscar_comparables_todas
from calculator import (
    calcular_landing_price, calcular_beneficio, formato_tarjeta,
    calcular_sniper_score, formato_sniper_score,
)
from ai import parsear_modelo_nl

logging.basicConfig(
    format="%(asctime)s [WORKER] %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


async def _send(chat_id: int, texto: str, foto_url: str | None = None):
    async with httpx.AsyncClient(timeout=15) as client:
        if foto_url:
            r = await client.post(f"{TELEGRAM_API}/sendPhoto", json={
                "chat_id": chat_id, "photo": foto_url,
                "caption": texto, "parse_mode": "HTML",
            })
        else:
            r = await client.post(f"{TELEGRAM_API}/sendMessage", json={
                "chat_id": chat_id, "text": texto,
                "parse_mode": "HTML", "disable_web_page_preview": False,
            })
        if not r.is_success:
            logger.error(f"Telegram error {r.status_code}: {r.text[:200]}")


def _parse_filtros(mision: dict) -> dict:
    try:
        return json.loads(mision.get("filtros", "{}") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _get_precio_objetivo(mision: dict) -> float | None:
    v = mision.get("precio_objetivo_es")
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _get_beneficio_coche(coche: dict, precio_objetivo: float | None) -> float:
    calc = calcular_landing_price(coche["precio"], coche.get("co2", 0))
    precio_es = precio_objetivo if precio_objetivo is not None else coche.get("precio_medio_es", 0)
    return calcular_beneficio(calc["landing_price"], precio_es)["beneficio"]


async def procesar_mision(mision: dict, es_sniper: bool = False):
    mision_id       = mision["id"]
    user_id         = mision["user_id"]
    query           = mision["query_modelo"]
    precio_objetivo = _get_precio_objetivo(mision)
    ids_rechazados  = json.loads(mision.get("ids_rechazados", "[]") or "[]")
    filtros         = _parse_filtros(mision)
    modo            = "manual" if precio_objetivo is not None else "auto"

    label = "🎯 SNIPER" if es_sniper else "📋 NORMAL"
    logger.info(f"[{label}] Misión #{mision_id} ({query}) — modo: {modo}")

    # Parsear marca/modelo con IA
    parsed = await parsear_modelo_nl(query)
    marca  = parsed.get("marca") or query.split()[0]
    modelo = parsed.get("modelo") or (query.split(maxsplit=1)[1] if len(query.split()) > 1 else query)

    # Scraping multi-fuente
    if modo == "auto":
        coches = await buscar_y_cruzar(marca, modelo, filtros)
    else:
        coches = await buscar_coches_alemania(marca, modelo, filtros)

    if not coches:
        logger.info(f"Misión #{mision_id}: sin resultados.")
        return

    # Filtrar rechazados y ya notificados
    coches_nuevos = [
        c for c in coches
        if c["id"] not in ids_rechazados and not ya_enviada(mision_id, c["id"])
    ]

    if not coches_nuevos:
        logger.info(f"Misión #{mision_id}: no hay coches nuevos.")
        return

    # Seleccionar oportunidades
    oportunidades = [
        c for c in coches_nuevos
        if _get_beneficio_coche(c, precio_objetivo) >= MIN_BENEFICIO
    ]

    if not oportunidades:
        mejor = max(coches_nuevos, key=lambda c: _get_beneficio_coche(c, precio_objetivo))
        mejor_b = _get_beneficio_coche(mejor, precio_objetivo)
        logger.info(f"Misión #{mision_id}: mejor={mejor_b:,.0f}€ (umbral: {MIN_BENEFICIO:,}€)")
        return

    oportunidades.sort(key=lambda c: _get_beneficio_coche(c, precio_objetivo), reverse=True)
    oportunidades = oportunidades[:TOP_RESULTS]

    # Enviar alertas
    fuentes = set(c.get("fuente", "?") for c in oportunidades)
    urgencia = "🚨🚨 <b>¡ALERTA SNIPER!</b>" if es_sniper else "🚨 <b>¡OPORTUNIDAD DETECTADA!</b>"
    cabecera = (
        f"{urgencia}\n"
        f"📋 Misión #{mision_id} — {query}\n"
        f"📡 Fuentes: {' + '.join(fuentes)}\n"
        f"{'─' * 32}"
    )
    await _send(user_id, cabecera)

    for coche in oportunidades:
        tarjeta = formato_tarjeta(coche, precio_objetivo)
        score = calcular_sniper_score(coche, precio_objetivo)
        score_txt = formato_sniper_score(score)
        texto = f"{tarjeta}\n\n{score_txt}"

        await _send(user_id, texto, foto_url=coche.get("foto"))
        marcar_enviada(mision_id, coche["id"])
        logger.info(f"Misión #{mision_id}: notificado {coche['id']} (Score: {score['sniper_score']})")
        await asyncio.sleep(1.0)


async def _ciclo_normal():
    """Revisa misiones normales cada WORKER_INTERVAL_MINUTES."""
    while True:
        misiones = obtener_misiones_activas(prioridad="normal")
        logger.info(f"[NORMAL] Misiones activas: {len(misiones)}")
        for mision in misiones:
            try:
                await procesar_mision(mision, es_sniper=False)
            except Exception as e:
                logger.error(f"Error misión #{mision['id']}: {e}", exc_info=True)
        await asyncio.sleep(WORKER_INTERVAL_MINUTES * 60)


_HEALTH_FUENTES_REF = {"wallapop": 0, "coches.net": 0}


async def _ciclo_health():
    """
    Health check diario de fuentes ES. Lanza una búsqueda fija (Seat Ibiza 2018,
    60.000 km) y loguea WARNING si alguna fuente devuelve <3 items.
    """
    while True:
        try:
            items = await buscar_comparables_todas("Seat", "Ibiza", 2018, 60_000, n=20)
            por_fuente: dict[str, int] = {}
            for a in items:
                por_fuente[a.fuente] = por_fuente.get(a.fuente, 0) + 1
            for nombre in ("wallapop", "coches.net"):
                n = por_fuente.get(nombre, 0)
                if n < 3:
                    logger.warning(f"[HEALTH] {nombre} CAÍDA — solo {n} items en sonda")
                else:
                    logger.info(f"[HEALTH] {nombre} OK — {n} items")
                _HEALTH_FUENTES_REF[nombre] = n
        except Exception as e:
            logger.error(f"[HEALTH] Sonda falló: {e}")
        try:
            purgar_historico_antiguo(dias=180)
        except Exception as e:
            logger.error(f"[HEALTH] Error purgando histórico: {e}")
        await asyncio.sleep(24 * 60 * 60)


async def _ciclo_sniper():
    """Revisa misiones sniper cada SNIPER_INTERVAL_MINUTES."""
    while True:
        misiones = obtener_misiones_activas(prioridad="sniper")
        if misiones:
            logger.info(f"[SNIPER] Misiones activas: {len(misiones)}")
            for mision in misiones:
                try:
                    await procesar_mision(mision, es_sniper=True)
                except Exception as e:
                    logger.error(f"Error misión sniper #{mision['id']}: {e}", exc_info=True)
        await asyncio.sleep(SNIPER_INTERVAL_MINUTES * 60)


async def ciclo_worker():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN no configurado. Revisa tu archivo .env")
        return
    init_db()
    logger.info(f"Worker v3 arrancado.")
    logger.info(f"  Normal: cada {WORKER_INTERVAL_MINUTES} min | Sniper: cada {SNIPER_INTERVAL_MINUTES} min")
    logger.info(f"  Fuentes DE: AutoScout24 + mobile.de | Fuentes ES: Wallapop + coches.net")

    # Ejecutar ciclos en paralelo (normal + sniper + health diario)
    await asyncio.gather(
        _ciclo_normal(),
        _ciclo_sniper(),
        _ciclo_health(),
    )


if __name__ == "__main__":
    asyncio.run(ciclo_worker())
