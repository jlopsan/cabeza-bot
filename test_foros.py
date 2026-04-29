"""
test_foros.py - Pruebas para la nueva función de búsqueda en foros
"""
import asyncio
import sys
from pathlib import Path

# Añadir el directorio actual al path
sys.path.insert(0, str(Path(__file__).parent))

from ai import buscar_problemas_foros, _formato_problemas_foros

async def test_buscar_problemas():
    """Prueba la búsqueda de problemas en foros para diferentes coches."""
    
    print("=" * 60)
    print("TEST: Búsqueda de problemas en foros")
    print("=" * 60)
    
    # Casos de prueba
    test_cases = [
        ("volkswagen", "golf gti", 2018),
        ("bmw", "serie 3", 2019),
        ("audi", "a3", 2020),
        ("mercedes-benz", "clase c", 2017),
    ]
    
    for marca, modelo, anno in test_cases:
        print(f"\n🔍 Probando: {marca} {modelo} ({anno})")
        print("-" * 40)
        
        problemas = await buscar_problemas_foros(marca, modelo, anno)
        
        if problemas:
            print(f"✅ Encontrados {len(problemas)} problemas:")
            print(_formato_problemas_foros(problemas))
        else:
            print("⚠️ No se encontraron problemas")
        
        # Pequeña pausa para no saturar
        await asyncio.sleep(1)
    
    print("\n" + "=" * 60)
    print("TEST COMPLETADO")
    print("=" * 60)

if __name__ == "__main__":
    # Verificar que tenemos las variables de entorno necesarias
    import os
    from dotenv import load_dotenv
    load_dotenv()
    
    if not os.getenv("GROQ_API_KEY"):
        print("❌ Error: GROQ_API_KEY no configurada en .env")
        print("   Consigue una API key gratis en: https://console.groq.com")
        sys.exit(1)
    
    asyncio.run(test_buscar_problemas())