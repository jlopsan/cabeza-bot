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
import html
import logging
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes,
)

from config import TELEGRAM_TOKEN, TOP_RESULTS, MIN_BENEFICIO, ALLOWED_USER_IDS
from ai import (
    parsear_filtros_nl, parsear_modelo_nl, enriquecer_coches,
    texto_analisis, validar_precio_mercado, filtrar_por_extras,
    generar_veredicto_analizar, preguntas_y_checklist, formatear_qa,
    cache_get, cache_set,
)
from database import (
    init_db, crear_mision, eliminar_mision,
    obtener_misiones_usuario, pausar_mision, activar_mision,
    registrar_usuario, obtener_tier,
    guardar_historico_batch,
    get_o_crear_usuario, puede_analizar, registrar_analisis, minutos_hasta_reset,
    registrar_evento,
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

    if ALLOWED_USER_IDS and user.id in ALLOWED_USER_IDS:
        cuerpo = "🔓 Acceso ilimitado (beta).\n\n"
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
# /analizar <url> — semana 1
# ════════════════════════════════════════════════════════════════════════════

async def cmd_analizar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    import re
    import statistics as _stats

    user = update.effective_user
    allowed, _tier = _check_access(user.id, user.username or "")
    if not allowed:
        await update.message.reply_text("⛔ No tienes acceso a este bot.")
        return

    # ── Gating freemium: 3 análisis cada FREE_VENTANA_HORAS ────────────────
    get_o_crear_usuario(user.id, user.username or "", user.first_name or "")
    puede, restantes = puede_analizar(user.id)
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

    # Extraer URL del mensaje (funciona con /analizar <url> y texto libre)
    texto = update.message.text or ""
    # Acepta URLs tipo:
    #   https://es.wallapop.com/item/...
    #   https://wallapop.com/item/...        (compartir desde app móvil)
    #   http(s)://(cualquier_subdominio.)wallapop.(com|es)/...
    url_match = re.search(
        r"https?://(?:[\w-]+\.)*(?:wallapop\.[a-z]{2,}|coches\.net)/\S+",
        texto,
        re.IGNORECASE,
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

    # ── B3: caché 30 min por URL ─────────────────────────────────────────────
    cached = cache_get(url)
    if cached:
        veredicto_cache, contexto_cache, mins_ago = cached
        msg = await update.message.reply_text("⏳ Recuperando análisis…")
        prefijo = f"<i>♻️ Análisis cacheado hace {mins_ago} min</i>\n\n"
        await _enviar_largo(msg, prefijo + veredicto_cache,
                            parse_mode="HTML", disable_web_page_preview=True)
        if contexto_cache:
            ctx.user_data["analisis_qa_ctx"] = contexto_cache
            teclado = InlineKeyboardMarkup([[
                InlineKeyboardButton("💬 Sí, dame preguntas + checklist", callback_data="qa:si"),
                InlineKeyboardButton("No, gracias", callback_data="qa:no"),
            ]])
            await update.message.reply_text(
                "¿Quieres que te prepare <b>preguntas para el vendedor</b> y un "
                "<b>checklist</b> para cuando vayas a verlo en persona?",
                parse_mode="HTML", reply_markup=teclado,
            )
        return

    msg = await update.message.reply_text("⏳ Extrayendo datos del anuncio…")
    try:
        # ── 1. Extraer anuncio objetivo ──────────────────────────────────────
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

        # ── 2. Buscar comparables (multi-fuente) ─────────────────────────────
        try:
            comparables = await buscar_comparables_todas(marca, modelo, año, km, n=30)
        except Exception as e:
            logger.error(f"[BOT] Error buscando comparables: {e}")
            comparables = []

        comparables = [c for c in comparables if c.item_id != anuncio.item_id]
        fuentes_count = dict(Counter(c.fuente for c in comparables))
        logger.info(f"[BOT] Comparables por fuente: {fuentes_count}")

        # A6: filtrar histórico (precio>0, año>1990)
        historico = [a for a in ([anuncio] + comparables) if a.precio > 0 and a.año > 1990]
        try:
            guardar_historico_batch(historico)
        except Exception as e:
            logger.warning(f"[BOT] Error guardando histórico: {e}")

        # ── 3. Estadística de mercado ────────────────────────────────────────
        from models import EstadisticaMercado

        precios_comp = [c.precio for c in comparables if c.precio > 0]

        if len(precios_comp) < 3:
            await msg.edit_text(
                f"⚠️ Solo encontré {len(precios_comp)} comparable(s) para "
                f"<b>{html.escape(marca.title())} {html.escape(modelo.upper())}</b> con esos parámetros.\n"
                f"No hay datos suficientes para un veredicto fiable. Prueba un modelo más común.",
                parse_mode="HTML",
            )
            return

        mediana    = _stats.median(precios_comp)
        media      = _stats.mean(precios_comp)
        desviacion = _stats.stdev(precios_comp) if len(precios_comp) > 1 else 0.0
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

        # ── 4. Veredicto IA ──────────────────────────────────────────────────
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

        # Guardar en caché (B3)
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

        # Registrar consumo SOLO al terminar el análisis con éxito
        registrar_analisis(user.id)

        # ── 5. Oferta opcional: preguntas vendedor + checklist ───────────────
        if contexto_qa:
            ctx.user_data["analisis_qa_ctx"] = contexto_qa
            teclado = InlineKeyboardMarkup([[
                InlineKeyboardButton("💬 Sí, dame preguntas + checklist", callback_data="qa:si"),
                InlineKeyboardButton("No, gracias", callback_data="qa:no"),
            ]])
            await update.message.reply_text(
                "¿Quieres que te prepare <b>preguntas para el vendedor</b> y un "
                "<b>checklist</b> para cuando vayas a verlo en persona?",
                parse_mode="HTML",
                reply_markup=teclado,
            )

    except Exception:
        logger.error("[BOT] Excepción no capturada en cmd_analizar", exc_info=True)
        try:
            await msg.edit_text("😔 Algo se rompió en el análisis. Reintenta en 1 min.")
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════
# /cancelar
# ════════════════════════════════════════════════════════════════════════════

async def cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Operación cancelada.")
    return ConversationHandler.END


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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("analizar", cmd_analizar))
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
