"""
Detector determinista de señales de alerta en un anuncio de coche usado.

Reglas heurísticas, sin IA. Diseñadas para emitir un aviso cuando hay una
señal clara, no un veredicto definitivo. El texto siempre invita a verificar,
nunca afirma "estafa confirmada".
"""

import re
from datetime import datetime


_PATRONES_SCAM = re.compile(
    r"\b(transferencia\s+bancaria|env[ií]o\s+a\s+domicilio|estoy\s+fuera|"
    r"en\s+el\s+extranjero|herencia|gestor[ií]a|pago\s+por\s+adelantado|"
    r"trabajo\s+en\s+el\s+extranjero|empresa\s+de\s+envíos)\b",
    re.IGNORECASE,
)

_PATRONES_PRO = re.compile(r"\b(garant[ií]a|concesionario|IVA\s+deducible|factura)\b", re.IGNORECASE)


def detectar_red_flags(anuncio, stats) -> list[str]:
    """
    Devuelve una lista de strings con las señales de alerta detectadas.
    Cada string ya está formateado para mostrarse al usuario.
    Si no hay alertas, devuelve [].
    """
    flags: list[str] = []
    año_actual = datetime.utcnow().year
    descripcion = (anuncio.descripcion or "")

    # 1) Precio sospechosamente bajo vs mediana
    if stats and stats.mediana > 0 and anuncio.precio > 0:
        ratio = anuncio.precio / stats.mediana
        if ratio < 0.55:
            pct = round((1 - ratio) * 100)
            flags.append(
                f"Precio un {pct}% por debajo de la mediana — verifica que no sea estafa."
            )

    # 2) Km vs edad
    if anuncio.año and anuncio.año > 1990 and anuncio.km > 0:
        edad = max(1, año_actual - anuncio.año)
        km_anuales = anuncio.km / edad
        if km_anuales < 4000:
            flags.append(
                f"Solo {km_anuales:,.0f} km/año — pide histórico de ITV para confirmar."
            )
        elif km_anuales > 30000:
            flags.append(
                f"{km_anuales:,.0f} km/año — uso muy intensivo, espera mantenimiento caro."
            )

    # 3) Descripción muy corta
    if len(descripcion.strip()) < 40:
        flags.append("Descripción muy escueta — desconfía, pide más detalles.")

    # 4) Patrón scam clásico de Wallapop
    if _PATRONES_SCAM.search(descripcion):
        flags.append(
            "Patrón típico de estafa Wallapop. NO mandes dinero por adelantado "
            "ni aceptes envíos a domicilio."
        )

    # 5) Profesional camuflado
    if (anuncio.precio > 0
            and anuncio.precio == round(anuncio.precio / 1000) * 1000
            and _PATRONES_PRO.search(descripcion)):
        flags.append(
            "Vendedor profesional camuflado de particular — no aplican las garantías "
            "legales del comercio."
        )

    return flags
