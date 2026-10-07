from logging.config import fileConfig

from alembic import context
from geoalchemy2 import alembic_helpers

from app import models  # noqa: F401
from app.db import Base, engine

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def include_object(obj, name, type_, reflected, compare_to):
    # the postgis image ships its own tables (spatial_ref_sys, tiger, topology); only diff ours
    if type_ == "table" and reflected and compare_to is None:
        return False
    return alembic_helpers.include_object(obj, name, type_, reflected, compare_to)


with engine.connect() as connection:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        include_object=include_object,
        process_revision_directives=alembic_helpers.writer,
        render_item=alembic_helpers.render_item,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()
