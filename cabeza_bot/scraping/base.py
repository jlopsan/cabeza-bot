# scraper.py  ─  Multi-source scraping v3
#
# PRINCIPIOS:
#   1. NUNCA navegar fuera del listado durante la extracción de tarjetas
#   2. Detalles se visitan DESPUÉS, solo para top candidatos, en pestañas nuevas
#   3. mobile.de y coches.net: usar su buscador IA con query en texto natural
#   4. Post-filtrar SIEMPRE en código como red de seguridad
#
# Fuentes DE:
#   - AutoScout24: URL params + extracción 2 fases
#   - mobile.de:   query texto alemán → su IA filtra
#
# Fuentes ES (precios):
#   - Wallapop:    API REST
#   - coches.net:  query texto español → su IA filtra
#
import asyncio
import hashlib
import random
import re
import statistics
import logging
from abc import ABC, abstractmethod

import httpx
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# Cap global de browsers Playwright simultáneos. Cada Chromium consume ~300-500MB
# RAM. Más de 2 en paralelo tumba servidores pequeños. Los handlers que llegan
# terceros esperan aquí (no se pierden).
_PLAYWRIGHT_SEM = asyncio.Semaphore(2)

from cabeza_bot.config import (
    USER_AGENTS, PROXIES, TOP_RESULTS, MAX_PAGES_DE, MAX_COCHES_RAW,
    ENABLE_AUTOSCOUT24, ENABLE_MOBILE_DE, ENABLE_WALLAPOP, ENABLE_COCHES_NET,
    WALLAPOP_LATITUDE, WALLAPOP_LONGITUDE, WALLAPOP_DISTANCE, WALLAPOP_RESULTS,
    WALLAPOP_RETRY_MAX, WALLAPOP_APPVERSION, WALLAPOP_MPID, WALLAPOP_DEVICEID,
    COCHES_NET_RESULTS, COCHES_NET_RETRY_MAX,
    AÑO_TOLERANCIA, KM_TOLERANCIA,
    PRECIO_MINIMO_VALIDO, ANTI_SCAM_FACTOR, PRECIO_MEDIO_MUESTRA,
    COLORES_AS24, COLORES_MOBILE,
    CARROCERIAS_AS24, CARROCERIAS_MOBILE,
    COMBUSTIBLES_AS24, COMBUSTIBLES_MOBILE,
    CAJAS_MOBILE, EXTRAS_AEX, EXTRAS_MOBILE,
    MARCAS_MOBILE_ID,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# HELPERS COMUNES
# ════════════════════════════════════════════════════════════════════════════

def _parse_numero(texto: str) -> float:
    """Extrae el primer número de texto con formato europeo (1.234,56 → 1234.56)."""
    texto = texto.replace(".", "").replace(",", ".")
    nums = re.findall(r"\d+(?:\.\d+)?", texto)
    return float(nums[0]) if nums else 0.0


def _generar_id(fuente: str, titulo: str, precio: float, link: str = "") -> str:
    raw = f"{fuente}:{titulo}:{precio}:{link}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _nuevo_contexto_stealth(browser, user_agent: str, proxy_cfg: dict | None, locale: str = "de-DE"):
    return browser.new_context(
        user_agent=user_agent,
        proxy=proxy_cfg,
        locale=locale,
        viewport={"width": 1366, "height": 768},
        extra_http_headers={"Accept-Language": f"{locale},{locale.split('-')[0]};q=0.9,en;q=0.8"},
    )


def _normalizar_keywords_es(marca: str, modelo: str) -> str:
    """Limpia título alemán para búsqueda en portales españoles."""
    marca_clean = marca.strip().title()
    modelo_parts = modelo.strip().split()
    modelo_clean_parts = []
    # Palabras que indican specs técnicos (no parte del nombre del modelo)
    _STOP_WORDS = {"tfsi", "tdi", "cdi", "hdi", "tsi", "bhp", "ps", "kw", "hp"}
    for part in modelo_parts:
        low = part.lower().rstrip("+&°-")
        # Parar en specs técnicos como "35TFSI", "150PS"
        if low in _STOP_WORDS:
            break
        if re.match(r'^\d{2,3}(tfsi|tdi|cdi|ps|kw|hp|cv)$', low):
            break
        if '+' in part or '°' in part or '&' in part:
            break
        modelo_clean_parts.append(part)
        if len(modelo_clean_parts) >= 3:
            break
    modelo_clean = " ".join(modelo_clean_parts).strip() or modelo.split()[0]
    return f"{marca_clean} {modelo_clean}".strip()


def _resolver_extras_aex(extras_usuario: list[str]) -> tuple[list[str], list[str]]:
    if isinstance(extras_usuario, str):
        extras_usuario = [e.strip() for e in extras_usuario.split(",")]
    aex_codes, extras_sin_codigo = [], []
    for extra in extras_usuario:
        extra_low = extra.lower().strip()
        if not extra_low:
            continue
        if extra_low in EXTRAS_AEX:
            aex_codes.append(str(EXTRAS_AEX[extra_low]))
        else:
            encontrado = False
            for key, code in EXTRAS_AEX.items():
                if key in extra_low or extra_low in key:
                    aex_codes.append(str(code))
                    encontrado = True
                    break
            if not encontrado:
                extras_sin_codigo.append(extra)
    return list(dict.fromkeys(aex_codes)), extras_sin_codigo


def _detectar_combustible_titulo(titulo: str) -> str:
    t = titulo.lower()
    if any(x in t for x in ["tdi", "diesel", "diésel", "cdi", "hdi", "dci", "jtd", "cdti", "bluetec"]):
        return "diesel"
    # Híbrido ANTES que eléctrico: "e-hybrid"/"phev" son híbridos, no BEV.
    if any(x in t for x in ["phev", "plug-in", "plugin", "e-hybrid", "tfsi e", "hybrid", "híbrido", "hibrido"]):
        return "hibrido"
    if any(x in t for x in ["elektr", "electric", "e-golf", "e-up", "e-tron", "e-niro", "e-soul",
                             "id.3", "id.4", "id.5", "id.", "ioniq", "model 3", "model y",
                             "zoe", "leaf", "bev", "kwh", " ev "]):
        return "electrico"
    return "gasolina"


# ════════════════════════════════════════════════════════════════════════════
# TRADUCCIÓN DE FILTROS A TEXTO NATURAL
# ════════════════════════════════════════════════════════════════════════════

_FILTRO_A_ALEMAN = {
    "gasolina": "Benzin", "diesel": "Diesel", "electrico": "Elektro",
    "eléctrico": "Elektro", "hibrido": "Hybrid", "híbrido": "Hybrid",
    "glp": "LPG",
    "manual": "Schaltgetriebe", "automatico": "Automatik",
    "automático": "Automatik",
    "cabrio": "Cabrio", "descapotable": "Cabrio", "convertible": "Cabrio",
    "roadster": "Roadster",
    "sedan": "Limousine", "berlina": "Limousine",
    "familiar": "Kombi", "suv": "SUV", "todoterreno": "SUV",
    "coupe": "Coupé", "coupé": "Coupé", "monovolumen": "Van",
    "negro": "Schwarz", "blanco": "Weiß", "gris": "Grau",
    "azul": "Blau", "rojo": "Rot", "plata": "Silber",
    "verde": "Grün", "amarillo": "Gelb", "naranja": "Orange",
    "marron": "Braun", "dorado": "Gold", "morado": "Violett",
    "beige": "Beige",
}


def _construir_query_de(marca: str, modelo: str, filtros: dict) -> str:
    """
    Construye query en alemán para el buscador IA de mobile.de.
    "BMW M3 Cabrio Grau Benzin Schaltgetriebe"
    """
    partes = [marca.title(), modelo.upper()]
    for campo in ("carroceria", "color", "combustible", "caja"):
        valor = str(filtros.get(campo, "")).lower().strip()
        if valor and valor in _FILTRO_A_ALEMAN:
            partes.append(_FILTRO_A_ALEMAN[valor])
    if filtros.get("year_min"):
        partes.append(f"ab {filtros['year_min']}")
    if filtros.get("km_max"):
        partes.append(f"bis {filtros['km_max'] // 1000}tkm")
    query = " ".join(partes)
    logger.info(f"[MOBILE] Query alemán: '{query}'")
    return query


def _construir_query_es(marca: str, modelo: str, filtros: dict) -> str:
    """
    Construye query en español para el buscador IA de coches.net.
    "BMW M3 descapotable gris gasolina manual"
    """
    partes = [marca.title(), modelo.upper()]
    for campo in ("carroceria", "color", "combustible", "caja"):
        valor = str(filtros.get(campo, "")).lower().strip()
        if valor:
            partes.append(valor)
    query = " ".join(partes)
    logger.info(f"[COCHES.NET] Query español: '{query}'")
    return query


# ════════════════════════════════════════════════════════════════════════════
# NORMALIZADORES: texto alemán → valor español estandarizado
# ════════════════════════════════════════════════════════════════════════════

def _normalizar_caja_de(texto: str) -> str:
    t = texto.lower().strip()
    if not t:
        return ""
    if any(x in t for x in ["schalt", "manual", "manuell", "5-gang", "6-gang"]):
        return "manual"
    if any(x in t for x in ["automat", "doppelkuppl", "dsg", "pdk", "tiptronic",
                              "steptronic", "s tronic", "dct", "cvt", "sequential",
                              "halbautom", "semi-auto"]):
        return "automatico"
    return ""


def _normalizar_combustible_de(texto: str) -> str:
    t = texto.lower().strip()
    if not t:
        return ""
    # Híbrido PRIMERO — "Hybrid (Benzin/Elektro)" contiene "benzin"
    if any(x in t for x in ["hybrid", "plug-in"]):
        return "hibrido"
    if any(x in t for x in ["elektro", "electric", "strom"]):
        return "electrico"
    if any(x in t for x in ["benzin", "petrol", "gasoline", "super"]):
        return "gasolina"
    if any(x in t for x in ["diesel", "tdi", "cdi"]):
        return "diesel"
    if any(x in t for x in ["erdgas", "cng", "lpg", "autogas"]):
        return "glp"
    return ""


def _normalizar_carroceria_de(texto: str) -> str:
    t = texto.lower().strip()
    if not t:
        return ""
    if any(x in t for x in ["cabrio", "roadster", "spider", "spyder", "convertible"]):
        return "cabrio"
    if any(x in t for x in ["limousine", "sedan", "saloon", "stufenheck"]):
        return "sedan"
    if any(x in t for x in ["kombi", "estate", "touring", "avant", "variant"]):
        return "familiar"
    if any(x in t for x in ["suv", "geländewagen", "offroad", "crossover"]):
        return "suv"
    if any(x in t for x in ["coupé", "coupe"]):
        return "coupe"
    if any(x in t for x in ["van", "bus", "mpv", "kompaktvan"]):
        return "monovolumen"
    if any(x in t for x in ["pick-up", "pickup"]):
        return "pickup"
    return ""


# ════════════════════════════════════════════════════════════════════════════
# POST-FILTRADO CLIENT-SIDE (red de seguridad)
# ════════════════════════════════════════════════════════════════════════════

def _postfiltrar(coches: list[dict], filtros: dict) -> list[dict]:
    """
    Filtra coches en código después del scraping.
    Red de seguridad: si el portal no respetó un filtro, lo forzamos aquí.
    Beneficio de la duda: campo vacío = pasa.
    """
    if not filtros or not coches:
        return coches

    antes = len(coches)
    resultado = coches

    # Caja
    caja_pedida = str(filtros.get("caja", "")).lower().strip()
    if caja_pedida:
        if caja_pedida in ("automatico", "automático", "auto", "dsg", "pdk"):
            caja_norm = "automatico"
        elif caja_pedida in ("manual", "manuales"):
            caja_norm = "manual"
        else:
            caja_norm = ""
        if caja_norm:
            resultado = [c for c in resultado if not c.get("caja") or c["caja"] == caja_norm]
            logger.info(f"[POSTFILTRO] caja={caja_norm}: {antes} → {len(resultado)}")

    # Combustible
    comb_pedido = str(filtros.get("combustible", "")).lower().strip()
    if comb_pedido:
        antes_c = len(resultado)
        resultado = [c for c in resultado if not c.get("combustible") or c["combustible"] == comb_pedido]
        logger.info(f"[POSTFILTRO] combustible={comb_pedido}: {antes_c} → {len(resultado)}")

    # Carrocería
    carro_pedido = str(filtros.get("carroceria", "")).lower().strip()
    if carro_pedido:
        alias = {
            "descapotable": "cabrio", "convertible": "cabrio", "roadster": "cabrio",
            "berlina": "sedan", "limusina": "sedan",
            "todoterreno": "suv", "crossover": "suv", "4x4": "suv",
            "cupe": "coupe", "coupé": "coupe",
            "kombi": "familiar", "estate": "familiar",
        }
        carro_norm = alias.get(carro_pedido, carro_pedido)
        antes_cr = len(resultado)
        resultado = [c for c in resultado if not c.get("carroceria") or c["carroceria"] == carro_norm]
        logger.info(f"[POSTFILTRO] carroceria={carro_norm}: {antes_cr} → {len(resultado)}")

    # Numéricos
    if filtros.get("km_max"):
        resultado = [c for c in resultado if not c.get("km") or c["km"] <= filtros["km_max"]]
    if filtros.get("km_min"):
        resultado = [c for c in resultado if not c.get("km") or c["km"] >= filtros["km_min"]]
    if filtros.get("year_min"):
        resultado = [c for c in resultado if not c.get("año") or c["año"] >= filtros["year_min"]]
    if filtros.get("year_max"):
        resultado = [c for c in resultado if not c.get("año") or c["año"] <= filtros["year_max"]]
    if filtros.get("price_max"):
        resultado = [c for c in resultado if c["precio"] <= filtros["price_max"]]
    if filtros.get("price_min"):
        resultado = [c for c in resultado if c["precio"] >= filtros["price_min"]]

    logger.info(f"[POSTFILTRO] Total: {antes} → {len(resultado)}")
    return resultado


def postfiltrar(coches: list[dict], filtros: dict) -> list[dict]:
    """
    Alias público de _postfiltrar — usado por el sniper para aplicar los
    filtros ESPECÍFICOS de cada misión en Python, después de un scrapeo
    AMPLIO compartido por marca+modelo (agrupación de misiones). Cero
    scraping extra: el filtrado ocurre sobre datos que ya tenemos.
    """
    return _postfiltrar(coches, filtros)


# ════════════════════════════════════════════════════════════════════════════
# BASE ABSTRACTA
# ════════════════════════════════════════════════════════════════════════════

class ScraperDE(ABC):
    @abstractmethod
    async def buscar(self, marca: str, modelo: str, filtros: dict) -> list[dict]: ...
    @property
    @abstractmethod
    def nombre(self) -> str: ...


def _persistir_de_historico(anuncios: list[dict], marca: str, modelo: str,
                            fuente: str = "autoscout24") -> int:
    """
    Persiste los anuncios DE (dicts del scraper) en historico_precios con la
    `fuente` indicada (autoscout24 / mobile.de). Mismos filtros de calidad que
    ES: precio>0, año>1990. El dataset DE vs ES es un activo del producto —
    también alimenta el semáforo de riesgo (precio anómalo, plausibilidad km).
    """
    from cabeza_bot.models import Anuncio
    from cabeza_bot.data.database import guardar_historico_batch
    lote = []
    for a in anuncios:
        try:
            precio = float(a.get("precio", 0) or 0)
            año    = int(a.get("año", 0) or 0)
        except (ValueError, TypeError):
            continue
        if precio <= 0 or año <= 1990:
            continue
        lote.append(Anuncio(
            item_id=str(a.get("id", "")),
            fuente=fuente,
            marca=marca, modelo=modelo,
            año=año, km=int(a.get("km", 0) or 0), precio=precio,
            provincia="DE", descripcion=a.get("descripcion", ""),
            url=a.get("link", ""), foto=a.get("foto", ""),
            titulo=a.get("titulo", ""),
        ))
    if not lote:
        return 0
    try:
        return guardar_historico_batch(lote)
    except Exception as e:
        logger.warning(f"[AS24] persistir histórico DE falló: {e}")
        return 0

__all__ = ['ABC', 'ANTI_SCAM_FACTOR', 'AÑO_TOLERANCIA', 'CAJAS_MOBILE', 'CARROCERIAS_AS24', 'CARROCERIAS_MOBILE', 'COCHES_NET_RESULTS', 'COCHES_NET_RETRY_MAX', 'COLORES_AS24', 'COLORES_MOBILE', 'COMBUSTIBLES_AS24', 'COMBUSTIBLES_MOBILE', 'ENABLE_AUTOSCOUT24', 'ENABLE_COCHES_NET', 'ENABLE_MOBILE_DE', 'ENABLE_WALLAPOP', 'EXTRAS_AEX', 'EXTRAS_MOBILE', 'KM_TOLERANCIA', 'MARCAS_MOBILE_ID', 'MAX_COCHES_RAW', 'MAX_PAGES_DE', 'PRECIO_MEDIO_MUESTRA', 'PRECIO_MINIMO_VALIDO', 'PROXIES', 'PWTimeout', 'ScraperDE', 'TOP_RESULTS', 'USER_AGENTS', 'WALLAPOP_APPVERSION', 'WALLAPOP_DEVICEID', 'WALLAPOP_DISTANCE', 'WALLAPOP_LATITUDE', 'WALLAPOP_LONGITUDE', 'WALLAPOP_MPID', 'WALLAPOP_RESULTS', 'WALLAPOP_RETRY_MAX', '_FILTRO_A_ALEMAN', '_PLAYWRIGHT_SEM', '_construir_query_de', '_construir_query_es', '_detectar_combustible_titulo', '_generar_id', '_normalizar_caja_de', '_normalizar_carroceria_de', '_normalizar_combustible_de', '_normalizar_keywords_es', '_nuevo_contexto_stealth', '_parse_numero', '_persistir_de_historico', '_postfiltrar', '_resolver_extras_aex', 'abstractmethod', 'async_playwright', 'asyncio', 'hashlib', 'httpx', 'logger', 'postfiltrar', 'random', 're', 'statistics']
