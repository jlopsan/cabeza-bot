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
from config import STRIPE_API_KEY, STRIPE_PRICE_PACK_30, STRIPE_PRICE_PACK_100
from permisos import requiere_acceso
from ai import (
    parsear_filtros_nl, parsear_modelo_nl, enriquecer_coches,
    texto_analisis, validar_precio_mercado, filtrar_por_extras,
    generar_veredicto_analizar, preguntas_y_checklist, formatear_qa,
    cache_get, cache_set,
    parsear_perfil_ideal, validar_anuncios_modelo,
    investigar_coche, generar_veredicto_ideal,
    brainstorm_candidatos_ideal, seleccionar_top3_con_investigacion,
)
from ideal_pipeline import (
    nueva_sesion, get_sesion, reset_sesion, set_sesion,
    alimentar_slots, ejecutar_pipeline, fase_segunda_ronda,
)
import comparar_pipeline
from ideal_schema import generar_preguntas_clarificacion
from database import (
    init_db, crear_mision, eliminar_mision,
    obtener_misiones_usuario, pausar_mision, activar_mision,
    registrar_usuario, obtener_tier, obtener_usuario,
    guardar_historico_batch,
    get_o_crear_usuario, puede_analizar, registrar_analisis, minutos_hasta_reset,
    registrar_evento, resumen_stats,
    puede_usar, registrar_uso,
)
from config import FREE_ANALISIS_MAX, FREE_VENTANA_HORAS, FREE_CREDITOS_DIA, PAID_CREDITOS_PACK_30, PAID_CREDITOS_PACK_100
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
        "Soy el bot de <b>Juan Lopera — Coches con cabeza</b>.\n\n"
        "Analizo anuncios de coches usados en España en tiempo real: "
        "precio vs mercado, red flags, etiqueta DGT, historial del modelo.\n\n"
        "Estoy en construcción pública. Cada semana una función nueva.\n\n"
        f"Tienes <b>{FREE_CREDITOS_DIA} acciones gratuitas al día</b> para empezar.\n\n"
        "/analizar &lt;url&gt; — Analiza un anuncio de Wallapop o Coches.net\n"
        "/ideal — Encuentra tu coche ideal\n"
        "/plan — Ver tu uso y plan\n\n"
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

    if user.id in ADMIN_USER_IDS:
        cuerpo = "🔓 Acceso ilimitado (admin).\n\n"
    else:
        u = obtener_usuario(user.id) or {}
        tier = u.get("tier", "free")
        creditos = u.get("creditos_disponibles", 0) or 0

        if tier == "pro":
            cuerpo = "🚀 <b>Plan PRO</b> — acciones ilimitadas.\n\n"
        elif tier == "paid":
            cuerpo = (
                f"📦 <b>Pack activo</b>\n"
                f"Acciones disponibles: <b>{creditos}</b> (sin caducidad)\n\n"
            )
        else:
            usados = max(FREE_CREDITOS_DIA - creditos, 0)
            mins = minutos_hasta_reset(user.id)
            if mins <= 0:
                cuando = "medianoche UTC"
            else:
                h, m = divmod(mins, 60)
                cuando = f"{h}h {m}min" if h else f"{m} min"
            cuerpo = (
                f"🆓 <b>Plan FREE</b>\n"
                f"Acciones de hoy: <b>{usados}/{FREE_CREDITOS_DIA}</b>\n"
                f"⏳ Reset en: <b>{cuando}</b>\n\n"
                f"💳 Pack {PAID_CREDITOS_PACK_30} acciones — 4.90€ (sin caducidad)\n"
                f"💎 Pack {PAID_CREDITOS_PACK_100} acciones — 9.90€ (sin caducidad)\n\n"
            )

    await update.message.reply_text(
        "📋 <b>Tu plan</b>\n\n"
        f"{cuerpo}"
        "Actualizaciones del bot en:\n"
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

_ANALISIS_INFLIGHT: dict[str, asyncio.Future] = {}


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

    # Dedupe inflight: si otra corutina ya está analizando esta URL, esperar a
    # que termine y leer del caché. Evita scraping + IA duplicados.
    inflight = _ANALISIS_INFLIGHT.get(url)
    if inflight is not None:
        msg = await source_msg.reply_text("⏳ Otra petición ya analiza esta URL. Esperando…")
        try:
            await inflight
        except Exception:
            pass
        cached = cache_get(url)
        if cached:
            veredicto_cache, contexto_cache, mins_ago = cached
            prefijo = "<i>♻️ Compartido con otra petición simultánea</i>\n\n"
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
        await msg.edit_text("😔 El análisis paralelo no terminó bien. Vuelve a intentarlo.")
        return

    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    _ANALISIS_INFLIGHT[url] = fut

    msg = await source_msg.reply_text("⏳ Extrayendo datos del anuncio…")
    try:
        try:
            anuncio = await obtener_anuncio_por_url(url)
        except Exception as e:
            logger.error(f"[BOT] Error extrayendo anuncio: {e}")
            anuncio = None

        if not anuncio or anuncio.precio <= 0:
            if "coches.net" in url.lower():
                detalle = (
                    "• Coches.net a veces bloquea scrapers. Prueba en 1 min.\n"
                    "• Asegúrate de que el anuncio sigue activo."
                )
            else:
                detalle = (
                    "• Wallapop a veces bloquea temporalmente. Prueba en 1 min.\n"
                    "• Comprueba que la URL sea válida y el anuncio siga activo."
                )
            await msg.edit_text(f"😔 No pude extraer los datos del anuncio.\n{detalle}")
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
    finally:
        if not fut.done():
            fut.set_result(None)
        _ANALISIS_INFLIGHT.pop(url, None)


# ════════════════════════════════════════════════════════════════════════════
# /analizar <url> — semana 1
# ════════════════════════════════════════════════════════════════════════════

@requiere_acceso("/analizar")
async def cmd_analizar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    allowed, _tier = _check_access(user.id, user.username or "")
    if not allowed:
        await update.message.reply_text("⛔ No tienes acceso a este bot.")
        return

    es_admin = user.id in ADMIN_USER_IDS

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
    extra_modelos: list[tuple[str, str]] | None = None,
) -> list[dict]:
    """
    Sondea Wallapop con los modelos del tamaño dado (tabla determinista) +
    los `extra_modelos` extraídos de foros (Tavily). Devuelve los que tienen
    al menos 1 anuncio <= presupuesto_max.

    Devuelve list[{marca, modelo, año_min, año_max, precio_min_sondeo}]
    ordenada ASC por precio mínimo encontrado.
    Cacheado 24h por (marca, modelo, presupuesto_max // 2000).
    """
    from scraper import sondear_precio_modelo

    semilla = TAMANO_A_MODELOS.get(tamaño, [])
    universo = list(semilla)
    if extra_modelos:
        seen = {(m, mo) for m, mo in semilla}
        for m, mo in extra_modelos:
            if (m, mo) not in seen:
                universo.append((m, mo))
                seen.add((m, mo))
    candidatos = [(m, mo) for m, mo in universo if m not in marcas_evitar]
    if not candidatos:
        return []
    logger.info(
        f"[SONDEO] universo: {len(semilla)} hardcoded + "
        f"{len(extra_modelos or [])} foros → {len(candidatos)} a sondear"
    )

    tramo = (presupuesto_max // 2000) * 2000
    ahora = time.time()

    async def _check(marca: str, modelo: str) -> float | None:
        cache_key = f"{marca}_{modelo}_{tramo}"
        if cache_key in _SONDEO_CACHE:
            ts, precio_min = _SONDEO_CACHE[cache_key]
            if ahora - ts < _SONDEO_TTL_S:
                logger.info(f"[SONDEO] cache hit: {cache_key}")
                return precio_min
        precios = await sondear_precio_modelo(marca, modelo, n=8)
        # Filtro anti-despiece: el suelo es 25% del presupuesto o 1500€,
        # lo que sea mayor. Esto evita que un Civic con anuncios de despiece a
        # 2000€ aparezca como "viable" en presupuestos de 8k+ urbano.
        suelo = max(1_500, presupuesto_max * 0.25)
        precios_validos = [p for p in sorted(precios) if p >= suelo]
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
    Corazón del /ideal con flujo IA-first:
      1. IA brainstorm de candidatos (sin Wallapop, solo conocimiento).
      2. Investigación Tavily real por cada candidato (foros + fiabilidad).
      3. IA elige top 3 con comentario detallado citando la investigación.
      4. Wallapop solo al final: buscar mejor anuncio para cada top 3.
      5. Veredicto comparativo final con todo el contexto.
    """
    from datetime import datetime
    from statistics import median
    from scraper import ScraperWallapop
    from red_flags import detectar_red_flags

    perfil   = ctx.user_data.get("ideal_perfil", {})
    user_id  = ctx.user_data.get("ideal_user_id")
    es_admin = ctx.user_data.get("ideal_es_admin", False)

    faltan = []
    if not perfil.get("presupuesto_max"):
        faltan.append("presupuesto_max")
    if not perfil.get("tamaño"):
        faltan.append("tamaño")
    if faltan:
        ctx.user_data["ideal_huecos"] = faltan
        ctx.user_data["hueco_actual"] = faltan[0]
        await source_msg.reply_text("⚠️ Necesito un dato más para acertar:", parse_mode="HTML")
        await source_msg.reply_text(
            _IDEAL_TEXTOS[faltan[0]], parse_mode="HTML",
            reply_markup=_ideal_keyboard(faltan[0]),
        )
        return IDEAL_COLLECT

    presup_max_p    = perfil["presupuesto_max"]
    tamaño_p        = perfil["tamaño"]
    marcas_evitar_p = [m.lower() for m in (perfil.get("marcas_evitar") or [])]

    msg = await source_msg.reply_text(
        f"🤖 <b>Pensando qué modelos encajan…</b>\n"
        f"<i>{tamaño_p.replace('_', ' ')} hasta {presup_max_p:,}€</i>",
        parse_mode="HTML",
    )

    # ── PASO 1: IA brainstorm de candidatos (sin Wallapop) ────────────────
    candidatos = await brainstorm_candidatos_ideal(perfil, n=8)
    if not candidatos:
        # Fallback: usar la tabla hardcoded del segmento
        logger.warning("[IDEAL] brainstorm IA vacío, usando hardcoded")
        candidatos = [
            {"marca": m, "modelo": mo, "motor": "",
             "año_ini": 2014, "año_fin": 2020, "razon": ""}
            for m, mo in TAMANO_A_MODELOS.get(tamaño_p, [])[:8]
            if m not in marcas_evitar_p
        ]
    if not candidatos:
        await msg.edit_text(
            "⚠️ <b>No encontré candidatos para tu perfil.</b>\n"
            "Prueba a relajar restricciones (ej: subir presupuesto o ampliar segmento).",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    # Filtrar candidatos por marcas_evitar
    candidatos = [c for c in candidatos if c["marca"] not in marcas_evitar_p]

    cand_txt = ", ".join(f"{c['marca'].title()} {c['modelo'].title()}" for c in candidatos[:8])
    await msg.edit_text(
        f"✅ <b>{len(candidatos)} candidatos identificados.</b>\n"
        f"<i>{html.escape(cand_txt)}</i>\n\n"
        "🧠 Investigando fiabilidad real de cada uno en foros y comparativas…",
        parse_mode="HTML",
    )

    # ── PASO 2: Investigación Tavily PARALELA por candidato ────────────────
    async def _investigar_cand(c: dict) -> tuple[str, dict]:
        año_medio = ((_to_int(c.get("año_ini"), 2017) + _to_int(c.get("año_fin"), 2020)) // 2) or 2018
        try:
            datos = await investigar_coche(
                {"version": c.get("motor", "")},
                c["marca"], c["modelo"], año_medio,
            )
        except Exception as e:
            logger.warning(f"[IDEAL] investigar_coche {c['marca']} {c['modelo']}: {e}")
            datos = {}
        return f"{c['marca']} {c['modelo']}", datos

    investigacion: dict = {}
    try:
        invs = await asyncio.gather(
            *(_investigar_cand(c) for c in candidatos), return_exceptions=True
        )
        for r in invs:
            if isinstance(r, Exception):
                continue
            clave, datos = r
            if datos:
                investigacion[clave] = datos
    except Exception as e:
        logger.warning(f"[IDEAL] gather investigación falló: {e}")

    # ── PASO 3: IA elige TOP 3 con comentario detallado citando investigación ─
    await msg.edit_text(
        "✍️ <b>Eligiendo las 3 mejores opciones…</b>\n"
        "<i>Comparando con datos reales de foros y fiabilidad.</i>",
        parse_mode="HTML",
    )
    configs = await seleccionar_top3_con_investigacion(perfil, candidatos, investigacion)
    if not configs:
        logger.warning("[IDEAL] seleccionar_top3 vacío, usando primeros 3 candidatos")
        configs = [
            {**c, "comentario": c.get("razon") or "Candidato compatible con tu perfil."}
            for c in candidatos[:3]
        ]

    await msg.edit_text(
        "🔍 <b>Buscando el mejor anuncio de ejemplo en Wallapop…</b>\n"
        "<i>Filtrando por descripción, fotos y precio razonable.</i>",
        parse_mode="HTML",
    )

    # 3. Por cada config, buscar un anuncio de ejemplo de calidad
    km_max_ejemplo = min(180_000, presup_max_p * 15)
    # Suelo de precio: 65% del presupuesto. El usuario quiere ejemplos en su rango,
    # no chollos sospechosos. Para 20k → mínimo 13k.
    precio_min_ej = max(2_000, presup_max_p * 0.65)
    # Banda objetivo: 75-95% del presupuesto (donde están los buenos ejemplos)
    precio_obj_min = presup_max_p * 0.75
    precio_obj_max = presup_max_p * 0.95
    año_actual = datetime.utcnow().year

    # Filtro de combustible: si el usuario lo especificó, vetar el resto
    comb_user = perfil.get("combustible")
    if isinstance(comb_user, str):
        comb_user = [comb_user]
    comb_user_set = {c.lower() for c in (comb_user or [])}

    _COMB_PATRONES = {
        "diesel":    ["diesel", "diésel", "tdi", "hdi", "dci", "cdti", "tdci",
                      "bluetec", "blue hdi", "bluehdi", "gasoil", "gasoleo", "gasóleo"],
        "gasolina":  ["gasolin", "petrol", "tsi", "tfsi", "puretech", " vti",
                      " thp", " mpi", " gdi", " fsi"],
        "hibrido":   ["hibrid", "hybrid", "híbrid", " hev", "phev", "self charg"],
        "electrico": ["electric", "eléctric", " ev ", " bev"],
        "glp":       ["glp", "gnc", "autogas", " lpg"],
    }

    def _detectar_combustible(a) -> str:
        txt = f" {(a.motor or '').lower()} {(getattr(a, 'titulo', '') or '').lower()} "
        for comb in ("hibrido", "electrico", "diesel", "glp", "gasolina"):
            if any(p in txt for p in _COMB_PATRONES[comb]):
                return comb
        return ""

    def _score_ejemplo(a, cfg: dict, med: float) -> float:
        sc = 0.0
        # Mediana del modelo: priorizar ofertas ligeramente por debajo (5-20%)
        if med > 0:
            diff_pct = (med - a.precio) / med  # >0 → más barato que mediana
            if 0.05 <= diff_pct <= 0.20:
                sc += 50  # mejor zona: chollo razonable
            elif -0.05 <= diff_pct < 0.05:
                sc += 30  # en mediana
            elif 0.20 < diff_pct <= 0.35:
                sc += 15  # algo barato (sospechoso)
            elif diff_pct > 0.35:
                sc -= 30  # demasiado barato → red flag de scam
            else:
                sc -= 10  # más caro que mediana

        # Banda de presupuesto (secundario)
        if precio_obj_min <= a.precio <= precio_obj_max:
            sc += 15

        años_uso = max(1, año_actual - a.año) if a.año > 1990 else 10
        km_año = a.km / años_uso
        if 7_000 <= km_año <= 25_000:
            sc += 20
        elif km_año <= 30_000:
            sc += 10
        sc -= max(0.0, (a.km - 100_000) / 10_000) * 3

        # Premiar año más reciente dentro del rango
        if a.año >= cfg.get("año_ini", 2010):
            sc += (a.año - cfg.get("año_ini", 2010)) * 2

        # Red flags deterministas
        try:
            flags = detectar_red_flags(a, None)
            sc -= len(flags) * 25
        except Exception:
            pass

        return sc

    def _to_int(v, default: int) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    # Tokens técnicos comunes para detectar coincidencia de motor
    _TECH_TOKENS = [
        "tsi", "tfsi", "tdi", "hdi", "dci", "cdti", "tdci", "bluehdi", "blue hdi",
        "puretech", "vti", "thp", "mpi", "gdi", "fsi", "ecoboost", "vvt-i", "vvti",
        "vvt", "dualjet", "skyactiv", "dynamic force", "turbo", "ecotec",
        "hybrid", "hibrid", "phev", "hev", "self charg", "mhev", "e-tech",
        "crdi", "drive-e", "eat", "dsg",
    ]

    def _tokens_motor_cfg(motor_cfg: str) -> tuple[str, list[str]]:
        """Extrae cilindrada (ej '1.33') y tokens técnicos (ej ['vvt-i'])."""
        m = (motor_cfg or "").lower().strip()
        cil_match = _re.search(r'\b(\d\.\d{1,2})\b', m)
        cilindrada = cil_match.group(1) if cil_match else ""
        tech = [t for t in _TECH_TOKENS if t in m]
        return cilindrada, tech

    # Familias incompatibles: si la cfg está en una y el ad en otra, descartar.
    # Nota: las familias del AD incluyen palabras genéricas (diesel, gasolina) porque
    # Wallapop devuelve el motor como "Diesel 110.0" / "Gasolina 130.0".
    _MOTOR_FAMILIAS = {
        "diesel":   ["tdi", "hdi", "dci", "cdti", "tdci", "bluehdi", "blue hdi", "crdi",
                     "diesel", "diésel", "gasoil", "gasoleo", "gasóleo"],
        "gas":      ["tsi", "tfsi", "puretech", "vti", "thp", "mpi", "gdi", "fsi",
                     "ecoboost", "vvt-i", "vvti", "vvt", "dualjet", "skyactiv",
                     "gasolina", "petrol"],
        "hibrido":  ["hybrid", "hibrid", "híbrid", "phev", "hev", "self charg", "mhev", "e-tech"],
    }

    def _familia(motor_str: str) -> str:
        m = (motor_str or "").lower()
        for fam, toks in _MOTOR_FAMILIAS.items():
            if any(t in m for t in toks):
                return fam
        return ""

    def _motor_match(a, cilindrada: str, tech: list[str]) -> bool:
        """
        Rechaza solo si el anuncio CONTRADICE la cfg.
        - Cilindrada distinta detectada en el ad → rechazar.
        - Familia tech incompatible (cfg=gas vs ad=diesel) → rechazar.
        - Sin contradicción evidente → aceptar (Wallapop suele omitir esos datos).
        """
        if not cilindrada and not tech:
            return True  # cfg sin motor concreto → no filtrar

        txt = f" {(a.motor or '').lower()} {(getattr(a, 'titulo', '') or '').lower()} "

        # 1) Cilindrada contradictoria: el ad menciona una distinta a la cfg
        if cilindrada:
            cils_ad = _re.findall(r'\b(\d\.\d{1,2})\b', txt)
            if cils_ad and cilindrada not in cils_ad:
                return False  # ad declara otra cilindrada distinta

        # 2) Familia tech incompatible: cfg gasolina vs ad TDI, o cfg TSI vs ad híbrido
        fam_cfg = _familia(" ".join(tech)) if tech else ""
        fam_ad  = _familia(txt)
        if fam_cfg and fam_ad and fam_cfg != fam_ad:
            return False

        return True

    async def _buscar_ejemplo(cfg: dict) -> tuple:
        """Devuelve (Anuncio | None, mediana_modelo)."""
        kw = f"{cfg['marca'].title()} {cfg['modelo'].title()}"
        try:
            items = await ScraperWallapop().buscar_items(kw, año=0, km=0, n=25, order_by="newest")
        except Exception as e:
            logger.warning(f"[IDEAL] ejemplo {kw}: {e}")
            return None, 0.0
        if not items:
            logger.info(f"[IDEAL] ejemplo {kw}: 0 items de Wallapop")
            return None, 0.0

        # Validador IA: descartar items que no son el modelo buscado
        try:
            indices_validos = await validar_anuncios_modelo(
                cfg["marca"], cfg["modelo"], items[:15],
            )
            if indices_validos:
                items = [items[i] for i in indices_validos if i < len(items)] + items[15:]
        except Exception as e:
            logger.warning(f"[IDEAL] validador IA falló para {kw}: {e}")

        año_ini = _to_int(cfg.get("año_ini"), 2010)
        año_fin = _to_int(cfg.get("año_fin"), año_actual)
        año_min_abs = año_ini - 5
        año_max_abs = año_fin + 3

        cilindrada, tech = _tokens_motor_cfg(cfg.get("motor", ""))

        def _comb_ok(a) -> bool:
            if not comb_user_set:
                return True
            det = _detectar_combustible(a)
            return (not det) or det in comb_user_set

        def _mot_ok(a) -> bool:
            return _motor_match(a, cilindrada, tech)

        # Mediana ANTES de filtrar para usarla en el filtro de calidad de precio
        precios_validos = sorted(a.precio for a in items if a.precio >= 2_000)
        med = median(precios_validos) if precios_validos else 0.0

        def _calidad_ok(a) -> bool:
            """
            Filtros duros de calidad — descartan, no penalizan.
            Buscamos un anuncio confiable, no un chollo dudoso.
            Umbrales calibrados a la realidad de Wallapop.
            """
            # Saneamiento básico
            if a.precio <= 0 or a.km <= 0 or a.año <= 1990:
                return False
            # Precio NO sospechosamente bajo vs mediana del modelo
            if med > 0 and a.precio < 0.55 * med:
                return False
            # Descripción mínima — anuncios totalmente vacíos suelen ser scam
            if len(a.descripcion or "") < 50:
                return False
            # Mínimo de fotos — un vendedor decente sube ≥3
            if len(a.fotos or []) < 3:
                return False
            # Red flags: máximo 1 (cualquier cantidad ≥2 descarta)
            try:
                flags = detectar_red_flags(a, None)
                if len(flags) > 1:
                    return False
            except Exception:
                pass
            return True

        # Nivel 1: año estricto + comb + motor + precio 65-100% + km + CALIDAD
        cands = [
            a for a in items
            if precio_min_ej <= a.precio <= presup_max_p
            and 1_000 <= a.km <= km_max_ejemplo
            and (año_ini - 1) <= a.año <= (año_fin + 2)
            and _comb_ok(a) and _mot_ok(a)
            and _calidad_ok(a)
        ]
        # Nivel 2: año absoluto (±5/±3) + comb + motor + precio 65-100% + CALIDAD
        if not cands:
            cands = [
                a for a in items
                if precio_min_ej <= a.precio <= presup_max_p
                and 1_000 <= a.km <= km_max_ejemplo
                and año_min_abs <= a.año <= año_max_abs
                and _comb_ok(a) and _mot_ok(a)
                and _calidad_ok(a)
            ]
        # Nivel 3: bajar suelo de precio al 50% (sigue exigiendo calidad)
        if not cands:
            precio_floor = max(2_000, presup_max_p * 0.50)
            cands = [
                a for a in items
                if precio_floor <= a.precio <= presup_max_p
                and 1_000 <= a.km <= km_max_ejemplo
                and año_min_abs <= a.año <= año_max_abs
                and _comb_ok(a) and _mot_ok(a)
                and _calidad_ok(a)
            ]
        # Nivel 4: sin exigir calidad, mantener motor + año
        if not cands:
            precio_floor = max(2_000, presup_max_p * 0.40)
            cands = [
                a for a in items
                if precio_floor <= a.precio <= presup_max_p
                and 1_000 <= a.km <= km_max_ejemplo
                and año_min_abs <= a.año <= año_max_abs
                and _comb_ok(a) and _mot_ok(a)
            ]
        # Nivel 5: SIN filtro de motor (la cfg puede tener motor inventado).
        # Solo marca+modelo+año+precio+combustible. Mejor mostrar UN ejemplo del modelo
        # aunque no machee el motor exacto que decir "sin ejemplo".
        if not cands:
            precio_floor = max(2_000, presup_max_p * 0.40)
            cands = [
                a for a in items
                if precio_floor <= a.precio <= presup_max_p
                and 1_000 <= a.km <= km_max_ejemplo
                and año_min_abs <= a.año <= año_max_abs
                and _comb_ok(a)
            ]
            if cands:
                logger.info(f"[IDEAL] {kw}: nivel 5 — sin filtro de motor, {len(cands)} cands")
        if not cands:
            logger.info(
                f"[IDEAL] ejemplo {kw}: 0 candidatos en {len(items)} items "
                f"(motor cfg={cilindrada or '?'}/{tech or '?'})"
            )
            return None, med

        # Filtro anti-outliers: si hay >3 candidatos, descartar los <50% mediana o >150%
        if med > 0 and len(cands) > 3:
            filtrados = [a for a in cands if 0.50 * med <= a.precio <= 1.50 * med]
            if filtrados:
                cands = filtrados

        return max(cands, key=lambda a: _score_ejemplo(a, cfg, med)), med

    try:
        resultados_ej = await asyncio.gather(
            *(_buscar_ejemplo(c) for c in configs), return_exceptions=True
        )
    except Exception as e:
        logger.error(f"[IDEAL] gather ejemplos falló: {e}")
        resultados_ej = [(None, 0.0)] * len(configs)
    ejemplos: list = []
    medianas: dict = {}
    for cfg, r in zip(configs, resultados_ej):
        if isinstance(r, Exception):
            logger.warning(f"[IDEAL] _buscar_ejemplo lanzó: {r}")
            ejemplos.append(None)
            continue
        anuncio, mediana = r
        ejemplos.append(anuncio)
        if mediana > 0:
            medianas[f"{cfg['marca']} {cfg['modelo']}"] = mediana

    # 4. Render: cabecera IA + línea de ejemplo
    emojis = ["🥇", "🥈", "🥉"]
    lineas = []
    for i, (cfg, ej) in enumerate(zip(configs, ejemplos)):
        marca_t  = cfg["marca"].title()
        modelo_t = cfg["modelo"].title()
        motor    = cfg.get("motor", "")
        año_ini  = cfg.get("año_ini", "")
        año_fin  = cfg.get("año_fin", "")
        comentario = html.escape(cfg.get("comentario", ""))

        cabecera = (
            f"<b>{emojis[i]} {html.escape(marca_t)} {html.escape(modelo_t)}"
            + (f" {html.escape(motor)}" if motor else "")
            + (f" ({año_ini}-{año_fin})" if año_ini and año_fin else "")
            + "</b>"
        )
        bloque = cabecera
        if not motor:
            bloque += "\n🔧 <i>Motor sugerido: por confirmar</i>"
        if comentario:
            bloque += f"\n{comentario}"
        if ej:
            bloque += (
                f"\n💡 <i>Ej:</i> 📍{html.escape(ej.provincia or 'España')} · "
                f"{ej.km:,} km · <b>{ej.precio:,.0f}€</b> · "
                f"<a href='{ej.url}'>Ver anuncio</a>"
            )
        else:
            bloque += (
                "\n💡 <i>Sin ejemplo exacto del motor en presupuesto. "
                "Búscalo manualmente con /buscar.</i>"
            )
        lineas.append(bloque)

    # 5. Veredicto comparativo IA con investigación real por modelo
    try:
        await msg.edit_text(
            "✍️ <b>Comparando las 3 opciones…</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass

    veredicto = ""
    ejemplos_validos = [e for e in ejemplos if e is not None]
    if ejemplos_validos:
        try:
            veredicto = await generar_veredicto_ideal(
                perfil, ejemplos_validos, medianas, investigacion=investigacion or None,
            )
        except Exception as e:
            logger.warning(f"[IDEAL] generar_veredicto_ideal falló: {e}")
            veredicto = ""

    resultado_txt = (
        f"🎯 <b>3 configuraciones recomendadas</b>\n"
        f"<i>Para {presup_max_p:,}€ · {tamaño_p.replace('_', ' ')}</i>\n\n"
        + "\n\n".join(lineas)
    )
    if veredicto:
        resultado_txt += f"\n\n━━━━━━━━━━━━━━━\n{veredicto}"
    resultado_txt += "\n\n<i>Pulsa Analizar para un informe completo del anuncio.</i>"

    # Guardar URLs para botones Analizar #N
    urls_botones: list[tuple[int, str]] = []
    for idx, ej in enumerate(ejemplos):
        if ej and ej.url:
            urls_botones.append((idx + 1, ej.url))
    ctx.user_data["ideal_urls"] = [u for _, u in urls_botones]

    teclado = None
    if urls_botones:
        teclado = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"🔍 Analizar #{n}", callback_data=f"ideal_analizar:{i}")
            for i, (n, _) in enumerate(urls_botones)
        ]])

    await _enviar_largo(
        msg, resultado_txt,
        parse_mode="HTML", disable_web_page_preview=True, reply_markup=teclado,
    )

    if user_id and not es_admin:
        registrar_analisis(user_id)

    return ConversationHandler.END


# ════════════════════════════════════════════════════════════════════════════
# /ideal v2 — pipeline 6 fases (sustituye al flujo v1 anterior)
# ════════════════════════════════════════════════════════════════════════════

IDEAL_V2_FILLING = 30


@requiere_acceso("/ideal", registrar=False)
async def cmd_ideal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point del flujo /ideal v2. Crea sesión y pasa a slot-filling."""
    user = update.effective_user
    allowed, _tier = _check_access(user.id, user.username or "")
    if not allowed:
        await update.message.reply_text("⛔ No tienes acceso a este bot.")
        return ConversationHandler.END

    es_admin = user.id in ADMIN_USER_IDS

    # Limpiar estado anterior (v1 + v2)
    for k in [k for k in ctx.user_data if k.startswith("ideal_") or k == "hueco_actual"]:
        del ctx.user_data[k]
    ctx.user_data["ideal_user_id"] = user.id
    ctx.user_data["ideal_es_admin"] = es_admin

    reset_sesion(user.id)
    sesion = nueva_sesion(user.id)

    texto = (update.message.text or "").strip()
    if texto.lower().startswith("/ideal"):
        texto = texto[6:].strip()

    await update.message.reply_text(
        "🎯 <b>Vamos a encontrar tu coche ideal.</b>\n\n"
        "Cuéntame en una frase qué buscas: presupuesto, uso, dónde vives, "
        "cuántos sois, km/año.",
        parse_mode="HTML",
    )

    if texto:
        return await _ideal_v2_procesar_texto(update, ctx, texto, sesion)

    return IDEAL_V2_FILLING


async def ideal_v2_recibir_texto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Recibe respuestas de slot-filling y avanza el flujo."""
    user = update.effective_user
    sesion = get_sesion(user.id)
    if not sesion:
        await update.message.reply_text(
            "⏰ La sesión caducó. Vuelve a lanzar /ideal."
        )
        return ConversationHandler.END

    texto = (update.message.text or "").strip()
    if not texto:
        return IDEAL_V2_FILLING

    return await _ideal_v2_procesar_texto(update, ctx, texto, sesion)


async def _ideal_v2_procesar_texto(update, ctx, texto: str, sesion: dict) -> int:
    """Mete texto en slots → o pregunta más, o lanza pipeline."""
    user = update.effective_user
    es_admin = ctx.user_data.get("ideal_es_admin", False)

    msg_parse = await update.message.reply_text("🤖 Entendiendo lo que buscas…")
    try:
        slots = await alimentar_slots(sesion, texto)
    except Exception as e:
        logger.warning(f"[IDEAL_V2] alimentar_slots: {e}")
        await msg_parse.edit_text(
            "⚠️ No te entendí bien. Cuéntamelo de otra forma."
        )
        return IDEAL_V2_FILLING

    faltantes = slots.slots_criticos_faltantes()
    if faltantes:
        preguntas = generar_preguntas_clarificacion(slots)
        await msg_parse.edit_text(preguntas, parse_mode="HTML")
        return IDEAL_V2_FILLING

    # Slots completos → pipeline
    await msg_parse.edit_text(
        "🔧 <b>Buscándote el mejor coche…</b>\n"
        "<i>Voy a fondo. Tarda unos minutos.</i>",
        parse_mode="HTML",
    )

    try:
        await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    except Exception:
        pass

    try:
        top3, html_veredicto = await ejecutar_pipeline(sesion)
    except Exception as e:
        logger.error(f"[IDEAL_V2] pipeline error: {e}", exc_info=e)
        await msg_parse.edit_text(
            "⚠️ Algo se rompió en la búsqueda. Inténtalo de nuevo en un momento."
        )
        reset_sesion(user.id)
        return ConversationHandler.END

    if not top3:
        await msg_parse.edit_text(
            "😔 <b>No encontré opciones que encajen.</b>\n\n"
            "Prueba a relajar el presupuesto o cambiar el uso. "
            "Vuelve a lanzar /ideal con más detalle.",
            parse_mode="HTML",
        )
        reset_sesion(user.id)
        return ConversationHandler.END

    await _ideal_v2_render_top3(sesion, html_veredicto, msg_parse)

    if not es_admin:
        try:
            registrar_analisis(user.id)
        except Exception:
            pass

    # Registrar evento métrica
    try:
        from database import registrar_evento_ideal
        duracion = int(time.time() - sesion.get("duracion_inicio", time.time()))
        registrar_evento_ideal(
            user_id=user.id,
            slots=sesion["slots"].to_dict(),
            candidatos=sesion.get("candidatos_iniciales", []),
            top3=sesion.get("top3", []),
            accion="presentado",
            duracion_s=duracion,
        )
    except Exception as e:
        logger.warning(f"[IDEAL_V2] registrar_evento_ideal: {e}")

    return ConversationHandler.END


async def _ideal_v2_render_top3(sesion: dict, html_veredicto: str, msg_parse) -> None:
    """Envía el veredicto + botones de acción al usuario."""
    top3 = sesion.get("top3") or []

    keyboard_filas = []
    fila = []
    for i, item in enumerate(top3, start=1):
        anuncios = item.get("anuncios", [])
        label = f"👍 Me convence el {i}º" if anuncios else f"👍 El {i}º"
        fila.append(InlineKeyboardButton(label, callback_data=f"ideal_aceptar:{i}"))
    if fila:
        keyboard_filas.append(fila)
    if len(sesion.get("top5_validados", [])) > 3:
        keyboard_filas.append([InlineKeyboardButton("🔍 Ver más opciones", callback_data="ideal_mas")])
    keyboard_filas.append([InlineKeyboardButton("🤔 Ninguno encaja", callback_data="ideal_ninguno")])

    teclado = InlineKeyboardMarkup(keyboard_filas)

    if not html_veredicto:
        # Fallback minimalista
        partes = ["🎯 <b>Tu coche ideal según tu perfil</b>\n"]
        emojis = ["🥇", "🥈", "🥉"]
        for i, item in enumerate(top3):
            c = item["candidato"]
            partes.append(
                f"\n{emojis[i]} <b>{html.escape(c['marca'].title())} "
                f"{html.escape(c['modelo'].title())} "
                f"{html.escape(c.get('version_motor',''))}</b>"
            )
            for a in item.get("anuncios", [])[:2]:
                partes.append(
                    f"  • {a['año']} · {a['km']:,} km · {a['precio']:,.0f}€ · "
                    f"{html.escape(a.get('provincia') or 'España')} → "
                    f"<a href='{a['url']}'>ver</a>"
                )
        html_veredicto = "\n".join(partes)

    await _enviar_largo(
        msg_parse, html_veredicto,
        parse_mode="HTML", disable_web_page_preview=True, reply_markup=teclado,
    )


async def callback_ideal_v2_aceptar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User acepta una opción → muestra checklist + sugiere /analizar."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    partes = (query.data or "").split(":")
    if len(partes) < 2:
        return
    try:
        idx = int(partes[1]) - 1
    except ValueError:
        return

    sesion = get_sesion(user.id)
    if not sesion:
        await query.edit_message_text("⏰ La sesión caducó. Vuelve a lanzar /ideal.")
        return

    top3 = sesion.get("top3") or []
    if idx < 0 or idx >= len(top3):
        return

    item = top3[idx]
    c = item["candidato"]
    anuncios = item.get("anuncios") or []

    texto = (
        f"✅ <b>Buena elección: {html.escape(c['marca'].title())} "
        f"{html.escape(c['modelo'].title())} {html.escape(c.get('version_motor',''))}</b>\n\n"
        "Antes de ir a verlo, repasa esto:\n"
        "• Histórico ITV completo, sin saltos\n"
        "• Libro de revisiones oficial sellado\n"
        "• Aceite limpio, sin lechada en tapón\n"
        "• Frenado en línea recta, sin tirar\n"
        "• Sin testigos encendidos en cuadro\n"
    )
    if anuncios:
        url0 = anuncios[0].get("url", "")
        if url0:
            texto += f"\n\nPara informe completo del primer anuncio:\n<code>/analizar {url0}</code>"

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text(
        texto, parse_mode="HTML", disable_web_page_preview=True
    )

    try:
        from database import registrar_evento_ideal
        registrar_evento_ideal(
            user_id=user.id,
            slots=sesion["slots"].to_dict(),
            candidatos=sesion.get("candidatos_iniciales", []),
            top3=top3,
            accion=f"aceptar_{idx+1}",
            duracion_s=int(time.time() - sesion.get("duracion_inicio", time.time())),
        )
    except Exception:
        pass


async def callback_ideal_v2_mas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User pide más opciones del buffer (4-5 + alternativas)."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    sesion = get_sesion(user.id)
    if not sesion:
        await query.edit_message_text("⏰ La sesión caducó. Vuelve a lanzar /ideal.")
        return

    top5 = sesion.get("top5_validados") or []
    extras = top5[3:5]
    if not extras:
        await query.message.reply_text("ℹ️ No hay más opciones en este perfil. Prueba 'Ninguno encaja'.")
        return

    emojis = ["4️⃣", "5️⃣"]
    partes = ["🔍 <b>Más opciones que encajan</b>\n"]
    for i, item in enumerate(extras):
        c = item["candidato"]
        e = item.get("enriquecimiento", {})
        partes.append(
            f"\n{emojis[i]} <b>{html.escape(c['marca'].title())} "
            f"{html.escape(c['modelo'].title())} "
            f"{html.escape(c.get('version_motor',''))}</b>"
        )
        if c.get("razon_principal"):
            partes.append(f"<i>{html.escape(c['razon_principal'][:200])}</i>")
        if e.get("comentario_experto"):
            partes.append(html.escape(e["comentario_experto"][:300]))
        for a in item.get("anuncios", [])[:2]:
            partes.append(
                f"  • {a['año']} · {a['km']:,} km · {a['precio']:,.0f}€ · "
                f"{html.escape(a.get('provincia') or 'España')} → "
                f"<a href='{a['url']}'>ver</a>"
            )
        if not item.get("anuncios"):
            partes.append("  <i>Sin stock comprable hoy.</i>")

    # Alternativas mencionadas en foros (Fase 3)
    alternativas: list[str] = []
    for item in (sesion.get("top5_validados") or []):
        for alt in (item.get("enriquecimiento", {}).get("alternativas_mencionadas") or []):
            if alt and alt not in alternativas:
                alternativas.append(alt)
    if alternativas:
        partes.append("\n<i>Mencionadas en foros: " + html.escape(", ".join(alternativas[:5])) + "</i>")

    texto = "\n".join(partes)
    LIMITE = 4000
    if len(texto) <= LIMITE:
        await query.message.reply_text(texto, parse_mode="HTML", disable_web_page_preview=True)
    else:
        await query.message.reply_text(texto[:LIMITE], parse_mode="HTML", disable_web_page_preview=True)
        await query.message.reply_text(texto[LIMITE:], parse_mode="HTML", disable_web_page_preview=True)


async def callback_ideal_v2_ninguno(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User rechaza todo → segunda ronda con marcas distintas. Cuesta 1 crédito."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    sesion = get_sesion(user.id)
    if not sesion:
        await query.edit_message_text("⏰ La sesión caducó. Vuelve a lanzar /ideal.")
        return

    # Cobrar un crédito por la segunda ronda
    from permisos import _construir_info, _enviar_paywall
    puede, restantes = puede_usar(user.id, 1)
    if not puede:
        info = _construir_info(user.id, puede, restantes)
        await _enviar_paywall(update, info, "/ideal")
        return
    registrar_uso(user.id, 1)

    msg = await query.message.reply_text(
        "🔄 <b>Buscando otra ronda con enfoques distintos…</b>",
        parse_mode="HTML",
    )

    try:
        top3 = await fase_segunda_ronda(sesion)
    except Exception as e:
        logger.error(f"[IDEAL_V2] segunda_ronda: {e}", exc_info=e)
        await msg.edit_text("⚠️ Falló la segunda ronda. Vuelve a lanzar /ideal.")
        return

    if not top3:
        await msg.edit_text(
            "😔 No encontré más alternativas con tu perfil. Prueba a ampliar presupuesto.",
        )
        return

    try:
        from ai import generar_veredicto_ideal_v2
        html_veredicto = await generar_veredicto_ideal_v2(top3, sesion["slots"].to_dict())
    except Exception as e:
        logger.warning(f"[IDEAL_V2] veredicto segunda ronda: {e}")
        html_veredicto = ""

    await _ideal_v2_render_top3(sesion, html_veredicto, msg)


# Mantenido por compatibilidad si quedasen botones antiguos
async def cmd_ideal_v1_disabled(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Stub: el flujo v1 fue reemplazado por v2."""
    return ConversationHandler.END


# ════════════════════════════════════════════════════════════════════════════
# /comparar — Fase 4
# ════════════════════════════════════════════════════════════════════════════

COMPARAR_FILLING = 40


@requiere_acceso("/comparar", registrar=False)
async def cmd_comparar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point /comparar. Crea sesión y pasa a slot-filling si hace falta."""
    user = update.effective_user
    allowed, _tier = _check_access(user.id, user.username or "")
    if not allowed:
        await update.message.reply_text("⛔ No tienes acceso a este bot.")
        return ConversationHandler.END

    ctx.user_data["comparar_user_id"] = user.id
    comparar_pipeline.borrar_sesion(user.id)
    comparar_pipeline.nueva_sesion(user.id)

    texto = (update.message.text or "").strip()
    if texto.lower().startswith("/comparar"):
        texto = texto[9:].strip()

    if not texto:
        await update.message.reply_text(
            "🆚 <b>Compara dos coches a nivel modelo.</b>\n\n"
            "Ejemplos:\n"
            "• <code>/comparar Golf 7 GTI vs Civic Type R FK7</code>\n"
            "• <code>/comparar Megane RS contra Leon Cupra</code>\n"
            "• <code>/comparar &lt;url wallapop&gt; vs &lt;url coches.net&gt;</code>",
            parse_mode="HTML",
        )
        return COMPARAR_FILLING

    return await _comparar_procesar_texto(update, ctx, texto)


async def comparar_recibir_texto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handler de respuestas en estado COMPARAR_FILLING."""
    user = update.effective_user
    sesion = comparar_pipeline.get_sesion(user.id)
    if not sesion:
        await update.message.reply_text("⏰ Sesión caducada. Vuelve a lanzar /comparar.")
        return ConversationHandler.END

    texto = (update.message.text or "").strip()
    if not texto:
        return COMPARAR_FILLING

    return await _comparar_procesar_texto(update, ctx, texto)


async def _comparar_procesar_texto(update: Update, ctx: ContextTypes.DEFAULT_TYPE, texto: str):
    """Alimenta slots, pregunta si falta info, o lanza pipeline."""
    user = update.effective_user
    sesion = comparar_pipeline.get_sesion(user.id) or comparar_pipeline.nueva_sesion(user.id)

    msg_parse = await update.message.reply_text("🤖 Entendiendo qué quieres comparar…")
    try:
        completos, siguiente = await comparar_pipeline.alimentar_slots(sesion, texto)
    except Exception as e:
        logger.warning(f"[COMPARAR] alimentar_slots: {e}", exc_info=e)
        await msg_parse.edit_text("⚠️ No te entendí bien. Reformula la comparación.")
        return COMPARAR_FILLING

    if not completos:
        await msg_parse.edit_text(siguiente or "¿Puedes ser más concreto?", parse_mode="HTML")
        return COMPARAR_FILLING

    # Slots completos → pipeline.
    await msg_parse.edit_text(
        "🔧 <b>Comparando los dos modelos…</b>\n"
        "<i>Mercado + DGT + fiabilidad + economía. Tarda menos de un minuto.</i>",
        parse_mode="HTML",
    )
    try:
        await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    except Exception:
        pass

    try:
        html_veredicto = await asyncio.wait_for(
            comparar_pipeline.ejecutar_pipeline(sesion),
            timeout=180,
        )
    except asyncio.TimeoutError:
        logger.warning("[COMPARAR] pipeline timeout")
        await msg_parse.edit_text("⏱ La comparativa tardó demasiado. Reintenta en 1 min.")
        comparar_pipeline.borrar_sesion(user.id)
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"[COMPARAR] pipeline error: {e}", exc_info=e)
        await msg_parse.edit_text("⚠️ Algo se rompió en la comparativa. Reintenta en 1 min.")
        comparar_pipeline.borrar_sesion(user.id)
        return ConversationHandler.END

    if not html_veredicto:
        await msg_parse.edit_text(
            "😔 No conseguí datos suficientes para una comparativa fiable. "
            "Prueba con generación distinta o modelos más populares."
        )
        comparar_pipeline.borrar_sesion(user.id)
        return ConversationHandler.END

    await _enviar_largo(
        msg_parse, html_veredicto,
        parse_mode="HTML", disable_web_page_preview=True,
    )

    # Registrar uso (1 crédito) — solo si llegamos al veredicto.
    try:
        es_admin = user.id in ADMIN_USER_IDS
        if not es_admin:
            registrar_uso(user.id, 1)
    except Exception as e:
        logger.warning(f"[COMPARAR] registrar_uso: {e}")

    comparar_pipeline.borrar_sesion(user.id)
    return ConversationHandler.END


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
    puede, restantes = puede_analizar(user.id)
    if es_admin:
        puede = True
    if not puede:
        from permisos import _construir_info, _enviar_paywall
        info = _construir_info(user.id, puede, restantes)
        await _enviar_paywall(update, info, "/analizar")
        return

    await _core_analisis(url, query.message, ctx, es_admin, user.id)
    if not es_admin:
        registrar_analisis(user.id)


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


# ════════════════════════════════════════════════════════════════════════════
# Callback: botones de pago (paywall → Stripe Checkout)
# ════════════════════════════════════════════════════════════════════════════

_PAGO_PRODUCTOS = {
    "pagar_pack_30":  ("pack_30",  STRIPE_PRICE_PACK_30),
    "pagar_pack_100": ("pack_100", STRIPE_PRICE_PACK_100),
}


async def callback_pago(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    import stripe
    stripe.api_key = STRIPE_API_KEY

    producto = _PAGO_PRODUCTOS.get(query.data or "")
    if not producto:
        await query.message.reply_text("⚠️ Producto no reconocido.")
        return
    concepto, price_id = producto

    if not price_id:
        await query.message.reply_text(
            "⚠️ Los pagos todavía no están activados. "
            "Escríbeme a juanloperasanchez@gmail.com y te lo resuelvo."
        )
        return

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="payment",
            success_url="https://t.me/ConCabezaBot",
            cancel_url="https://t.me/ConCabezaBot",
            metadata={
                "telegram_user_id": str(query.from_user.id),
                "concepto":         concepto,
            },
            locale="es",
            expires_at=int(time.time()) + 1800,
        )
        await query.message.reply_text(
            f"🔗 <b>Completa el pago aquí:</b>\n\n{session.url}\n\n"
            "Cuando pagues el bot se activa automáticamente.\n"
            "<i>El enlace expira en 30 minutos.</i>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"[PAGO] Error Stripe: {e}")
        await query.message.reply_text(
            "⚠️ Error generando el enlace de pago. "
            "Escríbeme a juanloperasanchez@gmail.com y lo resuelvo."
        )


def main():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN no configurado. Revisa tu archivo .env")
        return
    
    logger.info("🔄 Eliminando webhook anterior (si existe) para evitar conflictos...")
    
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).concurrent_updates(5).build()

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

    # Conversación: /ideal v2 (slot-filling NL)
    conv_ideal = ConversationHandler(
        entry_points=[CommandHandler("ideal", cmd_ideal)],
        states={
            IDEAL_V2_FILLING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ideal_v2_recibir_texto),
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        allow_reentry=True,
    )

    # Conversación: /comparar (semana 4)
    conv_comparar = ConversationHandler(
        entry_points=[CommandHandler("comparar", cmd_comparar)],
        states={
            COMPARAR_FILLING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, comparar_recibir_texto),
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
    app.add_handler(conv_comparar)
    app.add_handler(CallbackQueryHandler(callback_ideal_analizar, pattern=r"^ideal_analizar:\d+$"))
    # /ideal v2 — botones del top3
    app.add_handler(CallbackQueryHandler(callback_ideal_v2_aceptar, pattern=r"^ideal_aceptar:\d+$"))
    app.add_handler(CallbackQueryHandler(callback_ideal_v2_mas, pattern=r"^ideal_mas$"))
    app.add_handler(CallbackQueryHandler(callback_ideal_v2_ninguno, pattern=r"^ideal_ninguno$"))
    # Ocultos en beta — código intacto, solo sin handler en Telegram:
    # app.add_handler(CommandHandler("misiones", mis_misiones))
    # app.add_handler(conv_buscar)
    # app.add_handler(conv_calcular)
    # app.add_handler(CallbackQueryHandler(callback_misiones, pattern=r"^(pausar|activar|eliminar)_\d+$"))
    app.add_handler(CallbackQueryHandler(callback_qa, pattern=r"^qa:(si|no)$"))
    app.add_handler(CallbackQueryHandler(callback_pago, pattern=r"^pagar_pack_(30|100)$"))

    # Manejador global de errores
    app.add_error_handler(error_handler)

    logger.info("🎯 German Sniper Bot v3 iniciado")
    logger.info("  Fuentes DE: AutoScout24 + mobile.de | Fuentes ES: Wallapop + coches.net")
    logger.info("  Features: Sniper Score, Calculadora Inversa, Modo Sniper, Tiers")
    
    # drop_pending_updates=True descarta actualizaciones pendientes para evitar conflictos
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
