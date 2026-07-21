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


# ════════════════════════════════════════════════════════════════════════════
# MOBILE.DE  (Query texto alemán → su buscador IA filtra)
# ════════════════════════════════════════════════════════════════════════════

class ScraperMobileDe(ScraperDE):
    nombre = "mobile.de"
    # mobile.de usa URLs SEO: /auto/volkswagen-golf-gti.html
    BASE_URL = "https://suchen.mobile.de/auto"

    def _construir_url(self, marca: str, modelo: str, filtros: dict) -> str:
        filtros = filtros or {}
        marca_slug = marca.lower().strip().replace(" ", "-")
        modelo_slug = modelo.lower().strip().replace(" ", "-")
        url = f"{self.BASE_URL}/{marca_slug}-{modelo_slug}.html"

        params = []
        if filtros.get("km_max"):    params.append(f"ml=:{filtros['km_max']}")
        if filtros.get("km_min"):    params.append(f"ml={filtros['km_min']}:")

        yr_min = filtros.get("year_min", "")
        yr_max = filtros.get("year_max", "")
        if yr_min or yr_max:
            params.append(f"fr={yr_min or ''}:{yr_max or ''}")

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
        return url

    async def buscar(self, marca: str, modelo: str, filtros: dict) -> list[dict]:
        filtros = filtros or {}
        user_agent = random.choice(USER_AGENTS)
        proxy_cfg = {"server": random.choice(PROXIES)} if PROXIES else None
        resultados: list[dict] = []

        url = self._construir_url(marca, modelo, filtros)
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

                detail_urls = await self._recolectar_urls_detalle(page)
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

    async def _recolectar_urls_detalle(self, page) -> set[str]:
        """Recolección barata (sin visitar detalle): URLs de anuncio del listado."""
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
        return detail_urls

    async def buscar_deteccion(self, marca: str, modelo: str, filtros: dict,
                               limite: int | None = None) -> tuple[list[dict], str]:
        """
        Detección para el sniper. mobile.de NO expone precio/km/año en el
        listado (a diferencia de AS24), así que la "detección barata" aquí es:
        recolectar enlaces (gratis) + visitar detalle completo de los primeros
        `limite` (SNIPER_MOBILE_DETALLES_LIM por defecto) — más caro por
        candidato que AS24, por eso se limita fuerte. CERO IA (usar_ia_co2=False).
        Devuelve (anuncios, señal) con señal ∈ {'ok','vacio','fallo'}, mismo
        contrato que AS24.buscar_deteccion. Los anuncios devueltos ya llevan
        `_detalle_completo=True` (no hace falta una segunda pasada de detalle).
        """
        from cabeza_bot.config import SNIPER_MOBILE_DETALLES_LIM
        filtros = filtros or {}
        limite = limite or SNIPER_MOBILE_DETALLES_LIM
        user_agent = random.choice(USER_AGENTS)
        proxy_cfg = {"server": random.choice(PROXIES)} if PROXIES else None
        url = self._construir_url(marca, modelo, filtros)

        async with _PLAYWRIGHT_SEM, async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await _nuevo_contexto_stealth(browser, user_agent, proxy_cfg)
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            try:
                page = await context.new_page()
                await page.goto(url, timeout=60_000, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(1.5, 3.0))

                for sel in ["button#mde-consent-accept-btn",
                             "button:has-text('Alle akzeptieren')",
                             "button:has-text('Einverstanden')",
                             "#gdpr-consent-accept-btn",
                             "button[class*='accept']"]:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible(timeout=3_000):
                            await btn.click()
                            await asyncio.sleep(1.0)
                            break
                    except Exception:
                        continue

                detail_urls = await self._recolectar_urls_detalle(page)
                estructura_ok = bool(detail_urls) or await self._pagina_tiene_landmark_mobile(page)
                await page.close()

                if not estructura_ok:
                    logger.warning(f"[MOBILE] Detección: estructura inesperada en {url}")
                    return [], "fallo"
                if not detail_urls:
                    return [], "vacio"

                resultados: list[dict] = []
                for detail_url in list(detail_urls)[:limite]:
                    coche = await self._extraer_detalle(context, detail_url, marca, modelo, usar_ia_co2=False)
                    if coche:
                        resultados.append(coche)
                    await asyncio.sleep(random.uniform(0.3, 0.8))

                if resultados:
                    _persistir_de_historico(resultados, marca, modelo, fuente="mobile.de")
                    return resultados, "ok"
                return [], "vacio"
            except Exception as e:
                logger.error(f"[MOBILE] Detección falló: {e}")
                return [], "fallo"
            finally:
                await browser.close()

    async def _pagina_tiene_landmark_mobile(self, page) -> bool:
        """Distingue 'mercado vacío' de 'HTML roto/bloqueo' cuando no hay links de resultado."""
        try:
            body = (await page.inner_text("body"))[:4000].lower()
        except Exception:
            return False
        # WAF/bot-block de mobile.de: "Zugriff verweigert" (acceso denegado).
        # Esto NUNCA es 'vacío legítimo' — debe contar como fallo para que el
        # circuit breaker actúe y pause la fuente en vez de reintentar en bucle.
        if any(s in body for s in ("zugriff verweigert", "access denied", "blocked",
                                     "verdächtige aktivität", "captcha")):
            return False
        if any(s in body for s in ("keine ergebnisse", "keine fahrzeuge gefunden", "leider")):
            return True
        for sel in ["h1", "[class*='SearchResult']", "form[role='search']"]:
            try:
                if await page.locator(sel).first.count():
                    return True
            except Exception:
                continue
        return False

    async def _extraer_detalle(self, context, url: str, marca: str, modelo: str,
                               usar_ia_co2: bool = True) -> dict | None:
        """
        `usar_ia_co2=False` para el sniper: CERO llamadas IA en el ciclo. La
        estimación cuando falta CO₂ la hace `calculator.estimar_co2_deterministico`
        aguas abajo (conservador, sin red).
        """
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
                    const r = {km:'', year:'', co2:'', caja:'', combustible:'', carroceria:'',
                               potencia:'', propietarios:''};
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
                    r.potencia = byId('power-v');
                    r.propietarios = byId('numberOfPreviousOwners-v');
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
                    const t = (document.body.innerText || '').toLowerCase();
                    r.netto = t.includes('mwst. ausweisbar') || t.includes('mwst ausweisbar')
                              || t.includes('nettopreis');
                    r.privado = t.includes('privatverkäufer') || t.includes('privatanbieter')
                                || t.includes('privatverkauf');
                    r.reimport = t.includes('reimport') || t.includes('eu-neuwagen')
                                 || t.includes('reimportfahrzeug');
                    r.unfallfrei = t.includes('unfallfrei');
                    r.scheckheft = t.includes('scheckheftgepflegt')
                                   || t.includes('scheckheft gepflegt');
                    r.numFotos = document.querySelectorAll(
                        "img[src*='img.classistatic.de']"
                    ).length;
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
            if co2 == 0.0 and usar_ia_co2:
                try:
                    from cabeza_bot.analisis.ai import estimar_co2
                    comb = _normalizar_combustible_de(datos.get("combustible", "")) or _detectar_combustible_titulo(titulo)
                    co2 = await estimar_co2(marca, modelo, año, comb)
                except Exception:
                    pass

            cv = 0
            pot_txt = datos.get("potencia", "") or ""
            m_ps = re.search(r"(\d{2,4})\s*PS", pot_txt)
            if m_ps:
                cv = int(m_ps.group(1))
            elif "kw" in pot_txt.lower():
                kw = _parse_numero(pot_txt)
                if kw > 0:
                    cv = round(kw * 1.35962)

            propietarios = 0
            prop_v = _parse_numero(datos.get("propietarios", "") or "0")
            if prop_v > 0:
                propietarios = int(prop_v)

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

            combustible = _normalizar_combustible_de(datos.get("combustible", ""))
            if not combustible:
                combustible = _detectar_combustible_titulo(titulo)

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
                "combustible": combustible,
                "carroceria":  _normalizar_carroceria_de(datos.get("carroceria", "")),
                "fuente":      "mobile.de",
                "cv":          cv,
                "propietarios": propietarios,
                "vendedor":    "particular" if datos.get("privado") else "haendler",
                "es_netto":    bool(datos.get("netto")),
                "reimport":    bool(datos.get("reimport")),
                "unfallfrei":  bool(datos.get("unfallfrei")),
                "scheckheftgepflegt": bool(datos.get("scheckheft")),
                "num_fotos":   int(datos.get("numFotos") or 0),
                "_detalle_completo": True,
            }
        except Exception as e:
            logger.warning(f"[MOBILE] Error detalle {url}: {e}")
            return None
        finally:
            await page.close()


