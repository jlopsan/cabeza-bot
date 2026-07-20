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


# ════════════════════════════════════════════════════════════════════════════
# BASE ABSTRACTA
# ════════════════════════════════════════════════════════════════════════════

class ScraperDE(ABC):
    @abstractmethod
    async def buscar(self, marca: str, modelo: str, filtros: dict) -> list[dict]: ...
    @property
    @abstractmethod
    def nombre(self) -> str: ...


# ════════════════════════════════════════════════════════════════════════════
# AUTOSCOUT24.DE  (2 fases: listado → detalles en pestaña nueva)
# ════════════════════════════════════════════════════════════════════════════

class ScraperAutoScout24(ScraperDE):
    nombre = "AutoScout24"
    BASE_URL = "https://www.autoscout24.de/lst"

    SELECTORS = {
        "card":   "article.cldt-summary-full-item",
        "titulo": "h2[class*='ListItemTitle_heading']",
        "precio": "span[data-testid='regular-price']",
        "foto":   "img[src*='prod.pictures.autoscout24.net']",
        "next":   "a[data-testid='pagination-step-forwards']",
    }

    async def buscar(self, marca: str, modelo: str, filtros: dict) -> list[dict]:
        filtros = filtros or {}
        user_agent = random.choice(USER_AGENTS)
        proxy_cfg = {"server": random.choice(PROXIES)} if PROXIES else None
        url_base = self._construir_url(marca, modelo, filtros)

        async with _PLAYWRIGHT_SEM, async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await _nuevo_contexto_stealth(browser, user_agent, proxy_cfg)
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            try:
                # FASE 1: Extraer datos básicos del listado (sin navegar fuera)
                coches = await self._fase1_listado(context, url_base)
                logger.info(f"[AS24] Fase 1: {len(coches)} coches del listado")
                if not coches:
                    return []

                # FASE 2: Visitar detalles solo de top candidatos (pestañas nuevas)
                await self._fase2_detalles(context, coches, marca, modelo)
                logger.info(f"[AS24] Fase 2: detalles completados")
                return coches

            except Exception as e:
                logger.error(f"[AS24] Error general: {e}")
                return []
            finally:
                await browser.close()

    async def _fase1_listado(self, context, url_base: str) -> list[dict]:
        """Extrae datos básicos de TODAS las tarjetas. NUNCA navega a detalles."""
        resultados = []
        page = await context.new_page()
        try:
            for pagina in range(1, MAX_PAGES_DE + 1):
                url = url_base if pagina == 1 else f"{url_base}&page={pagina}"
                logger.info(f"[AS24] Página {pagina}: {url}")

                await page.goto(url, timeout=60_000, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(2.0, 3.5))

                if pagina == 1:
                    await self._aceptar_cookies(page)

                try:
                    await page.wait_for_selector(self.SELECTORS["card"], state="attached", timeout=12_000)
                except Exception:
                    logger.warning(f"[AS24] Sin resultados en página {pagina}")
                    break

                cards = page.locator(self.SELECTORS["card"])
                total = await cards.count()
                logger.info(f"[AS24] Página {pagina}: {total} anuncios")
                if total == 0:
                    break

                for i in range(total):
                    if len(resultados) >= MAX_COCHES_RAW:
                        break
                    coche = await self._extraer_card_basico(cards.nth(i), i)
                    if coche:
                        resultados.append(coche)

                if len(resultados) >= MAX_COCHES_RAW:
                    break
                if not await page.locator(self.SELECTORS["next"]).count():
                    break
        except PWTimeout:
            logger.error("[AS24] Timeout en listado")
        except Exception as e:
            logger.error(f"[AS24] Error listado: {e}")
        finally:
            await page.close()
        return resultados

    async def _extraer_card_basico(self, card, idx: int) -> dict | None:
        """Extrae datos de una tarjeta del listado. NUNCA navega fuera."""
        try:
            coche_id = await card.get_attribute("data-guid") or f"as24_{idx}"

            precio_raw = await card.get_attribute("data-price") or "0"
            try:
                precio = float(precio_raw.replace(".", "").replace(",", "."))
            except (ValueError, TypeError):
                precio = 0.0
            if precio == 0.0:
                try:
                    p_el = card.locator(self.SELECTORS["precio"]).first
                    if await p_el.count():
                        precio = _parse_numero(await p_el.inner_text())
                except Exception:
                    pass
            if precio <= 0:
                return None

            km_raw = await card.get_attribute("data-mileage") or "0"
            km = int(km_raw) if km_raw.isdigit() else 0

            reg_raw = await card.get_attribute("data-first-registration") or "0"
            try:
                if "-" in reg_raw:
                    # Formato puede ser "MM-YYYY" o "YYYY-MM" — tomamos la parte de 4 dígitos
                    parts = reg_raw.split("-")
                    año = next((int(p) for p in parts if len(p) == 4 and p.isdigit()), 0)
                elif "/" in reg_raw:
                    parts = reg_raw.split("/")
                    año = next((int(p) for p in parts if len(p) == 4 and p.isdigit()), 0)
                else:
                    año = int(reg_raw) if reg_raw.isdigit() and len(reg_raw) == 4 else 0
            except (ValueError, TypeError):
                año = 0

            titulo = ""
            try:
                h2 = card.locator(self.SELECTORS["titulo"]).first
                if await h2.count():
                    titulo = " ".join((await h2.inner_text()).split())
            except Exception:
                pass
            if not titulo:
                make = await card.get_attribute("data-make") or ""
                model = await card.get_attribute("data-model") or ""
                titulo = f"{make.title()} {model.upper()}".strip() or "Sin título"

            link_href = ""
            try:
                anchors = card.locator("a[href*='/angebote/']")
                for ai in range(await anchors.count()):
                    href = await anchors.nth(ai).get_attribute("href") or ""
                    if "/angebote/" in href and len(href) > 15:
                        link_href = href
                        break
            except Exception:
                pass
            if not link_href and coche_id and not coche_id.startswith("as24_"):
                link_href = f"/angebote/{coche_id}"
            if link_href and link_href.startswith("/"):
                link_href = f"https://www.autoscout24.de{link_href}"

            foto = ""
            try:
                img = card.locator(self.SELECTORS["foto"]).first
                if await img.count():
                    foto = await img.get_attribute("src") or ""
            except Exception:
                pass

            return {
                "id": coche_id, "titulo": titulo, "precio": precio,
                "km": km, "año": año, "co2": 0.0,
                "link": link_href, "foto": foto, "descripcion": "",
                "caja": "", "combustible": "", "carroceria": "",
                "fuente": "AutoScout24",
            }
        except Exception as e:
            logger.warning(f"[AS24] Error card {idx}: {e}")
            return None

    async def _fase2_detalles(self, context, coches: list[dict],
                               marca: str, modelo: str):
        """Visita detalles de top candidatos en PESTAÑAS NUEVAS. Muta coches in-place."""
        max_detalles = min(len(coches), TOP_RESULTS * 3)
        for coche in coches[:max_detalles]:
            if not coche.get("link"):
                continue
            page = await context.new_page()
            try:
                await page.goto(coche["link"], timeout=25_000, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(0.8, 1.5))

                # CO₂
                for sel in ["dt:has-text('CO₂') + dd", "dt:has-text('CO2') + dd",
                             "span:has-text('g/km')"]:
                    try:
                        elem = page.locator(sel).first
                        if await elem.count():
                            val = _parse_numero(await elem.inner_text())
                            if 50 <= val <= 400:
                                coche["co2"] = val
                                break
                    except Exception:
                        continue

                # Specs técnicos
                try:
                    specs = await page.evaluate("""
                        () => {
                            const r = {};
                            for (const dt of document.querySelectorAll('dt')) {
                                const label = (dt.innerText || '').trim().toLowerCase();
                                const dd = dt.nextElementSibling;
                                if (!dd) continue;
                                const val = (dd.innerText || '').trim();
                                if (label.includes('getriebe'))   r.caja = val;
                                if (label.includes('kraftstoff')) r.combustible = val;
                                if (label.includes('karosserie') || label.includes('aufbau'))
                                    r.carroceria = r.carroceria || val;
                            }
                            return r;
                        }
                    """)
                    if specs:
                        coche["caja"]        = _normalizar_caja_de(specs.get("caja", ""))
                        coche["combustible"] = _normalizar_combustible_de(specs.get("combustible", ""))
                        coche["carroceria"]  = _normalizar_carroceria_de(specs.get("carroceria", ""))
                except Exception as e:
                    logger.debug(f"[AS24] Error specs: {e}")

                # Descripción
                try:
                    txt = await page.evaluate("""
                        () => {
                            var el = document.getElementById('sellerNotesSection');
                            if (el) return el.innerText || '';
                            var els = document.querySelectorAll('[class*="SellerNotesSection"]');
                            return els.length > 0 ? (els[0].innerText || '') : '';
                        }
                    """)
                    if txt and len(txt.strip()) > 30:
                        coche["descripcion"] = txt.strip()[:1500]
                except Exception:
                    pass

            except Exception as e:
                logger.debug(f"[AS24] Error detalle {coche['link']}: {e}")
            finally:
                await page.close()

            # Estimar CO₂ si no se encontró
            if coche["co2"] == 0.0:
                try:
                    from cabeza_bot.analisis.ai import estimar_co2
                    comb = coche.get("combustible") or _detectar_combustible_titulo(coche["titulo"])
                    coche["co2"] = await estimar_co2(marca, modelo, coche["año"], comb)
                except Exception:
                    pass
            await asyncio.sleep(random.uniform(0.3, 0.8))

    def _construir_url(self, marca: str, modelo: str, filtros: dict) -> str:
        # AutoScout24 usa guiones en la ruta: /lst/volkswagen/golf-gti
        marca_slug = marca.lower().strip().replace(" ", "-")
        modelo_slug = modelo.lower().strip().replace(" ", "-")
        ruta = f"{marca_slug}/{modelo_slug}"
        params = ["sort=standard", "desc=0", "ustate=N,U"]
        mapa = {
            "km_max": "kmto", "km_min": "kmfrom",
            "year_min": "fregfrom", "year_max": "fregto",
            "price_max": "priceto", "price_min": "pricefrom",
            "power_min": "powerfrom", "power_max": "powerto",
            "doors": "doors",
        }
        for kf, ku in mapa.items():
            if filtros.get(kf):
                params.append(f"{ku}={filtros[kf]}")

        color = str(filtros.get("color", "")).lower().strip()
        if color in COLORES_AS24:
            params.append(f"extcol={COLORES_AS24[color]}")

        carro = str(filtros.get("carroceria", "")).lower().strip()
        if carro in CARROCERIAS_AS24:
            params.append(f"body={CARROCERIAS_AS24[carro]}")

        comb = str(filtros.get("combustible", "")).lower().strip()
        if comb in COMBUSTIBLES_AS24:
            params.append(f"fuel={COMBUSTIBLES_AS24[comb]}")

        caja = str(filtros.get("caja", "")).lower().strip()
        if caja in ("automatico", "automático", "auto", "dsg", "pdk"):
            params.append("gear=A")
        elif caja in ("manual", "manuales"):
            params.append("gear=M")

        extras = filtros.get("extras", [])
        if extras:
            aex_codes, _ = _resolver_extras_aex(extras)
            if aex_codes:
                params.append(f"aex={','.join(aex_codes)}")

        return f"{self.BASE_URL}/{ruta}?{'&'.join(params)}"

    async def _aceptar_cookies(self, page):
        for sel in ["button:has-text('Alle akzeptieren')",
                     "button[data-testid='as24-cmp-accept-all-button']",
                     "button#didomi-notice-agree-button"]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=3_000):
                    await btn.click()
                    await asyncio.sleep(1.5)
                    break
            except Exception:
                continue

    # ── SNIPER: detección barata (fase 1) + URL normalizada para agrupar ──────

    def url_deteccion_normalizada(self, marca: str, modelo: str, filtros: dict) -> str:
        """
        URL de detección DETERMINISTA (params ordenados) para que el worker
        agrupe misiones con filtros equivalentes en un solo scrapeo.
        Ordena por publicación reciente (sort=age&desc=1).
        """
        filtros = filtros or {}
        marca_slug  = marca.lower().strip().replace(" ", "-")
        modelo_slug = modelo.lower().strip().replace(" ", "-")
        ruta = f"{marca_slug}/{modelo_slug}"

        params: dict[str, str] = {"sort": "age", "desc": "1", "ustate": "N,U"}
        mapa = {
            "km_max": "kmto", "km_min": "kmfrom",
            "year_min": "fregfrom", "year_max": "fregto",
            "price_max": "priceto", "price_min": "pricefrom",
            "power_min": "powerfrom", "power_max": "powerto",
        }
        for kf, ku in mapa.items():
            if filtros.get(kf):
                params[ku] = str(filtros[kf])

        comb = str(filtros.get("combustible", "")).lower().strip()
        if comb in COMBUSTIBLES_AS24:
            params["fuel"] = COMBUSTIBLES_AS24[comb]
        caja = str(filtros.get("caja", "")).lower().strip()
        if caja in ("automatico", "automático", "auto", "dsg", "pdk"):
            params["gear"] = "A"
        elif caja in ("manual", "manuales"):
            params["gear"] = "M"

        qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
        return f"{self.BASE_URL}/{ruta}?{qs}"

    async def buscar_deteccion(self, marca: str, modelo: str, filtros: dict,
                               paginas: int | None = None) -> tuple[list[dict], str]:
        """
        Detección barata para el sniper: SOLO fase 1 (listado) ordenado por más
        reciente. Devuelve (anuncios, señal) con señal ∈ {'ok','vacio','fallo'}:
          - 'ok'    : HTML válido con anuncios.
          - 'vacio' : HTML válido, 0 anuncios (mercado sin stock — NO es fallo).
          - 'fallo' : excepción/timeout/estructura rota (el breaker puede actuar).
        """
        from cabeza_bot.config import SNIPER_DETECCION_PAGINAS
        filtros = filtros or {}
        paginas = paginas or SNIPER_DETECCION_PAGINAS
        user_agent = random.choice(USER_AGENTS)
        proxy_cfg = {"server": random.choice(PROXIES)} if PROXIES else None
        url_base = self.url_deteccion_normalizada(marca, modelo, filtros)

        async with _PLAYWRIGHT_SEM, async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await _nuevo_contexto_stealth(browser, user_agent, proxy_cfg)
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            try:
                anuncios, estructura_ok = await self._fase1_deteccion(context, url_base, paginas)
                if not estructura_ok:
                    logger.warning(f"[AS24] Detección: estructura inesperada en {url_base}")
                    return [], "fallo"
                if anuncios:
                    _persistir_de_historico(anuncios, marca, modelo)
                    return anuncios, "ok"
                return [], "vacio"
            except Exception as e:
                logger.error(f"[AS24] Detección falló: {e}")
                return [], "fallo"
            finally:
                await browser.close()

    async def _fase1_deteccion(self, context, url_base: str,
                               paginas: int) -> tuple[list[dict], bool]:
        """
        Extrae el listado (sin visitar detalles) de las primeras `paginas`.
        Devuelve (anuncios, estructura_ok). estructura_ok=False solo si la página
        cargó pero NO tiene ni tarjetas ni landmark de resultados (HTML roto/bloqueo).
        """
        resultados: list[dict] = []
        estructura_ok = True
        page = await context.new_page()
        try:
            for pagina in range(1, paginas + 1):
                url = url_base if pagina == 1 else f"{url_base}&page={pagina}"
                await page.goto(url, timeout=60_000, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(1.5, 3.0))  # jitter
                if pagina == 1:
                    await self._aceptar_cookies(page)

                tiene_cards = False
                try:
                    await page.wait_for_selector(self.SELECTORS["card"], state="attached", timeout=10_000)
                    tiene_cards = True
                except Exception:
                    tiene_cards = False

                if not tiene_cards:
                    # ¿vacío legítimo o estructura rota? Buscamos landmark de resultados.
                    if pagina == 1:
                        estructura_ok = await self._pagina_tiene_landmark(page)
                    break

                cards = page.locator(self.SELECTORS["card"])
                total = await cards.count()
                for i in range(total):
                    if len(resultados) >= MAX_COCHES_RAW:
                        break
                    coche = await self._extraer_card_basico(cards.nth(i), i)
                    if coche:
                        resultados.append(coche)

                if len(resultados) >= MAX_COCHES_RAW:
                    break
                if not await page.locator(self.SELECTORS["next"]).count():
                    break
        except PWTimeout:
            logger.error("[AS24] Timeout en detección")
            estructura_ok = False
        except Exception as e:
            logger.error(f"[AS24] Error detección listado: {e}")
            estructura_ok = False
        finally:
            await page.close()
        return resultados, estructura_ok

    async def _pagina_tiene_landmark(self, page) -> bool:
        """
        True si la página parece una búsqueda válida (aunque sin resultados):
        distingue 'mercado vacío' (True) de 'HTML roto/bloqueo' (False).
        """
        # Texto alemán típico de "sin resultados" → vacío legítimo.
        try:
            body = (await page.inner_text("body"))[:4000].lower()
        except Exception:
            return False
        señales_vacio = ["keine fahrzeuge", "0 angebote", "keine angebote",
                          "leider", "keine treffer"]
        if any(s in body for s in señales_vacio):
            return True
        # Landmark de la página de resultados (cabecera/formulario de búsqueda).
        for sel in ["[class*='ListHeader']", "[data-testid*='SortDropdown']",
                     "[class*='ListPage']", "form[role='search']", "h1"]:
            try:
                if await page.locator(sel).first.count():
                    return True
            except Exception:
                continue
        return False

    async def obtener_detalle_candidato(self, coche: dict) -> dict:
        """
        Visita el detalle de UN candidato y rellena campos para la cuenta:
        co2, potencia (PS), nº propietarios, vendedor (haendler/particular),
        es_netto (MwSt. ausweisbar), caja, combustible, descripción.
        NUNCA llama a IA. Muta y devuelve el dict.
        """
        coche.setdefault("cv", 0)
        coche.setdefault("propietarios", 0)
        coche.setdefault("vendedor", "")
        coche.setdefault("es_netto", False)
        link = coche.get("link")
        if not link:
            return coche
        user_agent = random.choice(USER_AGENTS)
        proxy_cfg = {"server": random.choice(PROXIES)} if PROXIES else None
        async with _PLAYWRIGHT_SEM, async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await _nuevo_contexto_stealth(browser, user_agent, proxy_cfg)
            page = await context.new_page()
            try:
                await page.goto(link, timeout=25_000, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(0.8, 1.5))

                # CO₂
                for sel in ["dt:has-text('CO₂') + dd", "dt:has-text('CO2') + dd",
                             "span:has-text('g/km')"]:
                    try:
                        elem = page.locator(sel).first
                        if await elem.count():
                            val = _parse_numero(await elem.inner_text())
                            if 20 <= val <= 400:
                                coche["co2"] = val
                                break
                    except Exception:
                        continue

                datos = await page.evaluate("""
                    () => {
                        const r = {};
                        for (const dt of document.querySelectorAll('dt')) {
                            const label = (dt.innerText || '').trim().toLowerCase();
                            const dd = dt.nextElementSibling;
                            if (!dd) continue;
                            const val = (dd.innerText || '').trim();
                            if (label.includes('getriebe'))   r.caja = val;
                            if (label.includes('kraftstoff')) r.combustible = val;
                            if (label.includes('leistung'))   r.potencia = val;
                            if (label.includes('fahrzeughalter')) r.propietarios = val;
                        }
                        const t = (document.body.innerText || '').toLowerCase();
                        r.netto = t.includes('mwst. ausweisbar') || t.includes('mwst ausweisbar')
                                  || t.includes('nettopreis');
                        r.privado = t.includes('privatverkäufer') || t.includes('privatanbieter')
                                    || t.includes('privatverkauf');
                        return r;
                    }
                """)
                if datos:
                    coche["caja"]        = _normalizar_caja_de(datos.get("caja", "")) or coche.get("caja", "")
                    coche["combustible"] = _normalizar_combustible_de(datos.get("combustible", "")) or coche.get("combustible", "")
                    # Potencia: AS24 muestra "85 kW (116 PS)". Preferimos los PS;
                    # si solo hay kW, convertimos (1 kW ≈ 1,36 CV).
                    pot_txt = datos.get("potencia", "") or ""
                    m_ps = re.search(r"(\d{2,4})\s*PS", pot_txt)
                    if m_ps:
                        coche["cv"] = int(m_ps.group(1))
                    elif "kw" in pot_txt.lower():
                        kw = _parse_numero(pot_txt)
                        if kw > 0:
                            coche["cv"] = round(kw * 1.35962)
                    else:
                        n = _parse_numero(pot_txt)
                        if n > 0:
                            coche["cv"] = int(n)
                    prop = _parse_numero(datos.get("propietarios", "") or "0")
                    if prop > 0:
                        coche["propietarios"] = int(prop)
                    coche["es_netto"] = bool(datos.get("netto"))
                    coche["vendedor"] = "particular" if datos.get("privado") else "haendler"
            except Exception as e:
                logger.debug(f"[AS24] Detalle candidato falló {link}: {e}")
            finally:
                await browser.close()
        # Fallback de combustible por título (clave para el IEDMT si falta CO₂:
        # un eléctrico paga 0%, no el tramo por defecto).
        if not coche.get("combustible"):
            coche["combustible"] = _detectar_combustible_titulo(coche.get("titulo", ""))
        return coche


# ════════════════════════════════════════════════════════════════════════════
# MOBILE.DE  (Query texto alemán → su buscador IA filtra)
# ════════════════════════════════════════════════════════════════════════════

class ScraperMobileDe(ScraperDE):
    nombre = "mobile.de"
    # mobile.de usa URLs SEO: /auto/volkswagen-golf-gti.html
    BASE_URL = "https://suchen.mobile.de/auto"

    async def buscar(self, marca: str, modelo: str, filtros: dict) -> list[dict]:
        filtros = filtros or {}
        user_agent = random.choice(USER_AGENTS)
        proxy_cfg = {"server": random.choice(PROXIES)} if PROXIES else None
        resultados: list[dict] = []

        # URL SEO: /auto/volkswagen-golf-gti.html
        marca_slug = marca.lower().strip().replace(" ", "-")
        modelo_slug = modelo.lower().strip().replace(" ", "-")
        url = f"{self.BASE_URL}/{marca_slug}-{modelo_slug}.html"

        # Filtros como query params
        params = []
        if filtros.get("km_max"):    params.append(f"ml=:{filtros['km_max']}")
        if filtros.get("km_min"):    params.append(f"ml={filtros['km_min']}:")

        # year: combinar min y max en un solo param fr=MIN:MAX
        yr_min = filtros.get("year_min", "")
        yr_max = filtros.get("year_max", "")
        if yr_min or yr_max:
            params.append(f"fr={yr_min or ''}:{yr_max or ''}")

        # price: combinar min y max en un solo param p=MIN:MAX
        pr_min = filtros.get("price_min", "")
        pr_max = filtros.get("price_max", "")
        if pr_min or pr_max:
            params.append(f"p={pr_min or ''}:{pr_max or ''}")

        comb = str(filtros.get("combustible", "")).lower().strip()
        if comb in COMBUSTIBLES_MOBILE:
            params.append(f"ft={COMBUSTIBLES_MOBILE[comb]}")
        caja = str(filtros.get("caja", "")).lower().strip()
        if caja in CAJAS_MOBILE:
            params.append(f"tr={CAJAS_MOBILE[caja]}")
        color = str(filtros.get("color", "")).lower().strip()
        if color in COLORES_MOBILE:
            params.append(f"clr={COLORES_MOBILE[color]}")
        carro = str(filtros.get("carroceria", "")).lower().strip()
        if carro in CARROCERIAS_MOBILE:
            params.append(f"bod={CARROCERIAS_MOBILE[carro]}")

        # Extras / equipamiento
        extras = filtros.get("extras", [])
        if extras:
            for extra in extras:
                extra_low = extra.lower().strip()
                if extra_low in EXTRAS_MOBILE:
                    params.append(f"feat={EXTRAS_MOBILE[extra_low]}")
                else:
                    for key, code in EXTRAS_MOBILE.items():
                        if key in extra_low or extra_low in key:
                            params.append(f"feat={code}")
                            break

        if params:
            url += "?" + "&".join(params)

        logger.info(f"[MOBILE] URL: {url}")

        async with _PLAYWRIGHT_SEM, async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await _nuevo_contexto_stealth(browser, user_agent, proxy_cfg)
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            try:
                page = await context.new_page()
                await page.goto(url, timeout=60_000, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(2.5, 4.0))

                # Consent banner
                for sel in ["button#mde-consent-accept-btn",
                             "button:has-text('Alle akzeptieren')",
                             "button:has-text('Einverstanden')",
                             "#gdpr-consent-accept-btn",
                             "button[class*='accept']"]:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible(timeout=3_000):
                            await btn.click()
                            await asyncio.sleep(1.5)
                            break
                    except Exception:
                        continue

                # Recoger URLs de detalle — probar múltiples selectores
                detail_urls = set()
                for link_sel in [
                    "a[href*='/fahrzeuge/details']",
                    "a[href*='fahrzeuge/details.html']",
                    "a[data-testid*='result']",
                    "a[class*='result']",
                    "a[href*='id='][href*='.html']",
                ]:
                    els = page.locator(link_sel)
                    n = await els.count()
                    if n > 0:
                        logger.info(f"[MOBILE] Selector '{link_sel}': {n} links")
                        for i in range(n):
                            try:
                                href = await els.nth(i).get_attribute("href") or ""
                                if href and len(href) > 20:
                                    if not href.startswith("http"):
                                        href = f"https://suchen.mobile.de{href}"
                                    detail_urls.add(href)
                            except Exception:
                                continue
                        if detail_urls:
                            break

                logger.info(f"[MOBILE] {len(detail_urls)} URLs de detalle")

                if not detail_urls:
                    try:
                        await page.screenshot(path="debug_mobile.png", full_page=True)
                        logger.warning(f"[MOBILE] 0 resultados. URL final: {page.url}")
                    except Exception:
                        pass

                await page.close()

                # FASE 2: Visitar cada detalle en pestaña nueva
                for detail_url in list(detail_urls)[:MAX_COCHES_RAW]:
                    coche = await self._extraer_detalle(context, detail_url, marca, modelo)
                    if coche:
                        resultados.append(coche)
                    await asyncio.sleep(random.uniform(0.3, 0.8))

            except PWTimeout:
                logger.error("[MOBILE] Timeout")
            except Exception as e:
                logger.error(f"[MOBILE] Error: {e}")
            finally:
                await browser.close()

        logger.info(f"[MOBILE] Total extraídos: {len(resultados)}")
        return resultados

    async def _extraer_detalle(self, context, url: str, marca: str, modelo: str) -> dict | None:
        page = await context.new_page()
        try:
            await page.goto(url, timeout=25_000, wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(1.0, 2.0))

            titulo = ""
            for sel in ["h1#ad-title", "h1[class*='title']", "h1"]:
                try:
                    elem = page.locator(sel).first
                    if await elem.count():
                        titulo = (await elem.inner_text()).strip()
                        if titulo:
                            break
                except Exception:
                    continue
            titulo = titulo or "Sin título"

            precio = 0.0
            for sel in ["span.h3.u-block", "span[data-testid='price']",
                         "div[class*='price'] span", "span[class*='PriceInfo']"]:
                try:
                    elem = page.locator(sel).first
                    if await elem.count():
                        val = _parse_numero(await elem.inner_text())
                        if val > 500:
                            precio = val
                            break
                except Exception:
                    continue
            if precio <= 0:
                return None

            datos = await page.evaluate("""
                () => {
                    const r = {km:'', year:'', co2:'', caja:'', combustible:'', carroceria:''};
                    const byId = (id) => {
                        const el = document.getElementById(id) ||
                                   document.querySelector('[id*="'+id+'"]');
                        return el ? (el.innerText || '').trim() : '';
                    };
                    r.km = byId('mileage-v');
                    r.year = byId('firstRegistration-v');
                    r.co2 = byId('co2-v');
                    r.caja = byId('transmission-v');
                    r.combustible = byId('fuel-v');
                    r.carroceria = byId('category-v');
                    if (!r.caja || !r.combustible) {
                        for (const dt of document.querySelectorAll('dt')) {
                            const label = (dt.innerText||'').trim().toLowerCase();
                            const dd = dt.nextElementSibling;
                            if (!dd) continue;
                            const val = (dd.innerText||'').trim();
                            if (label.includes('getriebe') && !r.caja) r.caja = val;
                            if (label.includes('kraftstoff') && !r.combustible) r.combustible = val;
                            if ((label.includes('fahrzeugtyp')||label.includes('karosserie'))
                                && !r.carroceria) r.carroceria = val;
                        }
                    }
                    return r;
                }
            """)

            km = int(_parse_numero(datos.get("km", ""))) if datos.get("km") else 0
            año = 0
            if datos.get("year"):
                years = re.findall(r"(20\d{2}|19\d{2})", datos["year"])
                año = int(years[0]) if years else 0

            co2 = 0.0
            if datos.get("co2"):
                v = _parse_numero(datos["co2"])
                co2 = v if 50 <= v <= 400 else 0.0
            if co2 == 0.0:
                try:
                    from cabeza_bot.analisis.ai import estimar_co2
                    comb = _normalizar_combustible_de(datos.get("combustible", "")) or _detectar_combustible_titulo(titulo)
                    co2 = await estimar_co2(marca, modelo, año, comb)
                except Exception:
                    pass

            descripcion = ""
            try:
                txt = await page.evaluate("""
                    () => {
                        const el = document.querySelector('[class*="description-text"]') ||
                                   document.querySelector('[class*="vehicle-description"]') ||
                                   document.getElementById('seller-notes');
                        return el ? (el.innerText || '') : '';
                    }
                """)
                if txt and len(txt.strip()) > 20:
                    descripcion = txt.strip()[:1500]
            except Exception:
                pass

            foto = ""
            try:
                img = page.locator("img[src*='img.classistatic.de']").first
                if await img.count():
                    foto = await img.get_attribute("src") or ""
            except Exception:
                pass

            return {
                "id":          _generar_id("mobile", titulo, precio, url),
                "titulo":      titulo,
                "precio":      precio,
                "km":          km,
                "año":         año,
                "co2":         co2,
                "link":        url,
                "foto":        foto,
                "descripcion": descripcion,
                "caja":        _normalizar_caja_de(datos.get("caja", "")),
                "combustible": _normalizar_combustible_de(datos.get("combustible", "")),
                "carroceria":  _normalizar_carroceria_de(datos.get("carroceria", "")),
                "fuente":      "mobile.de",
            }
        except Exception as e:
            logger.warning(f"[MOBILE] Error detalle {url}: {e}")
            return None
        finally:
            await page.close()


# ════════════════════════════════════════════════════════════════════════════
# WALLAPOP API  (sin Playwright)
# ════════════════════════════════════════════════════════════════════════════

class ScraperWallapop:
    nombre = "Wallapop"
    _API_URL = "https://api.wallapop.com/api/v3/search/section"
    _HEADERS = {
        "Accept":             "application/json, text/plain, */*",
        "Accept-Language":    "es,es-ES;q=0.9,en;q=0.8",
        "Origin":             "https://es.wallapop.com",
        "Referer":            "https://es.wallapop.com/",
        "User-Agent":         ("Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/145.0.0.0 Mobile Safari/537.36"),
        "deviceos":           "0",
        "mpid":               WALLAPOP_MPID,
        "x-appversion":       WALLAPOP_APPVERSION,
        "x-deviceid":         WALLAPOP_DEVICEID,
        "x-deviceos":         "0",
        "sec-ch-ua-mobile":   "?1",
        "Sec-Fetch-Dest":     "empty",
        "Sec-Fetch-Mode":     "cors",
        "Sec-Fetch-Site":     "same-site",
    }

    async def buscar_precios(self, marca: str, modelo: str, año: int, km: int) -> dict:
        try:
            from cabeza_bot.analisis.ai import normalizar_modelo_wallapop
            modelo_base = await normalizar_modelo_wallapop(marca, modelo)
            keywords = f"{marca.strip().title()} {modelo_base}"
        except Exception:
            keywords = _normalizar_keywords_es(marca, modelo)
        logger.info(f"[Wallapop] Buscando: '{keywords}' (año±{AÑO_TOLERANCIA}, km≤{km + KM_TOLERANCIA})")

        params = {
            "keywords": keywords, "source": "search_box",
            "latitude": WALLAPOP_LATITUDE, "longitude": WALLAPOP_LONGITUDE,
            "distance": WALLAPOP_DISTANCE, "order_by": "price_low_to_high",
            "category_id": 100, "section_type": "organic_search_results",
            "min_year": año - AÑO_TOLERANCIA, "max_year": año + AÑO_TOLERANCIA,
            "max_km": km + KM_TOLERANCIA, "items_count": WALLAPOP_RESULTS,
        }

        data = await self._fetch(params)
        items = self._extraer_items(data)
        if not items:
            logger.warning("[Wallapop] Reintentando sin año/km")
            params2 = {k: v for k, v in params.items() if k not in ("min_year", "max_year", "max_km")}
            items = self._extraer_items(await self._fetch(params2))
        if not items:
            return self._vacio("Sin resultados en Wallapop")

        precios = [p for it in items if (p := self._extraer_precio(it)) and p > 0]
        logger.info(f"[Wallapop] {len(precios)} precios: {precios[:8]}")
        return self._calcular_precio_medio(precios)

    async def _fetch(self, params: dict) -> dict:
        delays = [0, 2, 5]
        for intento in range(WALLAPOP_RETRY_MAX):
            delay = delays[intento] if intento < len(delays) else delays[-1]
            if delay:
                await asyncio.sleep(delay)
            try:
                async with httpx.AsyncClient(timeout=20, headers=self._HEADERS) as c:
                    r = await c.get(self._API_URL, params=params)
                    if r.status_code == 429:
                        logger.warning(f"[Wallapop] Rate limit (429) — esperando 10s")
                        await asyncio.sleep(10)
                        continue
                    if r.status_code >= 500:
                        logger.warning(
                            f"[Wallapop] Intento {intento+1}/{WALLAPOP_RETRY_MAX} fallido: "
                            f"HTTP {r.status_code}"
                        )
                        continue
                    r.raise_for_status()
                    data = r.json()
                    if intento > 0:
                        logger.info(f"[Wallapop] Éxito en intento {intento+1}")
                    return data
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.TimeoutException) as e:
                logger.warning(
                    f"[Wallapop] Intento {intento+1}/{WALLAPOP_RETRY_MAX} fallido: {e}"
                )
            except Exception as e:
                logger.error(f"[Wallapop] Error no recuperable: {e}")
                return {}
        logger.error(f"[Wallapop] _fetch agotó {WALLAPOP_RETRY_MAX} intentos")
        return {}

    @staticmethod
    def _extraer_items(data: dict) -> list:
        if not data:
            return []
        for path in [
            lambda d: d.get("data", {}).get("section", {}).get("items", []),
            lambda d: d.get("search_objects"),
            lambda d: d.get("data", {}).get("section", {}).get("payload", {}).get("items", []),
        ]:
            items = path(data)
            if isinstance(items, list) and items:
                return items
        return []

    @staticmethod
    def _extraer_precio(item: dict) -> float | None:
        for fn in [
            lambda i: float(i["content"]["price"]["amount"]),
            lambda i: float(i["content"]["price"]) if isinstance(i["content"]["price"], (int, float)) else None,
            lambda i: float(i["price"]["amount"]),
            lambda i: float(i["price"]) if isinstance(i["price"], (int, float)) else None,
            lambda i: float(i["sale_price"]),
        ]:
            try:
                p = fn(item)
                if p and p > 0: return p
            except (KeyError, TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _calcular_precio_medio(precios_raw: list[float]) -> dict:
        if not precios_raw:
            return ScraperWallapop._vacio("Sin precios")
        precios = [p for p in precios_raw if p >= PRECIO_MINIMO_VALIDO]
        if not precios:
            return ScraperWallapop._vacio("Precios bajo mínimo")
        med = statistics.median(precios)
        precios = [p for p in precios if p >= med * ANTI_SCAM_FACTOR]
        if not precios:
            return ScraperWallapop._vacio("Anti-scam filtró todos")
        precios.sort()
        muestra = precios[:PRECIO_MEDIO_MUESTRA]
        return {
            "precio_medio": round(statistics.mean(muestra), 2),
            "n_muestras": len(muestra),
            "precios_raw": precios_raw, "precios_usados": muestra, "error": None,
        }

    @staticmethod
    def _vacio(error: str) -> dict:
        return {"precio_medio": 0.0, "n_muestras": 0,
                "precios_raw": [], "precios_usados": [], "error": error}

    # ── Extracción de anuncio individual ────────────────────────────────────

    async def obtener_item(self, item_id: str, url_pagina: str = ""):
        """
        Extrae un anuncio individual de Wallapop. 4 estrategias en cascada.
        Para en la primera que devuelva un Anuncio válido.
        """
        slug = url_pagina.split("/item/")[-1] if "/item/" in url_pagina else item_id
        slug_limpio = slug.split("?")[0]
        url = url_pagina or f"https://es.wallapop.com/item/{slug_limpio}"
        fallos: list[str] = []

        params_base = {
            "source": "search_box",
            "latitude": WALLAPOP_LATITUDE, "longitude": WALLAPOP_LONGITUDE,
            "distance": WALLAPOP_DISTANCE, "order_by": "newest",
            "category_id": 100, "section_type": "organic_search_results",
            "items_count": 50,
        }

        # ── S1: keywords del slug → match por web_slug ──────────────────────
        logger.info(f"[Wallapop S1] item_id={item_id} slug='{slug_limpio}'")
        slug_sin_id = re.sub(r"-\d{6,}$", "", slug_limpio)
        keywords_s1 = " ".join(p for p in slug_sin_id.split("-") if p) or slug_sin_id or item_id
        data_s1 = await self._fetch({**params_base, "keywords": keywords_s1})
        items_s1 = self._extraer_items(data_s1)
        for it in items_s1:
            ws = it.get("web_slug", "")
            if ws.endswith(f"-{item_id}") or ws == slug_limpio or item_id in ws:
                logger.info(f"[Wallapop S1 OK] Encontrado por web_slug match")
                return self._item_a_anuncio(it, item_id, url_pagina=url)
        fallos.append(f"S1: {len(items_s1)} resultados, ninguno con web_slug terminando en -{item_id}")

        # ── S2: item_id numérico como keyword directa ───────────────────────
        logger.info(f"[Wallapop S2] Buscando item_id='{item_id}' como keyword")
        data_s2 = await self._fetch({**params_base, "keywords": item_id})
        items_s2 = self._extraer_items(data_s2)
        for it in items_s2:
            ws = it.get("web_slug", "")
            if ws.endswith(f"-{item_id}") or item_id in ws:
                logger.info(f"[Wallapop S2 OK] Encontrado por ID como keyword")
                return self._item_a_anuncio(it, item_id, url_pagina=url)
        fallos.append(f"S2: {len(items_s2)} resultados, ningún web_slug con {item_id}")

        # ── S3: endpoint REST público por ID numérico ───────────────────────
        logger.info(f"[Wallapop S3] GET /api/v3/items/{item_id}")
        try:
            async with httpx.AsyncClient(timeout=15, headers=self._HEADERS) as c:
                r = await c.get(f"https://api.wallapop.com/api/v3/items/{item_id}")
            if r.status_code == 200:
                d = r.json()
                precio_s3 = 0.0
                p = d.get("price") or {}
                if isinstance(p, dict):
                    precio_s3 = float(p.get("amount") or p.get("value") or 0)
                elif isinstance(p, (int, float)):
                    precio_s3 = float(p)
                if precio_s3 > 0:
                    logger.info(f"[Wallapop S3 OK] precio={precio_s3:.0f}€")
                    return self._item_a_anuncio(d, item_id, url_pagina=url)
                fallos.append(f"S3: HTTP 200 pero precio=0 en la respuesta")
            else:
                fallos.append(f"S3: HTTP {r.status_code}")
        except Exception as e:
            fallos.append(f"S3: excepción {e}")

        # ── S4: httpx sobre la web + extracción __NEXT_DATA__ ───────────────
        logger.info(f"[Wallapop S4] Extrayendo __NEXT_DATA__ de {url}")
        try:
            hdrs_web = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/124.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "es-ES,es;q=0.9",
            }
            async with httpx.AsyncClient(timeout=20, headers=hdrs_web,
                                         follow_redirects=True) as c:
                r = await c.get(url)
            if r.status_code == 200 and len(r.text) > 5_000:
                import json as _json
                m = re.search(
                    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                    r.text, re.DOTALL
                )
                if m:
                    nd = _json.loads(m.group(1))
                    # Wallapop Next.js: props.pageProps.item o props.pageProps.ad
                    item_data = (
                        (nd.get("props") or {}).get("pageProps") or {}
                    )
                    item_node = (item_data.get("item")
                                 or item_data.get("ad")
                                 or item_data.get("listing")
                                 or {})
                    if isinstance(item_node, dict) and item_node:
                        anuncio = self._item_a_anuncio(item_node, item_id, url_pagina=url)
                        if anuncio and anuncio.precio > 0:
                            logger.info(f"[Wallapop S4 OK] precio={anuncio.precio:.0f}€ vía __NEXT_DATA__")
                            return anuncio
                        fallos.append("S4: __NEXT_DATA__ encontrado pero precio=0")
                    else:
                        fallos.append("S4: __NEXT_DATA__ presente pero sin item_node reconocible")
                else:
                    fallos.append("S4: HTML recibido pero sin __NEXT_DATA__")
            else:
                fallos.append(f"S4: HTTP {r.status_code} o HTML < 5KB")
        except Exception as e:
            fallos.append(f"S4: excepción {e}")

        logger.error(
            f"[Wallapop] No se pudo obtener {item_id}. "
            + " | ".join(fallos)
        )
        return None

    async def buscar_items(
        self, keywords: str, año: int, km: int, n: int = 30,
        km_tolerancia: int = 20_000, año_tolerancia: int = 1,
        order_by: str = "newest",
    ) -> list:
        """
        Busca anuncios en Wallapop y devuelve lista de Anuncio.
        order_by: "newest" (default) o "price_low_to_high" para sondear baratos.
        """
        params = {
            "keywords": keywords, "source": "search_box",
            "latitude": WALLAPOP_LATITUDE, "longitude": WALLAPOP_LONGITUDE,
            "distance": WALLAPOP_DISTANCE, "order_by": order_by,
            "category_id": 100, "section_type": "organic_search_results",
            "items_count": n,
        }
        if año > 0:
            params["min_year"] = año - año_tolerancia
            params["max_year"] = año + año_tolerancia
        else:
            logger.info("[ES] año no detectado, busco sin filtro temporal")
        if km > 0:
            params["max_km"] = km + km_tolerancia
        else:
            logger.info("[ES] km no detectado, busco sin filtro de km")

        data = await self._fetch(params)
        items = self._extraer_items(data)

        if not items:
            logger.warning("[Wallapop] Reintentando comparables sin año/km")
            params2 = {k: v for k, v in params.items()
                       if k not in ("min_year", "max_year", "max_km")}
            params2["items_count"] = n
            items = self._extraer_items(await self._fetch(params2))

        anuncios = []
        for item in items:
            try:
                a = self._item_a_anuncio(item, str(item.get("id", "")))
                if a and a.precio > 0:
                    anuncios.append(a)
            except Exception as e:
                logger.debug(f"[Wallapop] Error parseando item comparable: {e}")
        logger.info(f"[Wallapop] {len(anuncios)} comparables con precio>0 de {len(items)} items")
        return anuncios

    @staticmethod
    def _item_a_anuncio(content: dict, fallback_id: str = "", url_pagina: str = ""):
        """
        Convierte un dict de la API de Wallapop en un dataclass Anuncio.
        Soporta la estructura actual (2025): type_attributes para datos de coche,
        price.amount o price.cash.amount para precio, images[].urls.medium para fotos.
        """
        from cabeza_bot.models import Anuncio
        from datetime import datetime as _dt, timezone as _tz

        item_id = str(content.get("id") or fallback_id)

        # Precio: buscar en múltiples ubicaciones de la estructura actual
        precio = 0.0
        p = content.get("price") or {}
        if isinstance(p, dict):
            # Estructura search: {"amount": 28500, "currency": "EUR"}
            # Estructura detail: {"cash": {"amount": 28500, ...}, ...}
            precio = float(p.get("amount") or
                           (p.get("cash") or {}).get("amount") or
                           p.get("value") or 0)
        elif isinstance(p, (int, float)):
            precio = float(p)

        # Descripción (string en search, {"original": "..."} en detail API)
        desc_raw = content.get("description") or ""
        if isinstance(desc_raw, dict):
            desc_raw = desc_raw.get("original") or desc_raw.get("text") or ""
        descripcion = str(desc_raw)[:1500]

        # Foto principal + galería completa
        foto = ""
        fotos: list[str] = []
        imgs = content.get("images") or []
        if isinstance(imgs, list):
            for it in imgs:
                urls = it.get("urls") or it
                u = urls.get("medium") or urls.get("original") or urls.get("small")
                if u:
                    fotos.append(u)
            if fotos:
                foto = fotos[0]
        elif isinstance(imgs, dict):
            foto = imgs.get("medium") or ""
            if foto:
                fotos = [foto]

        # Localización
        loc = content.get("location") or {}
        provincia = (loc.get("city") or loc.get("postal_code") or
                     loc.get("region") or loc.get("region_name") or "")

        # URL pública
        slug = content.get("web_slug") or content.get("slug") or item_id
        url = url_pagina or f"https://es.wallapop.com/item/{slug}"

        # Datos de coche: type_attributes (nueva API) > extra_info.cars (antigua)
        ta   = content.get("type_attributes") or {}
        extra = content.get("extra_info") or {}
        cars  = extra.get("cars") or (extra if isinstance(extra, dict) else {})

        km  = int(ta.get("km") or ta.get("kilometers") or
                  cars.get("km") or cars.get("kilometers") or 0)
        año = int(ta.get("year") or ta.get("registration_year") or
                  cars.get("year") or cars.get("registration_year") or 0)
        marca  = str(ta.get("brand") or ta.get("make") or
                     cars.get("brand") or cars.get("make") or "").lower().strip()
        modelo = str(ta.get("model") or
                     cars.get("model") or "").lower().strip()

        engine = str(ta.get("engine") or ta.get("fuel_type") or
                     cars.get("engine") or cars.get("fuel_type") or "").strip()
        cv = ta.get("horsepower") or ta.get("power") or cars.get("horsepower") or ""
        motor = f"{engine} {cv}cv".strip(" cv") if engine or cv else ""

        titulo = str(content.get("title") or "").strip()[:200]

        return Anuncio(
            item_id=item_id,
            fuente="wallapop",
            marca=marca,
            modelo=modelo,
            año=año,
            km=km,
            precio=precio,
            provincia=provincia,
            descripcion=descripcion[:1500],
            url=url,
            foto=foto,
            motor=motor,
            titulo=titulo,
            fotos=fotos,
            capturado_at=_dt.now(_tz.utc).isoformat(),
        )


# ════════════════════════════════════════════════════════════════════════════
# COCHES.NET  (Query texto español → su buscador IA filtra)
# ════════════════════════════════════════════════════════════════════════════

# Máximo 2 browsers Playwright simultáneos para coches.net (10 en paralelo revienta RAM)
_COCHES_NET_SEM = asyncio.Semaphore(2)


class ScraperCochesNet:
    nombre = "coches.net"
    SEARCH_URL = "https://www.coches.net/segunda-mano/"

    # Selectores tolerantes (orden = prioridad). Si uno falla, prueba el siguiente.
    CARD_SELECTORS = [
        "article[class*='mt-CardAd']",
        "div[class*='mt-CardAd']",
        "article:has(a[href*='/coches-segunda-mano/'])",
        "div[class*='CardAd']",
    ]
    PRICE_SELECTORS = [
        "[class*='mt-CardAdPrice'] strong",
        "[class*='mt-CardAdPrice']",
        "strong[class*='price']",
        "span[class*='price']",
        "[data-test*='price']",
    ]
    LINK_SELECTORS = [
        "a[href*='/coches-segunda-mano/']",
        "a[class*='mt-CardAd-titleLink']",
        "h2 a, h3 a",
    ]

    def acepta_url(self, url: str) -> bool:
        return "coches.net" in (url or "").lower()

    # ── API pública unificada (capa fuente-agnóstica) ────────────────────────
    def _query_busqueda(self, marca: str, modelo: str, filtros: dict) -> str:
        """Query para el buscador IA: 'KIA Rio 1.2 CVVT' o 'KIA Rio gasolina'."""
        version_motor = str(filtros.get("version_motor", "")).strip()
        combustible = str(filtros.get("combustible", "")).strip().lower()
        partes = [marca, modelo]
        if version_motor:
            engine = re.sub(
                r"\s*\d+\s*(cv|kw|ps|hp)\b.*$", "", version_motor, flags=re.IGNORECASE
            ).strip()
            if engine:
                partes.append(engine)
        elif combustible and combustible not in ("indistinto", ""):
            partes.append(combustible)
        return " ".join(partes)

    async def _buscar_httpx(
        self, marca: str, modelo: str, año: int, km: int, n: int,
        filtros: dict | None = None,
    ) -> list:
        """Scraping con httpx puro — coches.net usa SSR, el HTML llega completo."""
        import urllib.parse
        filtros = filtros or {}
        query = self._query_busqueda(marca, modelo, filtros)

        params: dict[str, str] = {
            "MakeModelGeneralSearch": query,
            "OrderTypeId": "Price",
            "OrderAsc": "True",
        }
        if año:
            params["MinYear"] = str(año - AÑO_TOLERANCIA)
            params["MaxYear"] = str(año + AÑO_TOLERANCIA)
        if km:
            params["MaxKms"] = str(km + KM_TOLERANCIA)

        url = self.SEARCH_URL + "?" + urllib.parse.urlencode(params)
        hdrs = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "es-ES,es;q=0.9",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://www.coches.net/",
        }

        logger.info(f"[coches.net httpx] GET '{query}' año±{AÑO_TOLERANCIA} km≤{km}")
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=20, headers=hdrs,
            ) as client:
                resp = await client.get(url)

            if resp.status_code != 200:
                logger.warning(f"[coches.net httpx] Status {resp.status_code}")
                return []
            if len(resp.text) < 10_000:
                logger.warning(
                    f"[coches.net httpx] Respuesta corta ({len(resp.text)} B) — posible bloqueo"
                )
                return []

            anuncios = self._parsear_cards_html(resp.text, marca, modelo, n)
            logger.info(f"[coches.net httpx] {len(anuncios)} anuncios")
            return anuncios
        except Exception as e:
            logger.error(f"[coches.net httpx] Error: {e}")
            return []

    def _parsear_cards_html(self, html: str, marca: str, modelo: str, n: int) -> list:
        """Parsea el HTML de listado coches.net (SSR). Título incluye motor exacto."""
        import html as _html
        from cabeza_bot.models import Anuncio
        from datetime import datetime

        anuncios: list = []
        # Segmentar por card usando data-ad-id
        posiciones = [(m.start(), m.group(1)) for m in re.finditer(r'data-ad-id="(\d+)"', html)]

        for idx, (pos, ad_id) in enumerate(posiciones[:n]):
            fin = posiciones[idx + 1][0] if idx + 1 < len(posiciones) else len(html)
            chunk = html[pos:fin]
            try:
                # Título + href (el título incluye motor: "KIA Rio 1.2 CVVT Concept")
                m = re.search(
                    r'class="mt-CardAd-infoHeaderTitleLink"[^>]*href="([^"]+)"[^>]*>'
                    r"([^<]+)</a>",
                    chunk,
                )
                if not m:
                    continue
                href = m.group(1)
                titulo = _html.unescape(m.group(2).strip())

                # Precio
                mp = re.search(r'data-testid="card-adPrice-price">([^<]+)</p>', chunk)
                if not mp:
                    continue
                precio = float(re.sub(r"[^\d]", "", _html.unescape(mp.group(1))) or "0")
                if precio <= 0:
                    continue

                # Atributos: año, km, combustible
                año_val = km_val = 0
                motor_texto = ""
                for item in re.findall(r'class="mt-CardAd-attrItem">([^<]+)</li>', chunk):
                    item = _html.unescape(item).strip()
                    if re.match(r"^\d{4}$", item):
                        año_val = int(item)
                    elif "km" in item.lower():
                        km_val = int(re.sub(r"[^\d]", "", item) or "0")
                    elif any(c in item.lower() for c in
                             ("gasol", "diesel", "híbrido", "electr", "gasoil")):
                        motor_texto = item

                full_url = (
                    f"https://www.coches.net{href}" if href.startswith("/") else href
                )
                anuncios.append(Anuncio(
                    item_id=ad_id,
                    titulo=titulo,
                    precio=precio,
                    año=año_val,
                    km=km_val,
                    marca=marca,
                    modelo=modelo,
                    motor=motor_texto,
                    url=full_url,
                    fuente="coches.net",
                    fecha_scraping=datetime.utcnow(),
                ))
            except Exception as e:
                logger.debug(f"[coches.net httpx] card {ad_id} skip: {e}")

        return anuncios

    async def buscar_comparables(
        self, marca: str, modelo: str, año: int, km: int, n: int = 20,
        filtros: dict | None = None,
    ) -> list:
        # Intentar httpx primero (evita Playwright si la IP no está bloqueada)
        try:
            items_httpx = await self._buscar_httpx(marca, modelo, año, km, n, filtros)
            if items_httpx:
                logger.info(f"[coches.net comparables] httpx OK {len(items_httpx)}")
                return items_httpx
            logger.info("[coches.net comparables] httpx vacío → Playwright")
        except Exception as e:
            logger.info(f"[coches.net comparables] httpx falló ({e}) → Playwright")

        async with _COCHES_NET_SEM:
            try:
                items = await self._buscar_playwright(marca, modelo, año, km, n, filtros or {})
                if items:
                    logger.info(f"[coches.net comparables] Playwright OK: {len(items)} items")
                return items
            except Exception as e:
                logger.warning(f"[coches.net comparables] Playwright falló: {e}")
                return []

    @staticmethod
    def _extraer_json_ld(html_text: str) -> tuple[float, str]:
        """Extrae precio y título del JSON-LD de una página de coches.net."""
        import json as _json
        ld_precio, ld_titulo = 0.0, ""
        for raw_ld in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html_text, re.DOTALL | re.IGNORECASE
        ):
            try:
                d = _json.loads(raw_ld)
                nodes = d if isinstance(d, list) else [d]
                for node in nodes:
                    offers = node.get('offers', {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    p = float(str(offers.get('price', 0) or 0))
                    if p >= 1000:
                        ld_precio = p
                        ld_titulo = node.get('name', '') or ld_titulo
                        break
                if ld_precio > 0:
                    break
            except Exception:
                continue
        return ld_precio, ld_titulo

    @staticmethod
    def _construir_anuncio_desde_html(html_text: str, url: str,
                                       ld_precio: float, ld_titulo: str,
                                       ua: str = ""):
        """Construye un Anuncio desde HTML estático (ruta httpx S1)."""
        from cabeza_bot.models import Anuncio
        from datetime import datetime as _dt, timezone as _tz

        titulo = ld_titulo or ""
        texto = re.sub(r'<[^>]+>', ' ', html_text)

        anno = 0
        m = re.search(r"\b(19[89]\d|20[0-3]\d)\b", titulo + " " + texto)
        if m:
            anno = int(m.group(1))
        kms = 0
        m = re.search(r"([\d\.]+)\s*km", texto, re.IGNORECASE)
        if m:
            try:
                kms = int(m.group(1).replace(".", ""))
            except ValueError:
                pass

        marca, modelo, provincia = "", "", ""
        try:
            raw = url.split("coches.net/", 1)[1].split("?")[0].split("#")[0]
            skip = {"km-0", "segunda-mano", "ocasion", "ocasión", "coches", ""}
            parts = [p for p in raw.split("/") if p and p not in skip]
            if (len(parts) >= 2
                    and not parts[0].endswith(".aspx")
                    and "-" not in parts[0]
                    and len(parts[0]) <= 20
                    and len(parts[1]) <= 30):
                marca = parts[0]
                modelo = parts[1]
                if len(parts) >= 3 and not parts[2].endswith(".aspx"):
                    provincia = parts[2].replace("-", " ").title()
        except Exception:
            pass
        if not provincia:
            m2 = re.search(r"-en-([a-z\-]+?)-\d", url, re.IGNORECASE)
            if m2:
                provincia = m2.group(1).replace("-", " ").title()

        fotos = re.findall(r'https://[^\s"\'<>]+\.(?:jpg|jpeg|webp|png)[^\s"\'<>]*', html_text)
        fotos = [f for f in fotos if "cochesnet" in f or "coches.net" in f][:8]
        foto = fotos[0] if fotos else ""

        item_id = ""
        m3 = re.search(r"(\d{6,})", url)
        if m3:
            item_id = m3.group(1)
        if not item_id:
            item_id = _generar_id("coches.net", titulo[:60], ld_precio, url)

        if ld_precio <= 0 or not (marca or titulo):
            return None

        return Anuncio(
            item_id=item_id,
            fuente="coches.net",
            marca=(marca or "").lower(),
            modelo=(modelo or "").lower(),
            año=anno,
            km=kms,
            precio=ld_precio,
            provincia=provincia,
            descripcion=titulo[:1500],
            url=url,
            foto=foto,
            motor=titulo[:120],
            fotos=fotos,
            capturado_at=_dt.now(_tz.utc).isoformat(),
        )

    async def obtener_anuncio(self, url: str):
        """Extrae datos de un anuncio individual de coches.net por URL.
        S1: httpx con cookies precalentadas (evita Playwright si no hay bloqueo).
        S2: Playwright headed con stealth mejorado + pre-warm + retry.
        """
        from cabeza_bot.models import Anuncio
        from datetime import datetime as _dt, timezone as _tz

        ua_chrome = next(
            (ua for ua in USER_AGENTS if "Chrome/" in ua and ("Windows" in ua or "Macintosh" in ua)),
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )

        # ── S1: httpx con cookies precalentadas ──────────────────────────────
        logger.info(f"[coches.net S1] httpx prewarming para {url}")
        try:
            hdrs_httpx = {
                "User-Agent": ua_chrome,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "es-ES,es;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Referer": "https://www.coches.net/",
            }
            async with httpx.AsyncClient(
                timeout=20, headers=hdrs_httpx, follow_redirects=True,
            ) as c:
                homepage = await c.get("https://www.coches.net/")
                if len(homepage.text) >= 10_000:
                    resp = await c.get(url)
                    if resp.status_code == 200 and len(resp.text) >= 15_000:
                        ld_precio, ld_titulo = self._extraer_json_ld(resp.text)
                        if ld_precio >= 1000:
                            logger.info(f"[coches.net S1 OK] precio={ld_precio:.0f}€ vía httpx+JSON-LD")
                            anuncio_s1 = self._construir_anuncio_desde_html(
                                resp.text, url, ld_precio, ld_titulo, ua_chrome,
                            )
                            if anuncio_s1:
                                return anuncio_s1
                    logger.info("[coches.net S1] httpx sin JSON-LD válido → S2 Playwright")
                else:
                    logger.info(f"[coches.net S1] Homepage < 10KB (IP bloqueada) → S2 Playwright")
        except Exception as e:
            logger.info(f"[coches.net S1] httpx falló ({e}) → S2 Playwright")

        # ── S2: Playwright headed con stealth mejorado ────────────────────────
        proxy_cfg = {"server": random.choice(PROXIES)} if PROXIES else None
        _STEALTH_JS = (
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
            "Object.defineProperty(navigator,'languages',{get:()=>['es-ES','es']});"
            "window.chrome={runtime:{}};"
        )
        async with _PLAYWRIGHT_SEM, async_playwright() as p:
            browser = None
            try:
                browser = await p.chromium.launch(headless=False)
            except Exception as _e:
                _emsg = str(_e).lower()
                if any(x in _emsg for x in ("missing x", "display", "x11", "cannot open")):
                    logger.warning("[coches.net S2] Sin display — intentando headless=True")
                    try:
                        browser = await p.chromium.launch(headless=True)
                    except Exception as _e2:
                        logger.error(f"[coches.net S2] headless=True también falló: {_e2}")
                        return None
                else:
                    logger.error(f"[coches.net S2] Error lanzando browser: {_e}")
                    return None
            context = await _nuevo_contexto_stealth(browser, ua_chrome, proxy_cfg, locale="es-ES")
            await context.add_init_script(_STEALTH_JS)
            page = await context.new_page()
            try:
                # Pre-warm: visitar homepage para adquirir cookies de sesión
                try:
                    await page.goto("https://www.coches.net/", timeout=10_000, wait_until="load")
                except Exception:
                    pass

                for intento_pw in range(COCHES_NET_RETRY_MAX + 1):
                    if intento_pw > 0:
                        logger.info(f"[coches.net S2] Reintento {intento_pw}/{COCHES_NET_RETRY_MAX}")
                        await asyncio.sleep(5)

                    logger.info(f"[coches.net S2] Navegando a {url}")
                    await page.goto(url, timeout=30_000, wait_until="domcontentloaded")
                    await asyncio.sleep(random.uniform(2.5, 3.5))

                    for sel in ["button#didomi-notice-agree-button",
                                 "button:has-text('Aceptar')",
                                 "button:has-text('Aceptar todo')"]:
                        try:
                            btn = page.locator(sel).first
                            if await btn.is_visible(timeout=2_000):
                                await btn.click()
                                await asyncio.sleep(1.0)
                                break
                        except Exception:
                            continue

                    try:
                        await page.wait_for_selector(
                            "h1, [class*='DetailHead'], [class*='priceMain'], [class*='DetailPrice']",
                            timeout=12_000,
                        )
                    except Exception:
                        logger.warning("[coches.net S2] Timeout esperando bloque de detalle")

                    texto = ""
                    try:
                        texto = await page.locator("body").inner_text(timeout=5_000)
                    except Exception:
                        pass
                    html = ""
                    try:
                        html = await page.content()
                    except Exception:
                        pass

                    ld_precio, ld_titulo = self._extraer_json_ld(html)

                    if len(html) < 15_000 and ld_precio <= 0:
                        if intento_pw < COCHES_NET_RETRY_MAX:
                            logger.warning(
                                f"[coches.net S2] HTML {len(html)}B sin JSON-LD — "
                                f"bloqueado, reintentando ({intento_pw+1}/{COCHES_NET_RETRY_MAX})"
                            )
                            continue
                        logger.error(
                            f"[coches.net S2] HTML solo {len(html)} bytes y sin JSON-LD "
                            f"tras {COCHES_NET_RETRY_MAX+1} intentos — bloqueado."
                        )
                        return None
                    break  # HTML OK, salir del loop de retry

                # Precio: usa JSON-LD si disponible, luego CSS selectors.
                precio = ld_precio
                if precio <= 0:
                    for psel in ["[class*='mt-DetailHead-priceMain']",
                                 "[class*='priceMain']",
                                 "[class*='DetailPrice'] strong",
                                 "[class*='DetailHead'] strong",
                                 "[class*='DetailHead'] [class*='price']"]:
                        try:
                            loc = page.locator(psel)
                            n = min(await loc.count(), 5)
                            for i in range(n):
                                t = (await loc.nth(i).inner_text()).strip()
                                low = t.lower()
                                if "/mes" in low or "mes" in low.split() or "cuota" in low:
                                    continue
                                v = _parse_numero(t)
                                if v >= 1000:
                                    precio = v
                                    break
                            if precio > 0:
                                break
                        except Exception:
                            continue

                # Fallback regex SOLO dentro del bloque de detalle (jamás en aside ni footer)
                if precio <= 0:
                    head_text = ""
                    for hsel in ("[class*='DetailHead']", "main", "article"):
                        try:
                            head_text = await page.locator(hsel).first.inner_text(timeout=2_000)
                            if head_text:
                                break
                        except Exception:
                            continue
                    for m in re.finditer(r"(\d{1,3}(?:\.\d{3})+)\s*€", head_text or ""):
                        # Saltar si va seguido de "/mes" o precedido de "cuota"
                        ctx_after = (head_text or "")[m.end():m.end()+10].lower()
                        ctx_before = (head_text or "")[max(0, m.start()-30):m.start()].lower()
                        if "/mes" in ctx_after or "mes" in ctx_after.split() or "cuota" in ctx_before:
                            continue
                        v = float(m.group(1).replace(".", ""))
                        if 1000 <= v <= 5_000_000:
                            precio = v
                            break

                # Título: JSON-LD primero, luego selectores CSS
                titulo = ld_titulo
                if not titulo:
                    for tsel in ["h1", "[class*='DetailHead-titleMain']",
                                 "[class*='titleMain']", "[class*='title']"]:
                        try:
                            el = page.locator(tsel).first
                            if await el.count() > 0:
                                titulo = (await el.inner_text()).strip()
                                if titulo:
                                    break
                        except Exception:
                            continue

                # Descripción
                descripcion = ""
                for dsel in ["[class*='DetailDescription']",
                             "[class*='description']",
                             "section:has(h2:has-text('Descripción'))"]:
                    try:
                        el = page.locator(dsel).first
                        if await el.count() > 0:
                            descripcion = (await el.inner_text()).strip()
                            if descripcion:
                                break
                    except Exception:
                        continue

                # Año / km del bloque de specs
                anno = 0
                m = re.search(r"\b(19[89]\d|20[0-3]\d)\b", titulo + " " + texto)
                if m:
                    anno = int(m.group(1))
                kms = 0
                m = re.search(r"([\d\.]+)\s*km", texto, re.IGNORECASE)
                if m:
                    try:
                        kms = int(m.group(1).replace(".", ""))
                    except ValueError:
                        pass

                # Marca/modelo: prioridad path limpio /km-0/seat/ibiza/provincia/...
                # Si el path solo tiene un slug largo terminado en .aspx, usar IA sobre el título.
                marca = ""
                modelo = ""
                provincia = ""
                try:
                    raw = url.split("coches.net/", 1)[1].split("?")[0].split("#")[0]
                    skip = {"km-0", "segunda-mano", "ocasion", "ocasión", "coches", ""}
                    parts = [p for p in raw.split("/") if p and p not in skip]
                    # Path limpio si los 2 primeros segmentos son cortos (no slug.aspx)
                    if (len(parts) >= 2
                            and not parts[0].endswith(".aspx")
                            and "-" not in parts[0]
                            and len(parts[0]) <= 20
                            and len(parts[1]) <= 30):
                        marca = parts[0]
                        modelo = parts[1]
                        if len(parts) >= 3 and not parts[2].endswith(".aspx"):
                            provincia = parts[2].replace("-", " ").title()
                except Exception:
                    pass

                # Fallback: extraer marca/modelo del título con IA
                if (not marca or not modelo) and titulo:
                    try:
                        from cabeza_bot.analisis.ai import parsear_modelo_nl
                        parsed = await parsear_modelo_nl(titulo)
                        marca = marca or parsed.get("marca", "")
                        modelo = modelo or parsed.get("modelo", "")
                    except Exception as e:
                        logger.warning(f"[coches.net] parsear_modelo_nl falló: {e}")

                # Provincia: si vacío, intenta sacarla del slug ('en-madrid')
                if not provincia:
                    m = re.search(r"-en-([a-z\-]+?)-\d", url, re.IGNORECASE)
                    if m:
                        provincia = m.group(1).replace("-", " ").title()

                # Foto principal + galería
                foto = ""
                fotos: list[str] = []
                try:
                    for gsel in ("[class*='gallery'] img",
                                 "[class*='Gallery'] img",
                                 "img[src*='cochesnet']",
                                 "img"):
                        loc = page.locator(gsel)
                        n = await loc.count()
                        if n == 0:
                            continue
                        for i in range(min(n, 12)):
                            el = loc.nth(i)
                            src = (await el.get_attribute("src")
                                   or await el.get_attribute("data-src") or "")
                            if src and src.startswith("http") and src not in fotos:
                                fotos.append(src)
                        if len(fotos) >= 3:
                            break
                    if fotos:
                        foto = fotos[0]
                except Exception:
                    pass

                # item_id desde la URL (último número largo antes de -kovn.aspx)
                item_id = ""
                m = re.search(r"(\d{6,})", url)
                if m:
                    item_id = m.group(1)
                if not item_id:
                    item_id = _generar_id("coches.net", titulo[:60], precio, url)

                if precio <= 0 or not (marca or titulo):
                    logger.error(f"[coches.net] No pude extraer datos mínimos de {url}")
                    return None

                return Anuncio(
                    item_id=item_id,
                    fuente="coches.net",
                    marca=(marca or "").lower(),
                    modelo=(modelo or "").lower(),
                    año=anno,
                    km=kms,
                    precio=precio,
                    provincia=provincia,
                    descripcion=(descripcion or titulo)[:1500],
                    url=url,
                    foto=foto,
                    motor=titulo[:120],
                    fotos=fotos,
                    capturado_at=_dt.now(_tz.utc).isoformat(),
                )
            except Exception as e:
                logger.error(f"[coches.net] obtener_anuncio falló: {e}")
                return None
            finally:
                await browser.close()

    # ── Backend Playwright ────────────────────────────────────────────────────
    async def _buscar_playwright(
        self, marca: str, modelo: str, año: int, km: int, n: int,
        filtros: dict | None = None,
    ) -> list:
        filtros = filtros or {}
        query = self._query_busqueda(marca, modelo, filtros)

        user_agent = random.choice(USER_AGENTS)
        proxy_cfg = {"server": random.choice(PROXIES)} if PROXIES else None
        anuncios: list = []

        _STEALTH_JS = (
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
            "Object.defineProperty(navigator,'languages',{get:()=>['es-ES','es']});"
            "window.chrome={runtime:{}};"
        )
        async with _PLAYWRIGHT_SEM, async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await _nuevo_contexto_stealth(browser, user_agent, proxy_cfg, locale="es-ES")
            await context.add_init_script(_STEALTH_JS)
            page = await context.new_page()
            try:
                logger.info(f"[coches.net] Buscando con IA: '{query}'")
                await page.goto(self.SEARCH_URL, timeout=30_000, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(1.5, 2.5))

                for sel in ["button#didomi-notice-agree-button",
                             "button:has-text('Aceptar')",
                             "button:has-text('Aceptar todo')"]:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible(timeout=2_000):
                            await btn.click()
                            await asyncio.sleep(0.8)
                            break
                    except Exception:
                        continue

                # Escribir en el buscador IA y enviar
                used_search_box = False
                try:
                    search_input = page.locator("input#search-ai-input-id")
                    await search_input.wait_for(state="visible", timeout=5_000)
                    await search_input.click()
                    await asyncio.sleep(0.3)
                    await search_input.fill(query)
                    await asyncio.sleep(0.4)
                    await search_input.press("Enter")
                    await asyncio.sleep(random.uniform(3.5, 5.0))
                    used_search_box = True
                    logger.info(f"[coches.net] Query IA enviada: '{query}'")
                except Exception as e:
                    # Fallback: URL con params si el input no carga
                    logger.warning(f"[coches.net] Search box no disponible ({e}), fallback URL")
                    fallback_url = (
                        f"{self.SEARCH_URL}?MakeModelGeneralSearch={query}"
                        f"&OrderTypeId=Price&OrderAsc=True"
                    )
                    if año:
                        fallback_url += f"&MinYear={año - AÑO_TOLERANCIA}&MaxYear={año + AÑO_TOLERANCIA}"
                    if km:
                        fallback_url += f"&MaxKms={km + KM_TOLERANCIA}"
                    await page.goto(fallback_url, timeout=30_000, wait_until="domcontentloaded")
                    await asyncio.sleep(random.uniform(2.0, 3.5))

                # Localizar tarjetas con cascada de selectores
                cards = None
                for csel in self.CARD_SELECTORS:
                    cs = page.locator(csel)
                    if await cs.count() >= 3:
                        cards = cs
                        logger.info(f"[coches.net] Cards selector: '{csel}' n={await cs.count()}")
                        break

                if cards is None:
                    logger.warning("[coches.net] Sin selector de cards aplicable")
                    return []

                total = min(await cards.count(), n)
                for i in range(total):
                    try:
                        a = await self._extraer_card(cards.nth(i), marca, modelo)
                        if a and a.precio > 0 and a.url:
                            anuncios.append(a)
                    except Exception as e:
                        logger.debug(f"[coches.net] card {i} skip: {e}")

            except Exception as e:
                logger.error(f"[coches.net] Playwright error: {e}")
            finally:
                await browser.close()

        logger.info(f"[coches.net] {len(anuncios)} anuncios extraídos")
        return anuncios

    async def _extraer_card(self, card, marca: str, modelo: str):
        from cabeza_bot.models import Anuncio
        from datetime import datetime as _dt, timezone as _tz

        # Texto crudo del card → fallback para año/km
        texto = ""
        try:
            texto = await card.inner_text()
        except Exception:
            pass

        # Precio
        precio = 0.0
        for psel in self.PRICE_SELECTORS:
            try:
                el = card.locator(psel).first
                if await el.count() > 0:
                    val = _parse_numero(await el.inner_text())
                    if val > 500:
                        precio = val
                        break
            except Exception:
                continue
        if precio <= 0:
            m = re.search(r"(\d{1,3}(?:\.\d{3})+)\s*€", texto)
            if m:
                precio = float(m.group(1).replace(".", ""))

        # URL
        href = ""
        for lsel in self.LINK_SELECTORS:
            try:
                el = card.locator(lsel).first
                if await el.count() > 0:
                    href = await el.get_attribute("href") or ""
                    if href:
                        break
            except Exception:
                continue
        if href and href.startswith("/"):
            href = f"https://www.coches.net{href}"

        item_id = ""
        if href:
            m = re.search(r"(\d{5,})", href)
            item_id = m.group(1) if m else _generar_id("coches.net", texto[:60], precio, href)
        else:
            item_id = _generar_id("coches.net", texto[:60], precio, "")

        # Año (regex sobre texto del card)
        anno = 0
        m = re.search(r"\b(19[89]\d|20[0-3]\d)\b", texto)
        if m:
            anno = int(m.group(1))

        # Kilómetros
        kms = 0
        m = re.search(r"([\d\.]+)\s*km", texto, re.IGNORECASE)
        if m:
            try:
                kms = int(m.group(1).replace(".", ""))
            except ValueError:
                pass

        # Provincia (heurística: línea con coma o tras año/km)
        provincia = ""
        for line in (texto or "").split("\n"):
            line = line.strip()
            if line and not re.search(r"\d", line) and 3 <= len(line) <= 40:
                provincia = line
                break

        # Foto
        foto = ""
        try:
            img = card.locator("img").first
            if await img.count() > 0:
                foto = await img.get_attribute("src") or await img.get_attribute("data-src") or ""
        except Exception:
            pass

        # Descripción (título h2/h3 si existe; si no, primera línea)
        desc = ""
        for tsel in ("h2", "h3", "[class*='title']"):
            try:
                el = card.locator(tsel).first
                if await el.count() > 0:
                    desc = (await el.inner_text()).strip()
                    if desc:
                        break
            except Exception:
                continue
        if not desc:
            desc = (texto or "").split("\n")[0][:120]

        return Anuncio(
            item_id=item_id,
            fuente="coches.net",
            marca=marca.strip().lower(),
            modelo=modelo.strip().lower(),
            año=anno,
            km=kms,
            precio=precio,
            provincia=provincia,
            descripcion=desc[:1500],
            url=href,
            foto=foto,
            motor="",
            titulo=desc[:200],
            capturado_at=_dt.now(_tz.utc).isoformat(),
        )

    async def buscar_precios(self, marca: str, modelo: str, año: int, km: int,
                              filtros: dict | None = None) -> dict:
        filtros = filtros or {}
        query_es = _construir_query_es(marca, modelo, filtros)
        user_agent = random.choice(USER_AGENTS)
        proxy_cfg = {"server": random.choice(PROXIES)} if PROXIES else None
        precios_raw: list[float] = []

        url = f"{self.SEARCH_URL}?MakeModelGeneralSearch={query_es}"
        url += "&OrderTypeId=Price&OrderAsc=True"
        if año:
            url += f"&MinYear={año - AÑO_TOLERANCIA}&MaxYear={año + AÑO_TOLERANCIA}"
        if km:
            url += f"&MaxKms={km + KM_TOLERANCIA}"

        async with _PLAYWRIGHT_SEM, async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await _nuevo_contexto_stealth(browser, user_agent, proxy_cfg, locale="es-ES")
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            page = await context.new_page()
            try:
                logger.info(f"[coches.net] URL: {url}")
                await page.goto(url, timeout=30_000, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(2.0, 3.5))

                for sel in ["button#didomi-notice-agree-button",
                             "button:has-text('Aceptar')",
                             "button:has-text('Aceptar todo')"]:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible(timeout=3_000):
                            await btn.click()
                            await asyncio.sleep(1.0)
                            break
                    except Exception:
                        continue

                for sel in ["span[class*='price']", "span[class*='Price']",
                             "div[class*='price'] span", "p[class*='price']"]:
                    elems = page.locator(sel)
                    count = await elems.count()
                    if count > 0:
                        for i in range(min(count, COCHES_NET_RESULTS)):
                            try:
                                val = _parse_numero(await elems.nth(i).inner_text())
                                if val > 500:
                                    precios_raw.append(val)
                            except Exception:
                                continue
                        if precios_raw:
                            break

                if not precios_raw:
                    try:
                        html = await page.content()
                        for m in re.findall(r'(\d{1,3}(?:\.\d{3})*)\s*€', html)[:COCHES_NET_RESULTS]:
                            val = float(m.replace(".", ""))
                            if 1000 < val < 500_000:
                                precios_raw.append(val)
                    except Exception:
                        pass

            except Exception as e:
                logger.error(f"[coches.net] Error: {e}")
            finally:
                await browser.close()

        logger.info(f"[coches.net] {len(precios_raw)} precios: {precios_raw[:8]}")
        if not precios_raw:
            return ScraperWallapop._vacio("Sin resultados en coches.net")
        return ScraperWallapop._calcular_precio_medio(precios_raw)


# ════════════════════════════════════════════════════════════════════════════
# FUNCIONES PÚBLICAS
# ════════════════════════════════════════════════════════════════════════════

def _persistir_de_historico(anuncios: list[dict], marca: str, modelo: str) -> int:
    """
    Persiste los anuncios DE (dicts del scraper) en historico_precios con
    fuente='autoscout24'. Mismos filtros de calidad que ES: precio>0, año>1990.
    El dataset DE vs ES es un activo del producto.
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
            fuente="autoscout24",
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