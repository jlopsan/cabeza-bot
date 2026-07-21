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

