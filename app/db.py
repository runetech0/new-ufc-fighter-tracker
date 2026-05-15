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
            image_url    TEXT,
            is_active    INTEGER NOT NULL DEFAULT 1
        )
    """)
    await db.commit()

    for col, definition in [
        ("image_url", "TEXT"),
        ("is_active", "INTEGER NOT NULL DEFAULT 1"),
    ]:
        try:
            await db.execute(f"ALTER TABLE athletes ADD COLUMN {col} {definition}")
            await db.commit()
            logger.info(f"DB migration: added '{col}' column.")
        except Exception:
            pass  # column already exists


async def get_athlete_count(db: aiosqlite.Connection) -> int:
    async with db.execute(
        "SELECT COUNT(*) FROM athletes WHERE is_active = 1"
    ) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_random_athlete(db: aiosqlite.Connection) -> Athlete | None:
    async with db.execute(
        "SELECT profile_url, name, nickname, weight_class, record, image_url "
        "FROM athletes WHERE is_active = 1 ORDER BY RANDOM() LIMIT 1"
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


async def get_random_removed_athlete(db: aiosqlite.Connection) -> Athlete | None:
    async with db.execute(
        "SELECT profile_url, name, nickname, weight_class, record, image_url "
        "FROM athletes WHERE is_active = 0 ORDER BY RANDOM() LIMIT 1"
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


async def get_all_active_profile_urls(db: aiosqlite.Connection) -> set[str]:
    """Return profile URLs of all athletes currently marked active."""
    urls: set[str] = set()
    async with db.execute(
        "SELECT profile_url FROM athletes WHERE is_active = 1"
    ) as cursor:
        async for row in cursor:
            urls.add(row[0])
    return urls


async def mark_athletes_removed(
    db: aiosqlite.Connection,
    profile_urls: set[str],
) -> list[Athlete]:
    """Mark athletes as inactive and return their full data for tweeting."""
    removed: list[Athlete] = []
    for url in profile_urls:
        async with db.execute(
            "SELECT profile_url, name, nickname, weight_class, record, image_url "
            "FROM athletes WHERE profile_url = ?",
            (url,),
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            removed.append(Athlete(
                profile_url=row[0],
                name=row[1],
                nickname=row[2],
                weight_class=row[3],
                record=row[4],
                image_url=row[5],
            ))
        await db.execute(
            "UPDATE athletes SET is_active = 0 WHERE profile_url = ?", (url,)
        )
    await db.commit()
    logger.info(f"Marked {len(removed)} athlete(s) as removed.")
    return removed


async def save_athlete(db: aiosqlite.Connection, athlete: Athlete) -> bool:
    """Insert athlete; returns True if it was new (or reactivated).
    Also backfills image_url and reactivates previously removed athletes."""
    cursor = await db.execute(
        """
        INSERT OR IGNORE INTO athletes
            (profile_url, name, nickname, weight_class, record, image_url, is_active)
        VALUES (?, ?, ?, ?, ?, ?, 1)
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

    if not is_new:
        # Reactivate if previously removed
        reactivate = await db.execute(
            "UPDATE athletes SET is_active = 1 WHERE profile_url = ? AND is_active = 0",
            (athlete.profile_url,),
        )
        if reactivate.rowcount > 0:
            logger.info(f"Athlete reactivated: {athlete.profile_url}")
            is_new = True  # treat re-appearance as new for tweeting

        # Backfill image_url if missing
        if athlete.image_url:
            await db.execute(
                "UPDATE athletes SET image_url = ? WHERE profile_url = ? AND image_url IS NULL",
                (athlete.image_url, athlete.profile_url),
            )

    await db.commit()
    return is_new
