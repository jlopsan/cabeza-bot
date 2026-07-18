# worker.py - Daemon en segundo plano (v4 — sniper Alemania)
#
# Ciclos:
#   - sniper (cada SNIPER_INTERVAL_MINUTES): misiones v2 de vigilancia DE→ES.
#     Agrupa misiones por clave de scrapeo, respeta presupuesto por pasada,
#     circuit breaker por fuente y cap de scrapes/hora. CERO IA, CERO scraping ES
#     en el ciclo (usa valoraciones cacheadas; refresca máx 1 por pasada).
#   - health (diario): sonda de fuentes ES + purga de histórico.
#
# Todo el estado vive en SQLite: reinicio del worker no re-alerta ni pierde snapshot.
#
import asyncio
import logging
import time

import httpx

from config import (
    TELEGRAM_TOKEN, ENABLE_SNIPER,
    SNIPER_INTERVAL_MINUTES, SNIPER_BUDGET_S, SNIPER_MAX_SCRAPES_HORA,
    SNIPER_CB_FALLOS, SNIPER_CB_PAUSA_MIN, SNIPER_ALERTAS_PASADA,
)
from database import (
    init_db, purgar_historico_antiguo,
    expirar_misiones_legacy, expirar_misiones_vencidas,
    obtener_misiones_sniper_activas, set_mision_run, incr_alertas_mision,
    fuente_pausada, incr_fallo_fuente, reset_fuente,
    incr_scrape_hora, scrapes_ultima_hora, registrar_evento_embudo,
)
from scraper import buscar_comparables_todas, ScraperAutoScout24
import sniper_pipeline as sp

logging.basicConfig(
    format="%(asctime)s [WORKER] %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
FUENTE_DE = "autoscout24"


async def _send(chat_id: int, texto: str, reply_markup: dict | None = None):
    payload = {
        "chat_id": chat_id, "text": texto,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{TELEGRAM_API}/sendMessage", json=payload)
        if not r.is_success:
            logger.error(f"Telegram error {r.status_code}: {r.text[:200]}")


# ════════════════════════════════════════════════════════════════════════════
# CICLO SNIPER
# ════════════════════════════════════════════════════════════════════════════

async def _procesar_mision(mision: dict, anuncios: list[dict], refrescos: int) -> int:
    """
    Evalúa los anuncios de la clave contra una misión. Devuelve el nº de refrescos
    de valoración acumulados en la pasada (para respetar el máx 1 por pasada).
    """
    mid    = mision["id"]
    marca  = mision["marca"]
    modelo = mision["modelo"]
    umbral_eur = mision.get("umbral_margen_eur") or 0
    umbral_pct = mision.get("umbral_margen_pct") or 0

    # Primera pasada: sembrar snapshot SIN alertar.
    if sp.necesita_siembra(mision):
        n = sp.sembrar(mision, anuncios)
        set_mision_run(mid)
        logger.info(f"[SNIPER] Misión #{mid} sembrada ({n} anuncios), sin alertas")
        return refrescos

    nuevos = sp.filtrar_nuevos(mision, anuncios)
    if not nuevos:
        set_mision_run(mid)
        return refrescos

    alertas = 0
    for anuncio in nuevos:
        año = anuncio.get("año", 0)
        km  = anuncio.get("km", 0)

        # Valoración: fresca de caché, o refresco (máx 1 por pasada global).
        v = sp.valoracion_fresca(marca, modelo, año, km)
        if v is None:
            if refrescos >= 1:
                continue  # sin marcar visto: se evalúa en la próxima pasada
            v = await sp.refrescar_valoracion(marca, modelo, año, km)
            refrescos += 1
            if v is None:
                sp.marcar_visto(mid, anuncio, tipo="snapshot")  # sin comparables: no reintentar
                continue

        # Pre-filtro con datos del listado (CO₂ estimado) para no bajar a detalle en balde.
        pre = sp.evaluar_candidato(anuncio, v, umbral_eur, umbral_pct)
        if not pre["alerta"]:
            sp.marcar_visto(mid, anuncio, tipo="snapshot", cuenta=pre["cuenta"])
            continue

        # Fase 2: detalle real (CO₂, Netto, propietarios…) y re-evaluación.
        anuncio = await ScraperAutoScout24().obtener_detalle_candidato(anuncio)
        final = sp.evaluar_candidato(anuncio, v, umbral_eur, umbral_pct)
        if not final["alerta"]:
            sp.marcar_visto(mid, anuncio, tipo="snapshot", cuenta=final["cuenta"])
            continue

        # Tope de alertas por misión/pasada: el resto se registra visto sin alertar.
        if alertas >= SNIPER_ALERTAS_PASADA:
            sp.marcar_visto(mid, anuncio, tipo="snapshot", cuenta=final["cuenta"])
            continue

        texto = sp.render_tarjeta_alerta(anuncio, v, final["cuenta"], mid)
        await _send(mision["user_id"], texto, reply_markup=sp.boton_ver_anuncio(anuncio.get("link", "")))
        sp.marcar_visto(mid, anuncio, tipo="alerta", cuenta=final["cuenta"])
        incr_alertas_mision(mid, 1)
        registrar_evento_embudo(
            mision["user_id"], "alerta_enviada",
            f"mision={mid};margen={final['cuenta']['margen_eur']:.0f}",
        )
        alertas += 1
        logger.info(f"[SNIPER] Misión #{mid}: alerta {anuncio.get('id')} margen {final['cuenta']['margen_eur']:.0f}€")
        await asyncio.sleep(1.0)

    set_mision_run(mid)
    return refrescos


async def _pasada_sniper():
    """Una pasada del ciclo sniper. Agrupa por clave, respeta presupuesto y breaker."""
    expiradas = expirar_misiones_vencidas()
    if expiradas:
        logger.info(f"[SNIPER] {expiradas} misiones expiradas")

    misiones = obtener_misiones_sniper_activas()
    if not misiones:
        return

    if fuente_pausada(FUENTE_DE):
        logger.warning("[SNIPER] AutoScout24 pausada (circuit breaker); salto pasada")
        return

    # Agrupar misiones por clave de scrapeo (filtros equivalentes → un scrapeo).
    grupos: dict[str, list[dict]] = {}
    for m in misiones:
        grupos.setdefault(sp.clave_scrapeo(m), []).append(m)

    # Orden por la misión más olvidada del grupo (round-robin justo).
    claves = sorted(grupos, key=lambda k: min((mm.get("last_run_at") or "") for mm in grupos[k]))
    logger.info(f"[SNIPER] {len(misiones)} misiones en {len(claves)} claves de scrapeo")

    inicio = time.monotonic()
    refrescos = 0
    for clave in claves:
        if time.monotonic() - inicio > SNIPER_BUDGET_S:
            logger.info("[SNIPER] Presupuesto de pasada agotado; resto en la próxima")
            break
        if scrapes_ultima_hora(FUENTE_DE) >= SNIPER_MAX_SCRAPES_HORA:
            logger.warning("[SNIPER] Cap de scrapes/hora alcanzado; salto resto de pasada")
            break
        if fuente_pausada(FUENTE_DE):
            break

        grupo   = grupos[clave]
        marca   = grupo[0]["marca"]
        modelo  = grupo[0]["modelo"]
        filtros = sp.filtros_mision(grupo[0])

        incr_scrape_hora(FUENTE_DE)
        anuncios, señal = await ScraperAutoScout24().buscar_deteccion(marca, modelo, filtros)

        if señal == "fallo":
            fallos, pausada = incr_fallo_fuente(FUENTE_DE, SNIPER_CB_FALLOS, SNIPER_CB_PAUSA_MIN)
            logger.warning(f"[SNIPER] Fallo AS24 ({fallos}/{SNIPER_CB_FALLOS}) clave={clave}")
            for m in grupo:
                set_mision_run(m["id"], error="scrape_fallo")
            if pausada:
                logger.error(f"[SNIPER] Circuit breaker ABIERTO — pausa {SNIPER_CB_PAUSA_MIN} min")
                break
            continue

        # 'ok' o 'vacio': la fuente respondió bien → sana.
        reset_fuente(FUENTE_DE)
        for m in grupo:
            try:
                refrescos = await _procesar_mision(m, anuncios, refrescos)
            except Exception as e:
                logger.error(f"[SNIPER] Misión #{m['id']} error: {e}", exc_info=True)
                set_mision_run(m["id"], error=str(e)[:200])


async def _ciclo_sniper():
    if not ENABLE_SNIPER:
        logger.info("[SNIPER] ENABLE_SNIPER=false — ciclo sniper dormido")
        return
    logger.info(f"[SNIPER] Ciclo activo cada {SNIPER_INTERVAL_MINUTES} min")
    while True:
        try:
            await _pasada_sniper()
        except Exception as e:
            logger.error(f"[SNIPER] Pasada falló: {e}", exc_info=True)
        await asyncio.sleep(SNIPER_INTERVAL_MINUTES * 60)


# ════════════════════════════════════════════════════════════════════════════
# CICLO HEALTH (diario) — sonda de fuentes ES + purga
# ════════════════════════════════════════════════════════════════════════════

_HEALTH_FUENTES_REF = {"wallapop": 0, "coches.net": 0}


async def _ciclo_health():
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


async def ciclo_worker():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN no configurado. Revisa tu archivo .env")
        return
    init_db()
    # Misiones legacy (pre-v2, sin marca parseada) no las procesa el ciclo v2.
    n_legacy = expirar_misiones_legacy()
    if n_legacy:
        logger.info(f"[WORKER] {n_legacy} misiones legacy marcadas EXPIRADA")
    logger.info("Worker v4 arrancado.")
    logger.info(f"  Sniper: {'ON' if ENABLE_SNIPER else 'OFF'} (cada {SNIPER_INTERVAL_MINUTES} min) | Health: diario")

    await asyncio.gather(
        _ciclo_sniper(),
        _ciclo_health(),
    )


if __name__ == "__main__":
    asyncio.run(ciclo_worker())
