# tests/test_sniper_riesgo.py — asserts simples, sin pytest (repo no lo usa).
# Ejecutar: python tests/test_sniper_riesgo.py
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from cabeza_bot.sniper.riesgo import (
    evaluar_riesgo, NIVEL_VERDE, NIVEL_AMARILLO, NIVEL_ROJO,
)

_fallos = []


def check(nombre, cond):
    if cond:
        print(f"  OK  {nombre}")
    else:
        print(f"  FAIL {nombre}")
        _fallos.append(nombre)


def anuncio_base(**overrides):
    base = {
        "precio": 20000, "km": 80000, "año": 2020,
        "reimport": False, "unfallfrei": True, "scheckheftgepflegt": True,
        "vendedor": "haendler", "propietarios": 1, "num_fotos": 20,
    }
    base.update(overrides)
    return base


# ─── Regla 1: reimport ───────────────────────────────────────────────────────
print("Regla 1 — Reimport")
r = evaluar_riesgo(anuncio_base(reimport=True))
check("reimport -> ROJO", r.nivel == NIVEL_ROJO)
check("reimport -> bandera menciona 'reimport'", any("reimport" in b.texto.lower() for b in r.banderas))

r2 = evaluar_riesgo(anuncio_base(reimport=False))
check("sin reimport -> no dispara esa bandera", not any("reimport" in b.texto.lower() for b in r2.banderas))


# ─── Regla 2: km vs percentil ────────────────────────────────────────────────
print("Regla 2 — Plausibilidad de km")
dataset_km = [70_000 + i * 2_000 for i in range(20)]  # 70k..108k, uniforme

r_normal = evaluar_riesgo(anuncio_base(km=90_000), km_dataset=dataset_km)
check("km en rango normal -> sin bandera de km",
      not any("percentil" in b.texto.lower() for b in r_normal.banderas))

r_bajo = evaluar_riesgo(anuncio_base(km=70_000), km_dataset=dataset_km)  # el mínimo -> pctl 2.5
check("km muy bajos (pctl<3) -> ROJO", r_bajo.nivel == NIVEL_ROJO)

r_medio_bajo = evaluar_riesgo(anuncio_base(km=72_000), km_dataset=dataset_km)  # pctl 7.5
check("km algo bajos (3<=pctl<10) -> AMARILLO al menos",
      r_medio_bajo.nivel in (NIVEL_AMARILLO, NIVEL_ROJO))

r_sin_datos = evaluar_riesgo(anuncio_base(km=1000), km_dataset=[70000, 80000])  # <min_muestra
check("dataset insuficiente -> NO inventa bandera de km",
      not any("percentil" in b.texto.lower() for b in r_sin_datos.banderas))
check("dataset insuficiente -> no sube a ROJO por km", r_sin_datos.nivel != NIVEL_ROJO)


# ─── Regla 3: precio anómalo vs gemelos DE ───────────────────────────────────
print("Regla 3 — Precio anómalo vs mercado alemán")
precios_de = [20000, 20500, 19800, 20200, 20100, 19900]  # mediana ~20050

r_normal_precio = evaluar_riesgo(anuncio_base(precio=19000), precios_de_dataset=precios_de)
check("precio normal vs gemelos DE -> sin bandera de precio",
      not any("gemelos alemanes" in b.texto.lower() for b in r_normal_precio.banderas))

r_anomalo = evaluar_riesgo(anuncio_base(precio=8000), precios_de_dataset=precios_de)  # <55% mediana
check("precio muy por debajo de gemelos DE -> ROJO", r_anomalo.nivel == NIVEL_ROJO)
check("bandera menciona 'estafa'", any("estafa" in b.texto.lower() for b in r_anomalo.banderas))

r_sin_muestra_precio = evaluar_riesgo(anuncio_base(precio=1000), precios_de_dataset=[20000, 21000])
check("muestra DE insuficiente -> no dispara bandera de precio anómalo",
      not any("gemelos alemanes" in b.texto.lower() for b in r_sin_muestra_precio.banderas))


# ─── Regla 4: señales blandas ─────────────────────────────────────────────────
print("Regla 4 — Señales blandas")
r_limpio = evaluar_riesgo(anuncio_base())
check("anuncio limpio (todo declarado, vendedor pro, pocas fotos no) -> VERDE",
      r_limpio.nivel == NIVEL_VERDE)

r_blandas = evaluar_riesgo(anuncio_base(
    vendedor="particular", unfallfrei=False, scheckheftgepflegt=False,
    propietarios=4, num_fotos=2,
))
check("5 señales blandas -> sube a AMARILLO (>= umbral)", r_blandas.nivel == NIVEL_AMARILLO)
check("5 señales blandas -> nunca ROJO por sí solas", r_blandas.nivel != NIVEL_ROJO)

r_una_blanda = evaluar_riesgo(anuncio_base(num_fotos=2))
check("1 señal blanda sola -> no sube a AMARILLO (bajo el umbral)",
      r_una_blanda.nivel == NIVEL_VERDE)


# ─── Composición: rojo domina sobre amarillo ────────────────────────────────
print("Composición")
r_mixto = evaluar_riesgo(anuncio_base(reimport=True, num_fotos=2), km_dataset=dataset_km)
check("reimport (rojo) + blanda -> el nivel sigue siendo ROJO", r_mixto.nivel == NIVEL_ROJO)
check("banderas rojas Y verdes conviven en la lista",
      any(b.nivel == NIVEL_ROJO for b in r_mixto.banderas)
      and any(b.nivel == NIVEL_VERDE for b in r_mixto.banderas))


# ─── to_dict serializa ────────────────────────────────────────────────────────
print("Serialización")
d = r_mixto.to_dict()
check("to_dict tiene nivel y banderas", d["nivel"] == "ROJO" and isinstance(d["banderas"], list))


print()
if _fallos:
    print(f"FALLOS: {len(_fallos)} -> {_fallos}")
    sys.exit(1)
else:
    print("TODOS LOS TESTS OK")
