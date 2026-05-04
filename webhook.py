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

from config import STRIPE_API_KEY, STRIPE_WEBHOOK_SEC, TELEGRAM_TOKEN
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
        user_id  = int(session.get("metadata", {}).get("telegram_user_id", 0))
        mode     = session.get("mode", "payment")
        concepto = "pack_20" if mode == "payment" else "pro_mes"
        if user_id:
            activar_plan(
                user_id                = user_id,
                concepto               = concepto,
                stripe_id              = event_id,
                stripe_customer_id     = session.get("customer", "") or "",
                stripe_subscription_id = session.get("subscription", "") or "",
            )
            await _notificar_user(user_id, concepto)
            logger.info(f"[STRIPE] user {user_id} → {concepto}")

    # Renovación mensual de suscripción pro
    elif tipo == "invoice.payment_succeeded":
        invoice = event["data"]["object"]
        # Skip de la primera factura (ya la maneja checkout.session.completed)
        if invoice.get("billing_reason") == "subscription_create":
            return {"ok": True, "skip": "primera_factura"}
        sub_id = invoice.get("subscription")
        if sub_id:
            try:
                sub     = stripe.Subscription.retrieve(sub_id)
                user_id = int(sub.metadata.get("telegram_user_id", 0))
                if user_id:
                    activar_plan(user_id, "pro_mes", event_id)
                    await _notificar_user(user_id, "pro_mes")
                    logger.info(f"[STRIPE] user {user_id} renovado pro")
            except Exception as e:
                logger.error(f"[STRIPE] Error renovación: {e}")

    # Cancelación de suscripción
    elif tipo == "customer.subscription.deleted":
        sub     = event["data"]["object"]
        user_id = int(sub.get("metadata", {}).get("telegram_user_id", 0))
        if user_id:
            desactivar_pro(user_id)
            await _notificar_user(user_id, "cancelado")
            logger.info(f"[STRIPE] user {user_id} pro cancelado")

    return {"ok": True}


async def _notificar_user(user_id: int, concepto: str):
    if not TELEGRAM_TOKEN:
        return
    if concepto == "pack_20":
        texto = "✅ <b>Pack activado.</b>\n\n20 análisis disponibles. ¡Vamos!"
    elif concepto == "pro_mes":
        texto = (
            "🚀 <b>Plan PRO activado.</b>\n\n"
            "Análisis ilimitados durante un mes. Se renueva automáticamente."
        )
    elif concepto == "cancelado":
        texto = (
            "ℹ️ Tu suscripción PRO ha sido cancelada.\n"
            "Has vuelto al plan gratuito (3 análisis cada 3h)."
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
