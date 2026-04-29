"""
debug_detalle.py — Inspecciona el HTML de un anuncio para encontrar
el selector correcto de la descripcion (Fahrzeugbeschreibung)
"""
import asyncio
import re
from playwright.async_api import async_playwright

# Pon aqui cualquier URL de un anuncio real de AutoScout24
URL = "https://www.autoscout24.de/angebote/audi-a3-a3-sportback-30-tfsi-s-tronic-sport-panorama-led-benzin-blau-e5bae72e-edef-4029-b02d-81adc11e8eb7"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="de-DE",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = await context.new_page()

        print(f"Cargando {URL}...")
        await page.goto(URL, timeout=30_000, wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # Aceptar cookies
        for sel in ["button:has-text('Alle akzeptieren')", "button[data-testid='as24-cmp-accept-all-button']"]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await asyncio.sleep(1.5)
                    print(f"Cookie aceptada: {sel}")
                    break
            except:
                continue

        html = await page.content()

        # Buscar la seccion Fahrzeugbeschreibung
        print("\n=== Buscando 'Fahrzeugbeschreibung' en el HTML ===")
        idx = html.find("Fahrzeugbeschreibung")
        if idx >= 0:
            print(f"Encontrado en posicion {idx}")
            # Mostrar contexto HTML alrededor
            snippet = html[max(0, idx-100):idx+800]
            print(snippet)
        else:
            print("NO encontrado en el HTML estatico — puede ser JS lazy load")

        # Probar selectores candidatos
        print("\n=== Probando selectores ===")
        selectores = [
            # Fahrzeugbeschreibung section
            "[data-testid='description-text']",
            "[data-testid='vehicle-description']",
            "section[data-testid*='description']",
            "div[data-testid*='description']",
            # Por clase
            "div[class*='description'] p",
            "div[class*='Description']",
            "p[class*='description']",
            "p[class*='Description']",
            # Por texto del encabezado
            "h2:has-text('Fahrzeugbeschreibung') + div",
            "h2:has-text('Fahrzeugbeschreibung') ~ p",
            "h3:has-text('Fahrzeugbeschreibung') + div",
            # Pre/code tags (descripciones largas)
            "div[class*='DescriptionSection']",
            "div[class*='vehicle-description']",
            "div[class*='VehicleDescription']",
            "article[class*='description']",
            # Fallback amplio
            "section:has(h2:has-text('Fahrzeug')) p",
            "section:has(h3:has-text('Fahrzeug')) p",
        ]

        for sel in selectores:
            try:
                elem = page.locator(sel).first
                count = await elem.count()
                if count:
                    txt = (await elem.inner_text()).strip()[:150]
                    print(f"  ENCONTRADO [{sel}]: {repr(txt)}")
                else:
                    print(f"  vacio      [{sel}]")
            except Exception as e:
                print(f"  ERROR      [{sel}]: {e}")

        # Buscar en el HTML patrones de clase relacionados con descripcion
        print("\n=== Clases que contienen 'descri' o 'Descri' ===")
        clases = re.findall(r'class="([^"]*[Dd]escri[^"]*)"', html)
        for c in set(clases):
            print(f"  {c}")

        print("\n=== Clases que contienen 'Fahrzeug' ===")
        clases2 = re.findall(r'class="([^"]*Fahrzeug[^"]*)"', html)
        for c in set(clases2):
            print(f"  {c}")

        await browser.close()

asyncio.run(main())