"""
Etiqueta DGT y restricciones ZBE.

Calcula la etiqueta medioambiental DGT (0, ECO, C, B o sin etiqueta) a partir
del combustible y el año de matriculación, y devuelve un texto explicativo
sobre dónde podrá circular el coche.

Tabla simplificada — válida para mayoría de turismos. Casos límite (Euro
intermedios, conversiones GLP a posteriori) pueden requerir consulta a la sede
electrónica de la DGT.
"""


def _normalizar_combustible(c: str) -> str:
    c = (c or "").strip().lower()
    if not c:
        return ""
    if any(k in c for k in ("eléctric", "electric", "ev", "bev")):
        return "electrico"
    if "phev" in c or "enchuf" in c or "plug" in c:
        return "phev"
    if any(k in c for k in ("hibrido", "híbrido", "hybrid", "hev")):
        return "hibrido"
    if "glp" in c or "gnc" in c or "gnv" in c or "gas natural" in c or "gas licuado" in c:
        return "gas"
    if any(k in c for k in ("diesel", "diésel", "tdi", "hdi", "cdi", "bluehdi",
                              "dci", "crdi", "jtd", "d-4d", "bluetec")):
        return "diesel"
    if any(k in c for k in ("gasolina", "petrol", "tsi", "tfsi", "puretech",
                              "vti", "tce", "mpi")):
        return "gasolina"
    return c


def calcular_etiqueta_dgt(combustible: str, año: int) -> str:
    """
    Devuelve '0', 'ECO', 'C', 'B' o 'sin etiqueta'.

    Reglas (turismo M1):
      - Eléctrico                         → 0
      - PHEV / GLP / GNC / Híbrido        → ECO
      - Gasolina ≥ 2006                   → C
      - Gasolina 2000-2005                → B
      - Gasolina < 2000                   → sin etiqueta
      - Diésel ≥ 2015 (Euro 6)            → C
      - Diésel 2006-2014 (Euro 4-5)       → B
      - Diésel < 2006                     → sin etiqueta
    """
    c = _normalizar_combustible(combustible)
    if c == "electrico":
        return "0"
    if c in ("phev", "hibrido", "gas"):
        return "ECO"
    if not año or año <= 0:
        return "sin etiqueta"
    if c == "gasolina":
        if año >= 2006:
            return "C"
        if año >= 2000:
            return "B"
        return "sin etiqueta"
    if c == "diesel":
        if año >= 2015:
            return "C"
        if año >= 2006:
            return "B"
        return "sin etiqueta"
    return "sin etiqueta"


_INFO_ZBE = {
    "0":   "Acceso libre a todas las ZBE (Madrid, Barcelona, Bilbao, etc). Aparcamiento SER gratis en muchas ciudades.",
    "ECO": "Acceso libre a Madrid Central / ZBE Barcelona / ZBE Bilbao. Algunas ventajas en SER.",
    "C":   "Entra a Madrid ZBE y a la mayoría de ZBE actuales. Restricciones futuras posibles a partir de 2030.",
    "B":   "Restringido en Madrid ZBE Distrito Centro. Restringido en ZBE Barcelona. Solo entrada para residentes o autorizados.",
    "sin etiqueta": "NO entra a Madrid ZBE. NO entra a ZBE Barcelona ni Bilbao. Útil solo en zonas rurales o pueblos.",
}


def info_zbe(etiqueta: str) -> str:
    return _INFO_ZBE.get(etiqueta, _INFO_ZBE["sin etiqueta"])
