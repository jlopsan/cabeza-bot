"""
comparar_pipeline.py — Orquestador del flujo /comparar (Fase 4).

El comando acepta:
  - 2 URLs de anuncio (Wallapop / Coches.net)
  - 2 modelos genéricos (ej. 'Golf 7 GTI vs Civic Type R FK7')
  - URL + modelo (se normaliza el URL extrayendo marca/modelo/año)

Multi-turno tipo /ideal: si falta generación de algún lado, el bot la pide.

Pipeline una vez completos los slots:
  1. Resolver años por generación (determinista o pregunta al user).
  2. Buscar comparables paralelo Wallapop + Coches.net.
  3. Estadísticas mediana/P25/P75.
  4. Guardar histórico_precios.
  5. Etiqueta DGT + ZBE (determinista).
  6. Enriquecer (Tavily 5 queries, caché 7d).
  7. Veredicto IA con ganador.

Sesiones en memoria por user_id, TTL 30 min.
"""
import asyncio
import logging
import re as _re
import statistics as _stats_mod
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ai import (
    parsear_comparar_input,
    enriquecer_modelo,
    generar_veredicto_comparar,
    resolver_generacion_años,
)
from config import COMPARAR_KM_AÑO_REF, COMPARAR_PRECIO_LITRO_EUR
from database import guardar_historico_batch
from dgt import calcular_etiqueta_dgt, info_zbe
from scraper import buscar_comparables_todas, obtener_anuncio_por_url

logger = logging.getLogger(__name__)

# ── Sesiones en memoria ────────────────────────────────────────────────────

_SESIONES_TTL_S = 30 * 60
_SESIONES: dict[int, dict[str, Any]] = {}

_URL_RE = _re.compile(
    r"https?://(?:[\w-]+\.)*(?:wallapop\.[a-z]{2,}|coches\.net)/\S+",
    _re.IGNORECASE,
)


@dataclass
class LadoComparar:
    marca: str = ""
    modelo: str = ""
    generacion: str = ""        # "Mk7", "FK7", "2017-2020", etc.
    version_motor: str = ""     # opcional, mejora precisión
    año_min: int = 0
    año_max: int = 0
    año_central: int = 0
    url_original: str = ""      # si vino como URL
    combustible: str = ""       # de URL si disponible, para DGT

    def listo_para_pipeline(self) -> bool:
        return bool(self.marca and self.modelo and self.año_central)

    def merge_lado(self, otro: dict) -> None:
        """Mete campos no vacíos de un dict del parser en este lado."""
        for k in ("marca", "modelo", "generacion", "version_motor"):
            v = (otro or {}).get(k, "")
            if v and not getattr(self, k):
                setattr(self, k, v)

    def nombre_display(self) -> str:
        """Identificador estable que la IA debe usar verbatim en todos los
        bloques del veredicto. Ej: 'Volkswagen Golf Mk7 GTI' o
        'Honda Civic FK7 Type R'. Nunca 'A' / 'B' / 'lado_a' / 'lado_b'.
        """
        partes = [self.marca.title(), self.modelo.title()]
        gen = (self.generacion or "").strip()
        ver = (self.version_motor or "").strip()
        if gen:
            partes.append(gen)
        if ver and ver.lower() not in gen.lower():
            partes.append(ver)
        return " ".join(p for p in partes if p).strip()


@dataclass
class SlotsComparar:
    lado_a: LadoComparar = field(default_factory=LadoComparar)
    lado_b: LadoComparar = field(default_factory=LadoComparar)
    slot_preguntando: str = ""   # "lado_a.generacion" | "lado_b.generacion" | ""
    intentos: dict = field(default_factory=dict)


def nueva_sesion(user_id: int) -> dict:
    sesion = {
        "slots": SlotsComparar(),
        "ts": time.time(),
    }
    _SESIONES[user_id] = sesion
    return sesion


def get_sesion(user_id: int) -> dict | None:
    sesion = _SESIONES.get(user_id)
    if not sesion:
        return None
    if time.time() - sesion.get("ts", 0) > _SESIONES_TTL_S:
        _SESIONES.pop(user_id, None)
        return None
    return sesion


def borrar_sesion(user_id: int) -> None:
    _SESIONES.pop(user_id, None)


# ── Resolución de años por generación / cadena ────────────────────────────

def _parsear_años(texto: str) -> tuple[int, int]:
    """
    Saca un rango (min, max) de un string. Acepta:
      '2017-2020', '2017', '2017 2020', 'desde 2018', 'Mk7 (2013-2019)'.
    Devuelve (0, 0) si no encuentra nada.
    """
    if not texto:
        return 0, 0
    matches = _re.findall(r"(19[8-9]\d|20[0-3]\d)", texto)
    if not matches:
        return 0, 0
    años = sorted({int(m) for m in matches})
    if len(años) == 1:
        a = años[0]
        return a, a
    return años[0], años[-1]


def _resolver_años_lado(lado: LadoComparar) -> None:
    """
    Si la generacion contiene años, los extrae. Si solo hay marca+modelo+gen
    sin año, deja año_central=0 para que el orquestador pida al user.
    Cuando hay URL, el año ya vino de obtener_anuncio_por_url.
    """
    if lado.año_central:
        return
    a_min, a_max = _parsear_años(lado.generacion)
    if not a_min:
        a_min, a_max = _parsear_años(lado.version_motor)
    if a_min:
        lado.año_min = a_min
        lado.año_max = a_max
        lado.año_central = (a_min + a_max) // 2


# ── Alimentar slots desde texto del user ──────────────────────────────────

async def _absorber_urls(sesion: dict, texto: str) -> str:
    """
    Si hay URLs en el texto, descarga cada anuncio y rellena el lado
    correspondiente con marca/modelo/año del anuncio. Devuelve el texto
    sin las URLs (para que el parser NL trabaje solo con el modelo
    genérico restante).
    """
    slots: SlotsComparar = sesion["slots"]
    urls = _URL_RE.findall(texto)
    if not urls:
        return texto

    # Descargar en paralelo los anuncios
    anuncios = await asyncio.gather(
        *(obtener_anuncio_por_url(u) for u in urls[:2]),
        return_exceptions=True,
    )
    for url, anuncio in zip(urls[:2], anuncios):
        if isinstance(anuncio, Exception) or not anuncio:
            logger.warning(f"[COMPARAR] URL no descargada: {url}")
            continue
        # Asignar al primer lado vacío
        lado = slots.lado_a if not slots.lado_a.marca else slots.lado_b
        if lado.marca:
            # Ambos lados ya tenían datos. Aviso.
            logger.info("[COMPARAR] 3+ URLs, ignorando extras")
            break
        lado.marca = (anuncio.marca or "").strip()
        lado.modelo = (anuncio.modelo or "").strip()
        lado.año_min = anuncio.año or 0
        lado.año_max = anuncio.año or 0
        lado.año_central = anuncio.año or 0
        lado.url_original = url
        lado.combustible = (getattr(anuncio, "motor", "") or "")

    # Quitar URLs del texto antes de mandarlo al parser NL
    texto_sin_urls = _URL_RE.sub("", texto).strip()
    return texto_sin_urls


def _siguiente_pregunta(slots: SlotsComparar) -> str | None:
    """Devuelve el slot que aún falta (o None si está completo)."""
    if not slots.lado_a.marca or not slots.lado_a.modelo:
        return "lado_a.identidad"
    if not slots.lado_b.marca or not slots.lado_b.modelo:
        return "lado_b.identidad"
    _resolver_años_lado(slots.lado_a)
    _resolver_años_lado(slots.lado_b)
    if not slots.lado_a.año_central:
        return "lado_a.generacion"
    if not slots.lado_b.año_central:
        return "lado_b.generacion"
    return None


def _texto_pregunta(slot: str, slots: SlotsComparar) -> str:
    """Render del mensaje al user para el slot que falta."""
    if slot == "lado_a.identidad":
        return (
            "🆚 Dime los dos coches que quiero comparar.\n\n"
            "Ejemplos:\n"
            "• <code>Golf 7 GTI vs Civic Type R FK7</code>\n"
            "• <code>Megane RS contra Leon Cupra</code>\n"
            "• Una URL de cada coche."
        )
    if slot == "lado_b.identidad":
        a = slots.lado_a
        return (
            f"Tengo <b>{(a.marca + ' ' + a.modelo).strip().title()}</b>. "
            "¿Cuál es el segundo coche?"
        )
    if slot == "lado_a.generacion":
        a = slots.lado_a
        return (
            f"¿Qué generación o año del <b>{a.marca.title()} {a.modelo.title()}</b>?\n"
            "Responde con código (Mk7, FK7, B8...) o rango de años (2017-2020)."
        )
    if slot == "lado_b.generacion":
        b = slots.lado_b
        return (
            f"¿Qué generación o año del <b>{b.marca.title()} {b.modelo.title()}</b>?\n"
            "Responde con código o rango de años."
        )
    return "¿Puedes ser más concreto?"


async def alimentar_slots(sesion: dict, texto: str) -> tuple[bool, str]:
    """
    Procesa la respuesta del user. Devuelve (slots_completos, siguiente_msg).
    Si los slots están completos, siguiente_msg está vacío.
    """
    slots: SlotsComparar = sesion["slots"]
    texto = (texto or "").strip()
    sesion["ts"] = time.time()

    if not texto:
        return False, _texto_pregunta(_siguiente_pregunta(slots) or "lado_a.identidad", slots)

    # 1. Si está esperando una generación concreta, intentar parser rápido determinista
    sp = slots.slot_preguntando
    if sp == "lado_a.generacion" or sp == "lado_b.generacion":
        a_min, a_max = _parsear_años(texto)
        lado = slots.lado_a if sp == "lado_a.generacion" else slots.lado_b
        if a_min:
            lado.año_min = a_min
            lado.año_max = a_max
            lado.año_central = (a_min + a_max) // 2
        elif texto:
            # Es un código de generación (Mk7, FK7...). Lo guarda y lo resuelve por IA.
            if not lado.generacion:
                lado.generacion = texto[:50]
            # Pedir a IA que resuelva años de ese código en pase parser NL más abajo.

    # 2. Parser IA (solo si todavía hace falta — falta marca/modelo en algún lado)
    necesita_ia = (
        not slots.lado_a.marca or not slots.lado_a.modelo
        or not slots.lado_b.marca or not slots.lado_b.modelo
    )

    texto_para_ia = texto
    if necesita_ia or _URL_RE.search(texto):
        # Absorber URLs primero — rellenan lados directamente.
        texto_para_ia = await _absorber_urls(sesion, texto)

    if necesita_ia and texto_para_ia and len(texto_para_ia) >= 2:
        try:
            data = await parsear_comparar_input(texto_para_ia, slots_previos={
                "lado_a": slots.lado_a.__dict__,
                "lado_b": slots.lado_b.__dict__,
            })
            slots.lado_a.merge_lado(data.get("lado_a") or {})
            slots.lado_b.merge_lado(data.get("lado_b") or {})
        except Exception as e:
            logger.warning(f"[COMPARAR] parsear_comparar_input falló: {e}")

    # 3. Resolver años: primero regex (19xx/20xx), luego mini-LLM para códigos
    #    de generación (Mk7, FK7, B9...) donde el regex no encuentra años.
    for lado in (slots.lado_a, slots.lado_b):
        _resolver_años_lado(lado)

    # 3b. Mini-llamada IA para lados con generación conocida pero sin año aún
    lados_sin_año = [
        l for l in (slots.lado_a, slots.lado_b)
        if not l.año_central and l.generacion and l.marca and l.modelo
    ]
    if lados_sin_año:
        resultados = await asyncio.gather(
            *(resolver_generacion_años(l.marca, l.modelo, l.generacion) for l in lados_sin_año),
            return_exceptions=True,
        )
        for lado, res in zip(lados_sin_año, resultados):
            if isinstance(res, Exception):
                logger.warning(f"[COMPARAR] resolver_generacion_años {lado.generacion}: {res}")
                continue
            a_min, a_max = res
            if a_min:
                lado.año_min = a_min
                lado.año_max = a_max
                lado.año_central = (a_min + a_max) // 2

    # 4. Detectar mismo coche en ambos lados
    if (slots.lado_a.marca and slots.lado_b.marca
            and slots.lado_a.marca.lower() == slots.lado_b.marca.lower()
            and slots.lado_a.modelo.lower() == slots.lado_b.modelo.lower()
            and slots.lado_a.generacion.lower() == slots.lado_b.generacion.lower()
            and slots.lado_a.version_motor.lower() == slots.lado_b.version_motor.lower()):
        # Borrar lado_b para forzar otra entrada
        slots.lado_b = LadoComparar()
        slots.slot_preguntando = "lado_b.identidad"
        return False, (
            "⚠️ Los dos lados son el mismo coche. Dime un segundo coche distinto."
        )

    # 5. Decidir si falta algo y formar la pregunta
    falta = _siguiente_pregunta(slots)
    slots.slot_preguntando = falta or ""
    if falta:
        return False, _texto_pregunta(falta, slots)

    return True, ""


# ── Estadísticas ───────────────────────────────────────────────────────────

def _estadistica(precios: list[float]) -> dict:
    if not precios:
        return {"n": 0, "mediana": 0, "p25": 0, "p75": 0}
    p = sorted(precios)
    mediana = _stats_mod.median(p)
    if len(p) >= 4:
        q = _stats_mod.quantiles(p, n=4)
        p25, p75 = q[0], q[2]
    elif len(p) >= 2:
        p25, p75 = p[0], p[-1]
    else:
        p25 = p75 = p[0]
    return {"n": len(p), "mediana": int(mediana), "p25": int(p25), "p75": int(p75)}


# ── Procesar un lado en paralelo ──────────────────────────────────────────

async def _procesar_lado(lado: LadoComparar) -> dict:
    """Busca comparables + enriquece. Devuelve dict listo para el veredicto."""
    año = lado.año_central or 2020
    km_ref = max(20000, min(140000, (datetime.utcnow().year - año) * 15000))
    try:
        anuncios = await buscar_comparables_todas(
            lado.marca, lado.modelo, año, km_ref, n=40,
        )
    except Exception as e:
        logger.warning(f"[COMPARAR] buscar_comparables_todas {lado.marca} {lado.modelo}: {e}")
        anuncios = []

    # Filtrar precios y años razonables
    validos = [
        a for a in (anuncios or [])
        if getattr(a, "precio", 0) > 0 and getattr(a, "año", 0) > 1990
    ]
    if lado.año_min and lado.año_max:
        validos = [
            a for a in validos
            if lado.año_min - 1 <= a.año <= lado.año_max + 1
        ] or validos

    precios = [a.precio for a in validos]
    stats = _estadistica(precios)

    # Histórico
    try:
        guardar_historico_batch(validos)
    except Exception as e:
        logger.warning(f"[COMPARAR] guardar_historico_batch: {e}")

    # DGT determinista
    comb = lado.combustible or ""
    if not comb and validos:
        comb = getattr(validos[0], "motor", "") or ""
    etiqueta = calcular_etiqueta_dgt(comb, año)
    zbe = info_zbe(etiqueta)

    # Enriquecimiento Tavily + IA
    try:
        enriq = await enriquecer_modelo(
            lado.marca, lado.modelo, lado.version_motor, año,
        )
    except Exception as e:
        logger.warning(f"[COMPARAR] enriquecer_modelo {lado.marca} {lado.modelo}: {e}")
        enriq = {
            "fiabilidad_score": 5, "averias_tipicas": [],
            "consumo_real_l100": None, "mantenimiento_anual_eur": None,
            "depreciacion_pct_3a": None, "puntos_fuertes": [],
            "pega_gorda": "", "fuentes_ok": 0,
        }

    # ── Derivados deterministas (no IA) ─────────────────────────────────────
    consumo = enriq.get("consumo_real_l100")
    mant = enriq.get("mantenimiento_anual_eur")
    deprec_pct = enriq.get("depreciacion_pct_3a")
    mediana = stats["mediana"]

    coste_combustible_anual_eur: int | None = None
    if isinstance(consumo, (int, float)) and consumo > 0:
        coste_combustible_anual_eur = int(round(consumo * COMPARAR_KM_AÑO_REF / 100 * COMPARAR_PRECIO_LITRO_EUR))

    perdida_3a_eur: int | None = None
    valor_residual_eur: int | None = None
    if isinstance(deprec_pct, (int, float)) and deprec_pct > 0 and mediana:
        perdida_3a_eur = int(round(mediana * deprec_pct / 100))
        valor_residual_eur = max(0, int(mediana) - perdida_3a_eur)

    tco_3a_eur: int | None = None
    if (perdida_3a_eur is not None and isinstance(mant, (int, float))
            and coste_combustible_anual_eur is not None):
        # TCO simplificado a 3 años:
        #   perdida_3a + 3 × mantenimiento + 3 × combustible
        tco_3a_eur = int(perdida_3a_eur + 3 * mant + 3 * coste_combustible_anual_eur)

    return {
        "marca": lado.marca.title(),
        "modelo": lado.modelo.title(),
        "generacion": lado.generacion,
        "version_motor": lado.version_motor,
        "nombre_display": lado.nombre_display(),   # alias verbatim para la IA
        "año_min": lado.año_min or año,
        "año_max": lado.año_max or año,
        "año_central": año,
        "n_comparables": stats["n"],
        "mediana": stats["mediana"],
        "p25": stats["p25"],
        "p75": stats["p75"],
        "etiqueta_dgt": etiqueta,
        "info_zbe": zbe,
        "enriquecimiento": enriq,
        # Derivados deterministas para que la IA NO calcule
        "coste_combustible_anual_eur": coste_combustible_anual_eur,
        "perdida_3a_eur": perdida_3a_eur,
        "valor_residual_eur": valor_residual_eur,
        "tco_3a_eur": tco_3a_eur,
        "km_año_referencia": COMPARAR_KM_AÑO_REF,
        "precio_litro_referencia": COMPARAR_PRECIO_LITRO_EUR,
    }


# ── Pipeline público ──────────────────────────────────────────────────────

async def ejecutar_pipeline(sesion: dict) -> str:
    """Lanza A y B en paralelo, luego veredicto IA. Devuelve HTML."""
    slots: SlotsComparar = sesion["slots"]
    a, b = await asyncio.gather(
        _procesar_lado(slots.lado_a),
        _procesar_lado(slots.lado_b),
        return_exceptions=False,
    )
    sesion["ts"] = time.time()
    logger.info(
        f"[COMPARAR] {a['nombre_display']}: n={a['n_comparables']} "
        f"mediana={a['mediana']}€ fuel={a['coste_combustible_anual_eur']}€/año "
        f"perdida3a={a['perdida_3a_eur']}€ tco3a={a['tco_3a_eur']}€"
    )
    logger.info(
        f"[COMPARAR] {b['nombre_display']}: n={b['n_comparables']} "
        f"mediana={b['mediana']}€ fuel={b['coste_combustible_anual_eur']}€/año "
        f"perdida3a={b['perdida_3a_eur']}€ tco3a={b['tco_3a_eur']}€"
    )
    html_veredicto = await generar_veredicto_comparar(a, b)
    return html_veredicto
