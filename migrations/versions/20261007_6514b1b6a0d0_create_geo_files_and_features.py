"""create geo_files and features

Revision ID: 6514b1b6a0d0
Revises:
Create Date: 2026-10-07 09:23:25.980132
"""

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql

revision = "6514b1b6a0d0"
down_revision = None
branch_labels = None
depends_on = None

FILE_STATUS = ("PENDING", "PROCESSING", "COMPLETED", "FAILED")
MEASURE_STATUS = ("OK", "NOT_APPLICABLE", "UNSUPPORTED", "SKIPPED")


def _str_enum(values: tuple[str, ...], name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=20)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.create_table(
        "geo_files",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("file_type", sa.String(10), nullable=False),
        sa.Column("status", _str_enum(FILE_STATUS, "filestatus"), nullable=False),
        sa.Column("crs", sa.String(255)),
        sa.Column("feature_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
    )
    op.create_geospatial_table(
        "features",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "file_id",
            sa.Uuid(),
            sa.ForeignKey("geo_files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("index", sa.Integer(), nullable=False),
        sa.Column("layer", sa.String(255)),
        sa.Column("geometry_type", sa.String(32)),
        sa.Column("geometry", Geometry(srid=4326, spatial_index=False)),
        sa.Column("crs", sa.String(255)),
        sa.Column("properties", postgresql.JSONB(), nullable=False),
        sa.Column("measure_type", sa.String(10)),
        sa.Column("value", sa.Float()),
        sa.Column("unit", sa.String(5)),
        sa.Column("projected_crs", sa.String(32)),
        sa.Column("status", _str_enum(MEASURE_STATUS, "measurestatus"), nullable=False),
        sa.Column("note", sa.Text()),
        sa.UniqueConstraint("file_id", "index"),
    )
    op.create_geospatial_index(
        "idx_features_geometry", "features", ["geometry"], postgresql_using="gist"
    )


def downgrade() -> None:
    op.drop_geospatial_index("idx_features_geometry", table_name="features", column_name="geometry")
    op.drop_geospatial_table("features")
    op.drop_table("geo_files")
