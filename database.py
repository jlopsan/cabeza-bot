# database.py - Gestión de SQLite para misiones de monitoreo + usuarios
import sqlite3
import json
import logging
from datetime import datetime, timedelta
from config import DB_PATH, ALLOWED_USER_IDS, FREE_ANALISIS_MAX, FREE_VENTANA_HORAS

logger = logging.getLogger(__name__)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
        # Migración: contadores de freemium
        for col, ddl in [
            ("first_name",      "TEXT    DEFAULT ''"),
            ("analisis_usados", "INTEGER DEFAULT 0"),
            ("ventana_inicio",  "TEXT    DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE usuarios ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass  # Ya existe

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


# ─── FREEMIUM: límite de análisis por ventana ──────────────────────────────

def get_o_crear_usuario(user_id: int, username: str = "",
                        first_name: str = "") -> dict:
    """Devuelve la fila de usuario; la crea si no existe."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO usuarios "
            "(user_id, username, first_name, tier, created_at, updated_at) "
            "VALUES (?, ?, ?, 'free', ?, ?)",
            (user_id, username, first_name, now, now),
        )
        # Refrescar username/first_name si cambiaron (sin tocar contadores)
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


def _ventana_expirada(ventana_inicio: str) -> bool:
    """True si la ventana está vacía o han pasado >= FREE_VENTANA_HORAS."""
    if not ventana_inicio:
        return True
    try:
        inicio = datetime.fromisoformat(ventana_inicio)
    except ValueError:
        return True
    return datetime.utcnow() - inicio >= timedelta(hours=FREE_VENTANA_HORAS)


def puede_analizar(user_id: int) -> tuple[bool, int]:
    """
    Devuelve (puede, restantes).
    - Whitelist (ALLOWED_USER_IDS) → ilimitado.
    - Si la ventana ha expirado → reset contador a 0.
    """
    if ALLOWED_USER_IDS and user_id in ALLOWED_USER_IDS:
        return True, 999

    with get_conn() as conn:
        row = conn.execute(
            "SELECT analisis_usados, ventana_inicio FROM usuarios WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return True, FREE_ANALISIS_MAX

        usados = row["analisis_usados"] or 0
        if _ventana_expirada(row["ventana_inicio"] or ""):
            conn.execute(
                "UPDATE usuarios SET analisis_usados = 0, ventana_inicio = '' "
                "WHERE user_id = ?",
                (user_id,),
            )
            conn.commit()
            usados = 0

    restantes = max(FREE_ANALISIS_MAX - usados, 0)
    return usados < FREE_ANALISIS_MAX, restantes


def registrar_analisis(user_id: int):
    """Incrementa el contador. Whitelist no descuenta."""
    if ALLOWED_USER_IDS and user_id in ALLOWED_USER_IDS:
        return

    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT analisis_usados, ventana_inicio FROM usuarios WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return

        ventana = row["ventana_inicio"] or ""
        if _ventana_expirada(ventana):
            ventana = now  # arranca nueva ventana
            usados = 1
        else:
            usados = (row["analisis_usados"] or 0) + 1

        conn.execute(
            "UPDATE usuarios SET analisis_usados = ?, ventana_inicio = ?, updated_at = ? "
            "WHERE user_id = ?",
            (usados, ventana, now, user_id),
        )
        conn.commit()


def minutos_hasta_reset(user_id: int) -> int:
    """Minutos restantes hasta que la ventana actual expire. 0 si ya expiró."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT ventana_inicio FROM usuarios WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row or not row["ventana_inicio"]:
        return 0
    try:
        inicio = datetime.fromisoformat(row["ventana_inicio"])
    except ValueError:
        return 0
    fin = inicio + timedelta(hours=FREE_VENTANA_HORAS)
    delta = fin - datetime.utcnow()
    return max(int(delta.total_seconds() // 60), 0)


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