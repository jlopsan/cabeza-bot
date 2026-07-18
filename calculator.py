# calculator.py - Lógica fiscal + cálculo de beneficio (modo manual y modo auto)
#
# MODO MANUAL:  el usuario indica su precio objetivo de venta en España.
#               Beneficio = precio_objetivo_usuario - landing_price
#
# MODO AUTO:    el bot busca el precio medio de mercado en Wallapop.
#               Beneficio = precio_medio_es (Wallapop) - landing_price
#
# Ambos modos usan la misma función calcular_beneficio().
# La tarjeta se genera con formato_tarjeta(), que detecta el modo automáticamente.
#
from datetime import datetime

from config import COSTE_TRANSPORTE, COSTE_GESTORIA_ITV, IEDMT_TRAMOS, MIN_BENEFICIO
from config import (
    SNIPER_COSTES_FIJOS, SNIPER_COSTE_TRANSPORTE, SNIPER_COSTE_COC_GESTION,
    SNIPER_COSTE_HOMOLOG_ITV, SNIPER_COSTE_TASAS_DGT,
    CO2_TIPICO_POR_COMBUSTIBLE, CO2_TIPICO_DEFAULT,
    NUEVO_FISCAL_KM_MAX, NUEVO_FISCAL_MESES_MAX,
)


# ────────────────────────────────────────────────────────────────────────────
# FISCAL
# ────────────────────────────────────────────────────────────────────────────

def calcular_tipo_iedmt(co2: float) -> float:
    """Devuelve el tipo impositivo IEDMT según CO₂ (g/km)."""
    co2 = float(co2 or 0)
    for limite, tipo in IEDMT_TRAMOS:
        if co2 <= limite:
            return tipo
    return IEDMT_TRAMOS[-1][1]


# ────────────────────────────────────────────────────────────────────────────
# SNIPER — cuenta de importación v2 (base IEDMT = valor de mercado ES)
# ────────────────────────────────────────────────────────────────────────────
# Aislada del cálculo legacy: base imponible = mediana de comparables ES,
# NO el precio de compra alemán. IEDMT siempre etiquetado "estimado".

def _normalizar_comb(combustible: str) -> str:
    """Mapea variantes de combustible a las claves de CO2_TIPICO_POR_COMBUSTIBLE."""
    c = (combustible or "").strip().lower()
    if not c:
        return ""
    if "phev" in c or "enchuf" in c:
        return "phev"
    if "híbr" in c or "hibr" in c or "hybrid" in c:
        return "hibrido"
    if "dies" in c or "gasoil" in c or c in ("d", "tdi", "cdi"):
        return "diesel"
    if "eléctr" in c or "electr" in c or c in ("e", "ev", "bev"):
        return "electrico"
    if "glp" in c or "lpg" in c:
        return "glp"
    if "gnc" in c or "cng" in c:
        return "gnc"
    if "gasolina" in c or "benz" in c or "petrol" in c or "nafta" in c or c in ("b", "g"):
        return "gasolina"
    return ""


def estimar_co2_deterministico(combustible: str, año: int) -> float:
    """
    Estima CO₂ (g/km) SIN IA cuando el anuncio no lo trae. Base por combustible
    ajustada levemente por antigüedad (coches nuevos emiten algo menos).
    Conservador: tira a alza para no infra-estimar el IEDMT.
    """
    comb = _normalizar_comb(combustible)
    base = CO2_TIPICO_POR_COMBUSTIBLE.get(comb, CO2_TIPICO_DEFAULT)
    try:
        ajuste = max(-15, min(15, 2015 - int(año))) if año else 0
    except (ValueError, TypeError):
        ajuste = 0
    return float(max(0, base + ajuste))


def calcular_cuenta_importacion(precio_de: float, mediana_es: float, co2: float = 0,
                                combustible: str = "", año: int = 0) -> dict:
    """
    Cuenta de importación del sniper. La base del IEDMT es el VALOR DE MERCADO ES
    (mediana de comparables), NO el precio de compra alemán.
    co2<=0 → estimación determinista (sin IA); marca co2_estimado=True.
    """
    co2_estimado = False
    co2_val = float(co2 or 0)
    if co2_val <= 0:
        co2_val = estimar_co2_deterministico(combustible, año)
        co2_estimado = True

    tipo  = calcular_tipo_iedmt(co2_val)
    iedmt = round(mediana_es * tipo, 2)
    importacion = round(SNIPER_COSTES_FIJOS + iedmt, 2)

    return {
        "precio_de":      precio_de,
        "mediana_es":     mediana_es,
        "co2":            co2_val,
        "co2_estimado":   co2_estimado,
        "tipo_iedmt_pct": round(tipo * 100, 2),
        "iedmt":          iedmt,
        "costes_fijos":   SNIPER_COSTES_FIJOS,
        "importacion":    importacion,
        "desglose": {
            "transporte":       SNIPER_COSTE_TRANSPORTE,
            "coc_gestion":      SNIPER_COSTE_COC_GESTION,
            "homologacion_itv": SNIPER_COSTE_HOMOLOG_ITV,
            "tasas_dgt":        SNIPER_COSTE_TASAS_DGT,
            "iedmt":            iedmt,
        },
    }


def calcular_margen_sniper(precio_de: float, mediana_es: float, co2: float = 0,
                           combustible: str = "", año: int = 0) -> dict:
    """
    Margen neto de importación: mediana_es − precio_de − importación.
    Devuelve la cuenta completa + margen_eur, margen_pct (sobre inversión) e inversión.
    """
    cuenta = calcular_cuenta_importacion(precio_de, mediana_es, co2, combustible, año)
    inversion  = precio_de + cuenta["importacion"]
    margen_eur = round(mediana_es - inversion, 2)
    margen_pct = round((margen_eur / inversion) * 100, 1) if inversion > 0 else 0.0
    cuenta.update({
        "margen_eur": margen_eur,
        "margen_pct": margen_pct,
        "inversion":  round(inversion, 2),
    })
    return cuenta


def es_nuevo_fiscal(km: float = 0, año: int = 0, antiguedad_meses: int | None = None) -> bool:
    """
    Nuevo fiscal (IVA español aplicable) si <NUEVO_FISCAL_KM_MAX km O <NUEVO_FISCAL_MESES_MAX meses.
    Sin dato de meses, usa el año de matriculación como señal (matriculado este año natural).
    """
    try:
        if km and int(km) < NUEVO_FISCAL_KM_MAX:
            return True
    except (ValueError, TypeError):
        pass
    if antiguedad_meses is not None:
        return antiguedad_meses < NUEVO_FISCAL_MESES_MAX
    try:
        return bool(año) and int(año) >= datetime.now().year
    except (ValueError, TypeError):
        return False


def calcular_landing_price(precio_de: float, co2: float) -> dict:
    """
    Coste total de poner el coche en España listo para vender.

    Fórmula MVP:
        landing = precio_DE + IEDMT(co2) * precio_DE + transporte + gestoría/ITV

    Returns dict con el desglose completo.
    """
    tipo        = calcular_tipo_iedmt(co2)
    iedmt       = round(precio_de * tipo, 2)
    gastos_fijos = COSTE_TRANSPORTE + COSTE_GESTORIA_ITV
    landing     = round(precio_de + iedmt + gastos_fijos, 2)

    return {
        "precio_de":      precio_de,
        "co2":            co2,
        "tipo_iedmt_pct": round(tipo * 100, 2),
        "importe_iedmt":  iedmt,
        "gastos_fijos":   gastos_fijos,
        "landing_price":  landing,
    }


# ────────────────────────────────────────────────────────────────────────────
# BENEFICIO  (soporta ambos modos)
# ────────────────────────────────────────────────────────────────────────────

def calcular_beneficio(landing_price: float, precio_venta_es: float) -> dict:
    """
    Calcula el beneficio bruto estimado.

    Args:
        landing_price:   Coste total del coche importado (€).
        precio_venta_es: Precio al que se vende en España (€).
                         Puede ser el precio objetivo del usuario
                         o el precio medio obtenido de Wallapop.

    Returns:
        {
          "beneficio":     float,   # positivo = ganancia
          "beneficio_pct": float,   # % sobre landing
          "es_oportunidad": bool,   # beneficio >= MIN_BENEFICIO
        }
    """
    beneficio     = round(precio_venta_es - landing_price, 2)
    beneficio_pct = round((beneficio / landing_price) * 100, 1) if landing_price else 0.0
    return {
        "beneficio":      beneficio,
        "beneficio_pct":  beneficio_pct,
        "es_oportunidad": beneficio >= MIN_BENEFICIO,
    }


# ─── Alias retrocompatible ───────────────────────────────────────────────────
def calcular_margen(landing_price: float, precio_objetivo_es: float) -> float:
    """Alias de calcular_beneficio() que devuelve solo el valor numérico."""
    return calcular_beneficio(landing_price, precio_objetivo_es)["beneficio"]


# ────────────────────────────────────────────────────────────────────────────
# SNIPER SCORE  (puntuación compuesta de oportunidad)
# ────────────────────────────────────────────────────────────────────────────

def calcular_sniper_score(coche: dict, precio_venta_es: float | None = None) -> dict:
    """
    Calcula un Sniper Score 0-100 que pondera:
      - Beneficio neto (40%)
      - Liquidez del modelo en ES (20%)  — nº de anuncios encontrados
      - Riesgo IA (20%)                  — veredicto del análisis
      - Frescura del anuncio (20%)       — penaliza si no es reciente

    Returns:
        {
          "sniper_score":     int (0-100),
          "score_beneficio":  int,
          "score_liquidez":   int,
          "score_riesgo":     int,
          "score_frescura":   int,
          "nivel":            str ("S", "A", "B", "C", "D"),
          "emoji":            str,
        }
    """
    # ── Beneficio (0-100, peso 40%) ─────────────────────────────────────────
    calc = calcular_landing_price(coche["precio"], coche.get("co2", 0))
    precio_es = precio_venta_es if precio_venta_es is not None else coche.get("precio_medio_es", 0)
    beneficio = (precio_es or 0) - calc["landing_price"]

    if beneficio >= 8_000:
        score_benef = 100
    elif beneficio >= 5_000:
        score_benef = 80
    elif beneficio >= 3_000:
        score_benef = 60
    elif beneficio >= 1_000:
        score_benef = 35
    elif beneficio >= 0:
        score_benef = 15
    else:
        score_benef = 0

    # ── Liquidez (0-100, peso 20%) ──────────────────────────────────────────
    n_muestras = coche.get("n_muestras_es", 0)
    if n_muestras >= 10:
        score_liq = 100
    elif n_muestras >= 5:
        score_liq = 80
    elif n_muestras >= 3:
        score_liq = 50
    elif n_muestras >= 1:
        score_liq = 25
    else:
        score_liq = 10  # sin datos = incertidumbre, no 0

    # ── Riesgo IA (0-100, peso 20%) ─────────────────────────────────────────
    analisis = coche.get("analisis_ia", {})
    veredicto = analisis.get("veredicto", "OK")
    confianza = analisis.get("confianza", 70)
    if veredicto == "OK":
        score_riesgo = min(100, confianza + 10)
    elif veredicto == "SOSPECHOSO":
        score_riesgo = max(10, 50 - (100 - confianza))
    else:  # DESCARTADO
        score_riesgo = 0

    # ── Frescura (0-100, peso 20%) ──────────────────────────────────────────
    # Sin fecha de publicación real, usamos heurística:
    # - Coches con descripción larga = vendedor activo = más fresco
    # - Coches con foto = anuncio cuidado = probablemente reciente
    desc_len = len(coche.get("descripcion", ""))
    tiene_foto = bool(coche.get("foto"))
    score_frescura = 50  # base
    if desc_len > 200:
        score_frescura += 25
    elif desc_len > 50:
        score_frescura += 10
    if tiene_foto:
        score_frescura += 25
    score_frescura = min(100, score_frescura)

    # ── Score final ponderado ───────────────────────────────────────────────
    total = round(
        score_benef   * 0.40 +
        score_liq     * 0.20 +
        score_riesgo  * 0.20 +
        score_frescura * 0.20
    )
    total = max(0, min(100, total))

    # ── Nivel y emoji ───────────────────────────────────────────────────────
    if total >= 85:
        nivel, emoji = "S", "🔥"
    elif total >= 70:
        nivel, emoji = "A", "🟢"
    elif total >= 50:
        nivel, emoji = "B", "🟡"
    elif total >= 30:
        nivel, emoji = "C", "🟠"
    else:
        nivel, emoji = "D", "🔴"

    return {
        "sniper_score":     total,
        "score_beneficio":  score_benef,
        "score_liquidez":   score_liq,
        "score_riesgo":     score_riesgo,
        "score_frescura":   score_frescura,
        "nivel":            nivel,
        "emoji":            emoji,
    }


def formato_sniper_score(score: dict) -> str:
    """Formato HTML del Sniper Score para Telegram."""
    return (
        f"{score['emoji']} <b>Sniper Score: {score['sniper_score']}/100</b> "
        f"(Nivel {score['nivel']})\n"
        f"  💰 Beneficio: {score['score_beneficio']}  ·  "
        f"📊 Liquidez: {score['score_liquidez']}  ·  "
        f"🛡️ Riesgo: {score['score_riesgo']}  ·  "
        f"⏱️ Frescura: {score['score_frescura']}"
    )


# ────────────────────────────────────────────────────────────────────────────
# CALCULADORA INVERSA  (¿cuánto puedo pagar máximo en DE?)
# ────────────────────────────────────────────────────────────────────────────

def calcular_precio_maximo_de(
    precio_venta_es: float,
    beneficio_minimo: float,
    co2: float = 0.0,
) -> dict:
    """
    Calcula el precio máximo de compra en Alemania para obtener
    un beneficio mínimo deseado.

    Fórmula inversa:
        precio_max_de = (precio_venta_es - beneficio_minimo - gastos_fijos) / (1 + tipo_iedmt)

    Returns:
        {
          "precio_max_de":     float,
          "precio_venta_es":   float,
          "beneficio_minimo":  float,
          "co2":               float,
          "tipo_iedmt_pct":    float,
          "importe_iedmt":     float,
          "gastos_fijos":      float,
          "landing_price":     float,
        }
    """
    co2 = float(co2 or 0)
    tipo = calcular_tipo_iedmt(co2)
    gastos_fijos = COSTE_TRANSPORTE + COSTE_GESTORIA_ITV

    # Inversión de la fórmula:
    # precio_venta = precio_de * (1 + tipo) + gastos_fijos + beneficio
    # precio_de = (precio_venta - gastos_fijos - beneficio) / (1 + tipo)
    neto_disponible = precio_venta_es - gastos_fijos - beneficio_minimo
    precio_max_de = round(neto_disponible / (1 + tipo), 2) if (1 + tipo) > 0 else 0.0
    precio_max_de = max(0.0, precio_max_de)

    iedmt = round(precio_max_de * tipo, 2)
    landing = round(precio_max_de + iedmt + gastos_fijos, 2)

    return {
        "precio_max_de":    precio_max_de,
        "precio_venta_es":  precio_venta_es,
        "beneficio_minimo": beneficio_minimo,
        "co2":              co2,
        "tipo_iedmt_pct":   round(tipo * 100, 2),
        "importe_iedmt":    iedmt,
        "gastos_fijos":     gastos_fijos,
        "landing_price":    landing,
    }


def formato_calculadora_inversa(resultado: dict) -> str:
    """Formato HTML de la calculadora inversa para Telegram."""
    return (
        f"🎯 <b>CALCULADORA INVERSA</b>\n"
        f"{'─' * 32}\n"
        f"\n"
        f"🏷️ Precio venta ES: <b>{resultado['precio_venta_es']:,.0f} €</b>\n"
        f"💰 Beneficio mínimo: <b>{resultado['beneficio_minimo']:,.0f} €</b>\n"
        f"💨 CO₂: {resultado['co2']:.0f} g/km → IEDMT {resultado['tipo_iedmt_pct']}%\n"
        f"\n"
        f"{'─' * 32}\n"
        f"🔽 IEDMT estimado: {resultado['importe_iedmt']:,.0f} €\n"
        f"🚛 Gastos fijos: {resultado['gastos_fijos']:,.0f} €\n"
        f"📦 Landing price: {resultado['landing_price']:,.0f} €\n"
        f"\n"
        f"{'═' * 32}\n"
        f"💶 <b>PRECIO MÁXIMO EN ALEMANIA:</b>\n"
        f"<b>     {resultado['precio_max_de']:,.0f} €</b>\n"
        f"{'═' * 32}\n"
        f"\n"
        f"👉 Busca coches por debajo de este precio para\n"
        f"    garantizar tu margen de {resultado['beneficio_minimo']:,.0f}€"
    )


# ────────────────────────────────────────────────────────────────────────────
# TARJETA FORMATEADA PARA TELEGRAM
# ────────────────────────────────────────────────────────────────────────────

def formato_tarjeta(
    coche: dict,
    precio_objetivo_es: float | None = None,
) -> str:
    """
    Genera el texto HTML de una tarjeta de oportunidad para Telegram.

    Modo manual:  pasar precio_objetivo_es (float).
    Modo auto:    dejar precio_objetivo_es=None → usa coche["precio_medio_es"].

    Si ninguno de los dos está disponible, muestra tarjeta sin datos de beneficio.
    """
    calculo = calcular_landing_price(coche["precio"], coche.get("co2", 0))

    # ── Resolver precio de venta ─────────────────────────────────────────────
    if precio_objetivo_es is not None:
        # Modo MANUAL: el usuario fijó el precio
        precio_es    = precio_objetivo_es
        fuente_precio = f"Precio objetivo (manual)"
        muestras_txt  = ""
    elif coche.get("precio_medio_es"):
        # Modo AUTO: precio medio de Wallapop
        precio_es    = coche["precio_medio_es"]
        n            = coche.get("n_muestras_es", 0)
        fuente_precio = f"Precio medio Wallapop"
        muestras_txt  = f" (muestra: {n} anuncios)"
    else:
        # Sin datos de precio ES: mostrar tarjeta incompleta
        km_val = coche.get('km', 0)
        km_str = f"{km_val:,}" if isinstance(km_val, (int, float)) and km_val > 0 else "N/D"
        co2_val = coche.get('co2', 0)
        co2_str = f"{co2_val:.0f}" if isinstance(co2_val, (int, float)) and co2_val > 0 else "N/D"
        return (
            f"🚗 <b>{coche['titulo']}</b>\n"
            f"📅 {coche.get('año','N/D')} · {km_str} km · "
            f"💨 {co2_str} g/km CO₂\n\n"
            f"💶 Precio DE: <b>{coche['precio']:,.0f} €</b>\n"
            f"📦 Landing ES: <b>{calculo['landing_price']:,.0f} €</b>\n"
            f"⚠️ No se pudo obtener precio de mercado ES\n"
            f"🔗 <a href='{coche.get('link','#')}'>Ver anuncio</a>"
        )

    # ── Calcular beneficio ───────────────────────────────────────────────────
    resultado   = calcular_beneficio(calculo["landing_price"], precio_es)
    beneficio   = resultado["beneficio"]
    benef_pct   = resultado["beneficio_pct"]

    if beneficio >= MIN_BENEFICIO:
        emoji = "🟢"
    elif beneficio >= 0:
        emoji = "🟡"
    else:
        emoji = "🔴"

    # ── Precios usados Wallapop (solo modo auto) ─────────────────────────────
    precios_debug = ""
    if precio_objetivo_es is None and coche.get("precios_usados_es"):
        precios_fmt  = " / ".join(f"{p:,.0f}€" for p in coche["precios_usados_es"])
        precios_debug = f"\n🔎 Muestras Wallapop: <i>{precios_fmt}</i>"

    km_val = coche.get('km', 0)
    km_str = f"{km_val:,}" if isinstance(km_val, (int, float)) and km_val > 0 else "N/D"
    co2_val = coche.get('co2', 0)
    co2_str = f"{co2_val:.0f}" if isinstance(co2_val, (int, float)) and co2_val > 0 else "N/D"

    return (
        f"🚗 <b>{coche['titulo']}</b>\n"
        f"📅 {coche.get('año','N/D')} · 📍 {km_str} km · "
        f"💨 {co2_str} g/km CO₂\n"
        f"\n"
        f"💶 <b>Precio DE:</b> {coche['precio']:,.0f} €\n"
        f"🔖 IEDMT ({calculo['tipo_iedmt_pct']}%): {calculo['importe_iedmt']:,.0f} €\n"
        f"🚛 Gastos fijos: {calculo['gastos_fijos']:,.0f} €\n"
        f"📦 <b>Landing ES:</b> {calculo['landing_price']:,.0f} €\n"
        f"\n"
        f"🏷️ {fuente_precio}: <b>{precio_es:,.0f} €</b>{muestras_txt}"
        f"{precios_debug}\n"
        f"\n"
        f"{emoji} <b>Beneficio estimado: {beneficio:+,.0f} € ({benef_pct:+.1f}%)</b>\n"
        f"🔗 <a href='{coche.get('link','#')}'>Ver anuncio</a>"
    )