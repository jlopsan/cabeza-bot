# permisos.py — Mapa de costes por comando + decorator de acceso.
#
# Para añadir un comando nuevo: añadir entrada en COSTE_COMANDO.
# Para que cueste más de 1 crédito: cambiar su valor.
# Para bloquear un comando a free hoy no hace falta nada extra — si no
# tiene créditos se bloquea solo. En el futuro se puede añadir lógica
# de "mínimo tier requerido" sin tocar la BD ni el decorator.

from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import FREE_CREDITOS_DIA, PAID_CREDITOS_PACK, ADMIN_USER_IDS
from database import get_o_crear_usuario, puede_usar, registrar_uso, minutos_hasta_reset

# Coste en créditos de cada comando.
# Hoy todo cuesta 1 — bucket unificado igual que "3 acciones/día".
# Mañana se puede cambiar /alertas a 5 sin tocar BD ni decorator.
COSTE_COMANDO: dict[str, int] = {
    "/analizar": 1,
    "/ideal":    1,
    "/comparar": 1,  # semana 4
    "/tasar":    1,  # semana 5
    "/alertas":  1,  # semana 6 — puede subir a 5 cuando sea tiempo real
}


def requiere_acceso(comando: str, registrar: bool = True):
    """
    Decorator para handlers de CommandHandler.
    - Crea usuario si no existe.
    - Admins: pasan siempre sin contar.
    - Comprueba créditos disponibles para el coste del comando.
    - Si bloqueado: muestra paywall con botones de pago.
    - Si pasa y registrar=True: descuenta créditos al terminar sin excepción.
      Usar registrar=False cuando el flujo es multi-paso y el registro
      se hace manualmente al final (ej: /ideal).
    """
    coste = COSTE_COMANDO.get(comando, 1)

    def decorator(handler):
        @wraps(handler)
        async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            user = update.effective_user
            if not user:
                return await handler(update, ctx)

            es_admin = user.id in ADMIN_USER_IDS
            get_o_crear_usuario(user.id, user.username or "", user.first_name or "")

            if es_admin:
                return await handler(update, ctx)

            puede, restantes = puede_usar(user.id, coste)
            info = _construir_info(user.id, puede, restantes)

            if not puede:
                await _enviar_paywall(update, info, comando)
                return

            if info["plan"] == "free" and restantes == coste:
                await update.effective_message.reply_text(
                    f"ℹ️ Esta es tu última acción gratuita de hoy. "
                    f"Mañana tienes {FREE_CREDITOS_DIA} nuevas. "
                    f"O continúa ahora por 4.90€ ({PAID_CREDITOS_PACK} acciones sin caducidad).",
                    parse_mode="HTML",
                )

            try:
                resultado = await handler(update, ctx)
            except Exception:
                raise
            else:
                if registrar:
                    registrar_uso(user.id, coste)
                return resultado
        return wrapper
    return decorator


def _construir_info(user_id: int, puede: bool, restantes: int) -> dict:
    from database import obtener_usuario
    u = obtener_usuario(user_id) or {}
    plan = u.get("tier", "free")

    if not puede and plan == "free":
        mins = minutos_hasta_reset(user_id)
        h, m = divmod(mins, 60)
        siguiente = f"{h}h {m}min" if h else "mañana al mediodía (UTC)"
    else:
        siguiente = ""

    return {
        "plan":            plan,
        "restantes":       restantes,
        "siguiente_reset": siguiente,
    }


async def _enviar_paywall(update: Update, info: dict, comando: str):
    plan      = info["plan"]
    siguiente = info.get("siguiente_reset", "")

    if plan == "free":
        texto = (
            f"⚡ <b>Has agotado tus {FREE_CREDITOS_DIA} acciones gratuitas de hoy.</b>\n\n"
            f"Reset: <b>{siguiente}</b>\n\n"
            "El bot cruza el precio con el mercado real, investiga el "
            "historial del modelo, detecta red flags y calcula la "
            "etiqueta DGT. Todo en segundos.\n\n"
            "Para seguir analizando ahora:\n"
            f"• <b>4.90€</b> — {PAID_CREDITOS_PACK} acciones sin caducidad\n"
            "• <b>9.90€/mes</b> — Ilimitado\n\n"
            "Los ingresos financian el proyecto."
        )
    elif plan == "paid":
        texto = (
            f"📦 <b>Has consumido las {PAID_CREDITOS_PACK} acciones del pack.</b>\n\n"
            "Recarga otro pack o pásate a PRO ilimitado:"
        )
    else:
        texto = (
            f"🔒 <b>{comando} no disponible en tu plan actual.</b>\n\n"
            "Desbloquea todas las funciones:"
        )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"💳 {PAID_CREDITOS_PACK} acciones — 4.90€", callback_data="pagar_pack"),
        InlineKeyboardButton("🚀 Ilimitado — 9.90€/mes", callback_data="pagar_pro"),
    ]])
    await update.effective_message.reply_text(
        texto, parse_mode="HTML", reply_markup=keyboard
    )
