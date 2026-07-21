# mobile_de_api.py — Cliente de la Search-API OFICIAL de mobile.de.
#
# Alternativa al scraper HTML de mobile.de (bloqueado hoy por su WAF). Sin
# credenciales configuradas (MOBILE_DE_API_USER/PASSWORD), este módulo NUNCA
# se activa — cero cambio de comportamiento. Con credenciales, sustituye al
# scraper: mismo contrato (anuncios, señal) que ScraperMobileDe.buscar_deteccion
# y ScraperAutoScout24.buscar_deteccion, así que encaja en detectar_multifuente
# sin tocar worker.py ni sniper_pipeline.py.
#
# Contrato verificado contra la documentación oficial (services.mobile.de/docs/
# search-api.html) el 2026-07-21. NO probado contra el servicio real (sin
# credenciales) — el mapeo marca+modelo→classification es best-effort con
# reintento (ver _buscar_pagina). Antes de activar en producción: correr
# probar_conexion() y revisar los logs de la primera pasada real.
import logging

import httpx

from cabeza_bot.config import (
    MOBILE_DE_API_USER, MOBILE_DE_API_PASSWORD, MOBILE_DE_API_BASE,
    COMBUSTIBLES_MOBILE,
)

logger = logging.getLogger(__name__)

_HEADERS = {"Accept": "application/vnd.de.mobile.api+json"}
_TIMEOUT = httpx.Timeout(15.0, connect=10.0)

# Inverso de COMBUSTIBLES_MOBILE (enum API → nuestra clave interna en minúsculas).
_COMBUSTIBLE_DESDE_API = {v: k for k, v in COMBUSTIBLES_MOBILE.items()
                          if k in ("gasolina", "diesel", "electrico", "hibrido", "glp", "gnc")}


def configurada() -> bool:
    """True si hay credenciales — única condición para que esta fuente se use."""
    return bool(MOBILE_DE_API_USER and MOBILE_DE_API_PASSWORD)


def _auth() -> httpx.BasicAuth:
    return httpx.BasicAuth(MOBILE_DE_API_USER, MOBILE_DE_API_PASSWORD)


def _mapear_filtros(filtros: dict) -> dict:
    """Filtros internos (year_min/km_max/...) → query params documentados de la API."""
    p: dict[str, str] = {}
    if filtros.get("year_min"):
        p["firstRegistrationDate.min"] = f"{int(filtros['year_min'])}-01"
    if filtros.get("year_max"):
        p["firstRegistrationDate.max"] = f"{int(filtros['year_max'])}-12"
    if filtros.get("km_min"):
        p["mileage.min"] = str(int(filtros["km_min"]))
    if filtros.get("km_max"):
        p["mileage.max"] = str(int(filtros["km_max"]))
    if filtros.get("price_min"):
        p["price.min"] = str(int(filtros["price_min"]))
    if filtros.get("price_max"):
        p["price.max"] = str(int(filtros["price_max"]))
    comb = str(filtros.get("combustible", "")).lower().strip()
    if comb in COMBUSTIBLES_MOBILE:
        p["fuel"] = COMBUSTIBLES_MOBILE[comb]
    return p


def _mapear_anuncio(ad: dict) -> dict | None:
    """JSON de un 'ad' de la API → dict común del sniper (mismas claves que AS24)."""
    try:
        precio = float((ad.get("price") or {}).get("consumerPriceGross", 0) or 0)
    except (ValueError, TypeError):
        precio = 0.0
    if precio <= 0:
        return None

    reg = str(ad.get("firstRegistration", "") or "")  # formato "YYYYMM"
    año = int(reg[:4]) if len(reg) >= 4 and reg[:4].isdigit() else 0

    fuel_api = (ad.get("fuel") or "").upper()
    combustible = _COMBUSTIBLE_DESDE_API.get(fuel_api, "")

    seller = ad.get("seller") or {}
    vendedor = "particular" if seller.get("type") == "FOR_SALE_BY_OWNER" else "haendler"

    ad_id = str(ad.get("mobileAdId", "") or "")
    marca = str(ad.get("make", "") or "").title()
    modelo_desc = str(ad.get("modelDescription") or ad.get("model") or "")
    titulo = f"{marca} {modelo_desc}".strip() or "Sin título"

    return {
        "id": ad_id,
        "titulo": titulo,
        "precio": precio,
        "km": int(ad.get("mileage", 0) or 0),
        "año": año,
        "co2": 0.0,  # no viene en la búsqueda (solo en el detalle de un ad) — se estima aguas abajo
        "link": f"https://www.mobile.de/fahrzeuge/details.html?id={ad_id}" if ad_id else "",
        "foto": "",
        "descripcion": "",
        "caja": "",
        "combustible": combustible,
        "carroceria": "",
        "fuente": "mobile.de",
        "vendedor": vendedor,
        "es_netto": False,  # no expuesto en la respuesta de búsqueda
        "reimport": False,
        "unfallfrei": not bool(ad.get("damageUnrepaired")) if "damageUnrepaired" in ad else False,
        "scheckheftgepflegt": False,
        "num_fotos": 0,
        "propietarios": 0,
        "cv": 0,
        "_detalle_completo": True,  # evita que obtener_detalle_candidato navegue a esta URL con selectores de AS24
    }


async def _peticion(params: dict) -> tuple[list[dict], str]:
    """
    Una llamada GET a /search. Devuelve (ads_json, señal) con señal ∈
    {'ok','vacio','fallo'}. 'fallo' incluye 400 (parámetro inválido — el
    caller decide si reintentar) para que el circuit breaker actúe si persiste.
    """
    url = f"{MOBILE_DE_API_BASE}/search"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url, params=params, headers=_HEADERS, auth=_auth())
    except Exception as e:
        logger.error(f"[MOBILE-API] Excepción de red: {e}")
        return [], "fallo"

    if r.status_code == 401:
        logger.error("[MOBILE-API] 401 — credenciales inválidas. Revisa MOBILE_DE_API_USER/PASSWORD.")
        return [], "fallo"
    if r.status_code == 400:
        logger.warning(f"[MOBILE-API] 400 Bad Request (params={params}): {r.text[:200]}")
        return [], "fallo"
    if r.status_code != 200:
        logger.warning(f"[MOBILE-API] HTTP {r.status_code}: {r.text[:200]}")
        return [], "fallo"

    try:
        data = r.json()
    except Exception as e:
        logger.error(f"[MOBILE-API] Respuesta no es JSON válido: {e}")
        return [], "fallo"

    ads = data.get("ads", [])
    return ads, ("ok" if ads else "vacio")


async def buscar_deteccion(marca: str, modelo: str, filtros: dict,
                           limite: int = 40) -> tuple[list[dict], str]:
    """
    Detección para el sniper vía API oficial. Mismo contrato que
    ScraperAutoScout24.buscar_deteccion / ScraperMobileDe.buscar_deteccion:
    (anuncios, señal) con señal ∈ {'ok','vacio','fallo'}.

    El código de modelo de mobile.de (classification) no está documentado
    para todas las marcas/modelos — se intenta marca+modelo primero; si la
    API rechaza esa clasificación (400), se reintenta SOLO con la marca
    (búsqueda más amplia) y se filtra el modelo en Python por texto — mismo
    patrón que la agrupación por marca+modelo del scraper AS24.
    """
    if not configurada():
        return [], "vacio"

    filtros = filtros or {}
    base_params = _mapear_filtros(filtros)
    base_params["sort.field"] = "modificationTime"
    base_params["sort.order"] = "DESCENDING"
    base_params["page.size"] = str(min(limite, 100))

    marca_up = marca.strip().upper()
    modelo_up = modelo.strip().upper().replace(" ", "_")

    # Intento 1: clasificación marca+modelo (funciona si el nombre coincide
    # con la taxonomía real de mobile.de — no garantizado sin probarlo en vivo).
    params = dict(base_params)
    params["classification"] = f"refdata/classes/Car/makes/{marca_up}/models/{modelo_up}"
    ads, señal = await _peticion(params)

    if señal == "fallo":
        # Puede ser el modelo (clasificación inválida) o un fallo real. Reintento
        # SOLO con la marca — si esto TAMBIÉN falla, es un fallo real (credenciales,
        # red, servicio caído) y se propaga tal cual para que el breaker actúe.
        logger.info(f"[MOBILE-API] Clasificación '{marca_up}/{modelo_up}' rechazada, reintentando solo marca")
        params_marca = dict(base_params)
        params_marca["classification"] = f"refdata/classes/Car/makes/{marca_up}"
        ads, señal = await _peticion(params_marca)
        if señal == "fallo":
            return [], "fallo"
        # Filtrado por modelo en Python (texto libre contra model/modelDescription).
        modelo_low = modelo.strip().lower()
        ads = [
            a for a in ads
            if modelo_low in str(a.get("model", "")).lower()
            or modelo_low in str(a.get("modelDescription", "")).lower()
        ]
        señal = "ok" if ads else "vacio"

    anuncios = [a for a in (_mapear_anuncio(ad) for ad in ads) if a is not None]
    return anuncios, señal


async def probar_conexion() -> tuple[bool, str]:
    """
    Diagnóstico manual: UNA petición mínima para validar credenciales antes de
    activar en producción. Úsalo tras configurar MOBILE_DE_API_USER/PASSWORD:

        python -c "import asyncio; from cabeza_bot.scraping.mobile_de_api import probar_conexion; \
                   print(asyncio.run(probar_conexion()))"

    Devuelve (ok, mensaje).
    """
    if not configurada():
        return False, "Sin credenciales (MOBILE_DE_API_USER/MOBILE_DE_API_PASSWORD vacíos)."
    ads, señal = await _peticion({
        "classification": "refdata/classes/Car/makes/VOLKSWAGEN",
        "page.size": "1",
    })
    if señal == "fallo":
        return False, "Fallo (401 credenciales inválidas, o error de red/servicio — revisa logs)."
    if señal == "vacio":
        return True, "Conexión OK pero 0 resultados (raro para VW — revisa la clasificación)."
    return True, f"Conexión OK — {len(ads)} anuncio(s) de ejemplo recibido(s)."
