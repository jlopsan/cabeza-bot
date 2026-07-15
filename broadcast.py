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
🎯 ¡Nueva función! /ideal

Hasta ahora analizabas anuncios sueltos. Ahora puedo decirte \
directamente qué coche comprar.

Le cuentas tu caso en lenguaje normal. Ej: "vivo en Madrid centro, \
llevo a 2 niños y al perro, máximo 12.000€". Te hago unas \
preguntas, y te devuelvo 3 modelos concretos con motor exacto \
y años recomendados.

Cada uno con:
- Anuncios reales de Wallapop que encajan ahora mismo
- Lo que dicen propietarios en foros
- Averías típicas: a qué km aparecen y cuánto cuestan
- Por qué encaja con TU caso concreto

Pruébala ahora:
/ideal

Cuesta 1 crédito. Tienes 3 gratis para empezar.\
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
