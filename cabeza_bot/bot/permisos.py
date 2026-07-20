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

from cabeza_bot.config import FREE_CREDITOS, PAID_CREDITOS_PACK_10, PAID_CREDITOS_PACK_100, ADMIN_USER_IDS
from cabeza_bot.data.database import get_o_crear_usuario, puede_usar, registrar_uso

# Coste en créditos de cada comando.
# Hoy todo cuesta 1 — bucket unificado igual que "3 acciones/día".
# Mañana se puede cambiar /alertas a 5 sin tocar BD ni decorator.
COSTE_COMANDO: dict[str, int] = {
    "/analizar": 1,
    "/ideal":    1,
    "/comparar": 1,  # semana 4
    "/tasar":    1,  # semana 5
    "/alertas":  1,  # semana 6 — puede subir a 5 cuando sea tiempo real
    # /sniper: gate mínimo del decorator = 1 crédito. El coste REAL depende del
    # tier (free 1 una-sola-vez / paid 5) y se resuelve en el handler, no aquí.
    "/sniper":   1,
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
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"⭐ {PAID_CREDITOS_PACK_100} análisis — 9,99€", callback_data="pagar_pack_100")],
                    [InlineKeyboardButton(f"🔍 {PAID_CREDITOS_PACK_10} análisis — 2,99€", callback_data="pagar_pack_10")],
                ])
                await update.effective_message.reply_text(
                    "ℹ️ Esta es tu última acción gratuita. "
                    "No hay más gratis después.\n\n"
                    "Sigue con un pack sin caducidad:",
                    parse_mode="HTML",
                    reply_markup=keyboard,
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
    from cabeza_bot.data.database import obtener_usuario
    u = obtener_usuario(user_id) or {}
    plan = u.get("tier", "free")

    return {
        "plan":      plan,
        "restantes": restantes,
    }


async def _enviar_paywall(update: Update, info: dict, comando: str):
    plan = info["plan"]

    if plan == "free":
        texto = (
            f"⚡ <b>Has usado tus {FREE_CREDITOS} análisis gratuitos.</b>\n\n"
            "Un fallo comprando coche usado cuesta de media\n"
            "2.000-6.000€ en averías que nadie te contó.\n"
            "Un informe oficial de UN solo coche: 8,50€.\n\n"
            "Sigue filtrando timos:\n\n"
            f"🔍 <b>{PAID_CREDITOS_PACK_10} análisis — 2,99€</b>\n"
            "   Para cerrar la compra que tienes entre manos.\n\n"
            f"⭐ <b>{PAID_CREDITOS_PACK_100} análisis — 9,99€</b> · MEJOR VALOR\n"
            "   Para buscar a fondo: compara todos los que\n"
            "   quieras hasta dar con el bueno. Sin caducidad."
        )
    elif plan == "paid":
        texto = (
            "⚡ <b>Has agotado tus análisis.</b>\n\n"
            "Recarga y sigue donde lo dejaste. Los análisis nuevos\n"
            "se acumulan con los que compres después y no caducan."
        )
    else:
        texto = (
            f"🔒 <b>{comando} no disponible en tu plan actual.</b>\n\n"
            "Desbloquea todas las funciones:"
        )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⭐ {PAID_CREDITOS_PACK_100} análisis — 9,99€", callback_data="pagar_pack_100")],
        [InlineKeyboardButton(f"🔍 {PAID_CREDITOS_PACK_10} análisis — 2,99€", callback_data="pagar_pack_10")],
    ])
    await update.effective_message.reply_text(
        texto, parse_mode="HTML", reply_markup=keyboard
    )
