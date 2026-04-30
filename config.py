# config.py - Variables de entorno y configuración global
# ─────────────────────────────────────────────────────────────────────────────
import os
from dotenv import load_dotenv

load_dotenv()

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ALLOWED_USER_IDS: list[int] = [
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()
]
ADMIN_USER_IDS: list[int] = [
    int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()
]

# ─── SCRAPING GENERAL ────────────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
]

PROXIES: list[str] = []  # "http://user:pass@host:port"

# ─── RESULTADOS ──────────────────────────────────────────────────────────────
TOP_RESULTS      = 5       # cuántos mostrar al usuario (de cada fuente combinada)
MAX_PAGES_DE     = 3       # páginas a scrapear por fuente DE (20 anuncios/pág en AS24)
MAX_COCHES_RAW   = 40      # máximo de anuncios brutos antes de filtrar/ordenar

# ─── FUENTES ALEMANIA ────────────────────────────────────────────────────────
# Activar/desactivar fuentes
ENABLE_AUTOSCOUT24 = True
ENABLE_MOBILE_DE   = True

# ─── FUENTES ESPAÑA ──────────────────────────────────────────────────────────
ENABLE_WALLAPOP    = True
ENABLE_COCHES_NET  = True

# ─── SCRAPING ESPAÑA: Wallapop ───────────────────────────────────────────────
WALLAPOP_LATITUDE  = 40.4168
WALLAPOP_LONGITUDE = -3.7038
WALLAPOP_DISTANCE  = 0       # 0 = toda España
WALLAPOP_RESULTS   = 20

# ─── SCRAPING ESPAÑA: coches.net ─────────────────────────────────────────────
COCHES_NET_RESULTS = 20

# ─── TOLERANCIAS CRUCE DE → ES ───────────────────────────────────────────────
AÑO_TOLERANCIA = 1       # ±1 año
KM_TOLERANCIA  = 20_000  # ±20 000 km

# ─── ANTI-SCAM (precios ES) ─────────────────────────────────────────────────
PRECIO_MINIMO_VALIDO = 1_500
ANTI_SCAM_FACTOR     = 0.50   # descarta precios < mediana * 0.50
PRECIO_MEDIO_MUESTRA = 5      # N precios más baratos para el promedio

# ─── BASE DE DATOS ───────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "cabeza_bot.db")

# ─── TAVILY (investigación experta del coche) ───────────────────────────────
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
TAVILY_CACHE_TTL_HOURS      = 24
TAVILY_DOMINIOS_FOROS       = ["forocoches.com", "reddit.com", "km77.com", "clubdelautomovil.com"]
TAVILY_DOMINIOS_FIABILIDAD  = ["adac.de", "tuv.com", "dekra.com", "fiabilidadcoches.es", "autobild.de"]
TAVILY_DOMINIOS_ARTICULOS   = ["km77.com", "motorpasion.es", "motor.es", "autocasion.com", "coches.com"]

# ─── VISION (análisis visual de fotos del anuncio) ──────────────────────────
ENABLE_VISION    = os.getenv("ENABLE_VISION", "false").lower() in ("1", "true", "yes")
VISION_MODEL     = os.getenv("VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
VISION_MAX_FOTOS = int(os.getenv("VISION_MAX_FOTOS", "4"))
VISION_TIMEOUT_S = int(os.getenv("VISION_TIMEOUT_S", "8"))

# ─── ROBUSTEZ ───────────────────────────────────────────────────────────────
AI_TIMEOUT_S          = int(os.getenv("AI_TIMEOUT_S", "30"))
ANALISIS_CACHE_TTL_S  = int(os.getenv("ANALISIS_CACHE_TTL_S", "1800"))   # 30 min
HISTORICO_RETENCION_DIAS = int(os.getenv("HISTORICO_RETENCION_DIAS", "180"))

# ─── WORKER ──────────────────────────────────────────────────────────────────
WORKER_INTERVAL_MINUTES    = 15    # misiones normales
SNIPER_INTERVAL_MINUTES    = 3     # misiones sniper (alertas rápidas)

# ─── SCANNER (gancho gratuito) ──────────────────────────────────────────────
SCANNER_CHANNEL_ID = os.getenv("SCANNER_CHANNEL_ID", "")  # @tu_canal o -100XXXX
SCANNER_INTERVAL_MINUTES = 60      # cada cuánto publica ofertas generales
SCANNER_TOP_DEALS = 3              # cuántas ofertas publica por ciclo
SCANNER_MODELS = [
    # Modelos populares que se escanean automáticamente como gancho gratuito
    # (marca, modelo, filtros_base)
    ("bmw", "serie 3", {"year_min": 2018, "km_max": 120000}),
    ("audi", "a4", {"year_min": 2018, "km_max": 120000}),
    ("mercedes-benz", "clase c", {"year_min": 2018, "km_max": 120000}),
    ("volkswagen", "golf", {"year_min": 2019, "km_max": 100000}),
    ("bmw", "serie 5", {"year_min": 2017, "km_max": 130000}),
    ("audi", "a3", {"year_min": 2019, "km_max": 100000}),
    ("volkswagen", "tiguan", {"year_min": 2019, "km_max": 100000}),
    ("bmw", "x3", {"year_min": 2018, "km_max": 120000}),
    ("mercedes-benz", "clase e", {"year_min": 2017, "km_max": 130000}),
    ("audi", "q5", {"year_min": 2018, "km_max": 120000}),
]

# ─── COSTES FIJOS IMPORTACIÓN (€) ────────────────────────────────────────────
COSTE_TRANSPORTE   = 1_200
COSTE_GESTORIA_ITV =   350

# ─── TRAMOS IEDMT ────────────────────────────────────────────────────────────
IEDMT_TRAMOS = [
    (120,          0.0000),
    (159,          0.0475),
    (199,          0.0975),
    (float("inf"), 0.1475),
]

# ─── LÓGICA DE NEGOCIO ───────────────────────────────────────────────────────
MIN_BENEFICIO = 3_000

# ─── FREEMIUM ────────────────────────────────────────────────────────────────
FREE_ANALISIS_MAX  = int(os.getenv("FREE_ANALISIS_MAX", "3"))
FREE_VENTANA_HORAS = int(os.getenv("FREE_VENTANA_HORAS", "3"))


# ═════════════════════════════════════════════════════════════════════════════
# TABLAS DE CÓDIGOS COMPARTIDAS (AutoScout24 + mobile.de + filtros)
# ═════════════════════════════════════════════════════════════════════════════

# ── Colores (AutoScout24 extcol=) ────────────────────────────────────────────
COLORES_AS24 = {
    "negro": 1, "azul": 2, "marron": 3, "café": 3, "amarillo": 4,
    "gris": 5, "verde": 6, "rojo": 7, "plata": 8, "plateado": 8,
    "blanco": 9, "dorado": 10, "naranja": 11, "morado": 12, "violeta": 12,
    "beige": 13, "burdeos": 7, "granate": 7, "celeste": 2, "azul marino": 2,
}

# ── Colores (mobile.de clr=) ─────────────────────────────────────────────────
COLORES_MOBILE = {
    "negro": "BLACK", "azul": "BLUE", "marron": "BROWN", "amarillo": "YELLOW",
    "gris": "GREY", "verde": "GREEN", "rojo": "RED", "plata": "SILVER",
    "plateado": "SILVER", "blanco": "WHITE", "dorado": "GOLD",
    "naranja": "ORANGE", "morado": "VIOLET", "violeta": "VIOLET",
    "beige": "BEIGE", "burdeos": "RED", "granate": "RED",
}

# ── Carrocerías (AutoScout24 body=) ──────────────────────────────────────────
CARROCERIAS_AS24 = {
    "limusina": 1, "sedan": 1, "berlina": 1, "saloon": 1,
    "familiar": 2, "kombi": 2, "estate": 2, "combi": 2, "break": 2,
    "suv": 3, "todoterreno": 3, "crossover": 3, "4x4": 3, "offroad": 3,
    "cabrio": 4, "descapotable": 4, "cabriolet": 4, "convertible": 4, "roadster": 4,
    "coupe": 5, "cupe": 5, "coupé": 5,
    "monovolumen": 6, "van": 6, "furgoneta": 6, "mpv": 6,
    "pickup": 8, "pick up": 8,
    "camper": 9, "autocaravana": 9,
}

# ── Carrocerías (mobile.de bod=) ─────────────────────────────────────────────
CARROCERIAS_MOBILE = {
    "sedan": "LIMOUSINE", "berlina": "LIMOUSINE", "limusina": "LIMOUSINE",
    "familiar": "ESTATE_CAR", "kombi": "ESTATE_CAR", "estate": "ESTATE_CAR",
    "suv": "OFF_ROAD", "todoterreno": "OFF_ROAD", "crossover": "OFF_ROAD",
    "cabrio": "CABRIO", "descapotable": "CABRIO", "cabriolet": "CABRIO",
    "convertible": "CABRIO", "roadster": "CABRIO",
    "coupe": "SPORTS_CAR", "cupe": "SPORTS_CAR", "coupé": "SPORTS_CAR",
    "monovolumen": "VAN", "van": "VAN", "mpv": "VAN",
    "pickup": "OTHER", "camper": "OTHER",
}

# ── Combustibles (AutoScout24 fuel=) ─────────────────────────────────────────
COMBUSTIBLES_AS24 = {
    "gasolina": "B", "nafta": "B", "benzina": "B",
    "diesel": "D", "gasoil": "D", "tdi": "D", "cdi": "D",
    "electrico": "E", "eléctrico": "E", "ev": "E", "bev": "E",
    "hibrido": "2", "híbrido": "2", "hybrid": "2", "phev": "2",
    "glp": "L", "gas": "L", "gnc": "M", "cng": "M",
    "hidrogeno": "H", "hidrógeno": "H",
}

# ── Combustibles (mobile.de ft=) ─────────────────────────────────────────────
COMBUSTIBLES_MOBILE = {
    "gasolina": "PETROL", "nafta": "PETROL", "benzina": "PETROL",
    "diesel": "DIESEL", "gasoil": "DIESEL", "tdi": "DIESEL",
    "electrico": "ELECTRICITY", "eléctrico": "ELECTRICITY", "ev": "ELECTRICITY",
    "hibrido": "HYBRID", "híbrido": "HYBRID", "hybrid": "HYBRID", "phev": "HYBRID",
    "glp": "LPG", "gas": "LPG", "gnc": "CNG", "cng": "CNG",
    "hidrogeno": "HYDROGEN", "hidrógeno": "HYDROGEN",
}

# ── Caja de cambios (mobile.de tr=) ─────────────────────────────────────────
CAJAS_MOBILE = {
    "manual": "MANUAL_GEAR", "manuales": "MANUAL_GEAR",
    "automatico": "AUTOMATIC_GEAR", "automático": "AUTOMATIC_GEAR",
    "auto": "AUTOMATIC_GEAR", "dsg": "AUTOMATIC_GEAR", "pdk": "AUTOMATIC_GEAR",
}

# ── Extras / equipamiento (AutoScout24 aex=) ────────────────────────────────
EXTRAS_AEX = {
    # Seguridad
    "abs": 1, "esp": 42, "airbag": 2, "isofix": 125,
    "detector angulo muerto": 210, "punto ciego": 210,
    "asistente carril": 216, "aviso carril": 216,
    "camara trasera": 57, "camara 360": 222, "camara 360°": 222,
    "sensores aparcamiento": 55, "sensores traseros": 55,
    "sensores delanteros": 56, "pdc trasero": 55, "pdc delantero": 56,
    "asistente aparcamiento": 75, "parking automatico": 75,
    "cuero": 5, "asientos cuero": 5, "cuero parcial": 6,
    # Confort
    "aire acondicionado": 3, "climatizador": 4, "climatizador bizona": 175,
    "techo panoramico": 30, "panoramico": 30, "techo solar": 30, "sunroof": 30,
    "asientos calefactados": 48, "calefaccion asientos": 48, "sitzheizung": 48,
    "asientos ventilados": 168, "asientos electricos": 161,
    "asientos masaje": 170, "masaje": 170,
    "volante calefactado": 176, "volante calefactable": 176,
    "arranque sin llave": 70, "keyless": 70, "acceso sin llave": 70,
    "head up display": 100, "hud": 100, "head-up": 100,
    "suspension neumatica": 172, "amortiguacion activa": 173,
    "traccion integral": 28, "4wd": 28, "awd": 28, "quattro": 28, "xdrive": 28,
    "maletero electrico": 162, "portón electrico": 162,
    # Multimedia / Navegación
    "navegacion": 19, "gps": 19, "navi": 19, "navegador": 19,
    "bluetooth": 23, "telefono manos libres": 23,
    "apple carplay": 212, "carplay": 212,
    "android auto": 213, "androidauto": 213,
    "pantalla tactil": 159, "touchscreen": 159,
    "sonido premium": 149, "harman kardon": 149, "bowers wilkins": 149,
    "bang olufsen": 149, "meridian": 149, "burmester": 149,
    "wifi": 214, "internet": 214, "hotspot": 214,
    # Iluminación
    "luces led": 68, "faros led": 68, "led": 68,
    "luces xenon": 34, "xenon": 34, "bi xenon": 34,
    "luces laser": 215, "laser": 215,
    "luces adaptativas": 203, "luces curva": 203,
    "luces automaticas": 52, "sensor luz": 52,
    # Exterior
    "llantas aluminio": 15, "llantas aleacion": 15, "rines": 15,
    "techo negro": 179, "techo contrastante": 179,
    # Motor / Rendimiento
    "turbo": 29, "supercargado": 29,
    "modo sport": 169, "modo conduccion": 169,
    # Otros
    "garantia": 43, "revision oficial": 44, "libro revisiones": 44,
    "sin accidente": 45, "primer propietario": 7,
    "non fumador": 46, "no fumador": 46,
    "remolque": 47, "enganche": 47, "bola": 47, "ahk": 47,
}

# ── Extras (mobile.de feat=) ─────────────────────────────────────────────────
EXTRAS_MOBILE = {
    "navegacion": "NAVIGATION_SYSTEM", "gps": "NAVIGATION_SYSTEM", "navi": "NAVIGATION_SYSTEM",
    "cuero": "FULL_LEATHER", "asientos cuero": "FULL_LEATHER",
    "techo panoramico": "PANORAMIC_ROOF", "panoramico": "PANORAMIC_ROOF",
    "techo solar": "SUNROOF", "sunroof": "SUNROOF",
    "asientos calefactados": "HEATED_SEATS", "calefaccion asientos": "HEATED_SEATS",
    "climatizador": "AUTOMATIC_CLIMATISATION", "aire acondicionado": "CLIMATISATION",
    "sensores aparcamiento": "PARKING_SENSORS", "pdc": "PARKING_SENSORS",
    "camara trasera": "REAR_CAMERA",
    "apple carplay": "APPLE_CARPLAY", "carplay": "APPLE_CARPLAY",
    "android auto": "ANDROID_AUTO",
    "bluetooth": "BLUETOOTH",
    "luces led": "LED_HEADLIGHTS", "led": "LED_HEADLIGHTS",
    "luces xenon": "XENON_HEADLIGHTS", "xenon": "XENON_HEADLIGHTS",
    "traccion integral": "ALL_WHEEL_DRIVE", "4wd": "ALL_WHEEL_DRIVE", "awd": "ALL_WHEEL_DRIVE",
    "enganche": "TRAILER_COUPLING", "remolque": "TRAILER_COUPLING", "ahk": "TRAILER_COUPLING",
    "llantas aluminio": "ALLOY_WHEELS",
    "head up display": "HEAD_UP_DISPLAY", "hud": "HEAD_UP_DISPLAY", "head-up": "HEAD_UP_DISPLAY",
    "suspension neumatica": "AIR_SUSPENSION",
    "keyless": "KEYLESS_ENTRY", "arranque sin llave": "KEYLESS_ENTRY",
    "garantia": "WARRANTY",
}


# ═════════════════════════════════════════════════════════════════════════════
# MAPAS DE MARCAS (normalización para cada portal)
# ═════════════════════════════════════════════════════════════════════════════

# mobile.de usa IDs numéricos para marca y modelo.
# Esta tabla cubre las más comunes. Para marcas no listadas, se usa búsqueda por texto.
MARCAS_MOBILE_ID = {
    "abarth": 66, "alfa romeo": 3, "alpina": 67, "aston martin": 5,
    "audi": 9, "bentley": 11, "bmw": 13, "bugatti": 81,
    "cadillac": 15, "chevrolet": 43, "chrysler": 16, "citroen": 17,
    "cupra": 309, "dacia": 22, "daewoo": 18, "daihatsu": 19,
    "dodge": 20, "ds": 283, "ferrari": 24, "fiat": 25,
    "ford": 26, "genesis": 310, "honda": 28, "hummer": 82,
    "hyundai": 29, "infiniti": 83, "isuzu": 30, "iveco": 127,
    "jaguar": 31, "jeep": 32, "kia": 33, "lamborghini": 34,
    "lancia": 35, "land rover": 36, "lexus": 37, "lincoln": 88,
    "lotus": 38, "maserati": 40, "mazda": 41, "mclaren": 279,
    "mercedes-benz": 42, "mini": 72, "mitsubishi": 44,
    "nissan": 47, "opel": 48, "peugeot": 50, "porsche": 51,
    "renault": 54, "rolls-royce": 56, "rover": 58, "saab": 59,
    "seat": 60, "skoda": 62, "smart": 63, "ssangyong": 95,
    "subaru": 64, "suzuki": 65, "tesla": 220, "toyota": 68,
    "volkswagen": 74, "vw": 74, "volvo": 75,
}