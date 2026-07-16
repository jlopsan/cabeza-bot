"""
broadcast.py — Envía un mensaje a todos los usuarios de la BD.
Uso: python broadcast.py
"""
import asyncio
import sqlite3
import time
import httpx
from config import TELEGRAM_TOKEN, DB_PATH

MENSAJE = """\
Hay dos momentos en los que todo el mundo se hace la misma pregunta: ¿a cuánto está esto realmente?

Cuando vas a vender tu coche y no sabes si pedir 8.000€ o 9.500€. Y cuando ves un anuncio y no sabes si el precio es justo o te están intentando colar algo. La mayoría mira dos o tres anuncios parecidos y tira de intuición. Eso no es un precio, es una apuesta.

Acabo de meter en el bot una función que hace ese trabajo bien: /tasar

Le pasas la marca, el modelo y el año. Motor y km son opcionales, pero si los das afina más. El bot rastrea Wallapop y Coches.net en tiempo real y te dice:

- El precio real de mercado para ese coche exacto, no una estimación genérica
- A cuánto puedes venderlo tú para que se mueva rápido sin regalarlo
- A cuánto tienes que comprarlo para que sea un buen negocio

Nada de tablas de amortización genéricas. Precio sacado de anuncios reales que hay ahora mismo en el mercado.

Para probarlo escribe /tasar aquí mismo.
Tienes 3 créditos gratis. Una tasación cuesta 1.

Juan Lopera · Coches con cabeza · juanlopera.es\
"""

API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


def get_user_ids() -> list[int]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT user_id FROM usuarios").fetchall()
    conn.close()
    return [r[0] for r in rows]


async def send_one(client: httpx.AsyncClient, user_id: int) -> bool:
    try:
        r = await client.post(API, json={"chat_id": user_id, "text": MENSAJE}, timeout=10)
        ok = r.status_code == 200 and r.json().get("ok")
        if not ok:
            desc = r.json().get("description", r.text)
            print(f"  SKIP {user_id}: {desc}")
        return ok
    except Exception as e:
        print(f"  ERROR {user_id}: {e}")
        return False


async def main():
    ids = get_user_ids()
    print(f"Usuarios en BD: {len(ids)}")
    if not ids:
        print("Nada que enviar.")
        return

    ok = err = 0
    async with httpx.AsyncClient() as client:
        for uid in ids:
            sent = await send_one(client, uid)
            if sent:
                ok += 1
                print(f"  OK  {uid}")
            else:
                err += 1
            await asyncio.sleep(0.05)  # ~20 msg/s, bajo el límite de Telegram

    print(f"\nEnviados: {ok}  Fallidos: {err}")


if __name__ == "__main__":
    asyncio.run(main())
