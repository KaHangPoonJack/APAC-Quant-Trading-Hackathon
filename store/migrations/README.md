# Database migrations (Alembic)

The trade-record schema is versioned here. The live DB is a SQLite file at
`data/roostoo_compet.db` (git-ignored); only the migration scripts are committed.

## Common commands

Run from the repo root:

```bash
# create a new migration after editing store/models.py
python -m alembic revision --autogenerate -m "describe change"

# apply migrations to the DB
python -m alembic upgrade head

# roll back one revision
python -m alembic downgrade -1

# show current revision
python -m alembic current
```

## Gotcha: custom column types

Our timestamps use the custom `store.timeutil.UTCDateTime` type. Alembic's
autogenerate renders it as `store.timeutil.UTCDateTime(...)` in the migration but
does **not** add the import. After autogenerating, add this line to the new
migration's imports if it isn't there:

```python
import store.timeutil
```

(The initial migration already includes it.)

## Gotcha: named constraints for SQLite

SQLite has no real `ALTER`, so Alembic runs schema changes in **batch mode**
(rebuild-and-copy), which requires **every constraint to have a name**.
Autogenerate emits `create_foreign_key(None, ...)` / `drop_constraint(None, ...)`
— replace the `None` with an explicit name (e.g. `'fk_orders_deployment_id'`) in
both `upgrade()` and `downgrade()`, or the migration fails with
`ValueError: Constraint must have a name`.
