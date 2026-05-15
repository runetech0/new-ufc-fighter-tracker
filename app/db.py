import aiosqlite

from app.logs_config import get_logger

from .models import Athlete

DB_PATH = "athletes.db"

logger = get_logger()


async def init_db(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS athletes (
            profile_url  TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            nickname     TEXT,
            weight_class TEXT,
            record       TEXT,
            image_url    TEXT
        )
    """)
    await db.commit()

    # Migration: add image_url to existing tables that pre-date this column
    try:
        await db.execute("ALTER TABLE athletes ADD COLUMN image_url TEXT")
        await db.commit()
        logger.info("DB migration: added image_url column.")
    except Exception:
        pass  # column already exists


async def get_athlete_count(db: aiosqlite.Connection) -> int:
    async with db.execute("SELECT COUNT(*) FROM athletes") as cursor:
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_random_athlete(db: aiosqlite.Connection) -> Athlete | None:
    async with db.execute(
        "SELECT profile_url, name, nickname, weight_class, record, image_url "
        "FROM athletes ORDER BY RANDOM() LIMIT 1"
    ) as cursor:
        row = await cursor.fetchone()
        if not row:
            return None
        return Athlete(
            profile_url=row[0],
            name=row[1],
            nickname=row[2],
            weight_class=row[3],
            record=row[4],
            image_url=row[5],
        )


async def save_athlete(db: aiosqlite.Connection, athlete: Athlete) -> bool:
    """Insert athlete; returns True if it was new, False if already existed.
    If the athlete already exists with a NULL image_url, backfills it."""
    cursor = await db.execute(
        """
        INSERT OR IGNORE INTO athletes
            (profile_url, name, nickname, weight_class, record, image_url)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            athlete.profile_url,
            athlete.name,
            athlete.nickname,
            athlete.weight_class,
            athlete.record,
            athlete.image_url,
        ),
    )
    is_new = cursor.rowcount > 0

    # Backfill image_url for existing athletes that were saved before image
    # extraction was added (their image_url column will be NULL).
    if not is_new and athlete.image_url:
        await db.execute(
            "UPDATE athletes SET image_url = ? WHERE profile_url = ? AND image_url IS NULL",
            (athlete.image_url, athlete.profile_url),
        )

    await db.commit()
    return is_new
