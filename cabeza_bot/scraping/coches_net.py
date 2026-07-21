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


