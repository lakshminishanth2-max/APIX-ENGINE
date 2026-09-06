"""PostgreSQL extensions, GiST exclusion constraints, and TimescaleDB readiness.

Architectural decision record:
1. Extensions:
   - pgcrypto: In-database digest and UUID generation for auditing.
   - btree_gist: Enables GiST indexing on scalar types for range exclusions.
   - pg_trgm: Trigram matching for carrier / flight admin search.
   - timescaledb: Active and ready for analytical projection tables.

2. Relational Integrity:
   - apix_raw_observation remains a standard relational table to preserve
     its global UNIQUE constraint on ingress_hash (write-once idempotency).
   - apix_canonical_fare remains a standard relational table to preserve
     its single-column primary key and self-referential foreign key
     (duplicate_of_id -> id).
   - apix_index_value remains a standard relational table to preserve
     its single-column primary key (Django 5.1.4 / DRF routing) and
     global UNIQUE constraint on provenance_hash (cryptographic integrity).
"""

from __future__ import annotations

from django.db import migrations

# --------------------------------------------------------------------------- #
# Extensions
# --------------------------------------------------------------------------- #
CREATE_EXTENSIONS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pgcrypto') THEN
        CREATE EXTENSION IF NOT EXISTS pgcrypto;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'btree_gist') THEN
        CREATE EXTENSION IF NOT EXISTS btree_gist;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_trgm') THEN
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb') THEN
        CREATE EXTENSION IF NOT EXISTS timescaledb;
    END IF;
END
$$;
"""

DROP_EXTENSIONS = """
-- Extensions are intentionally not dropped on reverse: other schemas may use
-- them, and dropping pgcrypto would cascade into anything referencing it.
SELECT 1;
"""

# --------------------------------------------------------------------------- #
# Non-overlapping weight versions
# --------------------------------------------------------------------------- #
# A route must never have two DGCA weight versions in force at the same instant,
# or the Laspeyres shares would double count. A CHECK constraint cannot express
# this across rows; a GiST exclusion constraint can.
ADD_WEIGHT_EXCLUSION = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'btree_gist') THEN
        BEGIN
            ALTER TABLE apix_route_weight
                ADD CONSTRAINT ex_route_weight_no_overlap
                EXCLUDE USING gist (
                    route_id WITH =,
                    daterange(valid_from, COALESCE(valid_to, 'infinity'::date), '[)') WITH &&
                );
        EXCEPTION
            WHEN duplicate_object THEN NULL;
            WHEN duplicate_table THEN NULL;
        END;
    END IF;
END
$$;
"""

DROP_WEIGHT_EXCLUSION = """
ALTER TABLE apix_route_weight DROP CONSTRAINT IF EXISTS ex_route_weight_no_overlap;
"""


class Migration(migrations.Migration):
    """Guarded, environment-adaptive database features."""

    atomic = False

    dependencies = [("apix", "0001_initial")]

    operations = [
        migrations.RunSQL(CREATE_EXTENSIONS, DROP_EXTENSIONS),
        migrations.RunSQL(ADD_WEIGHT_EXCLUSION, DROP_WEIGHT_EXCLUSION),
    ]