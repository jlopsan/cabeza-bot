"""
broadcast.py — Envía un mensaje a todos los usuarios de la BD.
Uso: python broadcast.py
"""
import asyncio
import sqlite3
import time
import httpx
from cabeza_bot.config import TELEGRAM_TOKEN, DB_PATH

MENSAJE = """\
Mis disculpas.

Durante las últimas horas /analizar se rompía y te mostraba un error. Fallo mío. Ya está arreglado y funcionando bien.

Como disculpa, he restablecido tus créditos gratis a 3. Vuelven a estar ahí para que sigas usando el bot con normalidad.

Gracias por la paciencia. Sigo construyendo esto en directo, semana a semana.

Juan Lopera · Coches con cabeza · juanlopera.es\
"""

API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


def get_user_ids() -> list[int]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT user_id FROM usuarios WHERE tier = 'free'").fetchall()
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
