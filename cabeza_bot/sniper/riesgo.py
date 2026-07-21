# riesgo.py — Semáforo de riesgo del sniper (fase 2).
#
# Funciones puras, sin I/O: reciben el anuncio y los datasets ya cargados
# (km_dataset, precios_de_dataset) y devuelven nivel + banderas. El caller
# (sniper_pipeline) es quien consulta la BD — así esto se testea sin red ni SQLite.
#
# Principio: priorización, nunca certeza. El mensaje conceptual es "en qué
# unidades merece la pena gastarse el informe VIN / la inspección", no "este
# coche está bien". Ninguna bandera afirma fraude confirmado.

from dataclasses import dataclass, field

NIVEL_VERDE = "VERDE"
NIVEL_AMARILLO = "AMARILLO"
NIVEL_ROJO = "ROJO"

_EMOJI_NIVEL = {NIVEL_VERDE: "🟢", NIVEL_AMARILLO: "🟡", NIVEL_ROJO: "🔴"}


@dataclass
class Bandera:
    nivel: str            # VERDE (informativa) / AMARILLO / ROJO
    texto: str


@dataclass
class Riesgo:
    nivel: str
    banderas: list[Bandera] = field(default_factory=list)

    @property
    def emoji(self) -> str:
        return _EMOJI_NIVEL.get(self.nivel, "🟡")

    def to_dict(self) -> dict:
        return {
            "nivel": self.nivel,
            "banderas": [{"nivel": b.nivel, "texto": b.texto} for b in self.banderas],
        }


# ─── REGLA 1 — REIMPORT ──────────────────────────────────────────────────────

def _regla_reimport(anuncio: dict) -> Bandera | None:
    """
    Reimport / EU-Neuwagen: el historial de km no es verificable entre países.
    El fraude de km se concentra en reimportados; primera matriculación alemana
    con historial continuo es la señal de seguridad, no al revés.
    """
    if anuncio.get("reimport"):
        return Bandera(
            NIVEL_ROJO,
            "Posible reimport — historial de km no verificable entre países",
        )
    return None


# ─── REGLA 2 — PLAUSIBILIDAD DE KM ───────────────────────────────────────────

def _percentil(valor: float, muestra: list[float]) -> float:
    """% de la muestra que está POR DEBAJO de `valor` (0-100)."""
    if not muestra:
        return 50.0
    menores = sum(1 for x in muestra if x < valor)
    iguales = sum(1 for x in muestra if x == valor)
    return round((menores + 0.5 * iguales) / len(muestra) * 100, 1)


def _regla_km(anuncio: dict, km_dataset: list[float] | None,
             min_muestra: int, pctl_amarillo: float, pctl_rojo: float) -> tuple[Bandera | None, str]:
    """
    Km del anuncio vs distribución del dataset (mismo modelo, año±tol). Km bajos
    para su edad = señal de alerta (posible retoque), NUNCA de chollo.
    Devuelve (bandera|None, sub_estado) — sub_estado='datos_insuficientes' si no
    hay muestra suficiente para opinar (nunca se inventa una bandera sin datos).
    """
    km = anuncio.get("km", 0) or 0
    if not km_dataset or len(km_dataset) < min_muestra:
        return None, "datos_insuficientes"

    pctl = _percentil(km, km_dataset)
    if pctl < pctl_rojo:
        return Bandera(
            NIVEL_ROJO,
            f"Km en el percentil {pctl:.0f} — muy por debajo de lo normal para su edad. "
            "Verifica historial de ITVs alemanas (TÜV/HU).",
        ), "evaluado"
    if pctl < pctl_amarillo:
        return Bandera(
            NIVEL_AMARILLO,
            f"Km en el percentil {pctl:.0f} — por debajo de lo normal para su edad. "
            "Pide histórico de revisiones.",
        ), "evaluado"
    return None, "evaluado"


# ─── REGLA 3 — PRECIO ANÓMALO vs GEMELOS ALEMANES ────────────────────────────

def _regla_precio_anomalo(anuncio: dict, precios_de_dataset: list[float] | None,
                          min_muestra: int, umbral_pct: float) -> Bandera | None:
    """
    Precio muy por debajo de sus gemelos EN ALEMANIA (no España) = patrón de
    estafa, no de chollo. Comparar dentro del mismo mercado es la clave: un
    precio bajo vs España puede ser solo el arbitraje normal del negocio.
    """
    precio = anuncio.get("precio", 0) or 0
    if not precios_de_dataset or len(precios_de_dataset) < min_muestra or precio <= 0:
        return None
    import statistics
    mediana = statistics.median(precios_de_dataset)
    if mediana <= 0:
        return None
    ratio = precio / mediana
    if ratio < (umbral_pct / 100):
        pct_bajo = round((1 - ratio) * 100)
        return Bandera(
            NIVEL_ROJO,
            f"Precio {pct_bajo}% por debajo de sus gemelos alemanes — "
            "patrón de estafa, no de chollo",
        )
    return None


# ─── REGLA 4 — SEÑALES BLANDAS ───────────────────────────────────────────────

def _señales_blandas(anuncio: dict, fotos_min: int, propietarios_max: int) -> list[Bandera]:
    """
    Ninguna es roja por sí sola; cada una suma al nivel general. Los alemanes
    declaran Unfallfrei/Scheckheftgepflegt siempre que lo tienen — su ausencia
    es una señal débil, no una acusación.
    """
    blandas: list[Bandera] = []

    if anuncio.get("vendedor") == "particular":
        blandas.append(Bandera(NIVEL_VERDE, "Vendedor particular — sin garantía legal de comercio"))

    if not anuncio.get("unfallfrei"):
        blandas.append(Bandera(NIVEL_VERDE, "No declara \"Unfallfrei\" (sin accidentes)"))

    if not anuncio.get("scheckheftgepflegt"):
        blandas.append(Bandera(NIVEL_VERDE, "No declara libro de revisiones (Scheckheftgepflegt)"))

    propietarios = anuncio.get("propietarios", 0) or 0
    if propietarios and propietarios > propietarios_max:
        blandas.append(Bandera(NIVEL_VERDE, f"{propietarios} propietarios anteriores"))

    num_fotos = anuncio.get("num_fotos", 0) or 0
    if 0 < num_fotos < fotos_min:
        blandas.append(Bandera(NIVEL_VERDE, f"Solo {num_fotos} fotos en el anuncio"))

    return blandas


# ─── COMPOSICIÓN DEL SEMÁFORO ────────────────────────────────────────────────

def evaluar_riesgo(
    anuncio: dict,
    km_dataset: list[float] | None = None,
    precios_de_dataset: list[float] | None = None,
    *,
    km_min_muestra: int = 8,
    km_pctl_amarillo: float = 10.0,
    km_pctl_rojo: float = 3.0,
    precio_de_min_muestra: int = 5,
    precio_de_anomalo_pct: float = 55.0,
    fotos_min: int = 4,
    propietarios_max: int = 3,
    blandas_amarillo: int = 2,
) -> Riesgo:
    """
    Semáforo VERDE/AMARILLO/ROJO. Cualquier bandera ROJA → nivel ROJO. Si no hay
    rojas: >=1 amarilla o >=N blandas → AMARILLO. Si no hay ninguna señal → VERDE.
    Las banderas VERDE son informativas (se muestran, no penalizan el nivel).
    """
    banderas: list[Bandera] = []

    b_reimport = _regla_reimport(anuncio)
    if b_reimport:
        banderas.append(b_reimport)

    b_km, _sub = _regla_km(anuncio, km_dataset, km_min_muestra, km_pctl_amarillo, km_pctl_rojo)
    if b_km:
        banderas.append(b_km)

    b_precio = _regla_precio_anomalo(anuncio, precios_de_dataset, precio_de_min_muestra, precio_de_anomalo_pct)
    if b_precio:
        banderas.append(b_precio)

    blandas = _señales_blandas(anuncio, fotos_min, propietarios_max)
    banderas.extend(blandas)

    if any(b.nivel == NIVEL_ROJO for b in banderas):
        nivel = NIVEL_ROJO
    elif any(b.nivel == NIVEL_AMARILLO for b in banderas) or len(blandas) >= blandas_amarillo:
        nivel = NIVEL_AMARILLO
    else:
        nivel = NIVEL_VERDE

    return Riesgo(nivel=nivel, banderas=banderas)
