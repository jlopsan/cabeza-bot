# webhook.py — Servidor FastAPI mínimo para webhooks de Stripe.
# NO tiene páginas de usuario. Solo recibe eventos de Stripe.
#
# Arrancar:
#   uvicorn webhook:app --host 0.0.0.0 --port 8080
#
# Test local con Stripe CLI:
#   stripe listen --forward-to localhost:8080/stripe/webhook

import logging
import httpx
import stripe

from fastapi import FastAPI, Request, HTTPException

from config import (
    STRIPE_API_KEY, STRIPE_WEBHOOK_SEC, TELEGRAM_TOKEN,
    PAID_CREDITOS_PACK_30, PAID_CREDITOS_PACK_100, FREE_CREDITOS_DIA,
)
from database import activar_plan, desactivar_pro, pago_ya_procesado

stripe.api_key = STRIPE_API_KEY
app    = FastAPI()
logger = logging.getLogger(__name__)


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig     = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SEC)
    except stripe.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Firma inválida")
    except Exception as e:
        logger.error(f"[STRIPE] Error webhook: {e}")
        raise HTTPException(status_code=400)

    tipo     = event["type"]
    event_id = event["id"]
    logger.info(f"[STRIPE] Evento: {tipo} ({event_id})")

    # Idempotencia: si ya procesamos este event_id, ignorar
    if pago_ya_procesado(event_id):
        logger.info(f"[STRIPE] {event_id} ya procesado, ignorando")
        return {"ok": True, "duplicado": True}

    # Pago único completado (pack) o inicio de suscripción
    if tipo == "checkout.session.completed":
        session  = event["data"]["object"]
        meta     = _field(session, "metadata")
        user_id  = int(_field(meta, "telegram_user_id", 0) or 0)
        # El concepto viaja en metadata desde callback_pago.
        # Si falta, inferir por mode (suscripción → pro, pago → pack_30).
        concepto = _field(meta, "concepto") or (
            "pro_mes" if _field(session, "mode") == "subscription" else "pack_30"
        )
        if user_id:
            activar_plan(
                user_id                = user_id,
                concepto               = concepto,
                stripe_id              = event_id,
                stripe_customer_id     = _field(session, "customer", "") or "",
                stripe_subscription_id = _field(session, "subscription", "") or "",
            )
            await _notificar_user(user_id, concepto)
            logger.info(f"[STRIPE] user {user_id} → {concepto}")

    # Renovación mensual de suscripción pro
    elif tipo == "invoice.payment_succeeded":
        invoice = event["data"]["object"]
        if _field(invoice, "billing_reason") == "subscription_create":
            return {"ok": True, "skip": "primera_factura"}
        sub_id = _field(invoice, "subscription")
        if sub_id:
            try:
                sub      = stripe.Subscription.retrieve(sub_id)
                sub_meta = _field(sub, "metadata")
                user_id  = int(_field(sub_meta, "telegram_user_id", 0) or 0)
                if user_id:
                    activar_plan(user_id, "pro_mes", event_id)
                    await _notificar_user(user_id, "pro_mes")
                    logger.info(f"[STRIPE] user {user_id} renovado pro")
            except Exception as e:
                logger.error(f"[STRIPE] Error renovación: {e}")

    # Cancelación de suscripción
    elif tipo == "customer.subscription.deleted":
        sub      = event["data"]["object"]
        sub_meta = _field(sub, "metadata")
        user_id  = int(_field(sub_meta, "telegram_user_id", 0) or 0)
        if user_id:
            desactivar_pro(user_id)
            await _notificar_user(user_id, "cancelado")
            logger.info(f"[STRIPE] user {user_id} pro cancelado")

    return {"ok": True}


def _field(obj, key, default=None):
    """
    Lee una clave de un StripeObject o dict, devolviendo default si falta.
    StripeObject no implementa .get() — sí soporta indexación con [] pero
    lanza KeyError. Esta función abstrae las dos formas.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        v = obj.get(key, default)
        return v if v is not None else default
    try:
        v = obj[key]
        return v if v is not None else default
    except (KeyError, AttributeError, TypeError):
        return default


async def _notificar_user(user_id: int, concepto: str):
    if not TELEGRAM_TOKEN:
        return
    if concepto == "pack_30":
        texto = (
            f"✅ <b>Pack activado.</b>\n\n"
            f"{PAID_CREDITOS_PACK_30} acciones disponibles, sin caducidad. ¡Vamos!"
        )
    elif concepto == "pack_100":
        texto = (
            f"✅ <b>Pack activado.</b>\n\n"
            f"{PAID_CREDITOS_PACK_100} acciones disponibles, sin caducidad. ¡Vamos!"
        )
    elif concepto == "pro_mes":
        texto = (
            "🚀 <b>Plan PRO activado.</b>\n\n"
            "Acciones ilimitadas durante un mes. Se renueva automáticamente."
        )
    elif concepto == "cancelado":
        texto = (
            "ℹ️ Tu suscripción PRO ha sido cancelada.\n"
            f"Has vuelto al plan gratuito ({FREE_CREDITOS_DIA} acciones al día, "
            "reset medianoche UTC)."
        )
    else:
        return

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": user_id, "text": texto, "parse_mode": "HTML"},
            )
    except Exception as e:
        logger.warning(f"[STRIPE] No se pudo notificar a user {user_id}: {e}")
