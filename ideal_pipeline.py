"""
ideal_pipeline.py — Orquestador del flujo /ideal v2 (6 fases).

Fase 1: Slot-filling (parseo NL multi-turn)
Fase 2: Brainstorm de candidatos (IA Llama 4)
Fase 3: Enriquecimiento Tavily + síntesis IA paralela
Fase 4: Validación en mercado (anuncios reales filtrados)
Fase 5: Veredicto experto (HTML Telegram)
Fase 6: Iteración (segunda ronda si user rechaza)

Sesiones en memoria por user_id, TTL 30 min.
"""
import asyncio
import logging
import re as _re
import time
from datetime import datetime
from typing import Any

from ai import (
    parsear_query_a_slots,
    generar_candidatos_modelos,
    enriquecer_candidato,
    generar_veredicto_ideal_v2,
)
from ideal_schema import (
    SlotsIdeal, DEFAULTS_SLOTS, es_skip, parsear_respuesta_corta,
)
from red_flags import detectar_red_flags
from scraper import buscar_comparables_todas

logger = logging.getLogger(__name__)

# ── Sesiones en memoria ────────────────────────────────────────────────────

_SESIONES_TTL_S = 30 * 60
_SESIONES: dict[int, dict[str, Any]] = {}


def get_sesion(user_id: int) -> dict | None:
    sesion = _SESIONES.get(user_id)
    if not sesion:
        return None
    if time.time() - sesion.get("ts", 0) > _SESIONES_TTL_S:
        _SESIONES.pop(user_id, None)
        return None
    return sesion


def set_sesion(user_id: int, sesion: dict) -> None:
    sesion["ts"] = time.time()
    _SESIONES[user_id] = sesion


def reset_sesion(user_id: int) -> None:
    _SESIONES.pop(user_id, None)


def nueva_sesion(user_id: int) -> dict:
    sesion = {
        "slots": SlotsIdeal(),
        "candidatos_iniciales": [],
        "enriquecimientos": {},     # clave: f"{marca}|{modelo}|{version}"
        "top5": [],
        "ronda": 1,
        "rechazados": [],
        "slot_preguntando": None,   # slot actualmente en flight
        "intentos_slot": {},        # slot → nº de intentos fallidos
        "duracion_inicio": time.time(),
        "ts": time.time(),
    }
    _SESIONES[user_id] = sesion
    return sesion


# ── FASE 1: slot-filling ───────────────────────────────────────────────────

_MAX_INTENTOS_SLOT = 2


async def alimentar_slots(sesion: dict, texto_usuario: str) -> SlotsIdeal:
    """
    Procesa la respuesta del usuario:
    1. Si hay slot en flight, intenta parser determinista corto.
    2. Si user dice skip → poner default (excepto presupuesto_max).
    3. Si tras N intentos seguimos atascados → poner default.
    4. Siempre llama al parser IA con el texto completo (puede traer extras).
    """
    slots: SlotsIdeal = sesion["slots"]
    slot_actual: str | None = sesion.get("slot_preguntando")
    intentos: dict = sesion.setdefault("intentos_slot", {})
    texto = (texto_usuario or "").strip()

    skip = es_skip(texto)
    resuelto_directo = False

    if slot_actual:
        # 1. Parser determinista
        valor = parsear_respuesta_corta(slot_actual, texto)
        if valor is not None:
            setattr(slots, slot_actual, valor)
            intentos.pop(slot_actual, None)
            resuelto_directo = True
        elif skip and slot_actual in DEFAULTS_SLOTS:
            setattr(slots, slot_actual, DEFAULTS_SLOTS[slot_actual])
            intentos.pop(slot_actual, None)
            resuelto_directo = True
        else:
            intentos[slot_actual] = intentos.get(slot_actual, 0) + 1

    # 2. Parser IA sobre texto completo (siempre — puede traer slots adicionales)
    if texto and len(texto) >= 3 and not (skip and resuelto_directo and len(texto) < 12):
        try:
            extraidos = await parsear_query_a_slots(texto, slots.to_dict())
            if extraidos:
                slots.merge(extraidos)
        except Exception as e:
            logger.warning(f"[IDEAL_V2] parsear_query_a_slots: {e}")

    # 3. Si tras IA el slot actual sigue None y se ha intentado >=N veces → default
    if slot_actual and getattr(slots, slot_actual, None) is None:
        if intentos.get(slot_actual, 0) >= _MAX_INTENTOS_SLOT:
            if slot_actual in DEFAULTS_SLOTS:
                setattr(slots, slot_actual, DEFAULTS_SLOTS[slot_actual])
                intentos.pop(slot_actual, None)

    # 4. Avanzar slot_preguntando al siguiente faltante
    faltantes = slots.slots_criticos_faltantes()
    sesion["slot_preguntando"] = faltantes[0] if faltantes else None

    sesion["slots"] = slots
    sesion["ts"] = time.time()
    return slots


# ── FASE 2: brainstorm + filtro determinista ───────────────────────────────

def filtrar_candidatos(candidatos: list[dict], slots: SlotsIdeal) -> list[dict]:
    """Aplica reglas duras post-IA. Descarta los que no cumplen."""
    presup_max = slots.presupuesto_max or 0
    rechazos = {r.lower() for r in (slots.rechazos_explicitos or [])}
    comb_pref = (slots.combustible_preferencia or "indistinto").lower()

    salida: list[dict] = []
    for c in candidatos:
        etiqueta = (c.get("etiqueta_dgt_estimada") or "").upper()
        if slots.zbe_relevante and etiqueta in ("B", "SIN"):
            continue
        if c.get("modelo", "").lower() in rechazos:
            continue
        if c.get("marca", "").lower() in rechazos:
            continue

        version_lower = (c.get("version_motor") or "").lower()
        es_diesel = any(t in version_lower for t in ("tdi", "hdi", "dci", "cdti", "tdci", "bluehdi", " diesel", "diésel", "crdi"))
        if es_diesel and (slots.km_anuales or 0) < 12000:
            continue

        # Combustible explícitamente rechazado
        if comb_pref not in ("indistinto", ""):
            if comb_pref == "gasolina" and es_diesel:
                continue
            if comb_pref == "diesel" and not es_diesel and "hibrid" not in version_lower:
                # asumimos no-diesel; aceptamos hibridos si user pidió diesel? No, descartar
                continue
            if comb_pref in ("hibrido", "phev") and not any(t in version_lower for t in ("hybrid", "hibrid", "phev", "hev", "e-tech")):
                continue
            if comb_pref == "electrico" and not any(t in version_lower for t in ("electric", "eléctric", " ev ", "bev")):
                continue

        salida.append(c)
    return salida


async def fase_brainstorm(sesion: dict) -> list[dict]:
    """
    Genera candidatos. Si tras filtro quedan <5, lanza segundo brainstorm.
    """
    slots: SlotsIdeal = sesion["slots"]
    slots_dict = slots.to_dict()

    cand1 = await generar_candidatos_modelos(slots_dict, segunda_ronda=False)
    filtrados = filtrar_candidatos(cand1, slots)

    if len(filtrados) < 5:
        cand2 = await generar_candidatos_modelos(
            slots_dict,
            segunda_ronda=True,
            rechazados=[c["modelo"] for c in cand1],
        )
        filtrados2 = filtrar_candidatos(cand2, slots)
        # Mergeamos sin duplicar por (marca, modelo, version_motor)
        vistos = {(c["marca"], c["modelo"], c["version_motor"]) for c in filtrados}
        for c in filtrados2:
            key = (c["marca"], c["modelo"], c["version_motor"])
            if key not in vistos:
                filtrados.append(c)
                vistos.add(key)

    sesion["candidatos_iniciales"] = filtrados
    sesion["ts"] = time.time()
    logger.info(f"[IDEAL_V2] Fase brainstorm: {len(filtrados)} candidatos finales")
    return filtrados


# ── FASE 3: enriquecimiento ────────────────────────────────────────────────

def _key_candidato(c: dict) -> str:
    return f"{c['marca']}|{c['modelo']}|{c['version_motor']}".lower()


async def fase_enriquecimiento(sesion: dict, top_n: int = 8) -> list[dict]:
    """
    Enriquecer top_n candidatos en paralelo. Devuelve los mismos con campo
    'enriquecimiento' añadido. Reusa caché de sesión si existe.
    """
    candidatos = sesion["candidatos_iniciales"][:top_n]
    cache: dict = sesion.setdefault("enriquecimientos", {})

    async def _enriq(c: dict) -> dict:
        k = _key_candidato(c)
        if k in cache:
            return {**c, "enriquecimiento": cache[k]}
        try:
            data = await enriquecer_candidato(c)
        except Exception as e:
            logger.warning(f"[IDEAL_V2] enriquecer {k} falló: {e}")
            data = {
                "fiabilidad_score": 5, "averías_típicas": [],
                "puntos_fuertes_reales": [], "verdict_propietarios": "recomendado_con_caveats",
                "alternativas_mencionadas": [], "comentario_experto": "",
            }
        cache[k] = data
        return {**c, "enriquecimiento": data}

    enriquecidos = await asyncio.gather(
        *(_enriq(c) for c in candidatos), return_exceptions=False
    )

    # Score combinado: 35% encaje + 45% fiabilidad + 20% market (placeholder)
    for item in enriquecidos:
        encaje = item.get("encaje_con_caso_uso", 5) / 10
        fiab = item.get("enriquecimiento", {}).get("fiabilidad_score", 5) / 10
        item["score_parcial"] = 0.35 * encaje + 0.45 * fiab + 0.20 * 1.0

    enriquecidos.sort(key=lambda x: x.get("score_parcial", 0), reverse=True)
    top5 = enriquecidos[:5]
    sesion["top5"] = top5
    sesion["ts"] = time.time()
    return top5


# ── FASE 4: validación en mercado ──────────────────────────────────────────

_TECH_TOKENS_MOTOR = [
    "tsi", "tfsi", "tdi", "hdi", "dci", "cdti", "tdci", "bluehdi", "blue hdi",
    "puretech", "vti", "thp", "mpi", "gdi", "fsi", "ecoboost", "vvt-i", "vvti",
    "vvt", "dualjet", "skyactiv", "turbo", "ecotec",
    "hybrid", "hibrid", "phev", "hev", "self charg", "mhev", "e-tech",
    "crdi", "drive-e",
]


def _keywords_motor(version_motor: str) -> list[str]:
    """Extrae cilindrada (1.0, 2.0) y tokens técnicos del motor para matching."""
    m = (version_motor or "").lower()
    keys: list[str] = []
    cil = _re.findall(r"\b(\d\.\d{1,2})\b", m)
    keys.extend(cil)
    for tok in _TECH_TOKENS_MOTOR:
        if tok in m:
            keys.append(tok)
    return keys


def _anuncio_encaja(anuncio, candidato: dict, slots: SlotsIdeal) -> bool:
    """Filtros duros sobre un anuncio para validar que es comprable."""
    if anuncio.precio <= 0 or anuncio.km <= 0 or anuncio.año <= 1990:
        return False

    keywords = _keywords_motor(candidato.get("version_motor", ""))
    if keywords:
        haystack = f"{(anuncio.titulo or '').lower()} {(anuncio.descripcion or '').lower()} {(getattr(anuncio, 'motor', '') or '').lower()}"
        # Solo filtra si el haystack tiene contenido suficiente (título largo o descripción)
        # Resultados de búsqueda suelen tener título corto sin código motor → pasar
        if len(haystack.strip()) > 60 and not any(k in haystack for k in keywords):
            return False

    # Km vs edad
    edad = max(1, datetime.utcnow().year - anuncio.año)
    km_max_razonable = edad * 22000
    if anuncio.km > km_max_razonable * 1.5:
        return False

    presup_max = slots.presupuesto_max or 0
    presup_min = slots.presupuesto_min or int(presup_max * 0.5)
    if not (presup_min <= anuncio.precio <= presup_max * 1.05):
        return False

    try:
        flags = detectar_red_flags(anuncio, None)
        graves = [f for f in flags if "estafa" in f.lower() or "scam" in f.lower()]
        if graves:
            return False
    except Exception:
        pass

    return True


def _año_busqueda_por_presupuesto(presup_max: int) -> int:
    """Año de búsqueda derivado del presupuesto real, no de estimados IA."""
    if presup_max <= 5000:  return 2012
    if presup_max <= 7000:  return 2014
    if presup_max <= 9000:  return 2016
    if presup_max <= 12000: return 2018
    if presup_max <= 16000: return 2019
    if presup_max <= 22000: return 2021
    return 2022


async def _buscar_anuncios_candidato(candidato: dict, slots: SlotsIdeal) -> list:
    """Busca anuncios reales en el rango de precio del usuario, sin estimar."""
    presup_max = slots.presupuesto_max or 0
    año_busqueda = _año_busqueda_por_presupuesto(presup_max)
    km_estimado = max(1, datetime.utcnow().year - año_busqueda) * 16000

    try:
        anuncios = await buscar_comparables_todas(
            marca=candidato["marca"],
            modelo=candidato["modelo"],
            año=año_busqueda,
            km=km_estimado,
            n=25,
        )
    except Exception as e:
        logger.warning(f"[IDEAL_V2] buscar {candidato['marca']} {candidato['modelo']}: {e}")
        return []

    if not anuncios:
        return []

    encajan = [a for a in anuncios if _anuncio_encaja(a, candidato, slots)]
    encajan.sort(key=lambda a: a.precio)
    logger.info(f"[IDEAL_V2] {candidato['marca']} {candidato['modelo']}: "
                f"{len(anuncios)} raw → {len(encajan)} dentro de presupuesto "
                f"(año búsqueda={año_busqueda}, max={presup_max}€)")
    return encajan[:4]


async def fase_validacion_mercado(sesion: dict) -> list[dict]:
    """
    Para cada uno del top5, busca anuncios reales y los adjunta.
    Recalcula score con score_mercado.
    Devuelve top3 final.
    """
    slots: SlotsIdeal = sesion["slots"]
    top5 = sesion.get("top5") or []

    resultados = await asyncio.gather(
        *(_buscar_anuncios_candidato(c, slots) for c in top5),
        return_exceptions=True,
    )

    items: list[dict] = []
    for cand, res in zip(top5, resultados):
        if isinstance(res, Exception):
            logger.warning(f"[IDEAL_V2] anuncios {cand['marca']} {cand['modelo']}: {res}")
            anuncios_buenos = []
        else:
            anuncios_buenos = res or []

        score_mercado = min(len(anuncios_buenos) / 3.0, 1.0)
        score_total = (
            0.35 * (cand.get("encaje_con_caso_uso", 5) / 10)
            + 0.45 * (cand.get("enriquecimiento", {}).get("fiabilidad_score", 5) / 10)
            + 0.20 * score_mercado
        )

        anuncios_dict = [
            {
                "año": a.año, "km": a.km, "precio": a.precio,
                "provincia": a.provincia, "url": a.url,
                "fuente": getattr(a, "fuente", ""),
            }
            for a in anuncios_buenos[:2]
        ]

        items.append({
            "candidato": cand,
            "enriquecimiento": cand.get("enriquecimiento", {}),
            "anuncios": anuncios_dict,
            "n_anuncios": len(anuncios_buenos),
            "score_total": score_total,
        })

    items.sort(key=lambda x: x["score_total"], reverse=True)

    # Buffer 4-5 para "Más opciones"
    sesion["top5_validados"] = items
    top3 = items[:3]
    sesion["top3"] = top3
    sesion["ts"] = time.time()
    return top3


# ── FASE 5: render ─────────────────────────────────────────────────────────

async def fase_render(sesion: dict) -> str:
    """Llama IA para HTML final."""
    slots: SlotsIdeal = sesion["slots"]
    top3 = sesion.get("top3") or []
    if not top3:
        return ""
    try:
        return await generar_veredicto_ideal_v2(top3, slots.to_dict())
    except Exception as e:
        logger.warning(f"[IDEAL_V2] generar_veredicto_ideal_v2 falló: {e}")
        return ""


# ── FASE 6: iteración ──────────────────────────────────────────────────────

async def fase_segunda_ronda(sesion: dict) -> list[dict]:
    """
    Recalcula candidatos rechazando los del top3 actual. Pasa por enriquecimiento
    + validación de mercado y devuelve el nuevo top3.
    """
    slots: SlotsIdeal = sesion["slots"]
    top3_actual = sesion.get("top3") or []
    rechazados = sesion.setdefault("rechazados", [])
    for item in top3_actual:
        c = item.get("candidato", {})
        modelo = c.get("modelo")
        if modelo and modelo not in rechazados:
            rechazados.append(modelo)

    sesion["ronda"] = sesion.get("ronda", 1) + 1

    cand_nuevos = await generar_candidatos_modelos(
        slots.to_dict(), segunda_ronda=True, rechazados=rechazados,
    )
    filtrados = filtrar_candidatos(cand_nuevos, slots)
    if not filtrados:
        return []

    sesion["candidatos_iniciales"] = filtrados
    await fase_enriquecimiento(sesion)
    return await fase_validacion_mercado(sesion)


# ── PIPELINE COMPLETO (Fases 2→5) ──────────────────────────────────────────

async def ejecutar_pipeline(sesion: dict) -> tuple[list[dict], str]:
    """
    Lanza brainstorm → enriquecimiento → mercado → render.
    Devuelve (top3, html_veredicto).
    """
    slots: SlotsIdeal = sesion["slots"]
    slots.normalizar()

    candidatos = await fase_brainstorm(sesion)
    if not candidatos:
        return [], ""

    await fase_enriquecimiento(sesion)
    top3 = await fase_validacion_mercado(sesion)
    if not top3:
        return [], ""

    html_veredicto = await fase_render(sesion)
    return top3, html_veredicto
