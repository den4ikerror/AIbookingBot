import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

import config


ACTIVE_STATUSES = ("pending", "accepted")


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(config.DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def initialize() -> None:
    with closing(connect()) as connection:
        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    price TEXT NOT NULL,
                    duration_minutes INTEGER NOT NULL,
                    appointment_at TEXT NOT NULL,
                    customer_name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_bookings_appointment "
                "ON bookings (appointment_at, status)"
            )


def get_active_bookings(day: datetime.date) -> list[sqlite3.Row]:
    day_start = datetime.combine(day, datetime.min.time()).isoformat(timespec="minutes")
    next_day = (datetime.combine(day, datetime.min.time()) + timedelta(days=1)).isoformat(
        timespec="minutes"
    )
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    with closing(connect()) as connection:
        return connection.execute(
            f"""
            SELECT appointment_at, duration_minutes
            FROM bookings
            WHERE status IN ({placeholders})
              AND appointment_at < ?
              AND appointment_at >= ?
            """,
            (*ACTIVE_STATUSES, next_day, day_start),
        ).fetchall()


def create_booking(
    *,
    user_id: int,
    username: str,
    service_name: str,
    price: str,
    duration_minutes: int,
    appointment_at: datetime,
    customer_name: str,
    phone: str,
) -> int | None:
    """Insert a booking only if it does not overlap another active booking."""
    starts_at = appointment_at.replace(tzinfo=None)
    ends_at = starts_at + timedelta(minutes=duration_minutes)
    with closing(connect()) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"""
                SELECT appointment_at, duration_minutes
                FROM bookings
                WHERE status IN ({",".join("?" for _ in ACTIVE_STATUSES)})
                  AND appointment_at < ?
                  AND appointment_at >= ?
                """,
                (
                    *ACTIVE_STATUSES,
                    ends_at.isoformat(timespec="minutes"),
                    (starts_at - timedelta(days=1)).isoformat(timespec="minutes"),
                ),
            ).fetchall()
            for row in rows:
                existing_start = datetime.fromisoformat(row["appointment_at"])
                existing_end = existing_start + timedelta(minutes=row["duration_minutes"])
                if starts_at < existing_end and ends_at > existing_start:
                    return None

            cursor = connection.execute(
                """
                INSERT INTO bookings (
                    user_id, username, service_name, price, duration_minutes,
                    appointment_at, customer_name, phone, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    user_id,
                    username,
                    service_name,
                    price,
                    duration_minutes,
                    starts_at.isoformat(timespec="minutes"),
                    customer_name,
                    phone,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            return cursor.lastrowid


def get_booking(booking_id: int) -> sqlite3.Row | None:
    with closing(connect()) as connection:
        return connection.execute(
            "SELECT * FROM bookings WHERE id = ?", (booking_id,)
        ).fetchone()


def set_booking_status(booking_id: int, status: str) -> bool:
    if status not in {"accepted", "rejected", "cancelled"}:
        raise ValueError(f"Unsupported booking status: {status}")
    with closing(connect()) as connection:
        with connection:
            cursor = connection.execute(
                "UPDATE bookings SET status = ? WHERE id = ? AND status = 'pending'",
                (status, booking_id),
            )
            return cursor.rowcount == 1
