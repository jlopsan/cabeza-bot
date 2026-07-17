# database.py - Gestión de SQLite para misiones de monitoreo + usuarios
import sqlite3
import json
import logging
from datetime import datetime
from config import DB_PATH, ALLOWED_USER_IDS, FREE_CREDITOS, PAID_CREDITOS_PACK_10, PAID_CREDITOS_PACK_100

logger = logging.getLogger(__name__)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    """Crea las tablas si no existen y ejecuta migraciones."""
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS misiones (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id            INTEGER NOT NULL,
                query_modelo       TEXT NOT NULL,
                filtros            TEXT DEFAULT '{}',
                precio_objetivo_es REAL,
                ids_rechazados     TEXT DEFAULT '[]',
                estado             TEXT DEFAULT 'ACTIVA',
                prioridad          TEXT DEFAULT 'normal',
                created_at         TEXT,
                updated_at         TEXT
            )
        """)
        # Migración: añadir columna prioridad si no existe
        try:
            conn.execute("SELECT prioridad FROM misiones LIMIT 1")
        except Exception:
            conn.execute("ALTER TABLE misiones ADD COLUMN prioridad TEXT DEFAULT 'normal'")
        # Migración: permitir NULL en precio_objetivo_es
        try:
            conn.execute("INSERT INTO misiones (user_id, query_modelo, precio_objetivo_es) "
                         "VALUES (-1, '__test__', NULL)")
            conn.execute("DELETE FROM misiones WHERE user_id = -1")
        except Exception:
            conn.execute("ALTER TABLE misiones RENAME TO misiones_old")
            conn.execute("""
                CREATE TABLE misiones (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id            INTEGER NOT NULL,
                    query_modelo       TEXT NOT NULL,
                    filtros            TEXT DEFAULT '{}',
                    precio_objetivo_es REAL,
                    ids_rechazados     TEXT DEFAULT '[]',
                    estado             TEXT DEFAULT 'ACTIVA',
                    prioridad          TEXT DEFAULT 'normal',
                    created_at         TEXT,
                    updated_at         TEXT
                )
            """)
            conn.execute("INSERT INTO misiones (id, user_id, query_modelo, filtros, "
                         "precio_objetivo_es, ids_rechazados, estado, created_at, updated_at) "
                         "SELECT * FROM misiones_old")
            conn.execute("DROP TABLE misiones_old")
        conn.commit()

        conn.execute("""
            CREATE TABLE IF NOT EXISTS oportunidades_enviadas (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                mision_id   INTEGER NOT NULL,
                coche_id    TEXT NOT NULL,
                enviado_at  TEXT,
                UNIQUE(mision_id, coche_id)
            )
        """)

        # Tabla de usuarios con tier de acceso
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                tier        TEXT DEFAULT 'free',
                created_at  TEXT,
                updated_at  TEXT
            )
        """)
        # Migraciones: columnas freemium (las antiguas se conservan por compatibilidad)
        for col, ddl in [
            ("first_name",             "TEXT    DEFAULT ''"),
            ("analisis_usados",        "INTEGER DEFAULT 0"),   # legacy
            ("ventana_inicio",         "TEXT    DEFAULT ''"),   # legacy
            ("analisis_pack",          "INTEGER DEFAULT 0"),    # legacy
            ("analisis_mes",           "INTEGER DEFAULT 0"),    # legacy
            ("mes_actual",             "TEXT    DEFAULT ''"),   # legacy
            ("stripe_customer_id",     "TEXT    DEFAULT ''"),
            ("stripe_subscription_id", "TEXT    DEFAULT ''"),
            # Plan A: créditos diarios unificados
            ("creditos_disponibles",   "INTEGER DEFAULT 3"),
            ("ultimo_reset_diario",    "TEXT    DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE usuarios ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass  # Ya existe

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pagos (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                stripe_id   TEXT UNIQUE,
                concepto    TEXT,
                importe     REAL,
                estado      TEXT DEFAULT 'completado',
                created_at  TEXT
            )
        """)

        # Ofertas ya publicadas en el canal (scanner)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scanner_enviados (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                coche_id    TEXT NOT NULL UNIQUE,
                enviado_at  TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS historico_precios (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                fuente        TEXT NOT NULL,
                item_id       TEXT NOT NULL,
                marca         TEXT,
                modelo        TEXT,
                año           INTEGER,
                km            INTEGER,
                precio        REAL,
                provincia     TEXT,
                url           TEXT,
                capturado_at  TEXT NOT NULL,
                UNIQUE(fuente, item_id, capturado_at)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_modelo ON historico_precios(marca, modelo)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_fecha  ON historico_precios(capturado_at)")

        # Eventos: una fila por uso de comando
        conn.execute("""
            CREATE TABLE IF NOT EXISTS eventos_comando (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER NOT NULL,
                comando  TEXT    NOT NULL,
                ts       TEXT    NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_evt_user ON eventos_comando(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_evt_cmd  ON eventos_comando(comando)")

        # Métricas /ideal v2: una fila por flujo terminado
        conn.execute("""
            CREATE TABLE IF NOT EXISTS eventos_ideal (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                timestamp       TEXT    NOT NULL,
                slots_json      TEXT,
                candidatos_json TEXT,
                top3_json       TEXT,
                accion_user     TEXT,
                duracion_s      INTEGER
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_eideal_user ON eventos_ideal(user_id)")
        conn.commit()


# ─── MISIONES ────────────────────────────────────────────────────────────────

def crear_mision(user_id: int, query_modelo: str, filtros: dict,
                 precio_objetivo_es: float | None,
                 prioridad: str = "normal") -> int:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO misiones
               (user_id, query_modelo, filtros, precio_objetivo_es, estado, prioridad, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'ACTIVA', ?, ?, ?)""",
            (user_id, query_modelo, json.dumps(filtros), precio_objetivo_es, prioridad, now, now),
        )
        conn.commit()
        return cur.lastrowid


def obtener_misiones_activas(prioridad: str | None = None) -> list[dict]:
    with get_conn() as conn:
        if prioridad:
            rows = conn.execute(
                "SELECT * FROM misiones WHERE estado = 'ACTIVA' AND prioridad = ?",
                (prioridad,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM misiones WHERE estado = 'ACTIVA'"
            ).fetchall()
    return [dict(r) for r in rows]


def obtener_misiones_usuario(user_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM misiones WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def pausar_mision(mision_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE misiones SET estado='PAUSADA', updated_at=? WHERE id=?",
            (datetime.utcnow().isoformat(), mision_id),
        )
        conn.commit()


def activar_mision(mision_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE misiones SET estado='ACTIVA', updated_at=? WHERE id=?",
            (datetime.utcnow().isoformat(), mision_id),
        )
        conn.commit()


def rechazar_coche(mision_id: int, coche_id: str):
    """Añade un ID de coche a la lista de rechazados de la misión."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT ids_rechazados FROM misiones WHERE id=?", (mision_id,)
        ).fetchone()
        if row:
            ids = json.loads(row["ids_rechazados"])
            if coche_id not in ids:
                ids.append(coche_id)
            conn.execute(
                "UPDATE misiones SET ids_rechazados=?, updated_at=? WHERE id=?",
                (json.dumps(ids), datetime.utcnow().isoformat(), mision_id),
            )
            conn.commit()


# ─── OPORTUNIDADES ───────────────────────────────────────────────────────────

def ya_enviada(mision_id: int, coche_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM oportunidades_enviadas WHERE mision_id=? AND coche_id=?",
            (mision_id, coche_id),
        ).fetchone()
    return row is not None


def marcar_enviada(mision_id: int, coche_id: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO oportunidades_enviadas (mision_id, coche_id, enviado_at) VALUES (?,?,?)",
            (mision_id, coche_id, datetime.utcnow().isoformat()),
        )
        conn.commit()


def eliminar_mision(mision_id: int, user_id: int) -> bool:
    """Elimina una misión si pertenece al usuario. Devuelve True si se borró."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM misiones WHERE id = ? AND user_id = ?",
            (mision_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


# ─── USUARIOS ───────────────────────────────────────────────────────────────

def obtener_usuario(user_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM usuarios WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def registrar_usuario(user_id: int, username: str = "", tier: str = "free"):
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO usuarios (user_id, username, tier, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, tier, now, now),
        )
        conn.commit()


def cambiar_tier(user_id: int, nuevo_tier: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE usuarios SET tier = ?, updated_at = ? WHERE user_id = ?",
            (nuevo_tier, datetime.utcnow().isoformat(), user_id),
        )
        conn.commit()


def obtener_tier(user_id: int) -> str:
    u = obtener_usuario(user_id)
    return u["tier"] if u else "free"


# ─── FREEMIUM: créditos unificados ─────────────────────────────────────────
#
# Modelo Plan A:
#   free  → FREE_CREDITOS créditos de por vida, una sola vez, SIN reset
#   paid  → créditos del pack comprado, sin caducidad (creditos_disponibles)
#   pro   → ilimitado (dormido — para cuando se lance suscripción)
#
# Cada comando tiene un coste en COSTE_COMANDO (permisos.py).
# Hoy todo cuesta 1. Mañana se puede cambiar el mapa sin tocar la BD.

def get_o_crear_usuario(user_id: int, username: str = "",
                        first_name: str = "") -> dict:
    """Devuelve la fila de usuario; la crea si no existe."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO usuarios "
            "(user_id, username, first_name, tier, creditos_disponibles, created_at, updated_at) "
            "VALUES (?, ?, ?, 'free', ?, ?, ?)",
            (user_id, username, first_name, FREE_CREDITOS, now, now),
        )
        conn.execute(
            "UPDATE usuarios SET username = ?, first_name = ?, updated_at = ? "
            "WHERE user_id = ?",
            (username, first_name, now, user_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM usuarios WHERE user_id = ?", (user_id,)
        ).fetchone()
    return dict(row)


def puede_usar(user_id: int, coste: int = 1) -> tuple[bool, int]:
    """
    Devuelve (puede, creditos_restantes).
    - Whitelist/admin → ilimitado.
    - pro  → siempre puede.
    - free → créditos de por vida; bloquea si creditos_disponibles < coste. SIN reset.
    - paid → descuenta de creditos_disponibles (sin caducidad).
    """
    if ALLOWED_USER_IDS and user_id in ALLOWED_USER_IDS:
        return True, 999

    with get_conn() as conn:
        row = conn.execute(
            "SELECT tier, creditos_disponibles FROM usuarios WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return True, FREE_CREDITOS

        tier     = row["tier"] or "free"
        creditos = row["creditos_disponibles"] if row["creditos_disponibles"] is not None else FREE_CREDITOS

        if tier == "pro":
            return True, 999

        return creditos >= coste, max(creditos, 0)


def registrar_uso(user_id: int, coste: int = 1):
    """
    Descuenta créditos según el tier. Whitelist no descuenta.
    - pro:  no hace nada (ilimitado).
    - free: descuenta de creditos_disponibles (de por vida, sin reset).
    - paid: descuenta de creditos_disponibles; al llegar a 0 queda bloqueado
            hasta recargar pack (el tier se mantiene paid).
    """
    if ALLOWED_USER_IDS and user_id in ALLOWED_USER_IDS:
        return

    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT tier, creditos_disponibles FROM usuarios WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return

        tier     = row["tier"] or "free"
        creditos = row["creditos_disponibles"] if row["creditos_disponibles"] is not None else 0

        if tier == "pro":
            return

        nuevo = max(creditos - coste, 0)
        conn.execute(
            "UPDATE usuarios SET creditos_disponibles = ?, updated_at = ? "
            "WHERE user_id = ?",
            (nuevo, now, user_id),
        )
        conn.commit()


# Alias para compatibilidad con código existente que llama registrar_analisis
def registrar_analisis(user_id: int):
    registrar_uso(user_id, coste=1)


# Alias para compatibilidad con código existente que llama puede_analizar
def puede_analizar(user_id: int) -> tuple[bool, int]:
    return puede_usar(user_id, coste=1)


_CREDITOS_POR_PACK = {
    "pack_10":  PAID_CREDITOS_PACK_10,
    "pack_100": PAID_CREDITOS_PACK_100,
}


def activar_plan(user_id: int, concepto: str, stripe_id: str = "",
                 stripe_customer_id: str = "", stripe_subscription_id: str = ""):
    """
    Activa el plan tras pago confirmado. Idempotente via stripe_id.
    concepto='pack_10'  → tier='paid', creditos_disponibles += 10 (acumula si ya era paid).
    concepto='pack_100' → tier='paid', creditos_disponibles += 100 (acumula si ya era paid).
    concepto='pro_mes'  → tier='pro' (dormido — para cuando se lance suscripción).
    """
    if concepto not in _CREDITOS_POR_PACK and concepto != "pro_mes":
        logger.warning(f"[PAGO] Concepto desconocido: {concepto}")
        return

    now = datetime.utcnow().isoformat()

    with get_conn() as conn:
        # Idempotencia atómica: si stripe_id ya existe en pagos, rowcount=0 → return.
        # Esto cierra la race entre dos webhooks paralelos para el mismo event_id.
        if stripe_id:
            cur = conn.execute(
                "INSERT OR IGNORE INTO pagos (user_id, stripe_id, concepto, estado, created_at) "
                "VALUES (?, ?, ?, 'completado', ?)",
                (user_id, stripe_id, concepto, now),
            )
            if cur.rowcount == 0:
                return

        if concepto in _CREDITOS_POR_PACK:
            row = conn.execute(
                "SELECT tier, creditos_disponibles FROM usuarios WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            actuales = (row["creditos_disponibles"] or 0) if row else 0
            nuevos = actuales + _CREDITOS_POR_PACK[concepto]
            conn.execute(
                "UPDATE usuarios SET tier = 'paid', creditos_disponibles = ?, "
                "stripe_customer_id = COALESCE(NULLIF(?, ''), stripe_customer_id), "
                "updated_at = ? WHERE user_id = ?",
                (nuevos, stripe_customer_id, now, user_id),
            )
        else:  # pro_mes
            conn.execute(
                "UPDATE usuarios SET tier = 'pro', "
                "stripe_customer_id = COALESCE(NULLIF(?, ''), stripe_customer_id), "
                "stripe_subscription_id = COALESCE(NULLIF(?, ''), stripe_subscription_id), "
                "updated_at = ? WHERE user_id = ?",
                (stripe_customer_id, stripe_subscription_id, now, user_id),
            )

        conn.commit()


def desactivar_pro(user_id: int):
    """Llamado cuando Stripe cancela la suscripción. Vuelve a free."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE usuarios SET tier = 'free', creditos_disponibles = ?, "
            "updated_at = ? WHERE user_id = ?",
            (FREE_CREDITOS, now, user_id),
        )
        conn.commit()


def pago_ya_procesado(stripe_id: str) -> bool:
    """True si el stripe_id ya está en la tabla pagos (idempotencia)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM pagos WHERE stripe_id = ?", (stripe_id,)
        ).fetchone()
    return row is not None


# ─── EVENTOS (uso de comandos) ─────────────────────────────────────────────

def registrar_evento(user_id: int, comando: str):
    """Guarda una fila por cada uso de comando."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO eventos_comando (user_id, comando, ts) VALUES (?, ?, ?)",
            (user_id, comando, datetime.utcnow().isoformat()),
        )
        conn.commit()


def stats_comandos_globales() -> list[dict]:
    """Total de usos por comando, agregado de todos los usuarios."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT comando, COUNT(*) AS usos, COUNT(DISTINCT user_id) AS usuarios "
            "FROM eventos_comando GROUP BY comando ORDER BY usos DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def resumen_stats() -> dict:
    """Resumen agregado para /stats: usuarios, eventos, top comandos, top usuarios."""
    with get_conn() as conn:
        total_usuarios = conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
        nuevos_hoy = conn.execute(
            "SELECT COUNT(*) FROM usuarios WHERE DATE(created_at) = DATE('now')"
        ).fetchone()[0]
        nuevos_7d = conn.execute(
            "SELECT COUNT(*) FROM usuarios WHERE DATE(created_at) >= DATE('now','-7 day')"
        ).fetchone()[0]
        total_eventos = conn.execute("SELECT COUNT(*) FROM eventos_comando").fetchone()[0]
        eventos_hoy = conn.execute(
            "SELECT COUNT(*) FROM eventos_comando WHERE DATE(ts) = DATE('now')"
        ).fetchone()[0]
        activos_hoy = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM eventos_comando WHERE DATE(ts) = DATE('now')"
        ).fetchone()[0]
        activos_7d = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM eventos_comando "
            "WHERE DATE(ts) >= DATE('now','-7 day')"
        ).fetchone()[0]
        top_cmd = conn.execute(
            "SELECT comando, COUNT(*) usos, COUNT(DISTINCT user_id) usuarios "
            "FROM eventos_comando GROUP BY comando ORDER BY usos DESC LIMIT 10"
        ).fetchall()
        top_users = conn.execute(
            "SELECT u.user_id, COALESCE(NULLIF(u.username,''), u.first_name, '?') AS nombre, "
            "COUNT(e.id) AS usos "
            "FROM usuarios u LEFT JOIN eventos_comando e ON e.user_id = u.user_id "
            "GROUP BY u.user_id HAVING usos > 0 ORDER BY usos DESC LIMIT 10"
        ).fetchall()
        ult_dias = conn.execute(
            "SELECT DATE(ts) dia, COUNT(*) usos, COUNT(DISTINCT user_id) usuarios "
            "FROM eventos_comando WHERE DATE(ts) >= DATE('now','-6 day') "
            "GROUP BY dia ORDER BY dia DESC"
        ).fetchall()
    return {
        "total_usuarios": total_usuarios,
        "nuevos_hoy": nuevos_hoy,
        "nuevos_7d": nuevos_7d,
        "total_eventos": total_eventos,
        "eventos_hoy": eventos_hoy,
        "activos_hoy": activos_hoy,
        "activos_7d": activos_7d,
        "top_comandos": [dict(r) for r in top_cmd],
        "top_usuarios": [dict(r) for r in top_users],
        "ultimos_dias": [dict(r) for r in ult_dias],
    }


def registrar_evento_ideal(user_id: int, slots: dict, candidatos: list,
                           top3: list, accion: str, duracion_s: int = 0):
    """Persiste un evento del flujo /ideal v2 para análisis posterior."""
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO eventos_ideal "
                "(user_id, timestamp, slots_json, candidatos_json, top3_json, accion_user, duracion_s) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    datetime.utcnow().isoformat(),
                    json.dumps(slots, ensure_ascii=False, default=str)[:8000],
                    json.dumps(candidatos, ensure_ascii=False, default=str)[:12000],
                    json.dumps(top3, ensure_ascii=False, default=str)[:12000],
                    accion,
                    duracion_s,
                ),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"[IDEAL_EVT] Error registrando: {e}")


def stats_comandos_usuario(user_id: int) -> list[dict]:
    """Usos por comando de un usuario concreto."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT comando, COUNT(*) AS usos FROM eventos_comando "
            "WHERE user_id = ? GROUP BY comando ORDER BY usos DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ─── SCANNER (canal gratuito) ──────────────────────────────────────────────

def scanner_ya_enviado(coche_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM scanner_enviados WHERE coche_id = ?", (coche_id,)
        ).fetchone()
    return row is not None


def scanner_marcar_enviado(coche_id: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO scanner_enviados (coche_id, enviado_at) VALUES (?, ?)",
            (coche_id, datetime.utcnow().isoformat()),
        )
        conn.commit()


# ─── HISTÓRICO DE PRECIOS ────────────────────────────────────────────────────

def purgar_historico_antiguo(dias: int = 180) -> int:
    """Elimina entradas de historico_precios anteriores a N días. Devuelve filas borradas."""
    try:
        with get_conn() as conn:
            cur = conn.execute(
                "DELETE FROM historico_precios WHERE capturado_at < datetime('now', ?)",
                (f"-{dias} days",),
            )
            conn.commit()
            n = cur.rowcount
            if n:
                logger.info(f"[HIST] Purgados {n} registros con más de {dias} días")
            return n
    except Exception as e:
        logger.error(f"[HIST] Error en purgar_historico_antiguo: {e}")
        return 0


def guardar_historico_batch(anuncios: list) -> int:
    """
    Persiste una lista de Anuncio en historico_precios.
    Ignora duplicados (fuente, item_id, capturado_at).
    Devuelve el número de filas insertadas.
    """
    if not anuncios:
        return 0
    insertados = 0
    try:
        with get_conn() as conn:
            for a in anuncios:
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO historico_precios
                           (fuente, item_id, marca, modelo, año, km, precio, provincia, url, capturado_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (a.fuente, a.item_id, a.marca, a.modelo,
                         a.año, a.km, a.precio, a.provincia, a.url, a.capturado_at),
                    )
                    insertados += 1
                except Exception as e:
                    logger.warning(f"[HIST] Error insertando {a.item_id}: {e}")
            conn.commit()
    except Exception as e:
        logger.error(f"[HIST] Error en guardar_historico_batch: {e}")
    logger.info(f"[HIST] {insertados}/{len(anuncios)} anuncios guardados en histórico")
    return insertados