"""Engine/session factory. Enables SQLite WAL so other processes can read while
the engine writes."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import ROOT


def default_db_path() -> Path:
    return ROOT / "data" / "roostoo_compet.db"


def make_engine(db_path: str | Path | None = None, echo: bool = False) -> Engine:
    path = Path(db_path) if db_path else default_db_path()
    # A relative db_path (e.g. config's "data/roostoo_compet.db") must mean
    # repo-root-relative, NOT CWD-relative — otherwise launching from another
    # directory silently creates and uses a fresh empty DB elsewhere.
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", echo=echo, future=True)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")    # concurrent reads + one writer
        cur.execute("PRAGMA foreign_keys=ON")     # enforce FK constraints
        cur.execute("PRAGMA synchronous=NORMAL")  # safe with WAL, faster
        cur.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
