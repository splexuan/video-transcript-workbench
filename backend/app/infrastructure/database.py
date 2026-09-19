from collections.abc import Generator
from datetime import UTC, datetime

from sqlalchemy import DateTime, TypeDecorator, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

settings.data_dir.mkdir(parents=True, exist_ok=True)


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator[datetime]):
    """始终按 UTC 读写的时间列。

    SQLite 不保存时区，写进去的 UTC 时间读回来会变成「不带时区的裸时间」，接口再
    原样吐给前端时也没有时区标记，浏览器就按本地时区去解析——整体差出一个时区。
    这里在写库时统一转成 UTC，读出来时补回 UTC 标记，各处拿到的就都是带时区的时间。
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:  # type: ignore[no-untyped-def]
        if value is None:
            return None
        return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:  # type: ignore[no-untyped-def]
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def configure_sqlite(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


# SQLite 没有迁移工具：建表之后再补上「后加的列」。只做加法，不改类型、不删列。
_LATER_COLUMNS: dict[str, dict[str, str]] = {
    "jobs": {
        "model_id": "VARCHAR(64)",
        "model_name": "VARCHAR(120)",
        "transcript_source": "VARCHAR(16)",
        "requested_model_id": "VARCHAR(64)",
        "prefer_subtitle": "BOOLEAN DEFAULT 1",
    },
    # 来源作品的元信息：作者、作品介绍、封面图文件名（封面存在数据目录的 covers/ 下）
    "documents": {
        "uploader": "VARCHAR(200)",
        "description": "TEXT",
        "cover_file": "VARCHAR(120)",
    },
}


def _add_missing_columns() -> None:
    with engine.begin() as connection:
        for table, columns in _LATER_COLUMNS.items():
            existing = {
                row[1]
                for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")
            }
            for name, ddl in columns.items():
                if name not in existing:
                    connection.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"
                    )


def init_database() -> None:
    from app.infrastructure import models  # noqa: F401

    Base.metadata.create_all(engine)
    _add_missing_columns()


def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session

