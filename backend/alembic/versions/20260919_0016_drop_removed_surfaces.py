"""Drop the tables of surfaces the September 2026 reset removed.

The in-app benchmark, passkeys, the repeatability runner and worker enrollment
tokens have no code left that reads or writes them. Nothing here touches a
table a model still references.
"""

from alembic import op

revision = "20260919_0016"
down_revision = "20260916_0015"
branch_labels = None
depends_on = None

TABLES = (
    "benchmark_results",
    "benchmark_runs",
    "benchmark_variants",
    "benchmark_sets",
    "repeatability_benchmarks",
    "passkey_credentials",
    "auth_challenges",
    "worker_enrollment_tokens",
)


def upgrade() -> None:
    for table in TABLES:
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')


def downgrade() -> None:
    raise RuntimeError("the removed surfaces are not restored by a downgrade; restore a dump")
