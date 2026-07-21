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

from cabeza_bot.config import (
    VALORACION_TTL_H, VALORACION_KM_BANDA,
    SNIPER_UMBRAL_EUR, SNIPER_UMBRAL_PCT,
    SNIPER_AVISO_IEDMT_ANOS,
    SNIPER_VALORACION_ANOS_TOL, SNIPER_MIN_COMPARABLES, ANTI_SCAM_FACTOR,
    ENABLE_SNIPER_MOBILE_DE, SNIPER_CB_FALLOS, SNIPER_CB_PAUSA_MIN,
    SNIPER_KM_ANOS_TOL, SNIPER_KM_MIN_MUESTRA, SNIPER_KM_PCTL_AMARILLO, SNIPER_KM_PCTL_ROJO,
    SNIPER_PRECIO_DE_MIN_MUESTRA, SNIPER_PRECIO_DE_ANOMALO_PCT,
    SNIPER_RIESGO_FOTOS_MIN, SNIPER_RIESGO_PROPIETARIOS_MAX, SNIPER_RIESGO_BLANDAS_AMARILLO,
)
from cabeza_bot.fiscal.calculator import calcular_margen_sniper, es_nuevo_fiscal
from cabeza_bot.scraping.scraper import ScraperAutoScout24, ScraperMobileDe, buscar_comparables_todas
from cabeza_bot.sniper.riesgo import evaluar_riesgo, NIVEL_ROJO
import cabeza_bot.data.database as db

logger = logging.getLogger(__name__)

FUENTES_DE = ("autoscout24", "mobile.de")


# ─── CLAVE DE SCRAPEO (agrupación de misiones) ───────────────────────────────

def filtros_mision(mision: dict) -> dict:
    try:
        return json.loads(mision.get("filtros", "{}") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def clave_scrapeo(mision: dict) -> str:
    """URL de detección normalizada (AS24). Misiones con la misma clave comparten scrapeo."""
    return ScraperAutoScout24().url_deteccion_normalizada(
        mision.get("marca", ""), mision.get("modelo", ""), filtros_mision(mision)
    )


def _dedupe_dicts(anuncios: list[dict]) -> list[dict]:
    """Dedup cross-fuente por (precio±200€, km±2000, año±1) — mismo coche en AS24 y mobile.de."""
    vistos: list[tuple[float, int, int]] = []
    unicos: list[dict] = []
    for a in anuncios:
        key = (a.get("precio", 0), a.get("km", 0), a.get("año", 0))
        if any(abs(v[0] - key[0]) < 200 and abs(v[1] - key[1]) < 2000 and abs(v[2] - key[2]) <= 1
               for v in vistos):
            continue
        vistos.append(key)
        unicos.append(a)
    return unicos


async def detectar_multifuente(marca: str, modelo: str, filtros: dict) -> list[dict]:
    """
    Detección DE mergeada: AS24 (siempre) + mobile.de (si ENABLE_SNIPER_MOBILE_DE
    y no pausada por su circuit breaker). Cada fuente respeta su propio breaker
    (fallo→incr, ok/vacío→reset) para que un bloqueo de mobile.de NUNCA afecte
    a AS24. Usado tanto por el ciclo del worker como por el escaneo inmediato.
    """
    resultados: list[dict] = []

    if not db.fuente_pausada("autoscout24"):
        db.incr_scrape_hora("autoscout24")
        anuncios, señal = await ScraperAutoScout24().buscar_deteccion(marca, modelo, filtros)
        if señal == "fallo":
            db.incr_fallo_fuente("autoscout24", SNIPER_CB_FALLOS, SNIPER_CB_PAUSA_MIN)
        else:
            db.reset_fuente("autoscout24")
            resultados.extend(anuncios)

    if ENABLE_SNIPER_MOBILE_DE and not db.fuente_pausada("mobile.de"):
        db.incr_scrape_hora("mobile.de")
        try:
            anuncios_m, señal_m = await ScraperMobileDe().buscar_deteccion(marca, modelo, filtros)
        except Exception as e:
            logger.warning(f"[SNIPER] mobile.de detección lanzó excepción: {e}")
            anuncios_m, señal_m = [], "fallo"
        if señal_m == "fallo":
            db.incr_fallo_fuente("mobile.de", SNIPER_CB_FALLOS, SNIPER_CB_PAUSA_MIN)
        else:
            db.reset_fuente("mobile.de")
            resultados.extend(anuncios_m)

    return _dedupe_dicts(resultados)


# ─── VALORACIÓN DE MERCADO ES (cacheada) ─────────────────────────────────────

def km_banda(km: int) -> int:
    return int((km or 0) // VALORACION_KM_BANDA)


def valoracion_fresca(marca: str, modelo: str, año: int, km: int) -> dict | None:
    """Valoración cacheada si NO ha caducado; None si no existe o está vieja."""
    v = db.get_valoracion(marca, modelo, año, km_banda(km))
    if v and not db.valoracion_caducada(v.get("actualizado_at", ""), VALORACION_TTL_H):
        return v
    return None


def _precios_fiables(comparables: list, año: int) -> list[float]:
    """
    Filtra los comparables para una valoración creíble:
      1. Mismo año ±SNIPER_VALORACION_ANOS_TOL (evita que un 2025 herede precios
         de un 320d de 2012 y saque una mediana falsa de 8.800€).
      2. Descarta outliers baratos/caros respecto a la mediana provisional
         (anti-scam + evita gama alta que infla).
    Los comparables sin año se excluyen (para valorar, mejor estricto).
    """
    año = int(año or 0)
    precios = []
    for a in comparables:
        p = float(getattr(a, "precio", 0) or 0)
        ay = int(getattr(a, "año", 0) or 0)
        if p <= 0:
            continue
        if año and (not ay or abs(ay - año) > SNIPER_VALORACION_ANOS_TOL):
            continue
        precios.append(p)
    precios.sort()
    if len(precios) >= 4:
        med0 = statistics.median(precios)
        precios = [p for p in precios if med0 * ANTI_SCAM_FACTOR <= p <= med0 * 2.0]
    return precios


async def refrescar_valoracion(marca: str, modelo: str, año: int, km: int) -> dict | None:
    """
    Scrapea comparables ES, filtra por año + outliers, calcula mediana, la
    persiste y la devuelve. None si NO hay comparables fiables suficientes
    (< SNIPER_MIN_COMPARABLES) → el producto muestra "sin valoración fiable"
    en vez de inventar un número. Reutiliza el pipeline de /analizar.
    """
    try:
        comparables = await buscar_comparables_todas(marca, modelo, año, km, n=40)
    except Exception as e:
        logger.warning(f"[SNIPER] comparables ES fallaron {marca} {modelo}: {e}")
        return None

    precios = _precios_fiables(comparables, año)
    if len(precios) < SNIPER_MIN_COMPARABLES:
        logger.info(f"[SNIPER] {marca} {modelo} {año}: solo {len(precios)} comparables fiables → sin valoración")
        return None

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


def evaluar_riesgo_anuncio(anuncio: dict, marca: str, modelo: str, año: int):
    """
    Semáforo de riesgo de un anuncio: consulta el dataset propio (km y precios
    DE) y aplica las reglas de `riesgo.evaluar_riesgo`. Devuelve un `Riesgo`.
    Prioriza el propio historico_precios que el sniper construye en cada
    scrapeo — nadie más tiene este dataset (activo del producto).
    """
    km_ds = db.km_dataset(marca, modelo, año, SNIPER_KM_ANOS_TOL)
    precios_de_ds = db.precios_de_dataset(marca, modelo, año, SNIPER_KM_ANOS_TOL)
    return evaluar_riesgo(
        anuncio, km_ds, precios_de_ds,
        km_min_muestra=SNIPER_KM_MIN_MUESTRA,
        km_pctl_amarillo=SNIPER_KM_PCTL_AMARILLO,
        km_pctl_rojo=SNIPER_KM_PCTL_ROJO,
        precio_de_min_muestra=SNIPER_PRECIO_DE_MIN_MUESTRA,
        precio_de_anomalo_pct=SNIPER_PRECIO_DE_ANOMALO_PCT,
        fotos_min=SNIPER_RIESGO_FOTOS_MIN,
        propietarios_max=SNIPER_RIESGO_PROPIETARIOS_MAX,
        blandas_amarillo=SNIPER_RIESGO_BLANDAS_AMARILLO,
    )


def evaluar_candidato(anuncio: dict, valoracion: dict,
                      umbral_eur: int | None = None,
                      umbral_pct: float | None = None) -> dict:
    """
    Calcula la cuenta de importación Y el semáforo de riesgo del candidato.
    Devuelve {alerta, cuenta, riesgo, n_comparables}. `alerta` (margen) y el
    nivel de riesgo son señales independientes — el caller decide si alertar
    ROJO o no; aquí solo se calculan ambas.
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

    riesgo = evaluar_riesgo_anuncio(
        anuncio, valoracion.get("marca", ""), valoracion.get("modelo", ""),
        anuncio.get("año", 0),
    )

    return {
        "alerta": alerta,
        "cuenta": cuenta,
        "riesgo": riesgo,
        "n_comparables": int(valoracion.get("n_comparables", 0) or 0),
    }


async def _con_detalle(anuncio: dict) -> dict:
    """Completa el detalle si aún no lo tiene (mobile.de ya lo trae completo)."""
    if anuncio.get("_detalle_completo"):
        return anuncio
    return await ScraperAutoScout24().obtener_detalle_candidato(anuncio)


async def mejores_del_mercado(marca: str, modelo: str, filtros: dict,
                              umbral_eur: int, umbral_pct: float,
                              top_n: int = 3, max_refrescos: int = 3) -> list[dict]:
    """
    Escaneo INMEDIATO para '¿qué hay ahora?'. Detecta en AS24 + mobile.de
    (multifuente), valora cada candidato (caché primero, máx `max_refrescos`
    scrapeos ES para no eternizar), afina los top con el detalle real y calcula
    el semáforo de riesgo. Devuelve top_n ordenado por margen desc, SOLO con
    valoración fiable. Cada item: {anuncio, valoracion, cuenta, riesgo, margen_eur}.
    """
    try:
        anuncios = await detectar_multifuente(marca, modelo, filtros)
    except Exception as e:
        logger.warning(f"[SNIPER] escaneo inmediato falló: {e}")
        return []
    if not anuncios:
        return []

    refrescos = 0
    evaluados = []
    for a in anuncios:
        v = valoracion_fresca(marca, modelo, a.get("año", 0), a.get("km", 0))
        if v is None and refrescos < max_refrescos:
            v = await refrescar_valoracion(marca, modelo, a.get("año", 0), a.get("km", 0))
            refrescos += 1
        if not v:
            continue
        r = evaluar_candidato(a, v, umbral_eur, umbral_pct)
        evaluados.append((a, v, r["cuenta"]))

    evaluados.sort(key=lambda t: t[2]["margen_eur"], reverse=True)

    salida = []
    for a, v, _c in evaluados[:top_n]:
        a = await _con_detalle(a)
        r = evaluar_candidato(a, v, umbral_eur, umbral_pct)
        salida.append({"anuncio": a, "valoracion": v, "cuenta": r["cuenta"], "riesgo": r["riesgo"],
                       "margen_eur": r["cuenta"]["margen_eur"]})
    salida.sort(key=lambda x: x["margen_eur"], reverse=True)
    return salida


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
                 cuenta: dict | None = None, riesgo=None):
    """
    Registra el anuncio como visto (snapshot) o alertado (alerta). Persiste el
    desglose de costes y el nivel/banderas de riesgo — transparencia del número
    mostrado y dataset de casos reales del sniper.
    """
    h = anuncio.get("_huella", "")
    db.registrar_visto(
        mision_id, str(anuncio.get("id", "")), h, tipo=tipo,
        precio=anuncio.get("precio", 0),
        margen_eur=(cuenta or {}).get("margen_eur", 0),
        margen_pct=(cuenta or {}).get("margen_pct", 0),
        url=anuncio.get("link", ""),
        desglose=(cuenta or {}).get("desglose"),
        riesgo=riesgo.to_dict() if riesgo is not None else None,
    )


# ─── RENDER DE LA TARJETA DE ALERTA ──────────────────────────────────────────

def boton_ver_anuncio(url: str, anuncio_id: str = "") -> dict:
    """
    reply_markup para la API HTTP de Telegram (worker) o InlineKeyboard (bot).
    Segunda fila: hueco para el informe VIN (afiliación/upsell futuro — ver
    proposal). Hoy es un stub informativo, no cobra ni pide datos de pago.
    """
    filas = [[{"text": "🔗 Ver anuncio", "url": url or "#"}]]
    if anuncio_id:
        filas.append([{"text": "🪪 Informe VIN (próximamente)", "callback_data": f"sniper_vin:{anuncio_id}"}])
    return {"inline_keyboard": filas}


def _eur(v: float) -> str:
    try:
        return f"{float(v):,.0f}€".replace(",", ".")
    except (ValueError, TypeError):
        return "N/D"


def render_tarjeta_alerta(anuncio: dict, valoracion: dict, cuenta: dict,
                          mision_id: int | None = None, riesgo=None) -> str:
    """
    Tarjeta de alerta con el formato del vídeo. html.escape en todo campo
    scrapeado. No promete datos que no tiene (sin CO₂ → IEDMT estimado, total).
    El semáforo (si se pasa `riesgo`) prioriza dónde gastar el informe VIN o
    la inspección — nunca afirma que un coche "está bien".
    """
    titulo = html.escape(anuncio.get("titulo", "") or f"{anuncio.get('año','')}")
    año = anuncio.get("año", "") or "N/D"
    km  = anuncio.get("km", 0) or 0
    km_str = f"{int(km):,}".replace(",", ".") if km else "N/D"
    fuente = anuncio.get("fuente", "") or "AutoScout24"

    emoji_conf, nivel_conf = confianza(valoracion.get("n_comparables", 0))
    margen = cuenta["margen_eur"]
    signo = "" if margen < 0 else ""

    lineas = [
        "🎯 <b>SNIPER — nuevo anuncio</b>",
        f"<b>{titulo}</b>",
        f"📅 {año} · 📍 {km_str} km · <i>{html.escape(fuente)}</i>",
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

    if riesgo is not None:
        lineas.append("")
        lineas.append(f"{riesgo.emoji} <b>Riesgo: {riesgo.nivel}</b> — en qué gastarte el informe VIN")
        for b in riesgo.banderas:
            emoji_b = {"ROJO": "🔴", "AMARILLO": "🟡", "VERDE": "▫️"}.get(b.nivel, "▫️")
            lineas.append(f"{emoji_b} {html.escape(b.texto)}")
        if not riesgo.banderas:
            lineas.append("Sin señales de riesgo detectables en el anuncio.")

    return "\n".join(lineas)
