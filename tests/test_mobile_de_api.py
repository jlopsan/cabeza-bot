# tests/test_mobile_de_api.py — lógica pura (sin red, sin credenciales).
# Ejecutar: python tests/test_mobile_de_api.py
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import cabeza_bot.scraping.mobile_de_api as api

_fallos = []


def check(nombre, cond):
    if cond:
        print(f"  OK  {nombre}")
    else:
        print(f"  FAIL {nombre}")
        _fallos.append(nombre)


print("configurada()")
check("sin credenciales -> False", api.configurada() is False)


print("_mapear_filtros")
f = api._mapear_filtros({
    "year_min": 2019, "year_max": 2021, "km_max": 100000,
    "price_max": 25000, "combustible": "diesel",
})
check("year_min -> firstRegistrationDate.min", f.get("firstRegistrationDate.min") == "2019-01")
check("year_max -> firstRegistrationDate.max", f.get("firstRegistrationDate.max") == "2021-12")
check("km_max -> mileage.max", f.get("mileage.max") == "100000")
check("price_max -> price.max", f.get("price.max") == "25000")
check("combustible diesel -> fuel DIESEL", f.get("fuel") == "DIESEL")
check("sin filtros -> dict vacío", api._mapear_filtros({}) == {})
check("combustible desconocido -> sin fuel", "fuel" not in api._mapear_filtros({"combustible": "hidrogeno_raro"}))


print("_mapear_anuncio")
ad = {
    "mobileAdId": "15012", "make": "BMW", "model": "3ER", "modelDescription": "320d M-Sport",
    "firstRegistration": "202007", "mileage": 87000, "fuel": "DIESEL",
    "price": {"consumerPriceGross": "21900.00", "type": "FIXED", "currency": "EUR"},
    "seller": {"type": "DEALER"},
    "damageUnrepaired": False,
}
m = api._mapear_anuncio(ad)
check("precio parseado", m["precio"] == 21900.0)
check("km parseado", m["km"] == 87000)
check("año extraído de firstRegistration YYYYMM", m["año"] == 2020)
check("combustible mapeado a minúsculas internas", m["combustible"] == "diesel")
check("vendedor DEALER -> haendler", m["vendedor"] == "haendler")
check("damageUnrepaired False -> unfallfrei True", m["unfallfrei"] is True)
check("link construido con mobileAdId", m["link"] == "https://www.mobile.de/fahrzeuge/details.html?id=15012")
check("_detalle_completo True (evita doble fetch)", m["_detalle_completo"] is True)

ad_particular = {**ad, "seller": {"type": "FOR_SALE_BY_OWNER"}}
check("vendedor FOR_SALE_BY_OWNER -> particular", api._mapear_anuncio(ad_particular)["vendedor"] == "particular")

check("precio 0 -> descartado (None)",
      api._mapear_anuncio({"mobileAdId": "x", "price": {"consumerPriceGross": "0"}}) is None)
check("sin campo price -> descartado (None)",
      api._mapear_anuncio({"mobileAdId": "x"}) is None)

ad_sin_reg = {**ad, "firstRegistration": ""}
check("firstRegistration vacío -> año 0 (no crashea)", api._mapear_anuncio(ad_sin_reg)["año"] == 0)


print()
if _fallos:
    print(f"FALLOS: {len(_fallos)} -> {_fallos}")
    sys.exit(1)
else:
    print("TODOS LOS TESTS OK")
