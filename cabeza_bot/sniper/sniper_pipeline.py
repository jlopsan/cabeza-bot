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
import cabeza_bot.analisis.motor as motor
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


def _comparables_fiables(comparables: list, año: int) -> list:
    """
    Filtra los comparables para una valoración creíble y devuelve los objetos
    (no solo el precio) — hace falta el título para poder afinar por motor
    después sin re-scrapear:
      1. Mismo año ±SNIPER_VALORACION_ANOS_TOL (evita que un 2025 herede precios
         de un 320d de 2012 y saque una mediana falsa de 8.800€).
      2. Descarta outliers baratos/caros respecto a la mediana provisional
         (anti-scam + evita gama alta que infla).
    Los comparables sin año se excluyen (para valorar, mejor estricto).
    """
    año = int(año or 0)
    filtrados = []
    for a in comparables:
        p = float(getattr(a, "precio", 0) or 0)
        ay = int(getattr(a, "año", 0) or 0)
        if p <= 0:
            continue
        if año and (not ay or abs(ay - año) > SNIPER_VALORACION_ANOS_TOL):
            continue
        filtrados.append(a)
    filtrados.sort(key=lambda a: a.precio)
    if len(filtrados) >= 4:
        med0 = statistics.median(a.precio for a in filtrados)
        filtrados = [a for a in filtrados if med0 * ANTI_SCAM_FACTOR <= a.precio <= med0 * 2.0]
    return filtrados


def _precios_fiables(comparables: list, año: int) -> list[float]:
    return [float(a.precio) for a in _comparables_fiables(comparables, año)]


async def refrescar_valoracion(marca: str, modelo: str, año: int, km: int) -> dict | None:
    """
    Scrapea comparables ES, filtra por año + outliers, calcula mediana, la
    persiste (con título de cada comparable, para afinar por motor después)
    y la devuelve. None si NO hay comparables fiables suficientes
    (< SNIPER_MIN_COMPARABLES) → el producto muestra "sin valoración fiable"
    en vez de inventar un número. Reutiliza el pipeline de /analizar.
    """
    try:
        comparables = await buscar_comparables_todas(marca, modelo, año, km, n=40)
    except Exception as e:
        logger.warning(f"[SNIPER] comparables ES fallaron {marca} {modelo}: {e}")
        return None

    fiables = _comparables_fiables(comparables, año)
    if len(fiables) < SNIPER_MIN_COMPARABLES:
        logger.info(f"[SNIPER] {marca} {modelo} {año}: solo {len(fiables)} comparables fiables → sin valoración")
        return None

    precios = [float(a.precio) for a in fiables]
    mediana = round(statistics.median(precios), 0)
    comparables_slim = [{"precio": a.precio, "titulo": a.titulo or ""} for a in fiables]
    db.upsert_valoracion(marca, modelo, año, km_banda(km), mediana, len(precios), precios, comparables_slim)

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


class _PseudoAnuncio:
    """Envoltorio mínimo para reusar motor.filtrar_por_motor sobre comparables
    guardados como {precio, titulo} (sin volver a scrapear)."""
    __slots__ = ("precio", "titulo", "motor", "modelo")

    def __init__(self, precio: float, titulo: str):
        self.precio = precio
        self.titulo = titulo
        self.motor = ""
        self.modelo = ""


def afinar_por_motor(anuncio: dict, valoracion: dict) -> dict:
    """
    Refina la mediana ES usando el motor del candidato (CV/combustible, del
    propio título del anuncio DE) contra los comparables ya guardados en la
    valoración. CERO scraping y CERO IA extra — mismo patrón que /tasar (evita
    el bug "GTI valorado como Golf base", aquí aplicado al sniper. Si no hay
    suficientes comparables del mismo motor, cae a la mediana amplia.
    Devuelve {mediana, n_comparables, criterio} — criterio='' si no se afinó.
    """
    mediana_base = float(valoracion.get("mediana", 0) or 0)
    n_base = int(valoracion.get("n_comparables", 0) or 0)
    sin_afinar = {"mediana": mediana_base, "n_comparables": n_base, "criterio": ""}

    try:
        items = json.loads(valoracion.get("comparables_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        items = []
    if not items:
        return sin_afinar

    titulo = anuncio.get("titulo", "") or ""
    cv_obj = motor.extraer_cv(titulo)
    comb_obj = motor.detectar_combustible(titulo)
    if not cv_obj and not comb_obj:
        return sin_afinar

    pseudo = [_PseudoAnuncio(it.get("precio", 0), it.get("titulo", ""))
             for it in items if it.get("precio", 0) > 0]
    filtrados, criterio, modo = motor.filtrar_por_motor(pseudo, cv_obj, comb_obj)
    if modo == "pool" or len(filtrados) < 3:
        return sin_afinar

    precios_afinados = sorted(p.precio for p in filtrados)
    return {
        "mediana": round(statistics.median(precios_afinados), 0),
        "n_comparables": len(precios_afinados),
        "criterio": criterio or "",
    }


def evaluar_candidato(anuncio: dict, valoracion: dict,
                      umbral_eur: int | None = None,
                      umbral_pct: float | None = None) -> dict:
    """
    Calcula la cuenta de importación Y el semáforo de riesgo del candidato.
    La mediana se afina por motor (CV/combustible) contra los comparables ya
    guardados en la valoración — sin coste extra. Devuelve {alerta, cuenta,
    riesgo, n_comparables, motor_afinado}. `alerta` y `riesgo` son señales
    independientes — el caller decide si alertar ROJO o no.
    """
    umbral_eur = SNIPER_UMBRAL_EUR if umbral_eur is None else umbral_eur
    umbral_pct = SNIPER_UMBRAL_PCT if umbral_pct is None else umbral_pct

    afinado = afinar_por_motor(anuncio, valoracion)
    mediana = afinado["mediana"]
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
        "n_comparables": afinado["n_comparables"],
        "motor_afinado": afinado["criterio"],
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
                       "n_comparables": r["n_comparables"], "motor_afinado": r["motor_afinado"],
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
    mostrado (botón "Cuenta completa") y dataset de casos reales del sniper.
    """
    h = anuncio.get("_huella", "")
    desglose = None
    if cuenta:
        # Se guarda el desglose fijo + los datos que dan contexto al número
        # (¿CO₂ real o estimado?, tramo IEDMT, mediana ES usada, motor afinado)
        # para que "Cuenta completa" no tenga que recalcular nada.
        desglose = {
            **(cuenta.get("desglose") or {}),
            "co2_estimado":   cuenta.get("co2_estimado", False),
            "co2":            cuenta.get("co2", 0),
            "tipo_iedmt_pct": cuenta.get("tipo_iedmt_pct", 0),
            "mediana_es":     cuenta.get("mediana_es", 0),
            "inversion":      cuenta.get("inversion", 0),
        }
    db.registrar_visto(
        mision_id, str(anuncio.get("id", "")), h, tipo=tipo,
        precio=anuncio.get("precio", 0),
        margen_eur=(cuenta or {}).get("margen_eur", 0),
        margen_pct=(cuenta or {}).get("margen_pct", 0),
        url=anuncio.get("link", ""),
        desglose=desglose,
        riesgo=riesgo.to_dict() if riesgo is not None else None,
    )


# ─── RENDER DE LA TARJETA DE ALERTA ──────────────────────────────────────────

def boton_ver_anuncio(url: str, anuncio_id: str = "", mision_id: int | None = None) -> dict:
    """
    reply_markup para la API HTTP de Telegram (worker) o InlineKeyboard (bot).
    Fila 2: hueco para el informe VIN (afiliación/upsell futuro). Fila 3:
    desglose completo de la cuenta (ya persistido, cero coste — lee de BD).
    """
    filas = [[{"text": "🔗 Ver anuncio", "url": url or "#"}]]
    if anuncio_id:
        filas.append([{"text": "🪪 Informe VIN (próximamente)", "callback_data": f"sniper_vin:{anuncio_id}"}])
    if anuncio_id and mision_id is not None:
        filas.append([{"text": "🧾 Cuenta completa", "callback_data": f"sniper_cuenta:{mision_id}:{anuncio_id}"}])
    return {"inline_keyboard": filas}


def render_cuenta_completa(registro: dict) -> str:
    """
    Desglose completo desde un registro de alertas_enviadas (cero coste extra
    — solo lee lo ya calculado en el momento de la alerta). Muestra cada
    partida de la importación y por qué el IEDMT es el que es.
    """
    try:
        d = json.loads(registro.get("desglose_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        d = {}
    if not d:
        return "🧾 No hay desglose guardado para este anuncio."

    iedmt_nota = " <i>(estimado)</i>" if d.get("co2_estimado") else ""
    co2_txt = f"{d.get('co2', 0):.0f} g/km" if d.get("co2") else "no publicado"

    lineas = [
        "🧾 <b>Cuenta completa de importación</b>",
        "",
        f"🇪🇸 Mercado ES usado: <b>{_eur(d.get('mediana_es', 0))}</b>",
        f"🇩🇪 Precio Alemania: <b>{_eur(registro.get('precio', 0))}</b>",
        "",
        f"🚛 Transporte: {_eur(d.get('transporte', 0))}",
        f"📋 COC + gestión: {_eur(d.get('coc_gestion', 0))}",
        f"🔧 Homologación + ITV: {_eur(d.get('homologacion_itv', 0))}",
        f"🏛️ Tasas DGT: {_eur(d.get('tasas_dgt', 0))}",
        f"🔖 IEDMT ({d.get('tipo_iedmt_pct', 0)}%, CO₂ {co2_txt}): {_eur(d.get('iedmt', 0))}{iedmt_nota}",
        "─" * 28,
        f"💶 Inversión total: <b>{_eur(d.get('inversion', 0))}</b>",
        f"💰 Margen neto: <b>{_eur(registro.get('margen_eur', 0))} ({registro.get('margen_pct', 0):+.0f}%)</b>",
    ]
    return "\n".join(lineas)


def _eur(v: float) -> str:
    try:
        return f"{float(v):,.0f}€".replace(",", ".")
    except (ValueError, TypeError):
        return "N/D"


def render_tarjeta_alerta(anuncio: dict, valoracion: dict, cuenta: dict,
                          mision_id: int | None = None, riesgo=None,
                          n_comparables: int | None = None, motor_afinado: str = "") -> str:
    """
    Tarjeta de alerta con el formato del vídeo. html.escape en todo campo
    scrapeado. No promete datos que no tiene (sin CO₂ → IEDMT estimado, total).
    El semáforo (si se pasa `riesgo`) prioriza dónde gastar el informe VIN o
    la inspección — nunca afirma que un coche "está bien".

    `n_comparables`/`motor_afinado` (de evaluar_candidato): si la valoración se
    afinó por motor (CV/combustible), el nº mostrado es el del grupo afinado,
    no el de la banda amplia — coherente con la mediana que usa `cuenta`
    (mediana_es), no `valoracion.mediana` (que puede ser la sin afinar).
    """
    titulo = html.escape(anuncio.get("titulo", "") or f"{anuncio.get('año','')}")
    año = anuncio.get("año", "") or "N/D"
    km  = anuncio.get("km", 0) or 0
    km_str = f"{int(km):,}".replace(",", ".") if km else "N/D"
    fuente = anuncio.get("fuente", "") or "AutoScout24"

    n_comp = valoracion.get("n_comparables", 0) if n_comparables is None else n_comparables
    mediana_mostrada = cuenta.get("mediana_es", valoracion.get("mediana", 0))
    emoji_conf, nivel_conf = confianza(n_comp)
    margen = cuenta["margen_eur"]
    signo = "" if margen < 0 else ""

    lineas = [
        "🎯 <b>SNIPER — nuevo anuncio</b>",
        f"<b>{titulo}</b>",
        f"📅 {año} · 📍 {km_str} km · <i>{html.escape(fuente)}</i>",
        "",
        f"🇩🇪 Alemania: <b>{_eur(anuncio.get('precio', 0))}</b>",
        f"🇪🇸 Mercado ES: <b>{_eur(mediana_mostrada)}</b>",
    ]

    iedmt_nota = " (estimado)" if cuenta.get("co2_estimado") else ""
    lineas.append(f"📦 Importación: ~{_eur(cuenta['importacion'])} · IEDMT {cuenta['tipo_iedmt_pct']}%{iedmt_nota}")

    emoji_m = "💰" if margen >= 0 else "🔻"
    lineas.append(f"{emoji_m} <b>Margen neto: {signo}{_eur(margen)} ({cuenta['margen_pct']:+.0f}%)</b>")
    comp_txt = f"{emoji_conf} Confianza {nivel_conf} · {n_comp} comparables"
    if motor_afinado:
        comp_txt += f" del mismo motor ({html.escape(motor_afinado)})"
    lineas.append(comp_txt)

    if n_comp < 4:
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
