from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Anuncio:
    item_id: str
    fuente: str          # 'wallapop'
    marca: str
    modelo: str
    año: int
    km: int
    precio: float
    provincia: str
    descripcion: str
    url: str
    foto: str = ""
    capturado_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class EstadisticaMercado:
    n_comparables: int
    mediana: float
    media: float
    desviacion: float       # desviación estándar
    percentil: float        # 0-100: posición del anuncio en la distribución
    desviacion_pct: float   # % sobre/bajo la mediana (negativo = más barato)
    precios: list[float] = field(default_factory=list)
