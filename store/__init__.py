"""Persistence layer: the trade-record database.

SQLite (WAL) via SQLAlchemy ORM, with Alembic migrations. The rest of the app
talks to `store.repository`, never to SQL directly. All timestamps are stored
timezone-aware in UTC. Reporting currency is USD.
"""
