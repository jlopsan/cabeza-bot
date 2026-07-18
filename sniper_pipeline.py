# sniper_pipeline.py — Lógica compartida del sniper (bot + worker).
#
# Se extrae aquí para que main.py (crear misión → evaluar candidatos iniciales)
# y worker.py (ciclo → evaluar anuncios nuevos) usen EXACTAMENTE la misma lógica
# de valoración, cuenta de importación, dedup/snapshot y render de tarjeta.
# NO importa main.py (evita ciclos): la estadística de precios es una mediana simple.
#
import html
import json
import logging
import statistics
from datetime import datetime

from config import (
    VALORACION_TTL_H, VALORACION_KM_BANDA,
    SNIPER_UMBRAL_EUR, SNIPER_UMBRAL_PCT,
    SNIPER_AVISO_IEDMT_ANOS,
)
from calculator import calcular_margen_sniper, es_nuevo_fiscal
from scraper import ScraperAutoScout24, buscar_comparables_todas
import database as db

logger = logging.getLogger(__name__)


# ─── CLAVE DE SCRAPEO (agrupación de misiones) ───────────────────────────────

def filtros_mision(mision: dict) -> dict:
    try:
        return json.loads(mision.get("filtros", "{}") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def clave_scrapeo(mision: dict) -> str:
    """URL de detección normalizada. Misiones con la misma clave comparten scrapeo."""
    return ScraperAutoScout24().url_deteccion_normalizada(
        mision.get("marca", ""), mision.get("modelo", ""), filtros_mision(mision)
    )


# ─── VALORACIÓN DE MERCADO ES (cacheada) ─────────────────────────────────────

def km_banda(km: int) -> int:
    return int((km or 0) // VALORACION_KM_BANDA)


def valoracion_fresca(marca: str, modelo: str, año: int, km: int) -> dict | None:
    """Valoración cacheada si NO ha caducado; None si no existe o está vieja."""
    v = db.get_valoracion(marca, modelo, año, km_banda(km))
    if v and not db.valoracion_caducada(v.get("actualizado_at", ""), VALORACION_TTL_H):
        return v
    return None


async def refrescar_valoracion(marca: str, modelo: str, año: int, km: int) -> dict | None:
    """
    Scrapea comparables ES, calcula mediana, la persiste (valoración + histórico)
    y la devuelve. None si no hay comparables suficientes (<3). Reutiliza el
    pipeline de comparables de /analizar (cero scraping nuevo).
    """
    try:
        comparables = await buscar_comparables_todas(marca, modelo, año, km, n=30)
    except Exception as e:
        logger.warning(f"[SNIPER] comparables ES fallaron {marca} {modelo}: {e}")
        return db.get_valoracion(marca, modelo, año, km_banda(km))  # devuelve lo viejo si hay

    precios = sorted(float(getattr(a, "precio", 0) or 0) for a in comparables)
    precios = [p for p in precios if p > 0]
    if len(precios) < 3:
        return db.get_valoracion(marca, modelo, año, km_banda(km))

    mediana = round(statistics.median(precios), 0)
    db.upsert_valoracion(marca, modelo, año, km_banda(km), mediana, len(precios), precios)

    # Dataset histórico (regla innegociable): persistir comparables ES.
    try:
        db.guardar_historico_batch(comparables)
    except Exception as e:
        logger.warning(f"[SNIPER] persistir histórico ES falló: {e}")

    return db.get_valoracion(marca, modelo, año, km_banda(km))


# ─── EVALUACIÓN DE CANDIDATO ─────────────────────────────────────────────────

def confianza(n_comparables: int) -> tuple[str, str]:
    """Emoji + nivel según nº de comparables ES."""
    if n_comparables >= 8:
        return "🟢", "alta"
    if n_comparables >= 4:
        return "🟡", "media"
    return "🔴", "baja"


def evaluar_candidato(anuncio: dict, valoracion: dict,
                      umbral_eur: int | None = None,
                      umbral_pct: float | None = None) -> dict:
    """
    Calcula la cuenta de importación del candidato contra la valoración ES.
    Devuelve {alerta, cuenta, n_comparables}. `alerta` exige AMBOS umbrales.
    """
    umbral_eur = SNIPER_UMBRAL_EUR if umbral_eur is None else umbral_eur
    umbral_pct = SNIPER_UMBRAL_PCT if umbral_pct is None else umbral_pct

    mediana = float(valoracion.get("mediana", 0) or 0)
    cuenta = calcular_margen_sniper(
        precio_de=float(anuncio.get("precio", 0) or 0),
        mediana_es=mediana,
        co2=anuncio.get("co2", 0),
        combustible=anuncio.get("combustible", ""),
        año=anuncio.get("año", 0),
    )
    alerta = cuenta["margen_eur"] >= umbral_eur and cuenta["margen_pct"] >= umbral_pct
    return {
        "alerta": alerta,
        "cuenta": cuenta,
        "n_comparables": int(valoracion.get("n_comparables", 0) or 0),
    }


# ─── DEDUP / SNAPSHOT ────────────────────────────────────────────────────────

def necesita_siembra(mision: dict) -> bool:
    return not mision.get("snapshot_sembrado")


def sembrar(mision: dict, anuncios: list[dict]) -> int:
    return db.sembrar_snapshot(mision["id"], anuncios, mision.get("marca", ""), mision.get("modelo", ""))


def filtrar_nuevos(mision: dict, anuncios: list[dict]) -> list[dict]:
    """
    Candidatos = anuncios cuyo ID no está registrado para la misión. Las
    re-publicaciones (ID nuevo, misma huella <30 días) se marcan vistas y se
    descartan. Cada candidato queda anotado con su huella en '_huella'.
    """
    mid   = mision["id"]
    marca = mision.get("marca", "")
    modelo = mision.get("modelo", "")
    nuevos: list[dict] = []
    for a in anuncios:
        aid = str(a.get("id", ""))
        if not aid or db.anuncio_ya_visto(mid, aid):
            continue
        h = db.huella_anuncio(marca, modelo, a.get("año", 0), a.get("km", 0), a.get("precio", 0))
        if db.huella_vista_reciente(mid, h, 30):
            db.registrar_visto(mid, aid, h, tipo="snapshot",
                               precio=a.get("precio", 0), url=a.get("link", ""))
            continue
        a["_huella"] = h
        nuevos.append(a)
    return nuevos


def marcar_visto(mision_id: int, anuncio: dict, tipo: str = "snapshot",
                 cuenta: dict | None = None):
    """Registra el anuncio como visto (snapshot) o alertado (alerta)."""
    h = anuncio.get("_huella", "")
    db.registrar_visto(
        mision_id, str(anuncio.get("id", "")), h, tipo=tipo,
        precio=anuncio.get("precio", 0),
        margen_eur=(cuenta or {}).get("margen_eur", 0),
        margen_pct=(cuenta or {}).get("margen_pct", 0),
        url=anuncio.get("link", ""),
    )


# ─── RENDER DE LA TARJETA DE ALERTA ──────────────────────────────────────────

def boton_ver_anuncio(url: str) -> dict:
    """reply_markup para la API HTTP de Telegram (worker) o InlineKeyboard (bot)."""
    return {"inline_keyboard": [[{"text": "🔗 Ver anuncio", "url": url or "#"}]]}


def _eur(v: float) -> str:
    try:
        return f"{float(v):,.0f}€".replace(",", ".")
    except (ValueError, TypeError):
        return "N/D"


def render_tarjeta_alerta(anuncio: dict, valoracion: dict, cuenta: dict,
                          mision_id: int | None = None) -> str:
    """
    Tarjeta de alerta con el formato del vídeo. html.escape en todo campo
    scrapeado. No promete datos que no tiene (sin CO₂ → IEDMT estimado, total).
    """
    titulo = html.escape(anuncio.get("titulo", "") or f"{anuncio.get('año','')}")
    año = anuncio.get("año", "") or "N/D"
    km  = anuncio.get("km", 0) or 0
    km_str = f"{int(km):,}".replace(",", ".") if km else "N/D"

    emoji_conf, nivel_conf = confianza(valoracion.get("n_comparables", 0))
    margen = cuenta["margen_eur"]
    signo = "" if margen < 0 else ""

    lineas = [
        "🎯 <b>SNIPER — nuevo anuncio</b>",
        f"<b>{titulo}</b>",
        f"📅 {año} · 📍 {km_str} km",
        "",
        f"🇩🇪 Alemania: <b>{_eur(anuncio.get('precio', 0))}</b>",
        f"🇪🇸 Mercado ES: <b>{_eur(valoracion.get('mediana', 0))}</b>",
    ]

    iedmt_nota = " (estimado)" if cuenta.get("co2_estimado") else ""
    lineas.append(f"📦 Importación: ~{_eur(cuenta['importacion'])} · IEDMT {cuenta['tipo_iedmt_pct']}%{iedmt_nota}")

    emoji_m = "💰" if margen >= 0 else "🔻"
    lineas.append(f"{emoji_m} <b>Margen neto: {signo}{_eur(margen)} ({cuenta['margen_pct']:+.0f}%)</b>")
    lineas.append(f"{emoji_conf} Confianza {nivel_conf} · {valoracion.get('n_comparables', 0)} comparables")

    if valoracion.get("n_comparables", 0) < 4:
        lineas.append("⚠️ <i>Pocos comparables. Margen poco fiable.</i>")

    # Avisos fiscales
    if es_nuevo_fiscal(km=km, año=anuncio.get("año", 0)):
        lineas.append("⚠️ <i>Nuevo fiscal: sumaría IVA español 21%.</i>")
    if anuncio.get("es_netto"):
        lineas.append("ℹ️ <i>Precio Netto (MwSt. ausweisbar). Con NIF-IVA el margen sube.</i>")

    # Coche antiguo: la tabla de Hacienda suele valorar por encima del mercado
    # → el IEDMT real puede superar la estimación (margen algo menor del mostrado).
    try:
        anio_int = int(anuncio.get("año", 0) or 0)
    except (ValueError, TypeError):
        anio_int = 0
    if anio_int and (datetime.now().year - anio_int) >= SNIPER_AVISO_IEDMT_ANOS:
        lineas.append("⚠️ <i>Coche antiguo: Hacienda puede valorar más alto. Verifica el IEDMT oficial.</i>")

    if cuenta.get("co2_estimado"):
        lineas.append("<i>CO₂ no publicado → IEDMT estimado.</i>")

    return "\n".join(lineas)
