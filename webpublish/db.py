from mautrix.util.async_db import UpgradeTable, Connection

upgrade_table = UpgradeTable()


@upgrade_table.register(description="Initial schema: published_rooms and messages tables")
async def upgrade_v1(conn: Connection) -> None:
    await conn.execute("""
        CREATE TABLE published_rooms (
            room_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            alias TEXT NOT NULL
        )
    """)
    await conn.execute("""
        CREATE TABLE messages (
            event_id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL,
            sender TEXT NOT NULL,
            sender_name TEXT,
            body TEXT NOT NULL,
            formatted_body TEXT,
            msgtype TEXT NOT NULL DEFAULT 'm.text',
            media_url TEXT,
            timestamp BIGINT NOT NULL,
            thread_root TEXT,
            edited BOOLEAN NOT NULL DEFAULT FALSE,
            redacted BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)
    await conn.execute(
        "CREATE INDEX idx_messages_room_ts ON messages (room_id, timestamp)"
    )
    await conn.execute(
        "CREATE INDEX idx_messages_thread ON messages (thread_root)"
    )


@upgrade_table.register(description="Add reply_to column for reply back-links")
async def upgrade_v2(conn: Connection) -> None:
    await conn.execute("ALTER TABLE messages ADD COLUMN reply_to TEXT")
