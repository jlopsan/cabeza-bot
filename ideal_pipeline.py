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
from scraper import buscar_comparables_wallapop

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

def _modelo_coincide(anun_marca: str, anun_modelo: str, cand_marca: str, cand_modelo: str) -> bool:
    """
    True si el anuncio pertenece al mismo marca+modelo que el candidato.
    Evita que una búsqueda de 'Mazda 2' muestre 'Mazda 3' o 'CX-5'.
    """
    a_mar = (anun_marca or "").lower().strip()
    c_mar = (cand_marca or "").lower().strip()
    a_mod = (anun_modelo or "").lower().strip()
    c_mod = (cand_modelo or "").lower().strip()

    # Marca: si ambas conocidas deben coincidir
    if a_mar and c_mar and a_mar != c_mar:
        return False

    # Modelo: si ambas conocidas
    if not a_mod or not c_mod:
        return True  # Sin datos → benefit of doubt

    if a_mod == c_mod:
        return True

    # Modelos cortos (≤3 chars: "2", "3", "a3"): solo acepta exacto
    # para evitar que "2" aparezca en "cx-2", "2016", etc.
    if len(c_mod) <= 3 and len(a_mod) <= 3:
        return False

    # Modelos largos: acepta si uno contiene al otro
    # ("golf" in "golf vii", "3" in "serie 3", "octavia" in "octavia rs")
    return c_mod in a_mod or a_mod in c_mod


def _familia_combustible(texto: str) -> str:
    """Extrae familia de combustible de cualquier texto. Devuelve '' si no detecta."""
    t = (texto or "").lower()
    if any(x in t for x in ("tdi", "hdi", "dci", "cdti", "tdci", "bluehdi", "crdi",
                             "diesel", "diésel", "gasoil")):
        return "diesel"
    if any(x in t for x in ("hybrid", "hibrid", "phev", "hev", "mhev", "e-tech",
                             "self-charg", "self charg")):
        return "hibrido"
    if any(x in t for x in ("electr", "bev", " ev ")):
        return "electrico"
    if any(x in t for x in ("tsi", "tfsi", "puretech", "ecoboost", "vvt", "gdi",
                             "fsi", "skyactiv", "turbo", "gasolina", "nafta",
                             "vti", "thp", "mpi", "ecotec", "dualjet")):
        return "gasolina"
    return ""


def _año_rango_por_presupuesto(presup_max: int) -> tuple[int, int]:
    """Dos años ancla para dos búsquedas paralelas. Cubre ~6 años de mercado."""
    if presup_max <= 5000:  return 2011, 2014
    if presup_max <= 7000:  return 2013, 2016
    if presup_max <= 9000:  return 2014, 2017
    if presup_max <= 12000: return 2016, 2019
    if presup_max <= 16000: return 2018, 2021
    if presup_max <= 22000: return 2020, 2022
    return 2021, 2023


def _km_max_por_presupuesto(presup_max: int) -> int:
    """Km máximo coherente con el presupuesto para pasar al scraper."""
    if presup_max <= 6000:  return 220000
    if presup_max <= 9000:  return 190000
    if presup_max <= 13000: return 160000
    if presup_max <= 18000: return 130000
    return 100000


def _merge_dedup(res_a, res_b) -> list:
    """Merge de dos resultados de búsqueda, deduplicando por item_id."""
    vistos: set = set()
    out: list = []
    for res in (res_a, res_b):
        if not isinstance(res, list):
            continue
        for a in res:
            if a.item_id not in vistos:
                vistos.add(a.item_id)
                out.append(a)
    return out


def _anuncio_encaja(anuncio, candidato: dict, slots: SlotsIdeal,
                    año_min: int = 0, año_max: int = 0,
                    check_motor: bool = True) -> bool:
    """
    Filtros duros sobre un anuncio.
    Motor: rechaza solo por CONTRADICCIÓN de familia combustible o cilindrada.
    Wallapop.motor = "Gasolina 95cv" (familia, no código). Coches.net.motor = "".
    """
    if anuncio.precio <= 0 or anuncio.km <= 0 or anuncio.año <= 1990:
        return False

    # ── Filtro de marca+modelo — el anuncio debe ser del coche buscado ───────
    if not _modelo_coincide(anuncio.marca, anuncio.modelo,
                             candidato.get("marca", ""), candidato.get("modelo", "")):
        return False

    # ── Filtro de motor — contradicción probada ──────────────────────────────
    if check_motor:
        version_motor = candidato.get("version_motor", "")
        fam_cand = _familia_combustible(version_motor)
        haystack_motor = (getattr(anuncio, "motor", "") or "") + " " + (anuncio.titulo or "")
        fam_anun = _familia_combustible(haystack_motor)

        # Contradicción de familia combustible
        if fam_cand and fam_anun and fam_cand != fam_anun:
            return False

        # Cilindrada contradictoria: anuncio menciona una distinta a la candidata
        cil_cand = _re.findall(r"\b(\d\.\d)\b", version_motor.lower())
        if cil_cand:
            cil_anun = _re.findall(r"\b(\d\.\d)\b", haystack_motor.lower())
            if cil_anun and cil_cand[0] not in cil_anun:
                return False

    # ── Filtro de año ────────────────────────────────────────────────────────
    if año_min and año_max:
        if not (año_min - 1 <= anuncio.año <= año_max + 1):
            return False

    # ── Km vs edad ───────────────────────────────────────────────────────────
    edad = max(1, datetime.utcnow().year - anuncio.año)
    if anuncio.km > edad * 22000 * 1.5:
        return False

    # ── Precio dentro del presupuesto ────────────────────────────────────────
    presup_max = slots.presupuesto_max or 0
    presup_min = slots.presupuesto_min or int(presup_max * 0.5)
    if not (presup_min <= anuncio.precio <= presup_max * 1.05):
        return False

    try:
        flags = detectar_red_flags(anuncio, None)
        if any("estafa" in f.lower() or "scam" in f.lower() for f in flags):
            return False
    except Exception:
        pass

    return True


async def _buscar_anuncios_candidato(candidato: dict, slots: SlotsIdeal) -> list:
    """
    Dos búsquedas paralelas con cobertura de ~6 años + fallbacks en cascada.
    Nunca devuelve vacío si el mercado tiene algo comprable.
    """
    presup_max = slots.presupuesto_max or 0
    presup_min = slots.presupuesto_min or int(presup_max * 0.5)
    año_bajo, año_alto = _año_rango_por_presupuesto(presup_max)
    km_max = _km_max_por_presupuesto(presup_max)
    marca, modelo = candidato["marca"], candidato["modelo"]

    # Construir filtros para coches.net (búsqueda IA con motor concreto)
    version_motor = candidato.get("version_motor", "")
    fam_comb = _familia_combustible(version_motor)
    filtros_busqueda: dict | None = (
        {"combustible": fam_comb, "version_motor": version_motor}
        if version_motor else None
    )

    # Dos búsquedas paralelas cubriendo ~6 años de mercado (solo Wallapop — coches.net devuelve marcas incorrectas)
    res_b, res_a = await asyncio.gather(
        buscar_comparables_wallapop(marca, modelo, año_bajo, km_max, n=20),
        buscar_comparables_wallapop(marca, modelo, año_alto, km_max, n=20),
        return_exceptions=True,
    )
    anuncios = _merge_dedup(res_b, res_a)

    # ── Intento 1: todos los filtros (motor familia + año + precio + km) ─────
    encajan = [a for a in anuncios
               if _anuncio_encaja(a, candidato, slots, año_bajo, año_alto, check_motor=True)]

    # ── Fallback 1: sin filtro motor (coches.net motor="", wallapop motor=tipo) ─
    if not encajan:
        encajan = [a for a in anuncios
                   if _anuncio_encaja(a, candidato, slots, año_bajo, año_alto, check_motor=False)]

    # ── Fallback 2: solo precio dentro de presupuesto, año mínimo razonable ──
    if not encajan:
        encajan = [a for a in anuncios
                   if a.precio > 0 and a.precio <= presup_max * 1.05
                   and a.precio >= presup_min * 0.7
                   and a.km > 0 and a.año > 2005]

    encajan.sort(key=lambda a: a.precio)
    logger.info(f"[IDEAL_V2] {marca} {modelo}: {len(anuncios)} raw → "
                f"{len(encajan)} encajan "
                f"(años {año_bajo}-{año_alto}, presup {presup_min}-{presup_max}€)")
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
