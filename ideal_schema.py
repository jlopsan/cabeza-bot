"""
ideal_schema.py — Schema de slots del flujo /ideal v2.

SlotsIdeal contiene los requisitos del usuario. Slot-filling antes de
recomendar. Sin slots críticos llenos, no hay candidatos.
"""
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class SlotsIdeal:
    # ── CRÍTICOS ────────────────────────────────────────────────────────────
    presupuesto_max: Optional[int] = None          # €
    presupuesto_min: Optional[int] = None          # auto: max * 0.5
    uso_principal: Optional[str] = None            # ciudad|mixto|autovia|montaña|offroad
    pasajeros_habituales: Optional[int] = None     # 1-7
    km_anuales: Optional[int] = None
    zbe_relevante: Optional[bool] = None
    aversion_taller: Optional[bool] = None

    # ── SECUNDARIOS ─────────────────────────────────────────────────────────
    combustible_preferencia: str = "indistinto"    # gasolina|diesel|hibrido|phev|electrico|indistinto
    cambio: str = "indistinto"                     # manual|automatico|indistinto
    tamaño_preferencia: str = "indistinto"         # urbano|compacto|familiar|suv|monovolumen|indistinto
    carga_habitual: str = "normal"                 # poca|normal|mucha|perro_grande
    antiguedad_max_años: Optional[int] = None
    km_max_aceptable: Optional[int] = None

    # ── CONTEXTO ────────────────────────────────────────────────────────────
    experiencia_previa: list[str] = field(default_factory=list)
    rechazos_explicitos: list[str] = field(default_factory=list)
    prioridad: str = "fiabilidad"                  # fiabilidad|comodidad|deportividad|eficiencia|espacio

    # ── SLOTS CRÍTICOS ──────────────────────────────────────────────────────
    SLOTS_CRITICOS = (
        "presupuesto_max", "uso_principal", "pasajeros_habituales",
        "km_anuales", "zbe_relevante", "aversion_taller",
    )

    def slots_criticos_faltantes(self) -> list[str]:
        faltantes: list[str] = []
        for slot in self.SLOTS_CRITICOS:
            if getattr(self, slot) is None:
                faltantes.append(slot)
        return faltantes

    def normalizar(self) -> None:
        """Rellena defaults derivables después de slot-filling."""
        if self.presupuesto_max and not self.presupuesto_min:
            self.presupuesto_min = int(self.presupuesto_max * 0.5)
        if self.presupuesto_max and not self.km_max_aceptable:
            # Heurística: presupuesto bajo → tolerar más km; alto → exigir menos
            if self.presupuesto_max < 8000:
                self.km_max_aceptable = 200_000
            elif self.presupuesto_max < 15000:
                self.km_max_aceptable = 160_000
            elif self.presupuesto_max < 25000:
                self.km_max_aceptable = 120_000
            else:
                self.km_max_aceptable = 90_000
        if self.presupuesto_max and not self.antiguedad_max_años:
            if self.presupuesto_max < 8000:
                self.antiguedad_max_años = 14
            elif self.presupuesto_max < 15000:
                self.antiguedad_max_años = 10
            elif self.presupuesto_max < 25000:
                self.antiguedad_max_años = 7
            else:
                self.antiguedad_max_años = 5

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SlotsIdeal":
        if not data:
            return cls()
        permitidos = {f for f in cls.__dataclass_fields__.keys()}
        limpio = {k: v for k, v in data.items() if k in permitidos}
        return cls(**limpio)

    def merge(self, otros: dict) -> None:
        """Sobrescribe slots con los valores no-None del dict otros."""
        if not otros:
            return
        for k, v in otros.items():
            if v is None:
                continue
            if k not in self.__dataclass_fields__:
                continue
            # listas: extender únicas
            actual = getattr(self, k)
            if isinstance(actual, list) and isinstance(v, list):
                for item in v:
                    if item not in actual:
                        actual.append(item)
            else:
                setattr(self, k, v)


PLANTILLAS_PREGUNTAS: dict[str, str] = {
    "presupuesto_max":      "💰 ¿Qué presupuesto máximo manejas? <i>(en euros)</i>",
    "uso_principal":        "🛣️ ¿Para qué lo usarás más? <i>Ciudad, autovía, mixto, montaña, offroad.</i>",
    "pasajeros_habituales": "👥 ¿Cuántos sois habitualmente? <i>(número)</i>",
    "km_anuales":           "📏 ¿Cuántos km haces al año?",
    "zbe_relevante":        "🚦 ¿Vives en ciudad con ZBE (Madrid Central, Barcelona…)? <i>Sí/No.</i>",
    "aversion_taller":      "🔧 ¿Prefieres pagar más por algo que no dé un solo problema? <i>Sí/No.</i>",
}

# Defaults razonables si el user no sabe / no responde tras 2 intentos.
# Presupuesto NO tiene default — sin él no se puede recomendar.
DEFAULTS_SLOTS: dict[str, object] = {
    "uso_principal":        "mixto",
    "pasajeros_habituales": 4,
    "km_anuales":           12000,
    "zbe_relevante":        False,
    "aversion_taller":      True,
}

_SKIP_PATTERNS = (
    "no sé", "no se", "ni idea", "no lo sé", "no lo se",
    "no me importa", "me da igual", "da igual", "lo que sea",
    "skip", "siguiente", "siguente", "pasa", "pasalo", "pásalo",
    "tu decides", "tu eliges", "elige tu", "como tu veas", "como veas",
    "no opino", "ninguna preferencia", "sin preferencia",
)


def es_skip(texto: str) -> bool:
    t = (texto or "").strip().lower()
    if not t:
        return False
    return any(p in t for p in _SKIP_PATTERNS)


def parsear_respuesta_corta(slot: str, texto: str):
    """
    Parser determinista para respuestas cortas a un slot concreto.
    Devuelve el valor parseado o None si no logra extraer nada.
    """
    import re as _re
    t = (texto or "").strip().lower()
    if not t:
        return None

    if slot == "presupuesto_max":
        m_k = _re.search(r"(\d+[\.,]?\d*)\s*k\b", t)
        if m_k:
            try:
                return int(float(m_k.group(1).replace(",", ".")) * 1000)
            except Exception:
                return None
        num = _re.sub(r"[^\d]", "", t)
        if not num:
            return None
        n = int(num)
        return n * 1000 if n < 1000 else n

    if slot == "pasajeros_habituales":
        if any(x in t for x in ("siete", "7+", "7 ")):
            return 7
        if any(x in t for x in ("seis", "6 ")):
            return 6
        if any(x in t for x in ("cinco", "5 ")):
            return 5
        if any(x in t for x in ("cuatro", "4 ")):
            return 4
        if any(x in t for x in ("tres", "3 ")):
            return 3
        if any(x in t for x in ("dos", "pareja", "2 ")):
            return 2
        if any(x in t for x in ("uno", "solo yo", "1 ")):
            return 1
        m = _re.search(r"\b(\d)\b", t)
        if m:
            return int(m.group(1))
        return None

    if slot == "km_anuales":
        m_k = _re.search(r"(\d+[\.,]?\d*)\s*k\b", t)
        if m_k:
            try:
                return int(float(m_k.group(1).replace(",", ".")) * 1000)
            except Exception:
                return None
        num = _re.sub(r"[^\d]", "", t)
        if not num:
            return None
        n = int(num)
        return n * 1000 if n < 1000 else n

    if slot in ("zbe_relevante", "aversion_taller"):
        if any(x in t for x in ("si", "sí", "yes", "claro", "afirmativo", "obvio", "prefiero")):
            return True
        if any(x in t for x in (" no", "no.", "nope", "negativo", "para nada", "qué va", "que va")):
            return False
        if t in ("si", "sí", "s"):
            return True
        if t in ("no", "n"):
            return False
        return None

    if slot == "uso_principal":
        _MAP = {
            "ciudad": "ciudad", "urbano": "ciudad", "urbana": "ciudad",
            "autovia": "autovia", "autovía": "autovia", "autopista": "autovia",
            "carretera": "autovia", "viaje": "autovia",
            "mixto": "mixto", "todo": "mixto",
            "montaña": "montaña", "montana": "montaña",
            "offroad": "offroad", "off-road": "offroad", "campo": "offroad", "4x4": "offroad",
        }
        for k, v in _MAP.items():
            if k in t:
                return v
        return None

    return None


def generar_preguntas_clarificacion(slots: SlotsIdeal) -> str:
    """Devuelve UNA pregunta concreta del primer slot crítico faltante."""
    faltan = slots.slots_criticos_faltantes()
    if not faltan:
        return ""
    siguiente = faltan[0]
    n_faltan = len(faltan)
    cabecera = "<b>Una cosa más para acertar:</b>" if n_faltan <= 1 else (
        f"<b>Me faltan {n_faltan} datos. Vamos uno a uno.</b>"
    )
    return (
        f"{cabecera}\n\n"
        f"{PLANTILLAS_PREGUNTAS[siguiente]}\n\n"
        "<i>Si no lo sabes, di «no sé» y sigo.</i>"
    )


def slot_actual_pregunta(slots: SlotsIdeal) -> str | None:
    faltan = slots.slots_criticos_faltantes()
    return faltan[0] if faltan else None
