# motor.py — Extracción determinista de motor (CV y combustible) desde texto.
#
# Compartido por /tasar (main.py) y el sniper (valoración afinada por motor).
# Movido desde main.py para no duplicar la cascada cv→comb→pool que /tasar
# validó en vivo (bug GTI-valorado-como-Golf-base). CERO IA: solo regex.

import re

CV_TOL = 20  # ± CV para considerar "mismo motor"


def extraer_cv(texto: str) -> int | None:
    """
    CV de un texto libre o título de anuncio. Acepta CV (ES), PS (DE — mismo
    caballo métrico, 1 PS = 1 CV) y kW (convierte, 1 kW ≈ 1,36 CV).
    """
    t = texto or ""
    m = re.search(r"(\d{2,3})\s*cv\b", t, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{2,3})\s*ps\b", t, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{2,3})\s*kw\b", t, re.IGNORECASE)
    if m:
        return round(int(m.group(1)) * 1.35962)
    return None


def detectar_combustible(texto: str) -> str | None:
    """Combustible normalizado desde texto libre o título."""
    t = (texto or "").lower()
    if any(k in t for k in ("phev", "enchufable")):
        return "híbrido enchufable"
    if any(k in t for k in ("híbrido", "hibrido", "hybrid", "hev")):
        return "híbrido"
    if any(k in t for k in ("eléctric", "electric", "e-golf", "kwh", " ev ")):
        return "eléctrico"
    if any(k in t for k in ("tdi", "diesel", "diésel", "dci", "hdi", "cdti", "gasoil", "gasóleo", "bluehdi")):
        return "diésel"
    if any(k in t for k in ("tsi", "tfsi", "gasolina", "mpi", "vti", "puretech", "gti", "tce")):
        return "gasolina"
    return None


def texto_motor(c) -> str:
    """Campos de un Anuncio donde puede venir el motor."""
    return f"{c.titulo or ''} {c.motor or ''} {c.modelo or ''}"


def match_cv(comparables, cv_obj, comb_obj, tol):
    out = []
    for c in comparables:
        cvc = extraer_cv(texto_motor(c))
        if cvc and abs(cvc - cv_obj) <= tol:
            if comb_obj and detectar_combustible(texto_motor(c)) not in (comb_obj, None):
                continue
            out.append(c)
    return out


def filtrar_por_motor(comparables, cv_obj, comb_obj):
    """
    Filtra comparables al motor pedido. Devuelve (lista, criterio_legible|None, modo).
    modo: 'cv'   → coincidencia por CV (aunque sean pocos: mejor 1 del motor correcto
                    que muchos del equivocado; jamás cae al pool base si pediste CV).
          'comb' → solo combustible (≥3).
          'pool' → sin filtro de motor.
    """
    if cv_obj:
        crit = f"≈{cv_obj} CV" + (f" · {comb_obj}" if comb_obj else "")
        # CV exacto y luego ampliado. Se usa lo que haya (≥1), no se cae al pool base.
        for tol in (CV_TOL, CV_TOL * 2):
            m = match_cv(comparables, cv_obj, comb_obj, tol)
            if len(m) >= 3:
                return m, crit, "cv"
        m = match_cv(comparables, cv_obj, comb_obj, CV_TOL * 2)
        if m:
            return m, crit, "cv"
        # 0 anuncios de ese CV → intenta combustible; si no, pool (con aviso en pipeline).
    if comb_obj:
        t2 = [c for c in comparables if detectar_combustible(texto_motor(c)) == comb_obj]
        if len(t2) >= 3:
            return t2, comb_obj, "comb"
    return comparables, None, "pool"
