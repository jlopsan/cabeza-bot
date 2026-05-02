"""
ai.py - Capa de IA usando SambaNova (Llama 4 Maverick).
Key en: https://cloud.sambanova.ai → API Keys
.env: SAMBANOVA_API_KEY=...
"""
import os, re, json, logging, asyncio, time, html as _html
from openai import AsyncOpenAI
from config import (
    TAVILY_CACHE_TTL_HOURS,
    TAVILY_DOMINIOS_FOROS,
    TAVILY_DOMINIOS_FIABILIDAD,
    TAVILY_DOMINIOS_ARTICULOS,
    ENABLE_VISION, VISION_MODEL, VISION_MAX_FOTOS, VISION_TIMEOUT_S,
    AI_TIMEOUT_S, ANALISIS_CACHE_TTL_S,
    IDEAL_CANDIDATOS_MAX,
    SAMBANOVA_API_KEY, SAMBANOVA_BASE_URL, AI_MODEL,
)

logger = logging.getLogger(__name__)
VEREDICTOS = ("OK", "SOSPECHOSO", "DESCARTADO")

# cache: (ts_epoch, dict) por (marca, modelo, año)
_INVESTIGACION_CACHE: dict[str, tuple[float, dict]] = {}

# cache de análisis completos por URL: (ts_epoch, html_str, contexto_qa)
_ANALISIS_CACHE: dict[str, tuple[float, str, dict]] = {}


def cache_get(url: str) -> tuple[str, dict, int] | None:
    """Devuelve (html, contexto, mins_ago) si hay hit válido, o None."""
    key = url.lower().split("?")[0].rstrip("/")
    ahora = time.time()
    if key in _ANALISIS_CACHE:
        ts, html_txt, contexto = _ANALISIS_CACHE[key]
        edad = ahora - ts
        if edad < ANALISIS_CACHE_TTL_S:
            return html_txt, contexto, int(edad / 60)
    return None


def cache_set(url: str, html_txt: str, contexto: dict):
    """Guarda veredicto en caché 30 min."""
    key = url.lower().split("?")[0].rstrip("/")
    _ANALISIS_CACHE[key] = (time.time(), html_txt, contexto)


# ── 1. Identificación de versión exacta del coche ──────────────────────────

async def _identificar_version(anuncio) -> dict:
    """
    Identifica versión concreta del coche (motor, CV, caja, trim) usando IA
    sobre marca/modelo/año/motor/descripción. Devuelve dict con claves:
    version (str), combustible (str), caja (str), codigo_motor (str).
    """
    system = (
        "Eres experto en motores de coches. Dado un anuncio, identifica la VERSIÓN "
        "técnica exacta (cilindrada, código motor, CV, caja, combustible, trim) "
        "Y estima peso en vacío (tara) y MMA típicos de esa versión. "
        "Responde SOLO con JSON puro sin backticks: "
        '{"version":"...","combustible":"...","caja":"...","codigo_motor":"...",'
        '"cv":int|null,"peso_vacio_kg":int|null,"mma_kg":int|null} '
        "Ejemplos: "
        "Peugeot 208 PureTech 110cv 2018 → "
        '{"version":"1.2 PureTech 110cv","combustible":"gasolina","caja":"manual",'
        '"codigo_motor":"EB2DTS","cv":110,"peso_vacio_kg":1090,"mma_kg":1565}. '
        "VW Golf 1.4 TSI 150cv DSG 2017 → "
        '{"version":"1.4 TSI 150cv DSG","combustible":"gasolina","caja":"automatico",'
        '"codigo_motor":"EA211","cv":150,"peso_vacio_kg":1320,"mma_kg":1830}. '
        "BMW 320d 2015 → "
        '{"version":"2.0d 184cv","combustible":"diesel","caja":"automatico",'
        '"codigo_motor":"N47/B47","cv":184,"peso_vacio_kg":1495,"mma_kg":2010}. '
        "Si la descripción es parca, deduce por año/modelo lo más probable. "
        "Para los pesos: para CUALQUIER modelo popular europeo (VW, Peugeot, Renault, "
        "Citroën, Opel, Ford, Seat, Skoda, Toyota, BMW, Audi, Mercedes, Hyundai, Kia, "
        "Nissan, Mazda, Honda, Fiat, Dacia) DEBES dar estimación numérica de "
        "peso_vacio_kg y mma_kg basándote en el segmento/versión similar — un margen "
        "del ±10% es totalmente aceptable y útil. Solo usa null para marcas exóticas o "
        "vehículos especiales (camiones, coches clásicos pre-1990, deportivos rarísimos)."
    )
    user_msg = (
        f"Marca: {anuncio.marca}\n"
        f"Modelo: {anuncio.modelo}\n"
        f"Año: {anuncio.año}\n"
        f"Motor (Wallapop): {getattr(anuncio, 'motor', '') or '(sin datos)'}\n"
        f"Descripción: {(anuncio.descripcion or '')[:500] or '(vacía)'}"
    )
    respuesta = await _llamar_ia(system, user_msg, max_tokens=250)

    def _to_int(v):
        try:
            n = int(v)
            return n if n > 0 else None
        except (TypeError, ValueError):
            return None

    def _cv_de_texto(*textos) -> int | None:
        """Extrae CV de strings tipo '110cv', '110 CV', '110 hp'."""
        for t in textos:
            if not t:
                continue
            m = re.search(r"(\d{2,4})\s*(?:cv|hp|ps)\b", str(t), re.IGNORECASE)
            if m:
                n = int(m.group(1))
                if 30 <= n <= 1500:
                    return n
        return None

    try:
        data = json.loads(_limpiar_json(respuesta))
        version = str(data.get("version", "")).strip()
        cv = _to_int(data.get("cv")) or _cv_de_texto(
            version, getattr(anuncio, "motor", ""), anuncio.descripcion
        )
        info = {
            "version": version,
            "combustible": str(data.get("combustible", "")).strip(),
            "caja": str(data.get("caja", "")).strip(),
            "codigo_motor": str(data.get("codigo_motor", "")).strip(),
            "cv": cv,
            "peso_vacio_kg": _to_int(data.get("peso_vacio_kg")),
            "mma_kg": _to_int(data.get("mma_kg")),
        }
        logger.info(
            f"[VERSION] cv={info['cv']} tara={info['peso_vacio_kg']} "
            f"mma={info['mma_kg']} version={info['version']!r}"
        )
        return info
    except Exception as e:
        logger.warning(f"[VERSION] Parse error: {e} | raw={respuesta!r}")
        cv = _cv_de_texto(getattr(anuncio, "motor", ""), anuncio.descripcion)
        return {"version": "", "combustible": "", "caja": "", "codigo_motor": "",
                "cv": cv, "peso_vacio_kg": None, "mma_kg": None}


# ── 2. Investigación multi-fuente via Tavily (4 queries en paralelo) ───────

async def _tavily_search(client, query: str, domains: list[str] | None, max_results: int) -> str:
    """Ejecuta una búsqueda Tavily y devuelve snippets formateados."""
    try:
        kwargs = {"query": query, "search_depth": "basic", "max_results": max_results}
        if domains:
            kwargs["include_domains"] = domains
        res = await client.search(**kwargs)
        snippets = [
            f"[{r['url']}] {(r.get('content') or '')[:250].strip()}"
            for r in res.get("results", []) if r.get("content")
        ]
        return "\n".join(snippets) if snippets else ""
    except Exception as e:
        logger.warning(f"[INVESTIGAR] Error en query '{query[:60]}': {e}")
        return ""


async def investigar_coche(version_info: dict, marca: str, modelo: str, anno: int) -> dict:
    """
    Lanza 4 búsquedas Tavily en paralelo: foros, fiabilidad, artículos, alternativas.
    Devuelve dict con 4 strings formateados para el prompt.
    Cachea 24h por (marca, modelo, año).
    """
    vacio = {"foros": "", "fiabilidad": "", "articulos": "", "alternativas": ""}
    api_key = os.getenv("TAVILY_API_KEY", "")
    if not api_key:
        return vacio

    cache_key = f"{marca.lower()}_{modelo.lower()}_{anno}"
    ahora = time.time()
    ttl = TAVILY_CACHE_TTL_HOURS * 3600
    if cache_key in _INVESTIGACION_CACHE:
        ts, cached = _INVESTIGACION_CACHE[cache_key]
        if ahora - ts < ttl:
            logger.info(f"[INVESTIGAR] Cache hit para {marca} {modelo} {anno}")
            return cached

    try:
        from tavily import AsyncTavilyClient
        client = AsyncTavilyClient(api_key=api_key)
        version = version_info.get("version", "") or ""

        q_foros = f"{marca} {modelo} {version} problemas averías opiniones"
        q_fiabilidad = f"{marca} {modelo} TÜV ADAC Dekra Pannenstatistik fiabilidad fallos"
        q_articulos = f"{marca} {modelo} {anno} análisis prueba opinión"
        q_alternativas = f"mejores alternativas {marca} {modelo} segmento fiabilidad similar precio"

        foros, fiabilidad, articulos, alternativas = await asyncio.gather(
            _tavily_search(client, q_foros, TAVILY_DOMINIOS_FOROS, 4),
            _tavily_search(client, q_fiabilidad, TAVILY_DOMINIOS_FIABILIDAD, 4),
            _tavily_search(client, q_articulos, TAVILY_DOMINIOS_ARTICULOS, 3),
            _tavily_search(client, q_alternativas, None, 4),
        )

        resultado = {
            "foros": foros,
            "fiabilidad": fiabilidad,
            "articulos": articulos,
            "alternativas": alternativas,
        }
        _INVESTIGACION_CACHE[cache_key] = (ahora, resultado)
        logger.info(
            f"[INVESTIGAR] {marca} {modelo}: "
            f"foros={len(foros.splitlines())}, fiab={len(fiabilidad.splitlines())}, "
            f"arts={len(articulos.splitlines())}, alts={len(alternativas.splitlines())}"
        )
        return resultado
    except Exception as e:
        logger.warning(f"[INVESTIGAR] Error global Tavily: {e}")
        return vacio


# Cache: (tamaño, tramo_presupuesto) → (ts, snippets_str)
_IDEAL_TAVILY_CACHE: dict[str, tuple[float, str]] = {}


async def _tavily_modelos_para_perfil(perfil: dict) -> str:
    """
    Busca en Tavily 2 queries con el perfil del usuario para obtener
    modelos REALES disponibles en su rango. Cacheado por (tamaño, presupuesto/2k).
    Devuelve string con snippets formateados o vacío.
    """
    api_key = os.getenv("TAVILY_API_KEY", "")
    if not api_key:
        return ""

    tamaño = perfil.get("tamaño") or ""
    presup = perfil.get("presupuesto_max") or 0
    if not tamaño or not presup:
        return ""

    # Clave de caché por tamaño + tramo de 2k (granularidad razonable)
    tramo = (presup // 2000) * 2000
    cache_key = f"{tamaño}_{tramo}"
    ahora = time.time()
    ttl = TAVILY_CACHE_TTL_HOURS * 3600
    if cache_key in _IDEAL_TAVILY_CACHE:
        ts, cached = _IDEAL_TAVILY_CACHE[cache_key]
        if ahora - ts < ttl:
            logger.info(f"[IDEAL_TAVILY] Cache hit: {cache_key}")
            return cached

    # Mapeo legible para queries
    _TAM = {
        "urbano": "coche urbano pequeño segmento A",
        "compacto": "coche compacto segmento B",
        "berlina": "berlina compacta segmento C",
        "suv_compacto": "SUV compacto",
        "suv_grande": "SUV grande 5 plazas",
        "familiar": "coche familiar ranchera/SW",
        "monovolumen": "monovolumen 7 plazas",
    }
    desc_tam = _TAM.get(tamaño, tamaño)

    try:
        from tavily import AsyncTavilyClient
        client = AsyncTavilyClient(api_key=api_key)

        q1 = f"mejor {desc_tam} segunda mano España {presup} euros qué modelo comprar"
        q2 = f"{desc_tam} usado {presup}€ fiable comparativa modelos recomendados"

        s1, s2 = await asyncio.gather(
            _tavily_search(client, q1, None, 5),
            _tavily_search(client, q2, None, 5),
        )
        resultado = (s1 + "\n" + s2).strip()
        _IDEAL_TAVILY_CACHE[cache_key] = (ahora, resultado)
        logger.info(f"[IDEAL_TAVILY] {cache_key}: {len(resultado.splitlines())} snippets")
        return resultado
    except Exception as e:
        logger.warning(f"[IDEAL_TAVILY] Error: {e}")
        return ""


def _client():
    return AsyncOpenAI(
        api_key=SAMBANOVA_API_KEY,
        base_url=SAMBANOVA_BASE_URL,
    )

async def _llamar_ia(
    system: str,
    user: str,
    max_tokens: int = 3000,
    model: str = AI_MODEL,
) -> str:
    try:
        resp = await asyncio.wait_for(
            _client().chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                temperature=0.1,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
            ),
            timeout=AI_TIMEOUT_S,
        )
        text = resp.choices[0].message.content.strip()
        print(f"[AI RAW] model={model} {repr(text)}")
        return text
    except asyncio.TimeoutError:
        logger.error(f"[AI] Timeout ({AI_TIMEOUT_S}s) model={model}")
        return ""
    except Exception as e:
        logger.error(f"[AI] Error SambaNova model={model}: {e}")
        return ""

def _limpiar_json(t: str) -> str:
    t = re.sub(r"^```[a-z]*\s*", "", t.strip())
    t = re.sub(r"\s*```$", "", t).strip()
    m = re.search(r"\{.*\}", t, re.DOTALL)
    return m.group(0) if m else t


async def validar_anuncios_modelo(
    marca_buscada: str,
    modelo_buscado: str,
    anuncios: list,
) -> list[int]:
    """
    Layer 1: batch-valida que los anuncios corresponden a marca+modelo buscado.
    Devuelve índices válidos (0-based). Fallback conservador si falla.
    """
    if not anuncios:
        return []

    batch = anuncios[:15]
    lineas = []
    for i, a in enumerate(batch):
        titulo = (getattr(a, "titulo", "") or "").strip()
        desc_corta = (a.descripcion or "")[:100].replace("\n", " ").strip()
        texto = titulo or desc_corta or f"{a.marca} {a.modelo}"
        lineas.append(f"{i}: {texto}")

    objetivo = f"{marca_buscada.title()} {modelo_buscado.title()}"
    system = (
        f"Validador de anuncios de coches. Se buscó: '{objetivo}'. "
        "Dado este batch (índice: texto del anuncio), devuelve SOLO un array JSON "
        "con los índices de los anuncios que SÍ son el modelo buscado. "
        "Si no estás seguro, inclúyelo. Solo excluye los claramente diferentes. "
        "Responde ÚNICAMENTE con un JSON array de enteros, ej: [0,1,3]. Sin explicación."
    )

    respuesta = await _llamar_ia(
        system, "\n".join(lineas),
        max_tokens=60,
    )

    try:
        m = re.search(r"\[[\d,\s]*\]", respuesta or "")
        if not m:
            logger.warning(f"[VALIDAR] No array en respuesta para {objetivo}: {respuesta!r}")
            return list(range(len(batch)))
        indices = json.loads(m.group(0))
        validos = [int(i) for i in indices if isinstance(i, int) and 0 <= i < len(batch)]
        if not validos:
            return list(range(len(batch)))
        n_drop = len(batch) - len(validos)
        if n_drop > 0:
            logger.info(f"[VALIDAR] {objetivo}: {len(batch)} → {len(validos)} válidos ({n_drop} descartados)")
        return validos
    except Exception as e:
        logger.warning(f"[VALIDAR] Error parseando respuesta: {e}. Pass-through.")
        return list(range(len(batch)))


def _limpiar_texto(s: str, max_chars: int = 700) -> str:
    """Normaliza texto de campo de anuncio antes de pasarlo a IA."""
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()[:max_chars]

# ── Parseo de filtros ─────────────────────────────────────────────────────

async def parsear_filtros_nl(texto_usuario: str) -> dict:
    texto = texto_usuario.strip().lower()
    if texto in ("no", "skip", "-", "", "ninguno", "sin filtros", "nada"):
        return {}

    system = (
        "Extrae filtros de busqueda de coches del texto del usuario. "
        "Responde SOLO con JSON puro sin texto ni backticks. "
        "Campos disponibles y sus tipos: "
        "  km_min (int), km_max (int): kilometraje. "
        "  year_min (int), year_max (int): año de matriculacion. "
        "  price_min (int), price_max (int): precio en euros. "
        "  power_min (int), power_max (int): potencia en CV/HP. "
        "  doors (int): numero de puertas. "
        "  color (str): uno de: negro, azul, marron, amarillo, gris, verde, rojo, plata, blanco, dorado, naranja, morado, beige. "
        "  carroceria (str): uno de: sedan, berlina, familiar, suv, todoterreno, cabrio, coupe, monovolumen, pickup. "
        "  combustible (str): uno de: gasolina, diesel, electrico, hibrido, glp. "
        "  caja (str): uno de: manual, automatico. "
        "  extras (list de str): lista de equipamientos deseados, en español. "
        "  Ejemplos extras: navegacion, cuero, techo panoramico, head-up, camara 360, "
        "    sensores aparcamiento, apple carplay, bluetooth, calefaccion asientos, "
        "    llantas aluminio, luces led, traccion integral, enganche remolque. "
        "Usa SOLO los campos mencionados por el usuario. "
        "Ejemplos: "
        '"menos de 80000 km" -> {"km_max": 80000} | '
        '"entre 2018 y 2021 color rojo" -> {"year_min": 2018, "year_max": 2021, "color": "rojo"} | '
        '"diesel automatico menos de 50k km" -> {"combustible": "diesel", "caja": "automatico", "km_max": 50000} | '
        '"suv entre 20000 y 35000 euros" -> {"carroceria": "suv", "price_min": 20000, "price_max": 35000} | '
        '"mas de 150cv hasta 2022" -> {"power_min": 150, "year_max": 2022} | '
        '"sin filtros" -> {}'
    )

    respuesta = await _llamar_ia(system, texto, max_tokens=80)
    if not respuesta:
        return _regex_fallback(texto_usuario)
    try:
        raw = json.loads(_limpiar_json(respuesta))
        result = {}
        # Campos numéricos
        for k in ("km_min", "km_max", "year_min", "year_max",
                   "price_min", "price_max", "power_min", "power_max", "doors"):
            if raw.get(k) is not None:
                try:
                    result[k] = int(raw[k])
                except (ValueError, TypeError):
                    pass
        # Campos de texto
        for k in ("color", "carroceria", "combustible", "caja"):
            if raw.get(k):
                result[k] = str(raw[k]).lower().strip()
        # Extras (lista)
        if raw.get("extras"):
            ex = raw["extras"]
            if isinstance(ex, list):
                result["extras"] = [str(e).lower().strip() for e in ex if e]
            elif isinstance(ex, str) and ex.strip():
                result["extras"] = [e.strip() for e in ex.split(",") if e.strip()]
        logger.info(f"[AI] Filtros: {result}")
        return result
    except Exception as e:
        logger.warning(f"[AI] Filtros error '{respuesta}': {e}")
        return _regex_fallback(texto_usuario)

def _regex_fallback(texto: str) -> dict:
    """Fallback regex cuando la IA no está disponible."""
    filtros = {}
    t = texto.lower()

    # km: busca patrones como "80k km", "80000 km", "entre 50k y 100k"
    kms = re.findall(r"(\d[\d.]*)\s*k(?:m\b|\b)", t)
    if len(kms) == 1:
        v = int(kms[0].replace(".", ""))
        filtros["km_max"] = v * 1000 if v < 1000 else v
    elif len(kms) >= 2:
        v0 = int(kms[0].replace(".", "")); v1 = int(kms[1].replace(".", ""))
        v0 = v0 * 1000 if v0 < 1000 else v0
        v1 = v1 * 1000 if v1 < 1000 else v1
        filtros["km_min"], filtros["km_max"] = min(v0,v1), max(v0,v1)

    # año: busca patrones como "del 2019", "hasta 2022", "entre 2018 y 2021"
    years = re.findall(r"(20\d{2})", t)
    if len(years) == 1:
        y = int(years[0])
        filtros["year_min" if "arriba" in t or "partir" in t else "year_max"] = y
    elif len(years) >= 2:
        filtros["year_min"] = min(int(y) for y in years[:2])
        filtros["year_max"] = max(int(y) for y in years[:2])

    # precio
    prices = re.findall(r"(\d[\d.]{3,})\s*[€e]", t)
    if len(prices) == 1:
        filtros["price_max"] = int(prices[0].replace(".", ""))
    elif len(prices) >= 2:
        p0 = int(prices[0].replace(".", "")); p1 = int(prices[1].replace(".", ""))
        filtros["price_min"], filtros["price_max"] = min(p0,p1), max(p0,p1)

    # color
    _COLORES = ["negro", "azul", "marron", "amarillo", "gris", "verde", "rojo",
                "plata", "plateado", "blanco", "dorado", "naranja", "morado", "beige",
                "burdeos", "granate"]
    for color in _COLORES:
        if color in t:
            filtros["color"] = color
            break

    # carrocería
    _CARROS = {"sedan": "sedan", "berlina": "sedan", "familiar": "familiar",
               "suv": "suv", "todoterreno": "suv", "cabrio": "cabrio",
               "descapotable": "cabrio", "coupe": "coupe", "coupé": "coupe",
               "monovolumen": "monovolumen", "pickup": "pickup"}
    for palabra, valor in _CARROS.items():
        if palabra in t:
            filtros["carroceria"] = valor
            break

    # combustible
    _COMBS = {"gasolina": "gasolina", "diesel": "diesel", "electrico": "electrico",
              "eléctrico": "electrico", "hibrido": "hibrido", "híbrido": "hibrido",
              "glp": "glp"}
    for palabra, valor in _COMBS.items():
        if palabra in t:
            filtros["combustible"] = valor
            break

    # caja de cambios
    if "manual" in t:
        filtros["caja"] = "manual"
    elif "automatico" in t or "automático" in t or "dsg" in t or "pdk" in t:
        filtros["caja"] = "automatico"

    # extras conocidos
    _EXTRAS_CONOCIDOS = [
        "navegacion", "cuero", "techo panoramico", "panoramico", "head-up", "hud",
        "camara 360", "camara trasera", "apple carplay", "carplay", "android auto",
        "bluetooth", "sensores aparcamiento", "luces led", "led", "xenon",
        "traccion integral", "4wd", "awd", "enganche", "remolque",
        "asientos calefactados", "llantas aluminio", "keyless", "techo solar",
    ]
    extras = [e for e in _EXTRAS_CONOCIDOS if e in t]
    if extras:
        filtros["extras"] = extras

    return filtros

# ── Análisis de anuncio ───────────────────────────────────────────────────

async def analizar_anuncio(coche: dict) -> dict:
    titulo = coche.get("titulo", "")
    precio = coche.get("precio", 0)
    km     = coche.get("km", 0)
    anno   = coche.get("año", coche.get("anno", 0))
    desc   = coche.get("descripcion", "")

    system = (
        "Eres un tasador profesional de coches usados muy experimentado. "
        "Analiza el anuncio y responde SOLO con JSON sin backticks: "
        '{"veredicto":"OK","confianza":80,"motivos":[],"resumen":""} '
        "REGLAS ESTRICTAS para cada veredicto: "
        "DESCARTADO (solo si hay evidencia CLARA y EXPLICITA): "
        "  - El texto dice literalmente 'accidentado', 'averiado', 'para piezas', 'sin ITV', 'embargado', 'inundado'. "
        "SOSPECHOSO (solo si hay señal concreta, no por precio bajo): "
        "  - Precio MUY inferior al mercado (mas del 40% por debajo del tipico para ese año/km). "
        "  - Descripcion de 0 palabras util o solo numeros de telefono. "
        "  - Fotos claramente de catalogo digital sin coche real. "
        "OK (caso por defecto): "
        "  - Precio normal o alto para el mercado. "
        "  - Descripcion aunque sea breve. "
        "  - Un precio bajo NO es motivo de SOSPECHOSO si no hay otras señales. "
        "  - La mayoria de anuncios normales deben ser OK. "
        "confianza: 70-90 para OK, 50-70 para SOSPECHOSO, 80-95 para DESCARTADO."
    )
    user_msg = (
        f"Titulo: {titulo}\n"
        f"Anno: {anno} | Km: {km:,} | Precio: {precio:,.0f} EUR\n"
        f"Descripcion: {desc[:500] if desc else 'sin descripcion'}"
    )

    respuesta = await _llamar_ia(system, user_msg, max_tokens=250)
    if not respuesta:
        return {"veredicto": "OK", "confianza": 50, "motivos": [], "resumen": "Sin analisis IA"}
    try:
        r = json.loads(_limpiar_json(respuesta))
        v = str(r.get("veredicto", "OK")).upper()
        r["veredicto"] = v if v in VEREDICTOS else "OK"
        r.setdefault("confianza", 70)
        r.setdefault("motivos", [])
        r.setdefault("resumen", "")
        return r
    except Exception as e:
        logger.warning(f"[AI] Analisis error: {e}")
        return {"veredicto": "OK", "confianza": 50, "motivos": [], "resumen": "Error analisis"}


# ════════════════════════════════════════════════════════════════════════════
# NORMALIZAR MODELO PARA BÚSQUEDA EN WALLAPOP
# ════════════════════════════════════════════════════════════════════════════

async def normalizar_modelo_wallapop(marca: str, modelo: str) -> str:
    """
    Extrae solo el nombre base del modelo para buscar en Wallapop ES.
    Elimina variantes, trims, niveles de equipamiento y extras.

    Ejemplos:
      "m3 competition"    → "m3"
      "golf gti"          → "golf"
      "a3 sportback 35"   → "a3"
      "clase c 220d"      → "clase c"
      "serie 3 320d"      → "serie 3"
    """
    system = (
        "Extrae solo el nombre BASE del modelo de coche, sin variantes ni trims. "
        "Responde SOLO con el nombre base, sin JSON ni explicacion. "
        "Ejemplos: "
        "'m3 competition' -> 'm3' | "
        "'golf gti' -> 'golf' | "
        "'a3 sportback 35 tfsi' -> 'a3' | "
        "'clase c 220d amg' -> 'clase c' | "
        "'serie 3 320d xdrive' -> 'serie 3' | "
        "'rs6 avant' -> 'rs6' | "
        "'mustang mach-e gt' -> 'mustang'"
    )
    respuesta = await _llamar_ia(system, modelo.strip(), max_tokens=20)
    modelo_base = respuesta.strip().lower() if respuesta else modelo.split()[0]
    # Sanity check: no devolver cadena vacía ni muy larga
    if not modelo_base or len(modelo_base) > 20:
        modelo_base = modelo.split()[0]
    logger.info(f"[AI] Modelo normalizado para Wallapop: '{modelo}' → '{modelo_base}'")
    return modelo_base


async def estimar_co2(marca: str, modelo: str, año: int, combustible: str = "") -> float:
    """
    Estima las emisiones CO₂ (g/km) cuando no están disponibles en el anuncio.
    Devuelve 0.0 si no puede estimar.
    """
    system = (
        "Eres un experto en especificaciones técnicas de coches. "
        "Dado un coche, estima sus emisiones de CO2 en g/km (ciclo WLTP o NEDC). "
        "Responde SOLO con el número entero, sin unidades ni texto. "
        "Ejemplos: 'audi a3 2019 gasolina' -> 128 | 'bmw m3 2020 gasolina' -> 185 | "
        "'volkswagen golf 2018 diesel' -> 112 | 'tesla model 3 2021 electrico' -> 0"
    )
    user = f"{marca} {modelo} {año} {combustible}".strip()
    respuesta = await _llamar_ia(system, user, max_tokens=10)
    try:
        val = float(respuesta.strip().split()[0])
        if 0 <= val <= 400:
            logger.info(f"[AI] CO2 estimado para {user}: {val} g/km")
            return val
    except Exception:
        pass
    return 0.0


async def filtrar_por_extras(coches: list[dict], extras_requeridos: list[str]) -> list[dict]:
    """
    CAPA 2 del filtrado de extras.
    Para cada coche, pregunta a la IA si tiene los extras requeridos
    basándose en el título y descripción del anuncio.

    Descarta coches que claramente NO tienen el extra pedido.
    Mantiene los que SÍ tienen o no hay suficiente información.
    """
    if not extras_requeridos or not coches:
        return coches

    extras_str = ", ".join(extras_requeridos)

    async def verificar_uno(coche: dict) -> dict | None:
        titulo = coche.get("titulo", "")
        desc   = coche.get("descripcion", "")
        texto  = titulo + "\n" + desc[:600]

        system = (
            "Eres experto en equipamiento de coches. "
            "Analiza si el anuncio menciona los extras pedidos. "
            'Responde SOLO con JSON: {"tiene": true/false, "certeza": 0-100} '
            "tiene=true si los extras aparecen en el texto (aunque sea en alemán). "
            "tiene=false SOLO si el texto contradice explícitamente su presencia. "
            "Si no hay información suficiente, pon tiene=true (beneficio de la duda). "
            "Traducciones útiles: Navi=navegación, Leder=cuero, Panorama=techo panorámico, "
            "SHZ=asientos calefactados, HUD=head-up, 360=cámara 360, AHK=enganche remolque, "
            "ACC=radar adaptativo, LED=faros led, HK=Harman Kardon."
        )
        user = "Extras buscados: " + extras_str + "\n\nAnuncio:\n" + texto

        respuesta = await _llamar_ia(system, user, max_tokens=60)
        try:
            r = json.loads(_limpiar_json(respuesta))
            tiene    = r.get("tiene", True)
            certeza  = int(r.get("certeza", 50))
            # Solo descartar si la IA está muy segura de que NO tiene el extra
            if not tiene and certeza >= 80:
                logger.info(f"[AI] Extra '{extras_str}' descartado: {titulo[:40]}")
                return None
        except Exception:
            pass
        return coche

    sem = asyncio.Semaphore(5)
    async def verificar_con_sem(c):
        async with sem:
            return await verificar_uno(c)

    resultados = await asyncio.gather(*[verificar_con_sem(c) for c in coches])
    filtrados = [c for c in resultados if c is not None]
    logger.info(f"[AI] Post-filtrado extras: {len(coches)} → {len(filtrados)} coches")
    return filtrados

# ── Enriquecer lista ──────────────────────────────────────────────────────

async def enriquecer_coches(coches: list[dict]) -> list[dict]:
    sem = asyncio.Semaphore(3)
    async def uno(c):
        async with sem:
            c["analisis_ia"] = await analizar_anuncio(c)
            return c
    result = await asyncio.gather(*[uno(c) for c in coches])
    orden = {"OK": 0, "SOSPECHOSO": 1, "DESCARTADO": 2}
    result.sort(key=lambda c: orden.get(c.get("analisis_ia", {}).get("veredicto", "OK"), 0))
    return result

# ── Helpers tarjeta ───────────────────────────────────────────────────────

def emoji_veredicto(a: dict | None) -> str:
    return {"OK": "✅", "SOSPECHOSO": "⚠️", "DESCARTADO": "🚫"}.get(
        (a or {}).get("veredicto", "OK"), ""
    )

def texto_analisis(a: dict | None) -> str:
    if not a:
        return ""
    v, resumen, motivos = a.get("veredicto", "OK"), a.get("resumen", ""), a.get("motivos", [])
    e = emoji_veredicto(a)
    if v == "OK" and not motivos:
        return f"{e} <i>Sin alertas</i>"
    lineas = [f"{e} <b>IA: {v}</b>"]
    if resumen:
        lineas.append(f"<i>{resumen}</i>")
    lineas.extend(f"• {m}" for m in motivos[:3])
    return "\n".join(lineas)


# ════════════════════════════════════════════════════════════════════════════
# EXTRA: PARSEO DE MARCA/MODELO EN LENGUAJE NATURAL
# ════════════════════════════════════════════════════════════════════════════

async def parsear_modelo_nl(texto: str) -> dict:
    """
    Extrae marca y modelo de texto libre.
    Devuelve {"marca": str, "modelo": str}
    Ejemplos:
      "un golf gti" -> {"marca": "volkswagen", "modelo": "golf gti"}
      "mercedes clase c 220"  -> {"marca": "mercedes-benz", "modelo": "clase c"}
      "bmw serie 3"  -> {"marca": "bmw", "modelo": "serie 3"}
    """
    system = (
        "Extrae la marca y modelo de coche del texto. "
        "Responde SOLO con JSON sin backticks: {\"marca\": \"volkswagen\", \"modelo\": \"golf\"} "
        "La marca debe ser el nombre oficial en minusculas tal como lo usa AutoScout24 "
        "(volkswagen, bmw, mercedes-benz, audi, ford, opel, seat, skoda, toyota, etc). "
        "El modelo debe ser solo el nombre del modelo sin la marca. "
        "Si no puedes extraer marca o modelo, pon string vacio."
    )
    respuesta = await _llamar_ia(system, texto.strip(), max_tokens=60)
    if not respuesta:
        # Fallback: primera palabra = marca, resto = modelo
        partes = texto.strip().split(maxsplit=1)
        return {"marca": partes[0].lower(), "modelo": partes[1].lower() if len(partes) > 1 else partes[0].lower()}
    try:
        r = json.loads(_limpiar_json(respuesta))
        return {
            "marca":  str(r.get("marca", "")).lower().strip(),
            "modelo": str(r.get("modelo", "")).lower().strip(),
        }
    except Exception:
        partes = texto.strip().split(maxsplit=1)
        return {"marca": partes[0].lower(), "modelo": partes[1].lower() if len(partes) > 1 else partes[0].lower()}


# ════════════════════════════════════════════════════════════════════════════
# EXTRA: VALIDAR PRECIO MEDIO DE WALLAPOP
# ════════════════════════════════════════════════════════════════════════════

# ── Análisis visual de fotos (vision LLM) ─────────────────────────────────

_DEFECTOS_VALIDOS = {
    "golpe_chapa", "oxido", "neumatico_liso", "asiento_roto",
    "salpicadero_dañado", "motor_sucio", "sin_revision", "otro",
}


async def _vision_una_foto(client, url: str, idx: int) -> dict | None:
    """Analiza una foto y devuelve dict {defectos, estado_general, km_cuadro}."""
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=VISION_MODEL,
                max_tokens=200,
                temperature=0.1,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": (
                            "Mira esta foto de un coche en venta. Devuelve SOLO JSON: "
                            '{"defectos":["..."],"estado_general":"bueno|aceptable|malo",'
                            '"km_cuadro":number_or_null,"alerta":"texto_corto_o_null"}. '
                            "Etiquetas válidas para defectos: golpe_chapa, oxido, neumatico_liso, "
                            "asiento_roto, salpicadero_dañado, motor_sucio, sin_revision, otro. "
                            "km_cuadro solo si la foto muestra el cuentakilómetros. "
                            "alerta: SOLO si ves algo grave (golpe estructural, óxido perforante)."
                        )},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                }],
            ),
            timeout=VISION_TIMEOUT_S,
        )
        raw = resp.choices[0].message.content
        data = json.loads(_limpiar_json(raw))
        defectos = [d for d in (data.get("defectos") or []) if d in _DEFECTOS_VALIDOS]
        return {
            "defectos": defectos,
            "estado_general": str(data.get("estado_general") or "").lower(),
            "km_cuadro": data.get("km_cuadro") if isinstance(data.get("km_cuadro"), (int, float)) else None,
            "alerta": (data.get("alerta") or None) if isinstance(data.get("alerta"), str) else None,
        }
    except asyncio.TimeoutError:
        logger.warning(f"[VISION] Foto #{idx} timeout")
    except Exception as e:
        logger.warning(f"[VISION] Foto #{idx} error: {e}")
    return None


async def analizar_fotos(fotos: list[str], anuncio_km: int = 0) -> dict | None:
    """
    Analiza hasta VISION_MAX_FOTOS en paralelo. Devuelve {"texto": str, "alerta_km": str|None}
    o None si no hay fotos / vision desactivada / todo falló.
    """
    if not ENABLE_VISION or not fotos:
        return None
    # Selección: 1ª, última, y hasta 2 al azar del medio
    n_max = min(VISION_MAX_FOTOS, len(fotos))
    seleccion = []
    if len(fotos) <= n_max:
        seleccion = list(fotos)
    else:
        seleccion = [fotos[0], fotos[-1]]
        medio = fotos[1:-1]
        if medio and n_max > 2:
            paso = max(1, len(medio) // (n_max - 2))
            seleccion += medio[::paso][: n_max - 2]
    seleccion = seleccion[:n_max]

    client = _client()
    resultados = await asyncio.gather(
        *[_vision_una_foto(client, u, i) for i, u in enumerate(seleccion)],
        return_exceptions=False,
    )
    resultados = [r for r in resultados if r]
    if not resultados:
        return None

    # Agregar
    defectos_agg: dict[str, int] = {}
    estados: list[str] = []
    km_cuadro: int | None = None
    alertas: list[str] = []
    for r in resultados:
        for d in r["defectos"]:
            defectos_agg[d] = defectos_agg.get(d, 0) + 1
        if r["estado_general"]:
            estados.append(r["estado_general"])
        if r["km_cuadro"] and km_cuadro is None:
            km_cuadro = int(r["km_cuadro"])
        if r["alerta"]:
            alertas.append(r["alerta"])

    # Texto sintético
    partes: list[str] = []
    if defectos_agg:
        top = sorted(defectos_agg.items(), key=lambda x: -x[1])
        partes.append("Detectado en fotos: " + ", ".join(d.replace("_", " ") for d, _ in top) + ".")
    if estados:
        peor = "malo" if "malo" in estados else ("aceptable" if "aceptable" in estados else "bueno")
        partes.append(f"Estado general aparente: {peor}.")
    if km_cuadro:
        partes.append(f"Cuadro muestra ~{km_cuadro:,} km.")
    if alertas:
        partes.append("⚠️ " + " · ".join(alertas[:2]))
    texto = " ".join(partes) if partes else "Sin defectos visibles claros."

    # Cross-check km cuadro vs anuncio
    alerta_km = None
    if km_cuadro and anuncio_km and abs(km_cuadro - anuncio_km) / max(anuncio_km, 1) > 0.10:
        alerta_km = (
            f"El cuadro muestra ~{km_cuadro:,} km pero el anuncio dice {anuncio_km:,} km. "
            "Diferencia >10% — pide explicación."
        )

    return {"texto": texto, "alerta_km": alerta_km}


# ── Preguntas para el vendedor + checklist presencial ─────────────────────

async def preguntas_y_checklist(version_info: dict, marca: str, modelo: str,
                                  averias_resumen: str = "") -> dict | None:
    """
    Una llamada IA que devuelve {"preguntas": [...], "checklist": [...]}.
    Personalizadas al motor identificado y a las averías típicas conocidas.
    Devuelve None si la IA falla o el JSON está malformado.
    """
    version = version_info.get("version") or f"{marca} {modelo}"
    codigo = version_info.get("codigo_motor") or ""
    combustible = version_info.get("combustible") or ""

    system = (
        "Eres un mecánico que ayuda a comprar coches usados. Genera preguntas "
        "para el vendedor (cortas, copiables a WhatsApp) y un checklist para "
        "revisar el coche en persona. TODO debe ser específico al motor y "
        "averías típicas conocidas, no genérico. "
        "Responde SOLO con JSON sin backticks: "
        '{"preguntas": ["¿...?", ...6 items], "checklist": ["...", ...10 items]}. '
        "Las preguntas empiezan con '¿' y terminan con '?'. "
        "El checklist son acciones imperativas cortas (Arrancar en frío, "
        "Comprobar fugas bajo el motor, etc.)."
    )
    user_msg = (
        f"Coche: {marca} {modelo}\n"
        f"Versión: {version}\n"
        f"Código motor: {codigo or '(sin datos)'}\n"
        f"Combustible: {combustible or '(sin datos)'}\n"
        f"Averías típicas conocidas (resumen): {averias_resumen[:600] or '(sin datos)'}"
    )
    raw = await _llamar_ia(system, user_msg, max_tokens=600)
    if not raw:
        return None
    try:
        data = json.loads(_limpiar_json(raw))
        preguntas = [str(p).strip() for p in (data.get("preguntas") or []) if str(p).strip()][:8]
        checklist = [str(c).strip() for c in (data.get("checklist") or []) if str(c).strip()][:12]
        if not preguntas or not checklist:
            return None
        return {"preguntas": preguntas, "checklist": checklist}
    except Exception as e:
        logger.warning(f"[PREGUNTAS] Parse error: {e} | raw={raw!r}")
        return None


def _normalizar_motor(s: str) -> str:
    """Extrae tokens clave del motor: cilindrada + tecnología + CV."""
    s = s.lower()
    tokens = []
    for t in re.findall(r"\d+[.,]?\d*", s):
        try:
            v = float(t.replace(",", "."))
            if 0.5 <= v <= 6.0:
                tokens.append(f"{v:.1f}")
            elif 50 <= v <= 600:
                tokens.append(str(int(v)))
        except ValueError:
            pass
    for kw in ("tsi", "tfsi", "tdi", "cdi", "hdi", "dci", "jtd", "tdci",
               "gdi", "crdi", "vtec", "puretech", "bluehdi", "tce", "dig-t",
               "mhev", "phev", "hybrid", "hibrido", "electric"):
        if kw in s:
            tokens.append(kw)
    return " ".join(tokens)


def _bloque_motor_mas_barato(anuncio, comparables: list, version_info: dict) -> str:
    """Busca comparables con mismo motor normalizado y precio >=5% menor."""
    motor_ref = _normalizar_motor(
        version_info.get("version") or getattr(anuncio, "motor", "") or ""
    )
    if not motor_ref or anuncio.precio <= 0:
        return ""
    alternativas = []
    for c in (comparables or []):
        if c.precio <= 0 or c.precio >= anuncio.precio * 0.95:
            continue
        motor_c = _normalizar_motor(getattr(c, "motor", "") or "")
        if not motor_c or motor_c != motor_ref:
            continue
        ahorro = anuncio.precio - c.precio
        if ahorro < 300:
            continue
        alternativas.append((ahorro, c))
    if not alternativas:
        return ""
    alternativas.sort(key=lambda x: -x[0])
    lineas = []
    for ahorro, c in alternativas[:2]:
        linea = (
            f"• {c.año} · {c.km:,} km · <b>{c.precio:,.0f}€</b> "
            f"({_html.escape(c.provincia or '?')}) — {ahorro:,.0f}€ menos"
        )
        if c.url:
            linea += f" <a href='{c.url}'>Ver anuncio</a>"
        lineas.append(linea)
    return (
        "\n\n<b>💸 OPCIÓN MÁS BARATA CON EL MISMO MOTOR</b>\n"
        + "\n".join(lineas)
    )


def _calcular_relacion_peso_potencia(version_info: dict) -> dict | None:
    """Calcula kg/CV y CV/ton en vacío y a plena carga. None si faltan datos."""
    cv   = version_info.get("cv")
    tara = version_info.get("peso_vacio_kg")
    mma  = version_info.get("mma_kg")
    if not cv or not tara:
        return None
    out = {
        "cv": cv,
        "tara": tara,
        "kg_por_cv_vacio":  round(tara / cv, 1),
        "cv_por_ton_vacio": round(cv * 1000 / tara, 1),
    }
    if mma and mma > tara:
        out["mma"] = mma
        out["carga_util"] = mma - tara
        out["kg_por_cv_carga"]  = round(mma / cv, 1)
        out["cv_por_ton_carga"] = round(cv * 1000 / mma, 1)
    r = out["kg_por_cv_vacio"]
    if   r < 8:  cat = "deportivo"
    elif r < 11: cat = "ágil"
    elif r < 14: cat = "normal"
    elif r < 17: cat = "justo"
    else:        cat = "muy justo (sobre todo cargado)"
    out["categoria"] = cat
    return out


async def generar_veredicto_analizar(
    anuncio, stats, comparables: list | None = None,
    fuentes_count: dict[str, int] | None = None,
) -> tuple[str, dict]:
    """
    Veredicto de experto: versión, precio, fiabilidad con score 0-100, averías
    específicas del motor, equipamiento, alternativas, artículos y veredicto final.
    Orquesta _identificar_version + investigar_coche (4 Tavily paralelos) + IA síntesis.
    """
    desv_pct = stats.desviacion_pct
    comparables = comparables or []

    # ── 1. Identificar versión exacta + investigar en paralelo ───────────────
    logger.info(f"[VEREDICTO] Identificando versión de {anuncio.marca} {anuncio.modelo} {anuncio.año}")
    version_info = await _identificar_version(anuncio)
    logger.info(f"[VEREDICTO] Versión: {version_info.get('version', '(sin datos)')}")

    # Investigación + análisis visual en paralelo
    fotos_anuncio = getattr(anuncio, "fotos", None) or ([anuncio.foto] if anuncio.foto else [])
    research, vision = await asyncio.gather(
        investigar_coche(version_info, anuncio.marca, anuncio.modelo, anuncio.año),
        analizar_fotos(fotos_anuncio, anuncio_km=anuncio.km),
    )

    # ── 2. Muestra de comparables (reducida a 3 para payload más pequeño) ────
    sample = sorted(comparables, key=lambda c: c.precio)[:3]
    sample_txt = "\n".join(
        f"  • {c.año} · {c.km:,}km · {c.precio:,.0f}€ "
        f"({_html.escape(c.provincia or '?')}) — "
        f"{_html.escape((c.descripcion or '').replace(chr(10), ' '))[:100].strip()}"
        for c in sample
    ) or "  (sin muestra disponible)"

    # ── 3. Bloque INVESTIGACIÓN para el prompt ───────────────────────────────
    def _seccion(titulo: str, contenido: str) -> str:
        return f"\n\n=== {titulo} ===\n{contenido}" if contenido else ""

    investigacion_txt = (
        _seccion("FOROS (problemas reportados por usuarios)", research["foros"])
        + _seccion("FIABILIDAD (TÜV / ADAC / Dekra / estudios)", research["fiabilidad"])
        + _seccion("ARTÍCULOS Y RESEÑAS", research["articulos"])
        + _seccion("ALTERNATIVAS SUGERIDAS POR LA WEB", research["alternativas"])
    )

    # ── 4. System prompt de 10 bloques con reglas duras ───────────────────────
    system = (
        "Eres Juan Lopera, experto en coches usados del mercado español. "
        "Analizas anuncios y das un veredicto de EXPERTO, con datos por delante. "
        "Tono directo, un poco incrédulo ante lo absurdo. Ni gurú ni payaso. "
        "Respondes SOLO en HTML de Telegram (<b>, <i>, <a href=\"\">, saltos de línea). "
        "NO uses markdown (nada de ** ni ``` ni #). Cero relleno ('en conclusión', 'en resumen').\n\n"
        "REGLAS DURAS:\n"
        "• Enlaces: usa SOLO URLs que aparezcan literalmente en la sección INVESTIGACIÓN. "
        "Si una URL no está ahí, NO la inventes.\n"
        "• Score fiabilidad 0-100: USA EL RANGO COMPLETO sin miedo. "
        "Anclas de referencia (calibra con estas): "
        "Lexus IS/ES/RX = 92-96 | Toyota Corolla/Yaris = 88-93 | "
        "Honda Civic/Jazz = 82-88 | VW Golf 1.6 TDI = 68-74 | "
        "Peugeot 208 1.2 PureTech EB2 (correa baño aceite) = 22-32 | "
        "BMW N47 (cadena trasera) = 38-48 | VW 2.0 TSI EA888 (consumo aceite) = 45-55 | "
        "Renault 1.2 TCe = 30-40 | DSG 7v mecatrónico seco = 40-50. "
        "Si no hay datos en INVESTIGACIÓN, di 'datos insuficientes, score provisional ~50/100'. "
        "NUNCA comprimas los scores hacia el centro — si es muy fiable, da 90+; si es notoriamente malo, da <35.\n"
        "• Averías: menciona el problema famoso del motor concreto si aparece "
        "(correa baño aceite PureTech EB2, consumo aceite EA888 TSI, cadena N47, "
        "DSG 7v mecatrónico, etc.). Cita fuente de INVESTIGACIÓN si existe.\n"
        "• Alternativas: si en INVESTIGACIÓN o tu conocimiento hay un modelo del "
        "MISMO SEGMENTO Y RANGO DE PRECIO con mejor fiabilidad, dilo CLARO.\n\n"
        "FORMATO EXACTO de 10 bloques en este orden:\n\n"
        "<b>🎯 VERSIÓN IDENTIFICADA</b>\n"
        "1 línea técnica: motor, CV, caja, combustible, código motor si aplica.\n\n"
        "<b>⏳ VIDA ÚTIL ESTIMADA</b>\n"
        "1-2 frases evaluando los kilómetros actuales. Explica si son excesivos y el coche ya no merece la pena, o si da para 10 años más de uso (sé realista).\n\n"
        "<b>🐎 POTENCIA Y DINÁMICA</b>\n"
        "Si en el input hay sección RELACIÓN PESO/POTENCIA con datos: "
        "primera línea LITERAL con el cálculo: '<b>X CV · ~Y kg vacío · Z kg/CV</b> (categoría)'. "
        "Si además hay datos cargado: segunda línea '<i>A tope de carga (W kg / Carga útil C kg): K kg/CV — pierde notablemente.</i>'. "
        "Después 1-2 frases en lenguaje natural: si los caballos son adecuados, "
        "qué tal con familia + maletas o cuesta arriba, y si el motor se queda corto cargado. "
        "Si los datos vienen marcados como estimación, no menciones la palabra 'estimación' al usuario "
        "(él no necesita saberlo, pero no des los kg como dato del anuncio). "
        "Si NO hay datos suficientes, salta el cálculo y da solo la valoración cualitativa. "
        "Para eléctricos matiza que el par instantáneo compensa el peso de batería.\n\n"
        "<b>💰 PRECIO vs MERCADO</b>\n"
        "2-3 frases. ¿Barato/justo/caro? Justifica con km, año y equipamiento detectado. "
        "Si la muestra mezcla Wallapop (particulares) y Coches.net (dealers) y la diferencia "
        "es notable, menciónalo (markup dealer vs precio particular).\n\n"
        "<b>🛡️ FIABILIDAD · SCORE X/100 · ETIQUETA</b>\n"
        "Sustituye X por el score numérico y ETIQUETA por una de estas según el score: "
        "90+ EXCELENTE | 75-89 MUY FIABLE | 60-74 FIABLE | 45-59 REGULAR | 30-44 POCO FIABLE | <30 MUY POCO FIABLE. "
        "2-3 frases justificando, SIN repetir el número (ya está en el título). "
        "Cita TÜV/ADAC/Dekra o volumen de quejas en foros.\n\n"
        "<b>🔧 AVERÍAS TÍPICAS DE ESTA VERSIÓN</b>\n"
        "2-4 frases específicas al motor identificado (no al modelo genérico). "
        "Termina con 1-2 cosas concretas a revisar al ir a verlo.\n\n"
        "<b>🎁 EQUIPAMIENTO</b>\n"
        "1-2 frases. Extras detectados en la descripción y si están a la altura del precio.\n\n"
        "<b>🏷️ ETIQUETA DGT · ZBE</b>\n"
        "Usa la etiqueta y el texto ZBE EXACTOS que aparecen en la sección 'ETIQUETA DGT' del input. "
        "1-2 frases en lenguaje claro: di qué etiqueta lleva y dónde podrá circular.\n\n"
        "<b>🔄 ALTERNATIVAS MEJORES</b>\n"
        "2-3 modelos del mismo segmento/precio. Una línea por alternativa con pro/contra. "
        "Si una tiene claramente mejor fiabilidad que éste, dilo sin rodeos.\n\n"
        "<b>📰 ARTÍCULOS RECOMENDADOS</b>\n"
        "2-3 enlaces <a href=\"URL\">título</a> con 1-line summary. URLs SOLO de INVESTIGACIÓN.\n\n"
        "<b>✅ VEREDICTO</b>\n"
        "Primero una etiqueta en negrita en su propia línea, OBLIGATORIAMENTE una de estas tres exactas:\n"
        "<b>✅ RECOMENDABLE</b> — si merece la pena comprarlo.\n"
        "<b>⚠️ NEGOCIAR PRECIO</b> — si puede ser buena compra bajando el precio.\n"
        "<b>❌ NO RECOMENDABLE</b> — si hay razones claras para descartarlo.\n"
        "REGLA DE ORO para elegir la etiqueta: la fiabilidad pesa MÁS que el precio. "
        "Score < 40 → siempre <b>❌ NO RECOMENDABLE</b>, no importa lo barato que esté "
        "(un coche barato con motor problemático sigue siendo una trampa). "
        "Score 40-60 + precio caro → <b>⚠️ NEGOCIAR PRECIO</b>. "
        "Score 40-60 + precio justo → <b>⚠️ NEGOCIAR PRECIO</b> o <b>❌ NO RECOMENDABLE</b> según averías. "
        "Score > 60 + precio razonable → <b>✅ RECOMENDABLE</b>. "
        "Después, en la línea siguiente, 1-2 frases explicando la razón principal."
    )

    # ── 5. User message ──────────────────────────────────────────────────────
    version = version_info.get("version") or "(no identificada)"
    codigo = version_info.get("codigo_motor") or "?"
    combustible = version_info.get("combustible") or "?"
    caja = version_info.get("caja") or "?"

    # DGT
    from dgt import calcular_etiqueta_dgt, info_zbe
    etiqueta = calcular_etiqueta_dgt(combustible, anuncio.año)
    zbe_txt = info_zbe(etiqueta)

    # Relación peso/potencia (determinista, basada en estimación del LLM)
    relacion = _calcular_relacion_peso_potencia(version_info)
    if relacion:
        linea1 = (
            f"En vacío: {relacion['tara']} kg / {relacion['cv']} CV "
            f"= {relacion['kg_por_cv_vacio']} kg/CV "
            f"({relacion['cv_por_ton_vacio']} CV/ton) → {relacion['categoria']}"
        )
        if "mma" in relacion:
            linea2 = (
                f"A plena carga (MMA {relacion['mma']} kg, carga útil "
                f"{relacion['carga_util']} kg): {relacion['kg_por_cv_carga']} kg/CV "
                f"({relacion['cv_por_ton_carga']} CV/ton). "
                "Pérdida de aceleración real con 5 personas + maletas notable."
            )
            relacion_txt = (
                "RELACIÓN PESO/POTENCIA (estimación, NO del anuncio):\n"
                f"  {linea1}\n  {linea2}\n\n"
            )
        else:
            relacion_txt = (
                "RELACIÓN PESO/POTENCIA (estimación, NO del anuncio):\n"
                f"  {linea1}\n\n"
            )
    else:
        relacion_txt = "RELACIÓN PESO/POTENCIA: datos insuficientes\n\n"

    desc_limpia = _limpiar_texto(anuncio.descripcion or "")
    user_msg = (
        "ANUNCIO:\n"
        f"Coche: {anuncio.marca.title()} {anuncio.modelo.upper()}\n"
        f"Año: {anuncio.año} | Km: {anuncio.km:,} | Precio: {anuncio.precio:,.0f}€\n"
        f"Provincia: {anuncio.provincia or 'desconocida'}\n"
        f"Descripción: {desc_limpia or '(vacía)'}\n\n"
        "VERSIÓN IDENTIFICADA:\n"
        f"{version} | combustible={combustible} | caja={caja} | código motor={codigo}\n\n"
        f"{relacion_txt}"
        "ETIQUETA DGT:\n"
        f"Etiqueta: {etiqueta}\n"
        f"ZBE: {zbe_txt}\n\n"
        f"MERCADO ({stats.n_comparables} comparables):\n"
        f"Mediana {stats.mediana:,.0f}€ | Media {stats.media:,.0f}€ | "
        f"Desv. típica {stats.desviacion:,.0f}€\n"
        f"Percentil del anuncio: {stats.percentil:.0f}/100 ({desv_pct:+.1f}% vs mediana)\n"
        f"Fuentes: Wallapop {(fuentes_count or {}).get('wallapop', 0)} (particulares) · "
        f"Coches.net {(fuentes_count or {}).get('coches.net', 0)} (dealers)\n\n"
        f"MUESTRA DE COMPARABLES (3 más baratos):\n{sample_txt}"
        f"{investigacion_txt}"
    )

    texto_ia = await _llamar_ia(system, user_msg, max_tokens=1500)

    p = int(stats.percentil)
    if p <= 25:
        posicion_txt = f"Más barato que el {100 - p}% del mercado 🟢"
    elif p <= 50:
        posicion_txt = f"Por debajo de la media ({100 - p}% son más caros) 🟢"
    elif p <= 75:
        posicion_txt = f"Por encima de la media ({p}% son más baratos) 🟡"
    else:
        posicion_txt = f"Más caro que el {p}% del mercado 🔴"

    cabecera_datos = (
        f"<b>📊 Resumen de mercado</b>\n"
        f"• Precio anuncio: <b>{anuncio.precio:,.0f}€</b>  ·  "
        f"Mediana: <b>{stats.mediana:,.0f}€</b>  ({desv_pct:+.1f}%)\n"
        f"• Comparables analizados: {stats.n_comparables}\n"
        f"• {posicion_txt}\n"
        f"{'─' * 30}\n\n"
    )

    # Precio anormalmente bajo (B1)
    bloque_precio_anomalo = ""
    if stats.mediana > 0 and anuncio.precio > 0 and anuncio.precio < stats.mediana * 0.40:
        pct = round((1 - anuncio.precio / stats.mediana) * 100)
        bloque_precio_anomalo = (
            f"🚨 <b>PRECIO ANORMALMENTE BAJO</b>\n"
            f"Este anuncio cuesta un {pct}% menos que la mediana del mercado. "
            "Casos típicos: estafa, golpe estructural oculto, urgencia real del vendedor, "
            "error tipográfico. Pide vídeo en directo y verifica DNI antes de mover dinero.\n\n"
        )

    # Señales de alerta (lógica determinista)
    from red_flags import detectar_red_flags
    flags = detectar_red_flags(anuncio, stats)
    if vision and vision.get("alerta_km"):
        flags.append(vision["alerta_km"])
    bloque_flags = ""
    if flags:
        bloque_flags = "<b>🚩 SEÑALES DE ALERTA</b>\n" + "\n".join(f"• {f}" for f in flags) + "\n\n"

    # Análisis visual
    bloque_fotos = ""
    if vision and vision.get("texto"):
        bloque_fotos = f"<b>📸 ANÁLISIS DE FOTOS</b>\n{vision['texto']}\n\n"

    cuerpo = texto_ia or "⚠️ No pude generar el análisis IA."
    # Alternativa más barata con mismo motor (B2)
    bloque_motor = _bloque_motor_mas_barato(anuncio, comparables, version_info)
    html_veredicto = (
        bloque_precio_anomalo
        + cabecera_datos
        + bloque_flags
        + bloque_fotos
        + cuerpo
        + bloque_motor
    )
    contexto = {
        "marca": anuncio.marca,
        "modelo": anuncio.modelo,
        "version_info": version_info,
        "foros": (research.get("foros", "") or "")[:600],
    }
    return html_veredicto, contexto


def formatear_qa(qa: dict) -> str:
    """Formatea el dict {preguntas, checklist} como HTML para Telegram."""
    if not qa:
        return ""
    preguntas_html = "\n".join(f"{i}. {p}" for i, p in enumerate(qa["preguntas"], 1))
    checklist_html = "\n".join(f"☐ {c}" for c in qa["checklist"])
    return (
        "<b>💬 PREGUNTAS PARA EL VENDEDOR</b>\n"
        "<i>(cópiate y mándalas por WhatsApp)</i>\n"
        f"{preguntas_html}\n\n"
        "<b>📋 CHECKLIST PARA VER EL COCHE</b>\n"
        f"{checklist_html}"
    )


async def validar_precio_mercado(marca: str, modelo: str, año: int, km: int,
                                  precio_medio: float, precios_muestra: list) -> dict:
    """
    Valida si el precio medio de Wallapop tiene sentido para el coche dado.
    Devuelve {"valido": bool, "confianza": int, "comentario": str}
    """
    if not precio_medio or precio_medio <= 0:
        return {"valido": False, "confianza": 0, "comentario": "Sin datos de precio"}

    system = (
        "Eres experto en precios de coches de segunda mano en España. "
        "Valida si el precio medio de mercado tiene sentido. "
        "Responde SOLO con JSON sin backticks: "
        "{\"valido\": true, \"confianza\": 85, \"comentario\": \"precio coherente\"} "
        "valido=false si el precio es absurdo (muy alto o muy bajo para año/km). "
        "confianza 0-100 según cuánto te fias de la muestra."
    )
    user_msg = (
        f"Coche: {marca} {modelo} | Año: {año} | Km: {km:,}\n"
        f"Precio medio calculado: {precio_medio:,.0f}€\n"
        f"Muestra de precios usados: {[f'{p:,.0f}€' for p in precios_muestra]}"
    )
    respuesta = await _llamar_ia(system, user_msg, max_tokens=100)
    if not respuesta:
        return {"valido": True, "confianza": 50, "comentario": "Sin validación IA"}
    try:
        r = json.loads(_limpiar_json(respuesta))
        r.setdefault("valido", True)
        r.setdefault("confianza", 50)
        r.setdefault("comentario", "")
        return r
    except Exception:
        return {"valido": True, "confianza": 50, "comentario": "Error validación"}


# ══════════════════════════════════════════════════════════════════════════════
# /ideal — Recomendador de coche usado
# ══════════════════════════════════════════════════════════════════════════════

_IDEAL_HUECOS_TODOS = [
    "presupuesto_max", "uso", "plazas_min", "tamaño",
    "combustible", "duracion_uso", "marcas_evitar"
]

_TAMAÑOS_VALIDOS = {
    "urbano", "compacto", "berlina",
    "suv_compacto", "suv_grande", "familiar", "monovolumen",
}

# Mapeo duracion_uso → km_max razonable (un coche dura ~250k km bien mantenido)
DURACION_USO_A_KM_MAX = {
    "corta":        200_000,   # 1-3 años: cualquier coche válido
    "media":        130_000,   # 5 años: necesita margen de vida útil
    "larga":         80_000,   # 10+ años: bajos km para que dure
    "primer_coche": 180_000,   # asequible, no obsesionarse con km
}


async def parsear_perfil_ideal(texto: str) -> dict:
    """
    Convierte lenguaje natural del usuario en un perfil técnico JSON.
    Devuelve el perfil con una clave 'huecos' indicando qué preguntar aún.
    """
    vacio = {
        "carrocerias": None, "presupuesto_max": None, "plazas_min": None,
        "uso": None, "combustible": None, "etiqueta_dgt_min": None,
        "duracion_uso": None, "km_max": None, "tamaño": None,
        "cv_min": None, "marcas_evitar": [],
        "huecos": list(_IDEAL_HUECOS_TODOS),
    }
    if not texto or not texto.strip():
        return vacio

    system = (
        "Extrae el perfil ideal de coche usado del usuario en España. "
        "El usuario probablemente NO sabe de coches. Interpreta lo que quiere decir. "
        "Responde SOLO JSON puro sin backticks:\n"
        '{"carrocerias":["suv"|"familiar"|"berlina"|"coupe"|"monovolumen"|"cabrio"|"pickup"] o null,'
        '"presupuesto_max":int o null,'
        '"plazas_min":int o null,'
        '"uso":"ciudad"|"autopista"|"mixto"|"offroad" o null,'
        '"combustible":["gasolina"|"diesel"|"hibrido"|"electrico"] o null,'
        '"etiqueta_dgt_min":"0"|"ECO"|"C"|"B" o null,'
        '"duracion_uso":"corta"|"media"|"larga"|"primer_coche" o null,'
        '"km_max":int o null,'
        '"cv_min":int o null,'
        '"tamaño":"urbano"|"compacto"|"berlina"|"suv_compacto"|"suv_grande"|"familiar"|"monovolumen" o null,'
        '"marcas_evitar":[],'
        '"huecos":["presupuesto_max","uso","plazas_min","tamaño","combustible","duracion_uso","marcas_evitar"]}\n\n'
        'Reglas de inferencia:\n'
        '"grande"/"familiar"/"para la familia" → carrocerias=["suv","familiar","monovolumen"], plazas_min=5\n'
        '"rápido" → cv_min=130; "muy rápido"/"deportivo" → cv_min=200\n'
        '"ciudad"/"urbano" → uso=ciudad; "viajes"/"autopista"/"carretera" → uso=autopista\n'
        '"ZBE"/"pegatina"/"Madrid Central"/"ECO" → etiqueta_dgt_min=ECO, combustible=[hibrido,electrico]\n'
        '"voy mucho por ciudad"/"para ir a trabajar al centro" → uso=ciudad, '
        'si NO menciona combustible: combustible=[hibrido,electrico]\n'
        '"barato"/"económico" → no añadir cv_min; esperar presupuesto_max\n'
        'Si menciona presupuesto explícito ("15000€", "máximo 20k") → presupuesto_max=ese valor\n'
        '\nReglas duracion_uso (cuánto tiempo lo va a tener):\n'
        '"primer coche"/"empiezo a conducir"/"recién carnet" → duracion_uso="primer_coche"\n'
        '"que me dure"/"para muchos años"/"10 años"/"que aguante" → duracion_uso="larga"\n'
        '"unos años"/"5 años"/"luego cambio"/"y luego vendo" → duracion_uso="media"\n'
        '"poco tiempo"/"1-2 años"/"temporal"/"de paso" → duracion_uso="corta"\n'
        'Solo pon km_max si el usuario menciona km explícitos ("máximo 100k km").\n'
        '\nReglas tamaño (DECISIVO para qué modelos sugerir):\n'
        '"primer coche"/"recién carnet" → tamaño="urbano" si presup<7000, "compacto" si 7000≤presup<11000\n'
        '"familia"/"niños"/"sillita"/"para la familia" → tamaño="familiar" o "monovolumen" si plazas>=7\n'
        '"SUV pequeño"/"crossover" → tamaño="suv_compacto"\n'
        '"SUV grande"/"todoterreno"/"4x4" → tamaño="suv_grande"\n'
        '"berlina"/"sedán"/"tipo Octavia/Golf" → tamaño="berlina"\n'
        '"ciudad"/"para aparcar"/"pequeño"/"urbano" → tamaño="urbano"\n'
        'Si NO hay pistas claras de tamaño, déjalo null (se preguntará).\n'
        '\nhuecos: lista SOLO los campos null/vacíos, en orden: '
        'presupuesto_max, uso, plazas_min, tamaño, combustible, duracion_uso, marcas_evitar'
    )
    respuesta = await _llamar_ia(system, texto.strip()[:400], max_tokens=300)
    if not respuesta:
        return vacio
    try:
        raw = json.loads(_limpiar_json(respuesta))

        def _int(v):
            try:
                n = int(v)
                return n if n > 0 else None
            except (TypeError, ValueError):
                return None

        duracion = raw.get("duracion_uso") or None
        if duracion not in DURACION_USO_A_KM_MAX:
            duracion = None
        km_max_raw = _int(raw.get("km_max"))
        # Si hay duracion pero no km explícito, derivar
        km_final = km_max_raw or (DURACION_USO_A_KM_MAX.get(duracion) if duracion else None)

        tamaño = raw.get("tamaño") or None
        if tamaño not in _TAMAÑOS_VALIDOS:
            tamaño = None

        perfil = {
            "carrocerias":     raw.get("carrocerias") or None,
            "presupuesto_max": _int(raw.get("presupuesto_max")),
            "plazas_min":      _int(raw.get("plazas_min")),
            "uso":             raw.get("uso") or None,
            "combustible":     raw.get("combustible") or None,
            "etiqueta_dgt_min": raw.get("etiqueta_dgt_min") or None,
            "duracion_uso":    duracion,
            "km_max":          km_final,
            "tamaño":          tamaño,
            "cv_min":          _int(raw.get("cv_min")),
            "marcas_evitar":   raw.get("marcas_evitar") or [],
        }

        # Inferencia automática de tamaño cuando NO viene del LLM pero hay pistas fuertes
        if perfil["tamaño"] is None:
            plazas = perfil.get("plazas_min") or 0
            presup = perfil.get("presupuesto_max") or 0
            carro  = perfil.get("carrocerias") or []
            duracion_p = perfil.get("duracion_uso")
            if plazas >= 7 or "monovolumen" in carro:
                perfil["tamaño"] = "monovolumen"
            elif "familiar" in carro:
                perfil["tamaño"] = "familiar"
            elif "suv" in carro:
                perfil["tamaño"] = "suv_grande" if plazas >= 7 else "suv_compacto"
            elif duracion_p == "primer_coche" and 0 < presup < 7000:
                perfil["tamaño"] = "urbano"
            elif duracion_p == "primer_coche" and 7000 <= presup < 11000:
                perfil["tamaño"] = "compacto"

        huecos_raw = raw.get("huecos") or _IDEAL_HUECOS_TODOS
        # Recalcular: si tras la inferencia tamaño quedó fijado, sacarlo de huecos
        huecos = [h for h in _IDEAL_HUECOS_TODOS if h in huecos_raw]
        if perfil["tamaño"] is not None and "tamaño" in huecos:
            huecos.remove("tamaño")
        elif perfil["tamaño"] is None and "tamaño" not in huecos:
            huecos.append("tamaño")
        perfil["huecos"] = huecos

        logger.info(f"[IDEAL] Perfil parseado: {perfil}")
        return perfil
    except Exception as e:
        logger.warning(f"[IDEAL] Error parseando perfil: {e} — raw: {respuesta!r}")
        return vacio


async def sugerir_modelos_candidatos(
    perfil: dict,
    evitar: list[str] | None = None,
    feedback: str = "",
) -> list[dict]:
    """
    Dado un perfil de comprador, devuelve 3-5 modelos concretos disponibles
    en el mercado de segunda mano español.
    Retorna list[{marca, modelo, año_min, año_max, motivo}].

    `evitar`: lista "marca modelo" rechazados en intentos previos (no repetir).
    `feedback`: mensaje del validador con qué problema corregir.
    """
    # Buscar en Tavily modelos reales del mercado para este perfil (cacheado)
    tavily_ctx = await _tavily_modelos_para_perfil(perfil)

    system = (
        "Eres experto en coches usados en España. Sugiere 3-5 modelos concretos "
        "abundantes en Wallapop y coches.net. Responde SOLO JSON puro:\n"
        '[{"marca":"...","modelo":"...","año_min":int,"año_max":int,"motivo":"..."}]\n\n'

        "═══ PRINCIPIO FUNDAMENTAL ═══\n"
        "AJUSTA EL AÑO al presupuesto+tamaño. Si el presupuesto solo da para un coche viejo, "
        "sugiere uno viejo. NUNCA sugieras un modelo nuevo si el presupuesto no llega. "
        "Mejor un Hyundai ix35 2011 que un Tucson 2020 imposible de pagar.\n"
        "Mediana del modelo+año debe estar al 70-100% del presupuesto.\n\n"

        "═══ REGLAS ═══\n"
        "- Si tamaño está fijado en perfil, SOLO sugerir de ese tamaño.\n"
        "- Prioriza fiabilidad: Toyota, Kia, Mazda, Skoda, Hyundai, Honda, Lexus.\n"
        "- duracion_uso=larga: SOLO Toyota, Lexus, Mazda, Honda, Subaru.\n"
        "- combustible=[hibrido,electrico] o etiqueta_dgt_min=ECO: solo modelos con esa motorización REAL "
        "(Toyota Corolla HV, Yaris HV, Kia Niro HV, Hyundai Ioniq, Auris HV, Renault Zoe, Nissan Leaf). "
        "Si el segmento+presupuesto NO admite hybrid moderno (ej: SUV grande con 8k), IGNORA el filtro hybrid "
        "y sugiere diésel/gasolina del segmento — NUNCA inventes hybrids inexistentes.\n"
        "- plazas_min>=7: solo monovolúmenes/SUV grandes 7p (Alhambra, Sharan, Touran, S-Max, Carnival).\n"
        "- marcas_evitar: NUNCA esas marcas.\n"
        f"- Devuelve entre 3 y {IDEAL_CANDIDATOS_MAX} modelos.\n"
        "- motivo: 1 frase CONCRETA (no 'buen coche', sino 'fiable y barato de seguro')."
    )

    if tavily_ctx:
        system += (
            "\n\n═══ CONTEXTO REAL DEL MERCADO (búsqueda en internet hoy) ═══\n"
            "Usa estos snippets como PRINCIPAL fuente de qué modelos son razonables "
            "para el presupuesto del usuario. Tienen información real de precios y modelos "
            "actuales en venta. Confía más en estos datos que en tu memoria.\n\n"
            f"{tavily_ctx[:3000]}"
        )

    if evitar:
        system += f"\n\n═══ NO SUGIERAS estos modelos (rechazados): {', '.join(evitar)} ═══"
    if feedback:
        system += (
            f"\n\n═══ FEEDBACK del validador: {feedback} ═══\n"
            "Corrige el problema. Sugiere modelos diferentes que sí encajen."
        )
    perfil_txt = json.dumps(
        {k: v for k, v in perfil.items() if k not in ("huecos",) and v is not None and v != []},
        ensure_ascii=False,
    )
    respuesta = await _llamar_ia(system, perfil_txt, max_tokens=500)
    if not respuesta:
        return []
    try:
        m = re.search(r"\[.*\]", respuesta, re.DOTALL)
        if not m:
            return []
        candidatos = json.loads(m.group(0))
        result = []
        for c in candidatos:
            if not c.get("marca") or not c.get("modelo"):
                continue
            result.append({
                "marca":    str(c["marca"]).lower().strip(),
                "modelo":   str(c["modelo"]).lower().strip(),
                "año_min":  int(c.get("año_min", 2015)),
                "año_max":  int(c.get("año_max", 2022)),
                "motivo":   str(c.get("motivo", "")),
            })
        nombres = [f"{c['marca']} {c['modelo']}" for c in result]
        logger.info(f"[IDEAL] Candidatos: {nombres}")
        return result[:IDEAL_CANDIDATOS_MAX]
    except Exception as e:
        logger.warning(f"[IDEAL] Error candidatos: {e}")
        return []


async def validar_candidatos_perfil(
    perfil: dict,
    candidatos: list[dict],
) -> dict:
    """
    Verifica que los candidatos sugeridos encajan con el perfil.
    Devuelve {"ok": bool, "problema": str, "modelos_a_evitar": [str]}.
    Conservador: en fallo retorna ok=True.
    """
    if not candidatos:
        return {"ok": True, "problema": "", "modelos_a_evitar": []}

    perfil_min = {
        k: v for k, v in perfil.items()
        if k in ("presupuesto_max", "plazas_min", "tamaño", "duracion_uso",
                 "uso", "combustible", "etiqueta_dgt_min", "marcas_evitar")
        and v is not None and v != []
    }
    cand_txt = "\n".join(
        f"- {c['marca'].title()} {c['modelo'].title()} ({c['año_min']}-{c['año_max']})"
        for c in candidatos
    )

    system = (
        "Eres verificador conservador de recomendaciones de coches usados en España. "
        "Tu trabajo es SOLO detectar errores graves y obvios, no opinar sobre gustos. "
        "Por defecto, ok=true. Solo marca ok=false si encuentras un error CLARO.\n\n"
        "REFERENCIAS DE SEGMENTO (ten esto MUY claro antes de validar):\n"
        "- Urbanos (segmento A): Kia Picanto, Hyundai i10, Toyota Aygo, Citroën C1, "
        "  Peugeot 107/108, Renault Twingo, VW Up!, Skoda Citigo, Seat Mii, Fiat Panda, "
        "  Dacia Sandero. NO son SUV, NO son segmento C/D.\n"
        "- Compactos (segmento B): Seat Ibiza, VW Polo, Skoda Fabia, Hyundai i20, "
        "  Toyota Yaris, Mazda 2, Ford Fiesta, Renault Clio, Peugeot 208, Opel Corsa.\n"
        "- Berlinas (C/D): Octavia, Leon, Golf, i30, Ceed, Corolla, Mazda 3, Focus.\n"
        "- SUVs: Tucson, Sportage, Qashqai, Ateca, Karoq, T-Roc, Kuga, 3008, CX-5, RAV4.\n\n"
        "Marca ok=false SOLO en estos casos OBVIOS:\n"
        "(a) Marca aparece en marcas_evitar del perfil.\n"
        "(b) Modelo claramente fuera de presupuesto (ej: Tucson 2020 con presup=6000€).\n"
        "(c) Modelo claramente del segmento equivocado (ej: tamaño=urbano y sugieren un SUV).\n"
        "(d) plazas_min=7 y sugieren un coche de 5 plazas claramente.\n\n"
        "Si dudas, ok=true. NUNCA marques ok=false con TODOS los candidatos a evitar — "
        "eso es señal de error tuyo, no de los candidatos.\n\n"
        "Responde SOLO JSON: "
        '{"ok":true|false,"problema":"frase concreta y específica","modelos_a_evitar":["Marca Modelo",...]}'
    )
    user = f"PERFIL: {json.dumps(perfil_min, ensure_ascii=False)}\n\nMODELOS:\n{cand_txt}"

    respuesta = await _llamar_ia(system, user, max_tokens=200)
    if not respuesta:
        return {"ok": True, "problema": "", "modelos_a_evitar": []}
    try:
        data = json.loads(_limpiar_json(respuesta))
        ok = bool(data.get("ok", True))
        evitar = [str(m).lower() for m in (data.get("modelos_a_evitar") or [])]
        problema = str(data.get("problema", ""))[:200]
        # Salvaguarda: si rechaza TODOS los candidatos, es señal de alucinación del 8B
        if not ok and len(evitar) >= len(candidatos):
            logger.warning(
                f"[IDEAL] Validador rechaza el 100% ({len(evitar)}/{len(candidatos)}) "
                f"— probable alucinación, ignorando. Problema: {problema}"
            )
            return {"ok": True, "problema": "", "modelos_a_evitar": []}
        return {"ok": ok, "problema": problema, "modelos_a_evitar": evitar}
    except Exception as e:
        logger.warning(f"[IDEAL] Error validar_candidatos_perfil: {e} — raw: {respuesta!r}")
        return {"ok": True, "problema": "", "modelos_a_evitar": []}


async def generar_veredicto_ideal(perfil: dict, top3: list, medianas: dict) -> str:
    """
    Genera HTML Telegram con el resumen del Top 3 del /ideal.
    top3: list[Anuncio]; medianas: {"marca modelo": float}
    """
    if not top3:
        return "😔 No encontré anuncios que encajen con tu perfil."

    def _safe(s: str, max_len: int = 60) -> str:
        """Limita longitud y elimina contenido inyectado en campos de anuncios."""
        return re.sub(r"[<>\[\]{}]", "", str(s or ""))[:max_len].strip()

    anuncios_txt = []
    for i, a in enumerate(top3, 1):
        mediana = next((v for k, v in medianas.items() if a.marca.lower() in k and a.modelo.lower() in k), 0)
        if mediana > 0:
            diff_pct = round((mediana - a.precio) / mediana * 100)
            precio_txt = f"{a.precio:,.0f}€ ({'+' if diff_pct > 0 else ''}{diff_pct}% vs mediana)"
        else:
            precio_txt = f"{a.precio:,.0f}€"
        anuncios_txt.append(
            f"#{i}: {_safe(a.marca).title()} {_safe(a.modelo).upper()} {a.año} "
            f"| {a.km:,} km | {precio_txt} | {_safe(a.provincia) or 'España'}"
        )

    perfil_txt = json.dumps(
        {k: v for k, v in perfil.items() if k not in ("huecos",) and v is not None and v != []},
        ensure_ascii=False,
    )
    system = (
        "Eres experto en coches usados en España con 20 años de experiencia. "
        "El usuario YA VE precio, km y año — NO los repitas. "
        "Tu valor: lo que el usuario NO sabe sobre estos modelos. "
        "Responde en HTML Telegram (<b>, <i>, saltos de línea). Máximo 850 chars.\n\n"
        "ESTRUCTURA OBLIGATORIA (3 bloques, sin encabezados extra):\n\n"
        "<b>🔬 Comparativa</b>\n"
        "Por cada modelo (#1, #2, #3): 1 frase con su punto fuerte real "
        "Y su punto débil conocido. Sé MUY concreto: no 'buena fiabilidad' sino "
        "'motor 2.0 TDI con problemas de inyectores pasados los 150k' o "
        "'caja CVT cara de reparar en talleres no oficiales'. "
        "Si es híbrido, menciona batería. Si es diésel antiguo, menciona FAP/DPF.\n\n"
        "<b>🏆 Mi elección</b>\n"
        "1-2 frases. Qué modelo elegirías y por qué concreto "
        "(no 'mejor equilibrio' — sé específico: motor, uso, coste de mantenimiento).\n\n"
        "<b>🔍 Al verlo revisar</b>\n"
        "2-3 puntos específicos para el modelo elegido "
        "(no genéricos: 'revisar cadena distribución en motor X', "
        "'comprobar historial cambios aceite', 'verificar que turbo no humea en frío').\n\n"
        "Sin saludos. Sin repetir datos. Solo conocimiento del dominio."
    )
    user_msg = f"PERFIL COMPRADOR:\n{perfil_txt}\n\nTOP 3 ENCONTRADOS:\n" + "\n".join(anuncios_txt)
    resultado = await _llamar_ia(system, user_msg, max_tokens=750)
    if resultado:
        return resultado
    # Fallback sin IA
    lineas = []
    for i, a in enumerate(top3):
        lineas.append(
            f"{'🥇🥈🥉'[i]} <b>{_html.escape(a.marca.title())} "
            f"{_html.escape(a.modelo.upper())}</b> {a.año} · "
            f"{a.km:,} km · <b>{a.precio:,.0f}€</b>"
        )
    return "\n".join(lineas)