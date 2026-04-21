# database.py - Gestión de SQLite para misiones de monitoreo + usuarios
import sqlite3
import json
import logging
from datetime import datetime
from config import DB_PATH

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