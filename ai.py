"""
ai.py - Capa de IA usando Groq (gratis, sin tarjeta)
Key gratis en: https://console.groq.com → API Keys
.env: GROQ_API_KEY=gsk_...
"""
import os, re, json, logging, asyncio
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)
VEREDICTOS = ("OK", "SOSPECHOSO", "DESCARTADO")

def _client():
    return AsyncOpenAI(
        api_key=os.getenv("GROQ_API_KEY", ""),
        base_url="https://api.groq.com/openai/v1",
    )

async def _llamar_ia(system: str, user: str, max_tokens: int = 300) -> str:
    try:
        resp = await _client().chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=max_tokens,
            temperature=0.1,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
        )
        text = resp.choices[0].message.content.strip()
        print(f"[AI RAW] {repr(text)}")
        return text
    except Exception as e:
        logger.error(f"[AI] Error Groq: {e}")
        return ""

def _limpiar_json(t: str) -> str:
    t = re.sub(r"^```[a-z]*\s*", "", t.strip())
    t = re.sub(r"\s*```$", "", t).strip()
    m = re.search(r"\{.*\}", t, re.DOTALL)
    return m.group(0) if m else t

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

async def generar_veredicto_analizar(anuncio, stats) -> str:
    """
    Genera el veredicto final humanizado para /analizar.
    anuncio: Anuncio dataclass
    stats: EstadisticaMercado dataclass
    Devuelve texto en HTML de Telegram listo para enviar.
    """
    desv_pct = stats.desviacion_pct

    if abs(desv_pct) <= 5:
        emoji = "✅"
        etiqueta = "Precio en línea con el mercado"
    elif desv_pct < -5 and desv_pct >= -20:
        emoji = "✅"
        etiqueta = f"Precio un {abs(desv_pct):.0f}% por debajo de la mediana"
    elif desv_pct < -20:
        emoji = "⚠️"
        etiqueta = f"Precio un {abs(desv_pct):.0f}% por debajo — merece investigar"
    elif desv_pct > 5 and desv_pct <= 20:
        emoji = "🟡"
        etiqueta = f"Precio un {desv_pct:.0f}% por encima de la mediana"
    else:
        emoji = "🔴"
        etiqueta = f"Precio un {desv_pct:.0f}% por encima — caro para el mercado"

    system = (
        "Eres Juan Lopera, experto en coches de segunda mano en España. "
        "Escribe un veredicto BREVE (3-5 frases) sobre si este anuncio merece la pena. "
        "Tono: directo, con datos, sin condescender. "
        "Menciona el precio vs mercado, los km, el año. "
        "Si hay algo raro en la descripción, dilo. "
        "Termina con 1-2 cosas concretas que preguntar al vendedor. "
        "Responde en español, sin emojis, sin JSON, solo texto plano."
    )
    user_msg = (
        f"Anuncio: {anuncio.marca.title()} {anuncio.modelo.upper()} "
        f"| Año: {anuncio.año} | Km: {anuncio.km:,} | Precio: {anuncio.precio:,.0f}€\n"
        f"Descripción: {anuncio.descripcion[:400] or 'Sin descripción'}\n\n"
        f"Mercado ({stats.n_comparables} comparables): "
        f"mediana {stats.mediana:,.0f}€ · media {stats.media:,.0f}€ · "
        f"desviación típica {stats.desviacion:,.0f}€\n"
        f"Este anuncio está en el percentil {stats.percentil:.0f} "
        f"({'más barato' if desv_pct < 0 else 'más caro'} que el {abs(desv_pct):.0f}% del mercado)"
    )

    texto_ia = await _llamar_ia(system, user_msg, max_tokens=350)

    resumen = (
        f"{emoji} <b>{etiqueta}</b>\n\n"
        f"<b>Datos del anuncio:</b>\n"
        f"• Precio: <b>{anuncio.precio:,.0f}€</b>  ·  Mediana mercado: <b>{stats.mediana:,.0f}€</b>\n"
        f"• Comparables encontrados: {stats.n_comparables}\n"
        f"• Percentil de precio: {stats.percentil:.0f}/100\n\n"
    )
    if texto_ia:
        resumen += f"<i>{texto_ia}</i>"

    return resumen


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