# main.py - Entry point del German Sniper Bot v3
#
# Nuevas features v3:
#   - Sniper Score en resultados
#   - /calcular — calculadora inversa
#   - Modo sniper (alertas cada 3 min) para misiones
#   - /eliminar — borrar misiones
#   - Control de acceso por tiers (free / pro / sniper)
#   - Restricción por ALLOWED_USER_IDS
#
import asyncio
import html
import logging
import re as _re
import statistics as _stats_mod
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes,
)

from config import TELEGRAM_TOKEN, TOP_RESULTS, MIN_BENEFICIO, ALLOWED_USER_IDS, ADMIN_USER_IDS
from config import IDEAL_TOP_N, IDEAL_KM_AÑO_MAX
from ai import (
    parsear_filtros_nl, parsear_modelo_nl, enriquecer_coches,
    texto_analisis, validar_precio_mercado, filtrar_por_extras,
    generar_veredicto_analizar, preguntas_y_checklist, formatear_qa,
    cache_get, cache_set,
    parsear_perfil_ideal, generar_veredicto_ideal,
)
from database import (
    init_db, crear_mision, eliminar_mision,
    obtener_misiones_usuario, pausar_mision, activar_mision,
    registrar_usuario, obtener_tier,
    guardar_historico_batch,
    get_o_crear_usuario, puede_analizar, registrar_analisis, minutos_hasta_reset,
    registrar_evento, resumen_stats,
)
from config import FREE_ANALISIS_MAX, FREE_VENTANA_HORAS
from scraper import (
    buscar_y_cruzar, buscar_coches_alemania,
    obtener_anuncio_por_url, buscar_comparables_todas,
)
from collections import Counter
from calculator import (
    formato_tarjeta,
    calcular_sniper_score, formato_sniper_score,
    calcular_precio_maximo_de, formato_calculadora_inversa,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── ESTADOS ─────────────────────────────────────────────────────────────────
ASK_MODELO, ASK_PRECIO_OBJETIVO, ASK_FILTROS, SHOW_RESULTS = range(4)
CALC_PRECIO, CALC_BENEFICIO, CALC_CO2 = range(10, 13)
IDEAL_COLLECT = 20

SKIP_KEYWORDS = {"auto", "no", "skip", "-", "automático", "automatico", "buscar"}

# ─── TIERS: límites por nivel ────────────────────────────────────────────────
TIER_LIMITS = {
    "free":   {"busquedas_dia": 3,  "misiones": 1,  "sniper": False},
    "pro":    {"busquedas_dia": 50, "misiones": 5,  "sniper": False},
    "sniper": {"busquedas_dia": -1, "misiones": 20, "sniper": True},
    "admin":  {"busquedas_dia": -1, "misiones": -1, "sniper": True},
}


# ════════════════════════════════════════════════════════════════════════════
# MIDDLEWARE: control de acceso
# ════════════════════════════════════════════════════════════════════════════

def _check_access(user_id: int, username: str = "") -> tuple[bool, str]:
    """
    Verifica si el usuario tiene acceso al bot.
    Returns (permitido, tier).
    """
    # Lista blanca: si está vacía, todos pasan
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return False, "blocked"
    # Registrar usuario si es nuevo
    registrar_usuario(user_id, username)
    tier = obtener_tier(user_id)
    return True, tier


def _tier_puede(tier: str, feature: str) -> bool:
    """Comprueba si un tier tiene acceso a una feature."""
    limits = TIER_LIMITS.get(tier, TIER_LIMITS["free"])
    if feature == "sniper":
        return limits["sniper"]
    return True


# ════════════════════════════════════════════════════════════════════════════
# /start
# ════════════════════════════════════════════════════════════════════════════

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    allowed, _tier = _check_access(user.id, user.username or "")
    if not allowed:
        await update.message.reply_text("⛔ No tienes acceso a este bot.")
        return

    get_o_crear_usuario(user.id, user.username or "", user.first_name or "")

    await update.message.reply_text(
        "Hola 👋\n\n"
        "Soy <b>Cabeza Bot</b>.\n\n"
        "Analizo anuncios de coches usados en España en tiempo real: "
        "precio vs mercado, red flags, etiqueta DGT, historial del modelo.\n\n"
        f"En fase beta. Tienes {FREE_ANALISIS_MAX} análisis gratuitos "
        f"cada {FREE_VENTANA_HORAS} horas.\n\n"
        "/analizar &lt;url&gt; — Analiza un anuncio de Wallapop o Coches.net\n"
        "/plan — Ver cuántos análisis te quedan\n\n"
        "<b>Actualizaciones en:</b>\n"
        "• YouTube: @juanloperaes\n"
        "• Instagram: @juanlopera.es\n"
        "• TikTok: @juanlopera.es",
        parse_mode="HTML",
    )


# ════════════════════════════════════════════════════════════════════════════
# /plan — ver tier y límites
# ════════════════════════════════════════════════════════════════════════════

async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    allowed, _tier = _check_access(user.id, user.username or "")
    if not allowed:
        await update.message.reply_text("⛔ No tienes acceso a este bot.")
        return

    get_o_crear_usuario(user.id, user.username or "", user.first_name or "")
    _puede, restantes = puede_analizar(user.id)
    usados = max(FREE_ANALISIS_MAX - restantes, 0) if restantes <= FREE_ANALISIS_MAX else 0

    mins = minutos_hasta_reset(user.id)
    if mins <= 0:
        cuando = "ahora (ventana nueva al próximo análisis)"
    else:
        h, m = divmod(mins, 60)
        cuando = f"{h}h {m}min" if h else f"{m} min"

    if user.id in ADMIN_USER_IDS:
        cuerpo = "🔓 Acceso ilimitado (admin).\n\n"
    else:
        cuerpo = (
            f"🔍 Análisis usados: <b>{usados}/{FREE_ANALISIS_MAX}</b>\n"
            f"⏳ Reset en: <b>{cuando}</b>\n\n"
        )

    await update.message.reply_text(
        "📋 <b>Tu uso</b>\n\n"
        f"{cuerpo}"
        "🚀 <b>Próximamente:</b>\n"
        "• Plan ilimitado por suscripción mensual\n"
        "• Más herramientas: /tasar, /ideal, alertas de chollos\n\n"
        "En fase beta. Actualizaciones en:\n"
        "• YouTube: @juanloperaes\n"
        "• Instagram: @juanlopera.es\n"
        "• TikTok: @juanlopera.es",
        parse_mode="HTML",
    )


# ════════════════════════════════════════════════════════════════════════════
# /buscar — flujo de búsqueda
# ════════════════════════════════════════════════════════════════════════════

async def buscar_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    allowed, tier = _check_access(user.id, user.username or "")
    if not allowed:
        await update.message.reply_text("⛔ No tienes acceso a este bot.")
        return ConversationHandler.END

    ctx.user_data.clear()
    ctx.user_data["tier"] = tier

    await update.message.reply_text(
        "🔍 <b>Nueva búsqueda</b>\n\n"
        "¿Qué coche buscas? Escribe <b>marca y modelo</b>.\n"
        "Ej: <code>BMW M3</code>  ·  <code>Audi RS3</code>  ·  <code>VW Golf GTI</code>",
        parse_mode="HTML",
    )
    return ASK_MODELO


async def recibir_modelo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query_raw = update.message.text.strip()
    if not query_raw:
        await update.message.reply_text(
            "⚠️ Escribe marca y modelo. Ej: <code>BMW M3</code>",
            parse_mode="HTML",
        )
        return ASK_MODELO
    ctx.user_data["query_raw"] = query_raw

    parsed = await parsear_modelo_nl(query_raw)
    partes = query_raw.split(maxsplit=1)
    ctx.user_data["marca"]  = parsed["marca"] or partes[0].lower()
    ctx.user_data["modelo"] = parsed["modelo"] or (
        partes[1].lower() if len(partes) > 1 else partes[0].lower()
    )
    logger.info(f"[BOT] Modelo parseado: marca={ctx.user_data['marca']} modelo={ctx.user_data['modelo']}")

    await update.message.reply_text(
        "💶 <b>¿A qué precio vendes este coche en España?</b>\n\n"
        "• Escribe el precio en €  →  Ej: <code>32000</code>\n"
        "• Escribe <code>auto</code>  →  Busco el precio medio en Wallapop + coches.net",
        parse_mode="HTML",
    )
    return ASK_PRECIO_OBJETIVO


async def recibir_precio_objetivo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip().lower()

    if texto in SKIP_KEYWORDS:
        ctx.user_data["precio_objetivo_es"] = None
        ctx.user_data["modo_precio"] = "auto"
    else:
        try:
            precio = float(texto.replace(".", "").replace(",", "."))
            ctx.user_data["precio_objetivo_es"] = precio
            ctx.user_data["modo_precio"] = "manual"
        except ValueError:
            await update.message.reply_text(
                "⚠️ No entendí el precio. Escribe un número (ej: <code>32000</code>) "
                "o <code>auto</code> para búsqueda automática.",
                parse_mode="HTML",
            )
            return ASK_PRECIO_OBJETIVO

    await update.message.reply_text(
        "🔧 <b>Filtros opcionales</b> — o escribe <code>no</code> para omitir\n\n"
        "Escríbelo como quieras, la IA lo entiende. Combina lo que quieras:\n\n"
        "<b>Básicos:</b> km, año, precio, potencia, puertas\n"
        "<i>ej: menos de 80k km, del 2019, máximo 25000€, más de 150cv</i>\n\n"
        "<b>Tipo:</b> color, carrocería, combustible, caja\n"
        "<i>ej: gris, descapotable, gasolina, manual</i>\n\n"
        "<b>Equipamiento:</b> navegación, cuero, techo panorámico, head-up, "
        "cámara 360, apple carplay, tracción integral…\n\n"
        "💡 <b>Ejemplo completo:</b>\n"
        "<code>descapotable gris, manual, menos de 60k km, del 2020, "
        "navegacion, cuero, apple carplay</code>",
        parse_mode="HTML",
    )
    return ASK_FILTROS


async def ejecutar_busqueda(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    texto_filtros = update.message.text.strip()

    # Parsear filtros con IA
    msg = await update.message.reply_text("🤖 Interpretando filtros…")
    filtros = await parsear_filtros_nl(texto_filtros)
    await msg.delete()

    if filtros:
        filtros_txt = ", ".join(f"{k}={v}" for k, v in filtros.items() if not k.startswith("_"))
        await update.message.reply_text(
            f"✅ Filtros detectados: <code>{filtros_txt}</code>",
            parse_mode="HTML",
        )

    ctx.user_data["filtros"] = filtros

    marca           = ctx.user_data["marca"]
    modelo          = ctx.user_data["modelo"]
    precio_objetivo = ctx.user_data["precio_objetivo_es"]
    modo            = ctx.user_data["modo_precio"]

    # ── Progreso ──────────────────────────────────────────────────────────────
    msg = await update.message.reply_text(
        "⏳ <b>Buscando en AutoScout24 + mobile.de…</b>\n"
        + ("🤖 Y cruzando precios con Wallapop + coches.net\n" if modo == "auto" else "")
        + "Esto puede tardar 90-120 segundos.",
        parse_mode="HTML",
    )

    # ── Scraping ──────────────────────────────────────────────────────────────
    if modo == "auto":
        coches = await buscar_y_cruzar(marca, modelo, filtros)
    else:
        coches = await buscar_coches_alemania(marca, modelo, filtros)

    if not coches:
        await msg.edit_text(
            "😔 No encontré resultados. Prueba con:\n"
            "• Filtros menos restrictivos\n"
            "• Otro nombre de modelo (ej: <code>serie 3</code> en vez de <code>320d</code>)",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    # ── Validar precios ES con IA (modo auto) ────────────────────────────────
    if modo == "auto":
        for coche in coches:
            if coche.get("precio_medio_es") and coche.get("precios_usados_es"):
                val = await validar_precio_mercado(
                    marca, modelo,
                    coche.get("año", 0), coche.get("km", 0),
                    coche["precio_medio_es"], coche["precios_usados_es"],
                )
                coche["validacion_precio"] = val
                if not val.get("valido", True):
                    logger.warning(f"[BOT] Precio medio invalidado: {val}")
                    coche["precio_medio_es"] = 0.0
                    coche["error_es"] = f"Precio descartado por IA: {val.get('comentario', '')}"

    # ── Post-filtrado extras IA (CAPA 2) ─────────────────────────────────────
    extras_sin_codigo = filtros.get("_extras_sin_codigo", [])
    if extras_sin_codigo and coches:
        await msg.edit_text(f"🔍 Verificando equipamiento con IA: {', '.join(extras_sin_codigo)}…")
        coches = await filtrar_por_extras(coches, extras_sin_codigo)
        if not coches:
            await msg.edit_text("😔 Ningún anuncio con ese equipamiento. Prueba con menos filtros.")
            return ConversationHandler.END

    # ── Análisis IA ───────────────────────────────────────────────────────────
    await msg.edit_text("🤖 Analizando anuncios con IA…")
    coches = await enriquecer_coches(coches)

    # ── Calcular Sniper Score y ordenar ──────────────────────────────────────
    for c in coches:
        c["_score"] = calcular_sniper_score(c, precio_objetivo)

    coches_ordenados = sorted(coches, key=lambda c: c["_score"]["sniper_score"], reverse=True)[:TOP_RESULTS]
    ctx.user_data["coches_mostrados"] = coches_ordenados

    # ── Resumen de fuentes ────────────────────────────────────────────────────
    fuentes_de = set(c.get("fuente", "?") for c in coches)
    fuentes_txt = " + ".join(fuentes_de)
    modo_label = "precio medio Wallapop+coches.net" if modo == "auto" else "tu precio objetivo"

    await msg.edit_text(
        f"✅ <b>TOP {len(coches_ordenados)} oportunidades</b> "
        f"({fuentes_txt})\n"
        f"Ordenadas por Sniper Score ({modo_label}):",
        parse_mode="HTML",
    )

    # ── Mostrar tarjetas ──────────────────────────────────────────────────────
    for idx, coche in enumerate(coches_ordenados, 1):
        score = coche["_score"]
        texto_tarjeta = f"<b>#{idx}</b> 📍<i>{coche.get('fuente', '?')}</i>\n"
        texto_tarjeta += formato_tarjeta(coche, precio_objetivo)
        texto_tarjeta += "\n\n" + formato_sniper_score(score)

        analisis = coche.get("analisis_ia", {})
        ia_txt = texto_analisis(analisis)
        if ia_txt:
            texto_tarjeta += "\n\n" + ia_txt

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Me sirve",  callback_data=f"ok_{coche['id']}"),
            InlineKeyboardButton("❌ Descartar", callback_data=f"skip_{coche['id']}"),
        ]])

        if coche.get("foto"):
            try:
                await update.message.reply_photo(
                    photo=coche["foto"],
                    caption=f"{score['emoji']} {texto_tarjeta}",
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                continue
            except Exception:
                pass
        await update.message.reply_text(
            f"{score['emoji']} {texto_tarjeta}",
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    # ── Botón guardar misión ──────────────────────────────────────────────────
    tier = ctx.user_data.get("tier", "free")
    puede_sniper = _tier_puede(tier, "sniper")

    botones = [
        [InlineKeyboardButton("📡 Guardar misión (cada 15 min)", callback_data="guardar_mision_normal")],
    ]
    if puede_sniper:
        botones.append(
            [InlineKeyboardButton("🎯 Guardar misión SNIPER (cada 3 min)", callback_data="guardar_mision_sniper")]
        )
    botones.append(
        [InlineKeyboardButton("🛑 Terminar", callback_data="terminar")]
    )

    await update.message.reply_text(
        f"¿Quieres que monitoree y te avise cuando haya beneficio ≥ {MIN_BENEFICIO:,}€?"
        + ("\n🎯 <i>Como usuario Sniper puedes activar alertas cada 3 min.</i>" if puede_sniper else ""),
        reply_markup=InlineKeyboardMarkup(botones),
        parse_mode="HTML",
    )
    return SHOW_RESULTS


# ════════════════════════════════════════════════════════════════════════════
# CALLBACKS de resultados
# ════════════════════════════════════════════════════════════════════════════

async def callback_resultados(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data in ("guardar_mision_normal", "guardar_mision_sniper"):
        prioridad = "sniper" if data == "guardar_mision_sniper" else "normal"
        intervalo = "3 min 🎯" if prioridad == "sniper" else "15 min"

        mision_id = crear_mision(
            user_id=query.from_user.id,
            query_modelo=ctx.user_data.get("query_raw", ""),
            filtros=ctx.user_data.get("filtros", {}),
            precio_objetivo_es=ctx.user_data.get("precio_objetivo_es"),
            prioridad=prioridad,
        )
        await query.edit_message_text(
            f"✅ <b>Misión #{mision_id} activada ({prioridad.upper()}).</b>\n"
            f"Monitorizando AutoScout24 + mobile.de cada {intervalo}.\n"
            f"Te aviso cuando el beneficio supere {MIN_BENEFICIO:,}€",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    elif data == "terminar":
        await query.edit_message_text("👍 Búsqueda finalizada. Usa /buscar cuando quieras.")
        return ConversationHandler.END

    elif data.startswith("ok_"):
        await query.answer("✅ ¡Genial! Espero que cierres buen negocio.", show_alert=True)

    elif data.startswith("skip_"):
        await query.answer("❌ Descartado.")


# ════════════════════════════════════════════════════════════════════════════
# /calcular — calculadora inversa
# ════════════════════════════════════════════════════════════════════════════

async def calcular_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    allowed, tier = _check_access(user.id, user.username or "")
    if not allowed:
        await update.message.reply_text("⛔ No tienes acceso a este bot.")
        return ConversationHandler.END

    ctx.user_data["calc"] = {}
    await update.message.reply_text(
        "🎯 <b>CALCULADORA INVERSA</b>\n\n"
        "Calculo el precio máximo que puedes pagar en Alemania\n"
        "para obtener el beneficio que quieres.\n\n"
        "💶 <b>¿A cuánto vendes el coche en España?</b>\n"
        "Ej: <code>35000</code>",
        parse_mode="HTML",
    )
    return CALC_PRECIO


async def calc_recibir_precio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        precio = float(update.message.text.strip().replace(".", "").replace(",", "."))
        ctx.user_data["calc"]["precio_es"] = precio
    except ValueError:
        await update.message.reply_text("⚠️ Escribe un número. Ej: <code>35000</code>", parse_mode="HTML")
        return CALC_PRECIO

    await update.message.reply_text(
        "💰 <b>¿Cuánto beneficio mínimo quieres?</b>\n"
        "Ej: <code>4000</code>",
        parse_mode="HTML",
    )
    return CALC_BENEFICIO


async def calc_recibir_beneficio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        beneficio = float(update.message.text.strip().replace(".", "").replace(",", "."))
        ctx.user_data["calc"]["beneficio"] = beneficio
    except ValueError:
        await update.message.reply_text("⚠️ Escribe un número. Ej: <code>4000</code>", parse_mode="HTML")
        return CALC_BENEFICIO

    await update.message.reply_text(
        "💨 <b>¿Emisiones CO₂ del coche?</b> (g/km)\n\n"
        "• Escribe el valor → Ej: <code>140</code>\n"
        "• Escribe <code>no</code> → Asumo ≤120 g/km (IEDMT 0%)\n\n"
        "<i>Tramos IEDMT:\n"
        "  ≤120 g/km → 0%\n"
        "  121-159 → 4.75%\n"
        "  160-199 → 9.75%\n"
        "  ≥200 → 14.75%</i>",
        parse_mode="HTML",
    )
    return CALC_CO2


async def calc_recibir_co2(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip().lower()
    if texto in ("no", "skip", "-", "0"):
        co2 = 0.0
    else:
        try:
            co2 = float(texto.replace(",", "."))
        except ValueError:
            await update.message.reply_text("⚠️ Escribe un número o <code>no</code>.", parse_mode="HTML")
            return CALC_CO2

    calc_data = ctx.user_data["calc"]
    resultado = calcular_precio_maximo_de(
        precio_venta_es=calc_data["precio_es"],
        beneficio_minimo=calc_data["beneficio"],
        co2=co2,
    )

    await update.message.reply_text(
        formato_calculadora_inversa(resultado),
        parse_mode="HTML",
    )
    return ConversationHandler.END


# ════════════════════════════════════════════════════════════════════════════
# /misiones — ver, pausar, activar, eliminar
# ════════════════════════════════════════════════════════════════════════════

async def mis_misiones(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    allowed, _tier = _check_access(user.id, user.username or "")
    if not allowed:
        await update.message.reply_text("⛔ No tienes acceso a este bot.")
        return

    misiones = obtener_misiones_usuario(user.id)
    if not misiones:
        await update.message.reply_text("No tienes misiones activas. Usa /buscar para crear una.")
        return

    texto = "📋 <b>Tus misiones</b>:\n\n"
    rows = []
    for m in misiones:
        estado = m["estado"]
        prioridad = m.get("prioridad", "normal")
        prio_emoji = "🎯" if prioridad == "sniper" else "📡"

        emoji = "🟢" if estado == "ACTIVA" else "⏸️"
        precio = m["precio_objetivo_es"]
        precio_s = f"{precio:,.0f}€" if precio else "auto"
        texto += f"{emoji}{prio_emoji} <b>#{m['id']}</b> — {m['query_modelo']} · {precio_s}\n"

        if estado == "ACTIVA":
            rows.append([
                InlineKeyboardButton(f"⏸ Pausar #{m['id']}", callback_data=f"pausar_{m['id']}"),
                InlineKeyboardButton(f"🗑 Eliminar #{m['id']}", callback_data=f"eliminar_{m['id']}"),
            ])
        else:
            rows.append([
                InlineKeyboardButton(f"▶ Activar #{m['id']}", callback_data=f"activar_{m['id']}"),
                InlineKeyboardButton(f"🗑 Eliminar #{m['id']}", callback_data=f"eliminar_{m['id']}"),
            ])

    await update.message.reply_text(texto, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def callback_misiones(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data.startswith("pausar_"):
        mid = int(data.split("_")[1])
        pausar_mision(mid)
        await query.edit_message_text(f"⏸️ Misión #{mid} pausada.")
    elif data.startswith("activar_"):
        mid = int(data.split("_")[1])
        activar_mision(mid)
        await query.edit_message_text(f"🟢 Misión #{mid} reactivada.")
    elif data.startswith("eliminar_"):
        mid = int(data.split("_")[1])
        if eliminar_mision(mid, user_id):
            await query.edit_message_text(f"🗑 Misión #{mid} eliminada.")
        else:
            await query.edit_message_text(f"⚠️ No se pudo eliminar la misión #{mid}.")


async def _enviar_largo(msg, texto: str, parse_mode: str = "HTML", **kwargs):
    """Edita msg con texto; si excede 4000 chars, lo divide en mensajes adicionales."""
    LIMITE = 4000
    if len(texto) <= LIMITE:
        await msg.edit_text(texto, parse_mode=parse_mode, **kwargs)
        return
    partes = []
    while len(texto) > LIMITE:
        corte = texto.rfind("\n\n", 0, LIMITE)
        if corte < 200:
            corte = LIMITE
        partes.append(texto[:corte])
        texto = texto[corte:].lstrip()
    if texto:
        partes.append(texto)
    await msg.edit_text(partes[0], parse_mode=parse_mode, **kwargs)
    for parte in partes[1:]:
        await msg.reply_text(parte, parse_mode=parse_mode, **kwargs)


# ════════════════════════════════════════════════════════════════════════════
# /analizar — núcleo compartido
# ════════════════════════════════════════════════════════════════════════════

async def _core_analisis(url: str, source_msg, ctx, es_admin: bool, user_id: int):
    """
    Lógica central de análisis de un anuncio. Reutilizada por /analizar y
    por el botón "Analizar #N" de /ideal.
    source_msg: Message desde donde enviar mensajes de progreso.
    """
    from models import EstadisticaMercado

    cached = cache_get(url)
    if cached:
        veredicto_cache, contexto_cache, mins_ago = cached
        msg = await source_msg.reply_text("⏳ Recuperando análisis…")
        prefijo = f"<i>♻️ Análisis cacheado hace {mins_ago} min</i>\n\n"
        await _enviar_largo(msg, prefijo + veredicto_cache,
                            parse_mode="HTML", disable_web_page_preview=True)
        if contexto_cache:
            ctx.user_data["analisis_qa_ctx"] = contexto_cache
            teclado = InlineKeyboardMarkup([[
                InlineKeyboardButton("💬 Sí, dame preguntas + checklist", callback_data="qa:si"),
                InlineKeyboardButton("No, gracias", callback_data="qa:no"),
            ]])
            await source_msg.reply_text(
                "¿Quieres que te prepare <b>preguntas para el vendedor</b> y un "
                "<b>checklist</b> para cuando vayas a verlo en persona?",
                parse_mode="HTML", reply_markup=teclado,
            )
        return

    msg = await source_msg.reply_text("⏳ Extrayendo datos del anuncio…")
    try:
        try:
            anuncio = await obtener_anuncio_por_url(url)
        except Exception as e:
            logger.error(f"[BOT] Error extrayendo anuncio: {e}")
            anuncio = None

        if not anuncio or anuncio.precio <= 0:
            await msg.edit_text(
                "😔 No pude extraer los datos del anuncio.\n"
                "• Comprueba que la URL sea de Wallapop o Coches.net y el anuncio siga activo.\n"
                "• A veces Wallapop bloquea temporalmente. Prueba en 1 min."
            )
            return

        marca  = anuncio.marca  or "desconocida"
        modelo = anuncio.modelo or "desconocido"
        año    = anuncio.año    or 0
        km     = anuncio.km     or 0

        await msg.edit_text(
            f"✅ Anuncio encontrado: <b>{html.escape(marca.title())} "
            f"{html.escape(modelo.upper())}</b> "
            f"{año} · {km:,} km · <b>{anuncio.precio:,.0f}€</b>\n\n"
            f"⏳ Buscando comparables en Wallapop y Coches.net…",
            parse_mode="HTML",
        )

        try:
            comparables = await buscar_comparables_todas(marca, modelo, año, km, n=30)
        except Exception as e:
            logger.error(f"[BOT] Error buscando comparables: {e}")
            comparables = []

        comparables = [c for c in comparables if c.item_id != anuncio.item_id]
        fuentes_count = dict(Counter(c.fuente for c in comparables))
        logger.info(f"[BOT] Comparables por fuente: {fuentes_count}")

        historico = [a for a in ([anuncio] + comparables) if a.precio > 0 and a.año > 1990]
        try:
            guardar_historico_batch(historico)
        except Exception as e:
            logger.warning(f"[BOT] Error guardando histórico: {e}")

        precios_comp = [c.precio for c in comparables if c.precio > 0]

        if len(precios_comp) < 3:
            await msg.edit_text(
                f"⚠️ Solo encontré {len(precios_comp)} comparable(s) para "
                f"<b>{html.escape(marca.title())} {html.escape(modelo.upper())}</b> con esos parámetros.\n"
                f"No hay datos suficientes para un veredicto fiable. Prueba un modelo más común.",
                parse_mode="HTML",
            )
            return

        mediana    = _stats_mod.median(precios_comp)
        media      = _stats_mod.mean(precios_comp)
        desviacion = _stats_mod.stdev(precios_comp) if len(precios_comp) > 1 else 0.0
        precios_ord = sorted(precios_comp)
        pos_menor   = sum(1 for p in precios_ord if p < anuncio.precio)
        percentil   = round((pos_menor / len(precios_ord)) * 100)
        desv_pct    = round(((anuncio.precio - mediana) / mediana) * 100, 1) if mediana else 0.0

        stats = EstadisticaMercado(
            n_comparables=len(precios_comp),
            mediana=round(mediana, 0),
            media=round(media, 0),
            desviacion=round(desviacion, 0),
            percentil=percentil,
            desviacion_pct=desv_pct,
            precios=precios_ord,
        )

        await msg.edit_text(
            f"📊 {stats.n_comparables} comparables encontrados. "
            f"Mediana: <b>{stats.mediana:,.0f}€</b>\n"
            f"⏳ Generando veredicto con IA…",
            parse_mode="HTML",
        )

        contexto_qa = None
        try:
            veredicto, contexto_qa = await generar_veredicto_analizar(
                anuncio, stats, comparables,
                fuentes_count=fuentes_count,
            )
        except Exception as e:
            logger.error(f"[BOT] Error generando veredicto: {e}")
            veredicto = (
                f"⚠️ No pude generar veredicto IA.\n"
                f"Precio: {anuncio.precio:,.0f}€ · Mediana: {stats.mediana:,.0f}€"
            )

        if contexto_qa:
            try:
                cache_set(url, veredicto, contexto_qa)
            except Exception:
                pass

        cabecera = (
            f"🔍 <b>{html.escape(marca.title())} {html.escape(modelo.upper())} {año}</b>\n"
            f"📍 {html.escape(anuncio.provincia or 'España')}  ·  {km:,} km  ·  "
            f"<a href='{url}'>Ver anuncio</a>\n"
            f"{'─' * 30}\n\n"
        )

        await _enviar_largo(
            msg, cabecera + veredicto,
            parse_mode="HTML", disable_web_page_preview=True,
        )

        if not es_admin:
            registrar_analisis(user_id)

        if contexto_qa:
            ctx.user_data["analisis_qa_ctx"] = contexto_qa
            teclado = InlineKeyboardMarkup([[
                InlineKeyboardButton("💬 Sí, dame preguntas + checklist", callback_data="qa:si"),
                InlineKeyboardButton("No, gracias", callback_data="qa:no"),
            ]])
            await source_msg.reply_text(
                "¿Quieres que te prepare <b>preguntas para el vendedor</b> y un "
                "<b>checklist</b> para cuando vayas a verlo en persona?",
                parse_mode="HTML",
                reply_markup=teclado,
            )

    except Exception:
        logger.error("[BOT] Excepción no capturada en _core_analisis", exc_info=True)
        try:
            await msg.edit_text("😔 Algo se rompió en el análisis. Reintenta en 1 min.")
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════
# /analizar <url> — semana 1
# ════════════════════════════════════════════════════════════════════════════

async def cmd_analizar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    allowed, _tier = _check_access(user.id, user.username or "")
    if not allowed:
        await update.message.reply_text("⛔ No tienes acceso a este bot.")
        return

    get_o_crear_usuario(user.id, user.username or "", user.first_name or "")
    es_admin = user.id in ADMIN_USER_IDS
    puede, restantes = puede_analizar(user.id)
    if es_admin:
        puede, restantes = True, FREE_ANALISIS_MAX
    if not puede:
        mins = minutos_hasta_reset(user.id)
        h, m = divmod(mins, 60)
        cuando = f"{h}h {m}min" if h else f"{m} min"
        await update.message.reply_text(
            f"⛔ <b>Has usado tus {FREE_ANALISIS_MAX} análisis gratuitos.</b>\n\n"
            "Cada análisis cuesta dinero real (scraping + IA). "
            "Por eso hay un tope mientras estoy en beta — si no, el bot se "
            "queda sin gasolina y no puedo mantenerlo abierto.\n\n"
            f"⏳ Tu límite se resetea en <b>{cuando}</b>.\n\n"
            "🚀 Pronto podrás desbloquear <b>análisis ilimitados</b> "
            "por una suscripción mensual. Estoy terminándolo.",
            parse_mode="HTML",
        )
        return
    if restantes == 1:
        await update.message.reply_text(
            f"ℹ️ Te queda <b>1 análisis</b> en esta ventana de "
            f"{FREE_VENTANA_HORAS}h.",
            parse_mode="HTML",
        )

    texto = update.message.text or ""
    url_match = _re.search(
        r"https?://(?:[\w-]+\.)*(?:wallapop\.[a-z]{2,}|coches\.net)/\S+",
        texto,
        _re.IGNORECASE,
    )
    if not url_match:
        await update.message.reply_text(
            "🔍 Pégame la URL del anuncio (Wallapop o Coches.net).\n"
            "• <code>/analizar https://es.wallapop.com/item/...</code>\n"
            "• <code>/analizar https://www.coches.net/...-kovn.aspx</code>",
            parse_mode="HTML",
        )
        return

    url = url_match.group(0).rstrip(",.;:)]}>'\"")
    await _core_analisis(url, update.message, ctx, es_admin, user.id)


# ════════════════════════════════════════════════════════════════════════════
# /cancelar
# ════════════════════════════════════════════════════════════════════════════

async def cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Operación cancelada.")
    return ConversationHandler.END


# ════════════════════════════════════════════════════════════════════════════
# /ideal — Recomendador de coche usado (semana 2)
# ════════════════════════════════════════════════════════════════════════════

_IDEAL_HUECOS_ORDEN = [
    "presupuesto_max", "tamaño", "uso", "plazas_min", "marcas_evitar"
]

# Tabla determinista: tamaño → modelos comunes en mercado español 2ª mano
TAMANO_A_MODELOS: dict[str, list[tuple[str, str]]] = {
    "urbano": [
        ("kia", "picanto"), ("hyundai", "i10"), ("toyota", "aygo"),
        ("citroen", "c1"), ("peugeot", "108"), ("renault", "twingo"),
        ("volkswagen", "up"), ("skoda", "citigo"), ("seat", "mii"),
        ("dacia", "sandero"), ("fiat", "panda"),
    ],
    "compacto": [
        ("seat", "ibiza"), ("volkswagen", "polo"), ("skoda", "fabia"),
        ("hyundai", "i20"), ("toyota", "yaris"), ("mazda", "2"),
        ("ford", "fiesta"), ("renault", "clio"), ("peugeot", "208"),
        ("opel", "corsa"),
    ],
    "berlina": [
        ("skoda", "octavia"), ("seat", "leon"), ("volkswagen", "golf"),
        ("hyundai", "i30"), ("kia", "ceed"), ("toyota", "corolla"),
        ("mazda", "3"), ("ford", "focus"), ("peugeot", "308"),
    ],
    "suv_compacto": [
        ("hyundai", "tucson"), ("hyundai", "ix35"), ("kia", "sportage"),
        ("nissan", "qashqai"), ("seat", "ateca"), ("skoda", "karoq"),
        ("volkswagen", "t-roc"), ("ford", "kuga"), ("peugeot", "3008"),
        ("mazda", "cx-5"), ("toyota", "rav4"), ("dacia", "duster"),
    ],
    "suv_grande": [
        ("volkswagen", "tiguan"), ("skoda", "kodiaq"),
        ("hyundai", "santa fe"), ("kia", "sorento"),
        ("nissan", "x-trail"), ("volkswagen", "touareg"),
        ("bmw", "x3"), ("audi", "q5"),
    ],
    "familiar": [
        ("skoda", "octavia combi"), ("seat", "leon st"),
        ("volkswagen", "passat variant"), ("kia", "ceed sw"),
        ("hyundai", "i30 tourer"), ("toyota", "corolla touring sports"),
        ("ford", "focus sw"),
    ],
    "monovolumen": [
        ("seat", "alhambra"), ("volkswagen", "sharan"),
        ("ford", "s-max"), ("ford", "galaxy"),
        ("kia", "carnival"), ("volkswagen", "touran"),
        ("citroen", "grand c4 picasso"), ("renault", "grand scenic"),
    ],
}

# Cache global del sondeo: f"{marca}_{modelo}_{tramo2k}" → (ts, precio_min)
_SONDEO_CACHE: dict[str, tuple[float, float]] = {}
_SONDEO_TTL_S = 24 * 3600

_IDEAL_TEXTOS = {
    "presupuesto_max": "💶 ¿Cuánto quieres gastar como máximo?\nO escribe el número (ej: <code>15000</code>):",
    "uso":             "🚗 ¿Cuál será el uso principal del coche?",
    "plazas_min":      "👥 ¿Cuántas plazas necesitas como mínimo?",
    "combustible":     ("⛽ ¿Qué combustible prefieres?\n"
                        "<i>Si no sabes, pulsa «No sé» y te recomiendo según el uso.</i>"),
    "duracion_uso":    ("⏱️ ¿Cuánto tiempo planeas usar este coche?\n"
                        "<i>Esto me ayuda a saber qué kilómetros buscar.</i>"),
    "tamaño":          ("📐 ¿Qué <b>tamaño</b> de coche buscas?\n"
                        "<i>Es lo más importante para acertar con los modelos.</i>"),
    "marcas_evitar":   "🚫 ¿Hay alguna marca que quieras evitar?\nEscribe el nombre o pulsa el botón:",
}


def _ideal_keyboard(hueco: str) -> InlineKeyboardMarkup | None:
    botones: dict = {
        "presupuesto_max": [
            [InlineKeyboardButton("Hasta 8.000€",   callback_data="ideal:presupuesto_max:8000"),
             InlineKeyboardButton("Hasta 12.000€",  callback_data="ideal:presupuesto_max:12000")],
            [InlineKeyboardButton("Hasta 15.000€",  callback_data="ideal:presupuesto_max:15000"),
             InlineKeyboardButton("Hasta 20.000€",  callback_data="ideal:presupuesto_max:20000")],
            [InlineKeyboardButton("Hasta 25.000€",  callback_data="ideal:presupuesto_max:25000"),
             InlineKeyboardButton("Más de 30.000€", callback_data="ideal:presupuesto_max:35000")],
        ],
        "uso": [
            [InlineKeyboardButton("Ciudad",           callback_data="ideal:uso:ciudad"),
             InlineKeyboardButton("Autopista/viajes", callback_data="ideal:uso:autopista")],
            [InlineKeyboardButton("Mixto (todo)",     callback_data="ideal:uso:mixto"),
             InlineKeyboardButton("Off-road/campo",   callback_data="ideal:uso:offroad")],
        ],
        "plazas_min": [
            [InlineKeyboardButton("2 plazas",   callback_data="ideal:plazas_min:2"),
             InlineKeyboardButton("4-5 plazas", callback_data="ideal:plazas_min:5"),
             InlineKeyboardButton("7+ plazas",  callback_data="ideal:plazas_min:7")],
        ],
        "combustible": [
            [InlineKeyboardButton("Gasolina",           callback_data="ideal:combustible:gasolina"),
             InlineKeyboardButton("Diésel",             callback_data="ideal:combustible:diesel")],
            [InlineKeyboardButton("Híbrido/Eléctrico",  callback_data="ideal:combustible:hibrido"),
             InlineKeyboardButton("Me da igual",        callback_data="ideal:combustible:cualquiera")],
            [InlineKeyboardButton("🤔 No sé, recomiéndame", callback_data="ideal:combustible:no_se")],
            [InlineKeyboardButton("🏷️ ECO/0 (necesito ZBE)",
                                  callback_data="ideal:combustible:eco_zbe")],
        ],
        "duracion_uso": [
            [InlineKeyboardButton("1-3 años (luego cambio)", callback_data="ideal:duracion_uso:corta")],
            [InlineKeyboardButton("Unos 5 años",             callback_data="ideal:duracion_uso:media")],
            [InlineKeyboardButton("10+ años (que dure)",     callback_data="ideal:duracion_uso:larga")],
            [InlineKeyboardButton("Es mi primer coche",      callback_data="ideal:duracion_uso:primer_coche")],
        ],
        "tamaño": [
            [InlineKeyboardButton("🚗 Urbano (Picanto, Up!, Aygo)",      callback_data="ideal:tamaño:urbano")],
            [InlineKeyboardButton("🚙 Compacto (Ibiza, Polo, i20)",      callback_data="ideal:tamaño:compacto")],
            [InlineKeyboardButton("🚘 Berlina/familiar (Octavia, Golf)", callback_data="ideal:tamaño:berlina")],
            [InlineKeyboardButton("🛻 SUV pequeño (Tucson, Qashqai)",    callback_data="ideal:tamaño:suv_compacto")],
            [InlineKeyboardButton("🚙 SUV grande / familiar 5+",         callback_data="ideal:tamaño:suv_grande")],
            [InlineKeyboardButton("👨‍👩‍👧‍👦 Monovolumen 7 plazas",    callback_data="ideal:tamaño:monovolumen")],
            [InlineKeyboardButton("🤔 Recomiéndame",                     callback_data="ideal:tamaño:recomiendame")],
        ],
        "marcas_evitar": [
            [InlineKeyboardButton("Sin preferencia", callback_data="ideal:marcas_evitar:ninguna")],
        ],
    }
    filas = botones.get(hueco)
    return InlineKeyboardMarkup(filas) if filas else None


def _ideal_parsear_texto(campo: str, texto: str):
    """Convierte la respuesta de texto libre del usuario al tipo correcto del campo."""
    t = texto.strip().lower()

    if campo == "presupuesto_max":
        m_k = _re.search(r"(\d+)\s*k\b", t)
        if m_k:
            return int(m_k.group(1)) * 1000
        num = _re.sub(r"[^\d]", "", t)
        if not num:
            return None
        n = int(num)
        if n < 1000:
            n *= 1000
        return n

    if campo == "uso":
        _MAP = {
            "ciudad": "ciudad", "urbano": "ciudad", "urbana": "ciudad",
            "autopista": "autopista", "viaje": "autopista", "viajes": "autopista",
            "carretera": "autopista", "mixto": "mixto", "todo": "mixto",
            "campo": "offroad", "montaña": "offroad", "offroad": "offroad",
        }
        for k, v in _MAP.items():
            if k in t:
                return v
        return "mixto"

    if campo == "plazas_min":
        if any(x in t for x in ("7", "siete", "grand", "7+")):
            return 7
        if any(x in t for x in ("2 ", "dos", "pareja", "cupé", "coupe")):
            return 2
        return 5

    if campo == "combustible":
        if any(x in t for x in ("no sé", "no se", "no idea", "recomien", "cualquiera me da")):
            return "no_se"
        if any(x in t for x in ("gasolina", "nafta", "benzina")):
            return "gasolina"
        if any(x in t for x in ("diesel", "diésel", "gasoil", "tdi", "cdi")):
            return "diesel"
        if any(x in t for x in ("electr", "ev ", "bev")):
            return "electrico"
        if any(x in t for x in ("hibrido", "híbrido", "hybrid", "phev")):
            return "hibrido"
        if any(x in t for x in ("zbe", "eco", "pegatina", "central", "madrid")):
            return "eco_zbe"
        return "cualquiera"

    if campo == "duracion_uso":
        # Detectar primero el caso especial
        if any(x in t for x in ("primer coche", "primer carro", "recién", "carnet", "novato")):
            return "primer_coche"
        if any(x in t for x in ("dure", "duradero", "muchos años", "para siempre",
                                 "10 año", "20 año", "que aguante", "largo plazo")):
            return "larga"
        if any(x in t for x in ("poco tiempo", "temporal", "1 año", "2 año",
                                 "1-2", "1-3", "de paso", "provisional")):
            return "corta"
        if any(x in t for x in ("5 año", "unos años", "luego cambio", "luego vendo",
                                 "medio plazo", "media", "después")):
            return "media"
        return "media"  # default razonable

    if campo == "tamaño":
        _MAP_T = {
            "urbano":"urbano", "ciudad":"urbano", "pequeñ":"urbano", "mini":"urbano",
            "compact":"compacto", "ibiza":"compacto", "polo":"compacto",
            "berlin":"berlina", "octavia":"berlina", "golf":"berlina", "leon":"berlina",
            "suv pequ":"suv_compacto", "crossover":"suv_compacto", "tucson":"suv_compacto",
            "suv grand":"suv_grande", "todoterreno":"suv_grande", "4x4":"suv_grande",
            "monovol":"monovolumen", "7 plaz":"monovolumen", "siete":"monovolumen",
            "familia":"familiar", "ranchera":"familiar", " sw":"familiar",
            "no sé":"recomiendame", "no se":"recomiendame", "recom":"recomiendame", "da igual":"recomiendame",
        }
        for k, v in _MAP_T.items():
            if k in t:
                return v
        return "recomiendame"

    if campo == "marcas_evitar":
        if any(x in t for x in ("no", "ninguna", "sin pref", "igual", "da igual", "-", "skip")):
            return []
        return [m.strip().capitalize() for m in _re.split(r"[,;\s/]+", t) if len(m.strip()) > 1]

    return t


async def _ideal_avanzar(source_msg, ctx) -> int:
    """Pregunta el siguiente hueco o lanza la búsqueda si no quedan huecos."""
    huecos = ctx.user_data.get("ideal_huecos", [])
    if huecos:
        siguiente = huecos[0]
        ctx.user_data["hueco_actual"] = siguiente
        await source_msg.reply_text(
            _IDEAL_TEXTOS[siguiente],
            parse_mode="HTML",
            reply_markup=_ideal_keyboard(siguiente),
        )
        return IDEAL_COLLECT
    return await _ideal_buscar(source_msg, ctx)


async def _ideal_guardar_y_continuar(campo: str, valor, source_msg, ctx) -> int:
    """Guarda valor en el perfil, elimina el hueco y avanza."""
    from ai import DURACION_USO_A_KM_MAX

    perfil = ctx.user_data.get("ideal_perfil", {})

    if campo == "combustible":
        if valor == "eco_zbe":
            perfil["combustible"] = ["hibrido", "electrico"]
            perfil["etiqueta_dgt_min"] = "ECO"
        elif valor == "cualquiera":
            perfil["combustible"] = None
        elif valor == "no_se":
            # Inferir de uso
            uso = perfil.get("uso")
            if uso == "ciudad":
                perfil["combustible"] = ["hibrido", "electrico"]
                perfil["etiqueta_dgt_min"] = perfil.get("etiqueta_dgt_min") or "ECO"
                sugerencia = "híbrido o eléctrico (perfecto para ciudad)"
            elif uso == "autopista":
                perfil["combustible"] = ["diesel"]
                sugerencia = "diésel (rentable en autopista)"
            elif uso == "offroad":
                perfil["combustible"] = ["diesel"]
                sugerencia = "diésel (mejor par para off-road)"
            else:  # mixto o None
                perfil["combustible"] = ["gasolina", "hibrido"]
                sugerencia = "gasolina o híbrido (versátil)"
            try:
                await source_msg.reply_text(
                    f"💡 Te recomiendo <b>{sugerencia}</b>.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        else:
            perfil["combustible"] = [valor] if isinstance(valor, str) else valor
    elif campo == "duracion_uso":
        perfil["duracion_uso"] = valor
        # Si el usuario no ha fijado km_max manualmente, derivarlo
        if not perfil.get("km_max"):
            perfil["km_max"] = DURACION_USO_A_KM_MAX.get(valor, 150_000)
    elif campo == "tamaño":
        if valor == "recomiendame":
            plazas   = perfil.get("plazas_min") or 5
            presup   = perfil.get("presupuesto_max") or 0
            duracion = perfil.get("duracion_uso")
            uso      = perfil.get("uso")
            if plazas >= 7:
                inf = "monovolumen"
            elif duracion == "primer_coche" or (presup and presup < 7000):
                inf = "urbano"
            elif presup and presup < 10000:
                inf = "compacto"
            elif uso == "offroad":
                inf = "suv_compacto"
            elif presup and presup >= 18000:
                inf = "suv_compacto"
            else:
                inf = "berlina"
            perfil["tamaño"] = inf
            try:
                await source_msg.reply_text(
                    f"💡 Te recomiendo un <b>{inf.replace('_', ' ')}</b>.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        else:
            perfil["tamaño"] = valor
    else:
        perfil[campo] = valor

    ctx.user_data["ideal_perfil"] = perfil

    huecos = ctx.user_data.get("ideal_huecos", [])
    if campo in huecos:
        huecos.remove(campo)
    ctx.user_data["ideal_huecos"] = huecos

    return await _ideal_avanzar(source_msg, ctx)


async def ideal_recibir_texto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Maneja respuesta de texto en el flujo /ideal."""
    campo = ctx.user_data.get("hueco_actual")
    if not campo:
        return ConversationHandler.END

    texto = update.message.text.strip()
    valor = _ideal_parsear_texto(campo, texto)

    if campo in ("presupuesto_max", "km_max") and valor is None:
        await update.message.reply_text(
            "⚠️ No entendí el número. Escribe solo el número, ej: <code>15000</code>",
            parse_mode="HTML",
        )
        return IDEAL_COLLECT

    return await _ideal_guardar_y_continuar(campo, valor, update.message, ctx)


async def ideal_recibir_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Maneja pulsación de botón inline en el flujo /ideal."""
    query = update.callback_query
    await query.answer()
    partes = (query.data or "").split(":", 2)
    if len(partes) < 3 or partes[0] != "ideal":
        return IDEAL_COLLECT
    _, campo, valor_raw = partes

    # Conversión de tipo según campo
    if campo in ("presupuesto_max", "plazas_min"):
        valor = int(valor_raw)
    else:
        valor = valor_raw

    return await _ideal_guardar_y_continuar(campo, valor, query.message, ctx)


async def _sondear_modelos_viables(
    tamaño: str, presupuesto_max: int, marcas_evitar: list[str],
) -> list[dict]:
    """
    Sondea Wallapop con los modelos del tamaño dado (tabla determinista) y
    devuelve los que tienen al menos 1 anuncio <= presupuesto_max.

    Devuelve list[{marca, modelo, año_min, año_max, precio_min_sondeo}]
    ordenada ASC por precio mínimo encontrado.
    Cacheado 24h por (marca, modelo, presupuesto_max // 2000).
    """
    from scraper import sondear_precio_modelo

    candidatos = TAMANO_A_MODELOS.get(tamaño, [])
    candidatos = [(m, mo) for m, mo in candidatos if m not in marcas_evitar]
    if not candidatos:
        return []

    tramo = (presupuesto_max // 2000) * 2000
    ahora = time.time()

    async def _check(marca: str, modelo: str) -> float | None:
        cache_key = f"{marca}_{modelo}_{tramo}"
        if cache_key in _SONDEO_CACHE:
            ts, precio_min = _SONDEO_CACHE[cache_key]
            if ahora - ts < _SONDEO_TTL_S:
                logger.info(f"[SONDEO] cache hit: {cache_key}")
                return precio_min
        precios = await sondear_precio_modelo(marca, modelo, n=5)
        # Filtro anti-scam: ignorar precios <500€ (chasis, despiece, scams)
        precios_validos = [p for p in precios if p >= 500]
        if not precios_validos:
            _SONDEO_CACHE[cache_key] = (ahora, 0.0)
            return None
        precio_min = precios_validos[0]
        _SONDEO_CACHE[cache_key] = (ahora, precio_min)
        return precio_min

    resultados = await asyncio.gather(
        *(_check(m, mo) for m, mo in candidatos),
        return_exceptions=True,
    )

    viables: list[dict] = []
    for (marca, modelo), res in zip(candidatos, resultados):
        if isinstance(res, Exception) or res is None or res == 0.0:
            continue
        precio_min = res
        if precio_min and precio_min <= presupuesto_max:
            viables.append({
                "marca": marca,
                "modelo": modelo,
                "año_min": 2010,
                "año_max": 2024,
                "motivo": f"hay anuncios desde {precio_min:,.0f}€",
                "precio_min_sondeo": precio_min,
            })

    viables.sort(key=lambda c: c["precio_min_sondeo"])
    return viables


async def _ideal_buscar(source_msg, ctx) -> int:
    """
    Corazón del /ideal: sugiere modelos, scrapea, puntúa y muestra Top 3.
    """
    from datetime import datetime
    from red_flags import detectar_red_flags
    from dgt import calcular_etiqueta_dgt
    from config import PRECIO_MINIMO_VALIDO, ANTI_SCAM_FACTOR
    from ai import validar_anuncios_modelo

    perfil    = ctx.user_data.get("ideal_perfil", {})
    user_id   = ctx.user_data.get("ideal_user_id")
    es_admin  = ctx.user_data.get("ideal_es_admin", False)
    año_actual = datetime.utcnow().year

    # Guardia: presupuesto y tamaño son OBLIGATORIOS para sugerir bien.
    # Si falta alguno, volver al cuestionario.
    faltan = []
    if not perfil.get("presupuesto_max"):
        faltan.append("presupuesto_max")
    if not perfil.get("tamaño"):
        faltan.append("tamaño")
    if faltan:
        ctx.user_data["ideal_huecos"] = faltan
        ctx.user_data["hueco_actual"] = faltan[0]
        await source_msg.reply_text(
            "⚠️ Necesito un dato más para acertar:",
            parse_mode="HTML",
        )
        await source_msg.reply_text(
            _IDEAL_TEXTOS[faltan[0]],
            parse_mode="HTML",
            reply_markup=_ideal_keyboard(faltan[0]),
        )
        return IDEAL_COLLECT

    presup_max_p   = perfil["presupuesto_max"]
    tamaño_p       = perfil["tamaño"]
    marcas_evitar_p = [m.lower() for m in (perfil.get("marcas_evitar") or [])]

    msg = await source_msg.reply_text(
        f"🔎 <b>Sondeando el mercado real…</b>\n"
        f"Buscando {tamaño_p.replace('_', ' ')} hasta {presup_max_p:,}€",
        parse_mode="HTML",
    )

    # 1. Sondeo barato: qué modelos del segmento tienen anuncios <= presupuesto
    viables = await _sondear_modelos_viables(tamaño_p, presup_max_p, marcas_evitar_p)

    if len(viables) < 3:
        nombres = ", ".join(
            f"{v['marca'].title()} {v['modelo'].title()}" for v in viables
        )
        sugerencia_presup = int(presup_max_p * 1.4 // 1000) * 1000
        await msg.edit_text(
            f"⚠️ <b>Pocos modelos viables en {presup_max_p:,}€.</b>\n\n"
            f"Encontré: {html.escape(nombres) if nombres else '<i>(ninguno)</i>'}.\n\n"
            f"Opciones:\n"
            f"• Subir presupuesto a <b>{sugerencia_presup:,}€</b>\n"
            f"• Cambiar a un tamaño más pequeño\n"
            f"• Lanzar /ideal otra vez con otras restricciones",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    # Tomar los 5 más asequibles para el scraping completo
    candidatos = viables[:5]

    candidatos_txt = ", ".join(
        f"{c['marca'].title()} {c['modelo'].title()}" for c in candidatos
    )
    await msg.edit_text(
        f"✅ <b>{len(viables)} modelos viables.</b> Analizando los más asequibles:\n"
        f"{html.escape(candidatos_txt)}\n\n"
        "⏳ Buscando anuncios concretos en Wallapop y Coches.net…",
        parse_mode="HTML",
    )

    # 2. Scraping paralelo
    km_ref = (perfil.get("km_max") or 200_000) // 2
    tareas = [
        buscar_comparables_todas(
            c["marca"], c["modelo"],
            (c["año_min"] + c["año_max"]) // 2,
            km_ref, n=20,
        )
        for c in candidatos
    ]
    resultados = await asyncio.gather(*tareas, return_exceptions=True)

    # 3. Por modelo: filtrar, calcular mediana, puntuar → elegir mejor anuncio
    medianas: dict[str, float] = {}
    presup_max    = perfil.get("presupuesto_max")
    marcas_evitar = [m.lower() for m in (perfil.get("marcas_evitar") or [])]
    _ORDEN_DGT    = {"0": 4, "ECO": 3, "C": 2, "B": 1, "sin etiqueta": 0}
    etiqueta_req  = perfil.get("etiqueta_dgt_min")

    def _score_ad(a, med: float) -> float:
        sc = 0.0
        if med > 0:
            sc += max(0.0, (med - a.precio) / med * 50)
        años_uso = max(1, año_actual - a.año) if a.año > 1990 else 10
        km_año   = a.km / años_uso
        if km_año < IDEAL_KM_AÑO_MAX:
            sc += 10
        sc -= max(0.0, (a.km - 150_000) / 10_000) * 5
        flags = detectar_red_flags(a, None)
        sc -= len(flags) * 15
        if etiqueta_req:
            etiqueta = calcular_etiqueta_dgt(a.motor or "gasolina", a.año)
            if _ORDEN_DGT.get(etiqueta, 0) < _ORDEN_DGT.get(etiqueta_req, 0):
                sc -= 20
        return sc

    # (score_modelo, mejor_anuncio) — uno por modelo
    mejor_por_modelo: list[tuple[float, object]] = []

    for candidato, resultado in zip(candidatos, resultados):
        if isinstance(resultado, Exception) or not resultado:
            continue

        marca_c  = candidato["marca"]
        modelo_c = candidato["modelo"]

        # Filtro absoluto: scam + año + marca evitar
        validos = [
            a for a in resultado
            if a.precio >= PRECIO_MINIMO_VALIDO
            and a.año >= candidato["año_min"] - 1
            and a.año <= candidato["año_max"] + 1
            and a.marca.lower() not in marcas_evitar
        ]
        if not validos:
            continue

        # ── LAYER 0: verificar que el anuncio es el modelo buscado ───────────
        # Primera palabra no-numérica del modelo para matching en título
        _kw_raw = [w for w in modelo_c.lower().split()
                   if not _re.match(r'^[\d.]+$', w) and len(w) >= 3]
        modelo_kw = _kw_raw[0] if _kw_raw else modelo_c.lower()[:5]

        def _es_modelo_correcto(a, _mc=marca_c, _mkw=modelo_kw) -> bool:
            if a.fuente == "wallapop":
                marca_ok = _mc.lower() in a.marca.lower() or a.marca.lower() in _mc.lower()
                titulo_a = (getattr(a, "titulo", "") or "").lower()
                modelo_ok = _mkw in a.modelo.lower() or _mkw in titulo_a
                return marca_ok and modelo_ok
            else:
                titulo_a = (getattr(a, "titulo", "") or "").lower()
                if not titulo_a:
                    return True  # sin título → conservador
                return _mkw in titulo_a or _mc.lower() in titulo_a

        validos_l0 = [a for a in validos if _es_modelo_correcto(a)]
        n_drop_l0 = len(validos) - len(validos_l0)
        if n_drop_l0 > 0:
            logger.info(f"[IDEAL] L0: {marca_c} {modelo_c} → {n_drop_l0} descartados")
        if not validos_l0:
            continue

        # ── LAYER 1: validación IA batch con 8B-instant ──────────────────────
        # Usar modelo_kw (base, sin variante) — Wallapop normaliza la búsqueda
        # al modelo base ("octavia", no "octavia combi"), así que L1 solo debe
        # validar marca+modelo base para capturar coches completamente distintos
        # (Berlingo en búsqueda de Tucson). Las variantes (Combi/SW/Tourer) las
        # gestiona el scoring, no L1.
        if len(validos_l0) > 3:
            try:
                indices_ok = await validar_anuncios_modelo(marca_c, modelo_kw, validos_l0[:15])
                candidatos_ia = [validos_l0[i] for i in indices_ok if i < len(validos_l0)]
                if candidatos_ia:
                    validos_l0 = candidatos_ia
            except Exception as e:
                logger.warning(f"[IDEAL] L1 falló {marca_c} {modelo_c}: {e}. Pass-through.")

        # Mediana del modelo (sin scams, usando anuncios ya validados)
        mediana_modelo = _stats_mod.median([a.precio for a in validos_l0])

        # Anti-scam relativo + rango presupuesto
        umbral_min = max(PRECIO_MINIMO_VALIDO, mediana_modelo * ANTI_SCAM_FACTOR)
        presup_min = presup_max * 0.50 if presup_max else 0
        if presup_max and mediana_modelo >= presup_max * 0.6:
            presup_min = presup_max * 0.65

        anuncios_modelo = [
            a for a in validos_l0
            if a.precio >= umbral_min
            and (not presup_max or a.precio <= presup_max)
            and a.precio >= presup_min
        ]
        if not anuncios_modelo:
            continue

        key = f"{marca_c} {modelo_c}"
        medianas[key] = mediana_modelo

        # Mejor anuncio de este modelo
        mejor = max(anuncios_modelo, key=lambda a: _score_ad(a, mediana_modelo))
        mejor_por_modelo.append((_score_ad(mejor, mediana_modelo), mejor))

    if not mejor_por_modelo:
        await msg.edit_text(
            "😔 No encontré anuncios que encajen con tu perfil.\n"
            "Prueba aumentar el presupuesto o los km máximos."
        )
        return ConversationHandler.END

    # Ordenar modelos por score y tomar top N
    mejor_por_modelo.sort(key=lambda x: x[0], reverse=True)
    top = [ad for _, ad in mejor_por_modelo[:IDEAL_TOP_N]]

    n_modelos = len(mejor_por_modelo)
    await msg.edit_text(
        f"✅ {n_modelos} modelos encontrados. Generando informe…",
        parse_mode="HTML",
    )

    # 5. Veredicto IA
    try:
        veredicto = await generar_veredicto_ideal(perfil, top, medianas)
    except Exception as e:
        logger.error(f"[IDEAL] Error veredicto: {e}")
        veredicto = ""

    # 6. Render
    emojis = ["🥇", "🥈", "🥉"]
    lineas = []
    for i, a in enumerate(top):
        key = f"{a.marca.lower()} {a.modelo.lower()}"
        med = next((v for k, v in medianas.items() if a.marca.lower() in k and a.modelo.lower() in k), 0)
        diff_txt = ""
        if med > 0:
            diff_pct = round((med - a.precio) / med * 100)
            diff_txt = f" <i>({'+' if diff_pct >= 0 else ''}{diff_pct}% vs mercado)</i>"
        lineas.append(
            f"{emojis[i]} <b>{html.escape(a.marca.title())} "
            f"{html.escape(a.modelo.upper())}</b> {a.año}\n"
            f"   📍 {html.escape(a.provincia or 'España')}  ·  "
            f"{a.km:,} km  ·  <b>{a.precio:,.0f}€</b>{diff_txt}\n"
            f"   <a href='{a.url}'>Ver anuncio</a>"
        )

    resultado_txt = (
        "🎯 <b>Tu coche ideal — Top 3</b>\n\n"
        + "\n\n".join(lineas)
        + ("\n\n" + veredicto if veredicto else "")
    )

    ctx.user_data["ideal_urls"] = [a.url for a in top]

    teclado = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"🔍 Analizar #{i + 1}", callback_data=f"ideal_analizar:{i}")
        for i in range(len(top))
    ]])

    await _enviar_largo(
        msg, resultado_txt,
        parse_mode="HTML", disable_web_page_preview=True, reply_markup=teclado,
    )

    if user_id and not es_admin:
        registrar_analisis(user_id)

    return ConversationHandler.END


async def cmd_ideal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    allowed, _tier = _check_access(user.id, user.username or "")
    if not allowed:
        await update.message.reply_text("⛔ No tienes acceso a este bot.")
        return ConversationHandler.END

    get_o_crear_usuario(user.id, user.username or "", user.first_name or "")
    es_admin = user.id in ADMIN_USER_IDS
    puede, restantes = puede_analizar(user.id)
    if es_admin:
        puede, restantes = True, FREE_ANALISIS_MAX
    if not puede:
        mins = minutos_hasta_reset(user.id)
        h, m = divmod(mins, 60)
        cuando = f"{h}h {m}min" if h else f"{m} min"
        await update.message.reply_text(
            f"⛔ <b>Has usado tus {FREE_ANALISIS_MAX} análisis gratuitos.</b>\n\n"
            f"⏳ Tu límite se resetea en <b>{cuando}</b>.",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    # Limpiar estado anterior
    for k in [k for k in ctx.user_data if k.startswith("ideal_") or k == "hueco_actual"]:
        del ctx.user_data[k]
    ctx.user_data["ideal_user_id"] = user.id
    ctx.user_data["ideal_es_admin"] = es_admin

    # Texto libre tras /ideal
    texto = (update.message.text or "").strip()
    if texto.lower().startswith("/ideal"):
        texto = texto[6:].strip()

    if texto:
        msg_parse = await update.message.reply_text("🤖 Entendiendo lo que buscas…")
        try:
            perfil = await parsear_perfil_ideal(texto)
        except Exception:
            perfil = {
                "carrocerias": None, "presupuesto_max": None, "plazas_min": None,
                "uso": None, "combustible": None, "etiqueta_dgt_min": None,
                "duracion_uso": None, "km_max": None, "tamaño": None,
                "cv_min": None, "marcas_evitar": [],
                "huecos": list(_IDEAL_HUECOS_ORDEN),
            }
        await msg_parse.delete()
        await update.message.reply_text("👍 Entendido. Te hago unas preguntas más.")
    else:
        perfil = {
            "carrocerias": None, "presupuesto_max": None, "plazas_min": None,
            "uso": None, "combustible": None, "etiqueta_dgt_min": None,
            "duracion_uso": None, "km_max": None, "tamaño": None,
            "cv_min": None, "marcas_evitar": [],
            "huecos": list(_IDEAL_HUECOS_ORDEN),
        }
        await update.message.reply_text(
            "🎯 <b>Vamos a encontrar tu coche ideal.</b>\n\n"
            "Te hago unas preguntas rápidas. Usa los botones o escribe.",
            parse_mode="HTML",
        )

    ctx.user_data["ideal_perfil"] = perfil
    ctx.user_data["ideal_huecos"] = list(perfil.get("huecos", _IDEAL_HUECOS_ORDEN))

    return await _ideal_avanzar(update.message, ctx)


async def callback_ideal_analizar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Callback para los botones 'Analizar #N' del resultado /ideal."""
    query = update.callback_query
    await query.answer()

    partes = (query.data or "").split(":")
    if len(partes) < 2:
        return
    idx = int(partes[1])
    urls = ctx.user_data.get("ideal_urls", [])
    if idx >= len(urls):
        await query.edit_message_text("⚠️ No encontré la URL del anuncio.")
        return

    url  = urls[idx]
    user = update.effective_user

    get_o_crear_usuario(user.id, user.username or "", user.first_name or "")
    es_admin = user.id in ADMIN_USER_IDS
    puede, _ = puede_analizar(user.id)
    if es_admin:
        puede = True
    if not puede:
        mins = minutos_hasta_reset(user.id)
        h, m = divmod(mins, 60)
        cuando = f"{h}h {m}min" if h else f"{m} min"
        await query.message.reply_text(
            f"⛔ <b>Has agotado tus análisis gratuitos.</b>\n"
            f"⏳ Reset en {cuando}.",
            parse_mode="HTML",
        )
        return

    await _core_analisis(url, query.message, ctx, es_admin, user.id)


# ════════════════════════════════════════════════════════════════════════════
# Callback: preguntas vendedor + checklist (post /analizar)
# ════════════════════════════════════════════════════════════════════════════

async def callback_qa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    eleccion = (query.data or "").split(":", 1)[-1]

    if eleccion == "no":
        await query.edit_message_text("👍 Perfecto, sin preguntas.")
        return

    contexto = ctx.user_data.get("analisis_qa_ctx")
    if not contexto:
        await query.edit_message_text(
            "⚠️ No tengo el contexto del último análisis. Vuelve a lanzar /analizar."
        )
        return

    await query.edit_message_text("⏳ Preparando preguntas y checklist…")
    qa = await preguntas_y_checklist(
        contexto["version_info"],
        contexto["marca"],
        contexto["modelo"],
        averias_resumen=contexto.get("foros", ""),
    )
    if not qa:
        await query.edit_message_text(
            "😔 No pude generar las preguntas en este momento. Inténtalo otra vez."
        )
        return

    texto = formatear_qa(qa)
    await query.edit_message_text(texto, parse_mode="HTML", disable_web_page_preview=True)
    ctx.user_data.pop("analisis_qa_ctx", None)


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

async def error_handler(update, context):
    """Manejador global de errores."""
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)
    
    # Si es un conflicto de polling, informar al usuario
    if hasattr(context.error, 'message') and 'terminated by other getUpdates' in str(context.error):
        logger.critical("⚠️  CONFLICTO DE POLLING: Otra instancia del bot está ejecutándose.")
        logger.critical("   Solución: Detén todos los procesos de Python y vuelve a iniciar.")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Resumen de uso. Solo admins."""
    user = update.effective_user
    if not ADMIN_USER_IDS or user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("⛔ No autorizado.")
        return

    s = resumen_stats()

    top_cmd = "\n".join(
        f"  • /{html.escape(r['comando'])} — {r['usos']} usos · {r['usuarios']} u"
        for r in s["top_comandos"]
    ) or "  (vacío)"

    top_users = "\n".join(
        f"  • <code>{r['user_id']}</code> {html.escape(str(r['nombre']))[:20]} — {r['usos']}"
        for r in s["top_usuarios"]
    ) or "  (vacío)"

    dias = "\n".join(
        f"  • {r['dia']} — {r['usos']} usos · {r['usuarios']} u"
        for r in s["ultimos_dias"]
    ) or "  (vacío)"

    msg = (
        "📊 <b>Stats globales</b>\n\n"
        f"👥 Usuarios: <b>{s['total_usuarios']}</b>  "
        f"(+{s['nuevos_hoy']} hoy · +{s['nuevos_7d']} 7d)\n"
        f"⚡ Eventos: <b>{s['total_eventos']}</b>  ({s['eventos_hoy']} hoy)\n"
        f"🟢 Activos hoy: <b>{s['activos_hoy']}</b>  ·  7d: <b>{s['activos_7d']}</b>\n\n"
        "<b>Top comandos</b>\n"
        f"{top_cmd}\n\n"
        "<b>Top usuarios</b>\n"
        f"{top_users}\n\n"
        "<b>Últimos 7 días</b>\n"
        f"{dias}"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


def main():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN no configurado. Revisa tu archivo .env")
        return
    
    logger.info("🔄 Eliminando webhook anterior (si existe) para evitar conflictos...")
    
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Conversación: búsqueda
    conv_buscar = ConversationHandler(
        entry_points=[CommandHandler("buscar", buscar_start)],
        states={
            ASK_MODELO:          [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_modelo)],
            ASK_PRECIO_OBJETIVO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_precio_objetivo)],
            ASK_FILTROS:         [MessageHandler(filters.TEXT & ~filters.COMMAND, ejecutar_busqueda)],
            SHOW_RESULTS:        [CallbackQueryHandler(callback_resultados)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
    )

    # Conversación: calculadora inversa
    conv_calcular = ConversationHandler(
        entry_points=[CommandHandler("calcular", calcular_start)],
        states={
            CALC_PRECIO:    [MessageHandler(filters.TEXT & ~filters.COMMAND, calc_recibir_precio)],
            CALC_BENEFICIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, calc_recibir_beneficio)],
            CALC_CO2:       [MessageHandler(filters.TEXT & ~filters.COMMAND, calc_recibir_co2)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
    )

    # Logger global de comandos en grupo -1 (corre antes que los handlers reales,
    # no consume el update porque no hace ApplicationHandlerStop)
    async def _log_cmd(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        user = update.effective_user
        if not msg or not msg.text or not user:
            return
        cmd = msg.text.split()[0].lstrip("/").split("@")[0].lower()
        try:
            registrar_evento(user.id, cmd)
        except Exception as e:
            logger.warning(f"[EVENTO] No se pudo registrar: {e}")

    app.add_handler(MessageHandler(filters.COMMAND, _log_cmd), group=-1)

    # Conversación: /ideal
    conv_ideal = ConversationHandler(
        entry_points=[CommandHandler("ideal", cmd_ideal)],
        states={
            IDEAL_COLLECT: [
                CallbackQueryHandler(ideal_recibir_callback, pattern=r"^ideal:[a-zñ_]+:.+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ideal_recibir_texto),
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("analizar", cmd_analizar))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(conv_ideal)
    app.add_handler(CallbackQueryHandler(callback_ideal_analizar, pattern=r"^ideal_analizar:\d+$"))
    # Ocultos en beta — código intacto, solo sin handler en Telegram:
    # app.add_handler(CommandHandler("misiones", mis_misiones))
    # app.add_handler(conv_buscar)
    # app.add_handler(conv_calcular)
    # app.add_handler(CallbackQueryHandler(callback_misiones, pattern=r"^(pausar|activar|eliminar)_\d+$"))
    app.add_handler(CallbackQueryHandler(callback_qa, pattern=r"^qa:(si|no)$"))
    
    # Manejador global de errores
    app.add_error_handler(error_handler)

    logger.info("🎯 German Sniper Bot v3 iniciado")
    logger.info("  Fuentes DE: AutoScout24 + mobile.de | Fuentes ES: Wallapop + coches.net")
    logger.info("  Features: Sniper Score, Calculadora Inversa, Modo Sniper, Tiers")
    
    # drop_pending_updates=True descarta actualizaciones pendientes para evitar conflictos
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
