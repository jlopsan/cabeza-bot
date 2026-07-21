import asyncio
import hashlib
import random
import re
import statistics
import logging
from abc import ABC, abstractmethod

import httpx
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from cabeza_bot.scraping.base import *  # helpers, config, _PLAYWRIGHT_SEM, ScraperDE

logger = logging.getLogger(__name__)


from cabeza_bot.scraping.autoscout24 import ScraperAutoScout24
from cabeza_bot.scraping.mobile_de import ScraperMobileDe
from cabeza_bot.scraping.wallapop import ScraperWallapop
from cabeza_bot.scraping.coches_net import ScraperCochesNet


async def buscar_coches_alemania(
    marca: str, modelo: str, filtros: dict | None = None,
) -> list[dict]:
    filtros = filtros or {}
    extras = filtros.get("extras", [])
    if extras:
        _, extras_sin = _resolver_extras_aex(extras)
        if extras_sin:
            filtros["_extras_sin_codigo"] = extras_sin

    tareas = []
    if ENABLE_AUTOSCOUT24:
        tareas.append(ScraperAutoScout24().buscar(marca, modelo, filtros))
    if ENABLE_MOBILE_DE:
        tareas.append(ScraperMobileDe().buscar(marca, modelo, filtros))
    if not tareas:
        return []

    resultados = await asyncio.gather(*tareas, return_exceptions=True)
    todos: list[dict] = []
    for res in resultados:
        if isinstance(res, Exception):
            logger.error(f"Error fuente DE: {res}")
        elif isinstance(res, list):
            todos.extend(res)

    dedup = _deduplicar_coches(todos)
    logger.info(f"[DE] Combinado: {len(todos)} → {len(dedup)} tras dedup")
    return _postfiltrar(dedup, filtros)


def _deduplicar_coches(coches: list[dict]) -> list[dict]:
    vistos: list[tuple[float, int, int]] = []
    unicos: list[dict] = []
    for c in coches:
        key = (c["precio"], c.get("km", 0), c.get("año", 0))
        if not any(abs(v[0]-key[0]) < 200 and abs(v[1]-key[1]) < 2000 and abs(v[2]-key[2]) <= 1
                   for v in vistos):
            vistos.append(key)
            unicos.append(c)
    return unicos


async def buscar_precio_mercado_es(
    marca: str, modelo: str, año: int, km: int,
    filtros: dict | None = None,
) -> dict:
    filtros = filtros or {}
    tareas, fuentes = [], []
    if ENABLE_WALLAPOP:
        tareas.append(ScraperWallapop().buscar_precios(marca, modelo, año, km))
        fuentes.append("Wallapop")
    if ENABLE_COCHES_NET:
        tareas.append(ScraperCochesNet().buscar_precios(marca, modelo, año, km, filtros))
        fuentes.append("coches.net")
    if not tareas:
        return ScraperWallapop._vacio("No hay fuentes ES")

    resultados = await asyncio.gather(*tareas, return_exceptions=True)
    precios: list[float] = []
    for i, res in enumerate(resultados):
        if isinstance(res, Exception):
            logger.error(f"Error {fuentes[i]}: {res}")
        elif isinstance(res, dict) and res.get("precios_raw"):
            precios.extend(res["precios_raw"])
            logger.info(f"[ES] {fuentes[i]}: {len(res['precios_raw'])} precios")

    if not precios:
        return ScraperWallapop._vacio("Sin resultados ES")
    r = ScraperWallapop._calcular_precio_medio(precios)
    logger.info(f"[ES] Precio medio ({'+'.join(fuentes)}): {r['precio_medio']:,.0f}€")
    return r


def _extraer_item_id_wallapop(url: str) -> str | None:
    """
    Extrae el item_id de una URL de Wallapop.
    Soporta múltiples formatos:
      https://es.wallapop.com/item/seat-ibiza-1020293871           → '1020293871'
      https://wallapop.com/item/audi-a3-2012-1244995621?utm_...    → '1244995621'  (share móvil)
      https://es.wallapop.com/item/seat-ibiza-1020293871/          → '1020293871'
      https://es.wallapop.com/item/1020293871                      → '1020293871'
    """
    # Quitar query/fragmento y barras finales
    clean = url.split("?")[0].split("#")[0].rstrip("/")
    # Caso 1: el último segmento del path termina en -<números> (con slug)
    last = clean.rsplit("/", 1)[-1]
    m = re.search(r"(\d{6,})$", last)
    if m:
        return m.group(1)
    # Fallback: último segmento si es puramente numérico
    if last.isdigit():
        return last
    return last or None


async def obtener_anuncio_wallapop(url: str):
    """
    Extrae los datos de un anuncio individual de Wallapop por URL.
    Devuelve Anuncio o None si no se puede extraer.
    Normaliza URLs tipo 'wallapop.com/...' (share de la app móvil) a 'es.wallapop.com/...'.
    """
    item_id = _extraer_item_id_wallapop(url)
    if not item_id:
        logger.error(f"[ES] No se pudo extraer item_id de: {url}")
        return None
    # Limpiar URL: quitar query/fragment y barras finales
    url_limpia = url.split("?")[0].split("#")[0].rstrip("/")
    # Normalizar dominio: wallapop.com → es.wallapop.com (para que la navegación funcione)
    url_limpia = re.sub(
        r"^(https?://)(?:www\.)?wallapop\.(com|es)",
        r"\1es.wallapop.\2",
        url_limpia,
        flags=re.IGNORECASE,
    )
    logger.info(f"[ES] Obteniendo anuncio Wallapop item_id={item_id} url={url_limpia}")
    return await ScraperWallapop().obtener_item(item_id, url_pagina=url_limpia)



async def buscar_comparables_wallapop(
    marca: str, modelo: str, año: int, km: int, n: int = 30,
) -> list:
    """
    Busca anuncios comparables en Wallapop y devuelve lista de Anuncio.
    Parámetros de tolerancia: año ±1, km ±20k.
    """
    try:
        from cabeza_bot.analisis.ai import normalizar_modelo_wallapop
        modelo_base = await normalizar_modelo_wallapop(marca, modelo)
        keywords = f"{marca.strip().title()} {modelo_base}"
    except Exception:
        keywords = _normalizar_keywords_es(marca, modelo)
    logger.info(f"[ES] Buscando comparables: '{keywords}' año={año} km={km}")
    return await ScraperWallapop().buscar_items(keywords, año, km, n=n)


async def sondear_precio_modelo(
    marca: str, modelo: str, n: int = 20,
) -> list[float]:
    """
    Devuelve los N precios del modelo en Wallapop, ordenados ASC.
    n=20 para muestra fiable incluso si Wallapop no ordena por precio.
    """
    keywords = f"{marca.strip().title()} {modelo.strip().title()}"
    try:
        items = await ScraperWallapop().buscar_items(
            keywords, año=0, km=0, n=n,
            order_by="newest",
        )
        precios = sorted([a.precio for a in items if a.precio > 0])
        logger.info(f"[SONDEO] {keywords}: {len(precios)} precios, min={precios[0] if precios else 0:.0f}€, lista={precios[:5]}")
        return precios
    except Exception as e:
        logger.warning(f"[SONDEO] {keywords} falló: {e}")
        return []


# ════════════════════════════════════════════════════════════════════════════
# CAPA UNIFICADA MULTI-FUENTE  (Wallapop + Coches.net en paralelo)
# ════════════════════════════════════════════════════════════════════════════

def _dedupe_anuncios(items: list) -> list:
    """Dedupe cross-fuente por (precio±200€, año±1, km±2000)."""
    vistos: list[tuple[float, int, int]] = []
    unicos = []
    for a in items:
        key = (a.precio, a.km or 0, a.año or 0)
        if any(abs(v[0] - key[0]) < 200 and abs(v[1] - key[1]) < 2000 and abs(v[2] - key[2]) <= 1
               for v in vistos):
            continue
        vistos.append(key)
        unicos.append(a)
    return unicos


def _fuentes_activas() -> list:
    """Devuelve scrapers ES habilitados respetando flags de config."""
    fuentes = []
    if ENABLE_WALLAPOP:
        fuentes.append(ScraperWallapop())
    if ENABLE_COCHES_NET:
        fuentes.append(ScraperCochesNet())
    return fuentes


async def obtener_anuncio_por_url(url: str):
    """
    Resuelve la URL al scraper que la acepta y extrae el Anuncio.
    Por compatibilidad sigue usando obtener_anuncio_wallapop para Wallapop.
    """
    if "wallapop" in (url or "").lower():
        return await obtener_anuncio_wallapop(url)
    for f in _fuentes_activas():
        if hasattr(f, "acepta_url") and f.acepta_url(url):
            try:
                return await f.obtener_anuncio(url)
            except Exception as e:
                logger.error(f"[{f.nombre}] obtener_anuncio falló: {e}")
                return None
    return None


async def buscar_comparables_todas(
    marca: str, modelo: str, año: int, km: int, n: int = 20,
    filtros: dict | None = None,
) -> list:
    """
    Lanza en paralelo las búsquedas de comparables en todas las fuentes ES
    activas. Devuelve lista mergeada y deduplicada de Anuncio.
    """
    tareas, fuentes = [], []
    if ENABLE_WALLAPOP:
        tareas.append(buscar_comparables_wallapop(marca, modelo, año, km, n=n))
        fuentes.append("wallapop")
    if ENABLE_COCHES_NET:
        tareas.append(ScraperCochesNet().buscar_comparables(marca, modelo, año, km, n=n, filtros=filtros))
        fuentes.append("coches.net")
    if not tareas:
        return []

    resultados = await asyncio.gather(*tareas, return_exceptions=True)
    items: list = []
    for nombre, r in zip(fuentes, resultados):
        if isinstance(r, list):
            items.extend(r)
            logger.info(f"[ES] {nombre}: {len(r)} comparables")
        else:
            logger.warning(f"[ES] {nombre} falló: {r}")

    dedup = _dedupe_anuncios(items)
    logger.info(f"[ES] Total comparables: {len(items)} → {len(dedup)} tras dedup")
    return dedup


async def buscar_y_cruzar(
    marca: str, modelo: str, filtros: dict | None = None,
) -> list[dict]:
    coches = await buscar_coches_alemania(marca, modelo, filtros)
    if not coches:
        return []

    cache: dict[tuple, dict] = {}
    for c in coches:
        año, km = c.get("año", 0), c.get("km", 0)
        key = (año, (km // 10_000) * 10_000)
        if key not in cache:
            cache[key] = await buscar_precio_mercado_es(marca, modelo, año, km, filtros)
            await asyncio.sleep(random.uniform(0.8, 1.5))
        res = cache[key]
        c["precio_medio_es"]   = res["precio_medio"]
        c["n_muestras_es"]     = res["n_muestras"]
        c["error_es"]          = res["error"]
        c["precios_usados_es"] = res["precios_usados"]

    return coches