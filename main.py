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
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes,
)

from config import TELEGRAM_TOKEN, TOP_RESULTS, MIN_BENEFICIO, ALLOWED_USER_IDS
from ai import (
    parsear_filtros_nl, parsear_modelo_nl, enriquecer_coches,
    texto_analisis, validar_precio_mercado, filtrar_por_extras,
    generar_veredicto_analizar,
)
from database import (
    init_db, crear_mision, eliminar_mision,
    obtener_misiones_usuario, pausar_mision, activar_mision,
    registrar_usuario, obtener_tier,
    guardar_historico_batch,
)
from scraper import (
    buscar_y_cruzar, buscar_coches_alemania,
    obtener_anuncio_wallapop, buscar_comparables_wallapop,
)
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
    allowed, tier = _check_access(user.id, user.username or "")
    if not allowed:
        await update.message.reply_text("⛔ No tienes acceso a este bot.")
        return

    tier_emoji = {"free": "🆓", "pro": "⭐", "sniper": "🎯", "admin": "👑"}.get(tier, "🆓")

    await update.message.reply_text(
        f"🎯 <b>German Sniper Bot v3</b>\n"
        f"{tier_emoji} Tu plan: <b>{tier.upper()}</b>\n\n"
        f"Busco coches en <b>AutoScout24</b> y <b>mobile.de</b>, calculo el coste "
        f"real de importación y lo cruzo con precios de <b>Wallapop</b> y <b>coches.net</b> "
        f"para encontrar las mejores oportunidades.\n\n"
        f"<b>Comandos:</b>\n"
        f"• /analizar &lt;url&gt; — Analiza un anuncio de Wallapop\n"
        f"• /buscar — Busca coches en Alemania para importar\n"
        f"• /calcular — Calculadora inversa (¿precio máx en DE?)\n"
        f"• /misiones — Ver misiones activas\n"
        f"• /plan — Ver tu plan y límites\n"
        f"• /cancelar — Cancelar operación en curso",
        parse_mode="HTML",
    )


# ════════════════════════════════════════════════════════════════════════════
# /plan — ver tier y límites
# ════════════════════════════════════════════════════════════════════════════

async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    allowed, tier = _check_access(user.id, user.username or "")
    if not allowed:
        await update.message.reply_text("⛔ No tienes acceso a este bot.")
        return

    limits = TIER_LIMITS.get(tier, TIER_LIMITS["free"])
    busq = limits["busquedas_dia"]
    mis = limits["misiones"]
    sniper = "✅" if limits["sniper"] else "❌"

    await update.message.reply_text(
        f"📋 <b>Tu plan: {tier.upper()}</b>\n\n"
        f"🔍 Búsquedas/día: <b>{'ilimitadas' if busq == -1 else busq}</b>\n"
        f"📡 Misiones activas: <b>{'ilimitadas' if mis == -1 else mis}</b>\n"
        f"🎯 Modo Sniper (alertas 3 min): {sniper}\n\n"
        f"{'─' * 30}\n"
        f"<b>Planes disponibles:</b>\n"
        f"🆓 <b>FREE</b> — 3 búsquedas/día, 1 misión\n"
        f"⭐ <b>PRO</b> — 50 búsquedas/día, 5 misiones\n"
        f"🎯 <b>SNIPER</b> — Ilimitado + alertas cada 3 min\n\n"
        f"Para cambiar de plan, contacta al admin.",
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

    # Extraer URL del mensaje (funciona con /analizar <url> y texto libre)
    texto = update.message.text or ""
    url_match = re.search(r"https?://[^\s]+wallapop[^\s]+", texto)
    if not url_match:
        await update.message.reply_text(
            "🔍 Pégame la URL del anuncio de Wallapop.\n"
            "Ej: <code>/analizar https://es.wallapop.com/item/seat-ibiza-123456789</code>",
            parse_mode="HTML",
        )
        return

    url = url_match.group(0).rstrip(")")
    msg = await update.message.reply_text("⏳ Extrayendo datos del anuncio…")

    # ── 1. Extraer anuncio objetivo ──────────────────────────────────────────
    try:
        anuncio = await obtener_anuncio_wallapop(url)
    except Exception as e:
        logger.error(f"[BOT] Error extrayendo anuncio: {e}")
        anuncio = None

    if not anuncio or anuncio.precio <= 0:
        await msg.edit_text(
            "😔 No pude extraer los datos del anuncio.\n"
            "• Comprueba que la URL sea de Wallapop y el anuncio siga activo.\n"
            "• A veces Wallapop bloquea temporalmente. Prueba en 1 min."
        )
        return

    marca  = anuncio.marca  or "desconocida"
    modelo = anuncio.modelo or "desconocido"
    año    = anuncio.año    or 0
    km     = anuncio.km     or 0

    await msg.edit_text(
        f"✅ Anuncio encontrado: <b>{marca.title()} {modelo.upper()}</b> "
        f"{año} · {km:,} km · <b>{anuncio.precio:,.0f}€</b>\n\n"
        f"⏳ Buscando comparables en Wallapop…",
        parse_mode="HTML",
    )

    # ── 2. Buscar comparables ────────────────────────────────────────────────
    try:
        comparables = await buscar_comparables_wallapop(marca, modelo, año, km, n=30)
    except Exception as e:
        logger.error(f"[BOT] Error buscando comparables: {e}")
        comparables = []

    # Guardar anuncio objetivo + comparables en histórico
    todos_para_hist = [anuncio] + comparables
    try:
        guardar_historico_batch(todos_para_hist)
    except Exception as e:
        logger.warning(f"[BOT] Error guardando histórico: {e}")

    # ── 3. Estadística de mercado ────────────────────────────────────────────
    from models import EstadisticaMercado

    precios_comp = [c.precio for c in comparables if c.precio > 0]

    if len(precios_comp) < 3:
        await msg.edit_text(
            f"⚠️ Solo encontré {len(precios_comp)} comparable(s) para "
            f"<b>{marca.title()} {modelo.upper()}</b> con esos parámetros.\n"
            f"No hay datos suficientes para un veredicto fiable. Prueba un modelo más común.",
            parse_mode="HTML",
        )
        return

    mediana   = _stats.median(precios_comp)
    media     = _stats.mean(precios_comp)
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

    # ── 4. Veredicto IA ──────────────────────────────────────────────────────
    try:
        veredicto = await generar_veredicto_analizar(anuncio, stats)
    except Exception as e:
        logger.error(f"[BOT] Error generando veredicto: {e}")
        veredicto = f"⚠️ No pude generar veredicto IA.\nPrecio: {anuncio.precio:,.0f}€ · Mediana: {stats.mediana:,.0f}€"

    cabecera = (
        f"🔍 <b>{marca.title()} {modelo.upper()} {año}</b>\n"
        f"📍 {anuncio.provincia or 'España'}  ·  {km:,} km  ·  "
        f"<a href='{url}'>Ver anuncio</a>\n"
        f"{'─' * 30}\n\n"
    )

    await msg.edit_text(
        cabecera + veredicto,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ════════════════════════════════════════════════════════════════════════════
# /cancelar
# ════════════════════════════════════════════════════════════════════════════

async def cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Operación cancelada.")
    return ConversationHandler.END


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

def main():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN no configurado. Revisa tu archivo .env")
        return
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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("misiones", mis_misiones))
    app.add_handler(CommandHandler("analizar", cmd_analizar))
    app.add_handler(conv_buscar)
    app.add_handler(conv_calcular)
    app.add_handler(CallbackQueryHandler(callback_misiones, pattern=r"^(pausar|activar|eliminar)_\d+$"))

    logger.info("🎯 German Sniper Bot v3 iniciado")
    logger.info("  Fuentes DE: AutoScout24 + mobile.de | Fuentes ES: Wallapop + coches.net")
    logger.info("  Features: Sniper Score, Calculadora Inversa, Modo Sniper, Tiers")
    app.run_polling()


if __name__ == "__main__":
    main()
