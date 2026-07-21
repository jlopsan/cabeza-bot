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
        Visita el detalle de UN candidato y rellena campos para la cuenta y el
        semáforo de riesgo: co2, potencia (PS), nº propietarios, vendedor
        (haendler/particular), es_netto (MwSt. ausweisbar), caja, combustible,
        descripción, reimport, unfallfrei, scheckheftgepflegt, nº de fotos.
        NUNCA llama a IA. Muta y devuelve el dict.
        """
        coche.setdefault("cv", 0)
        coche.setdefault("propietarios", 0)
        coche.setdefault("vendedor", "")
        coche.setdefault("es_netto", False)
        coche.setdefault("reimport", False)
        coche.setdefault("unfallfrei", False)
        coche.setdefault("scheckheftgepflegt", False)
        coche.setdefault("num_fotos", 0)
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
                        // Reimport / EU-Neuwagen: historial de km no verificable entre países.
                        r.reimport = t.includes('reimport') || t.includes('eu-neuwagen')
                                     || t.includes('reimportfahrzeug');
                        // Unfallfrei (sin accidentes) y Scheckheftgepflegt (libro de
                        // revisiones): los vendedores alemanes SIEMPRE lo declaran si
                        // lo tienen — la ausencia es la señal, no la mención de "unfall".
                        r.unfallfrei = t.includes('unfallfrei');
                        r.scheckheft = t.includes('scheckheftgepflegt')
                                       || t.includes('scheckheft gepflegt');
                        // Nº de fotos de la galería.
                        r.numFotos = document.querySelectorAll(
                            "[class*='gallery'] img, [data-testid*='gallery'] img, "
                            + "picture img[src*='pictures.autoscout24']"
                        ).length;
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
                    coche["reimport"] = bool(datos.get("reimport"))
                    coche["unfallfrei"] = bool(datos.get("unfallfrei"))
                    coche["scheckheftgepflegt"] = bool(datos.get("scheckheft"))
                    try:
                        coche["num_fotos"] = int(datos.get("numFotos") or 0)
                    except (ValueError, TypeError):
                        coche["num_fotos"] = 0
            except Exception as e:
                logger.debug(f"[AS24] Detalle candidato falló {link}: {e}")
            finally:
                await browser.close()
        # Fallback de combustible por título (clave para el IEDMT si falta CO₂:
        # un eléctrico paga 0%, no el tramo por defecto).
        if not coche.get("combustible"):
            coche["combustible"] = _detectar_combustible_titulo(coche.get("titulo", ""))
        return coche


