"""Database roles, separation of duties in the database, structured callbacks, sessions, currency.

Tables move to the NOLOGIN role payables_owner. The API and the agent connect as payables_api and
payables_agent: neither owns anything, so neither can ALTER a table or disable a trigger.

- payables_agent prepares drafts. It cannot record a decision (trigger on payment_drafts, column
  grants) and cannot create a simulated payment (no grant).
- payables_api records decisions for signed-in people. A decided draft can no longer change, and a
  simulated payment needs an approved draft with the same amount (triggers).

Passwords for the two login roles are set by `python -m payables.dbsetup`, not here.

Revision ID: 7c2d41a9e5b0
Revises: 1b89e67c6839
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7c2d41a9e5b0"
down_revision: str | None = "1b89e67c6839"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("users", "sessions", "vendors", "documents", "invoices", "payment_drafts", "simulated_payments", "audit_log")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT count(*) FROM audit_log")).scalar():
        raise RuntimeError(
            "audit_log has entries from the unkeyed chain: archive them and start from an empty "
            "database (docker compose down -v), because the new chain is keyed with an HMAC"
        )

    # ---- schema --------------------------------------------------------------------------------
    op.add_column("users", sa.Column("email", sa.String(200), nullable=True))
    op.create_unique_constraint("users_email_key", "users", ["email"])
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "vendors",
        sa.Column("phones", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
    )
    op.add_column("documents", sa.Column("original_sha256", sa.String(64), nullable=False))
    op.add_column("documents", sa.Column("normalized", sa.LargeBinary(), nullable=True))
    op.add_column("documents", sa.Column("preview_png", sa.LargeBinary(), nullable=False))
    op.add_column("documents", sa.Column("submitted_by_email", sa.String(200), nullable=True))
    op.add_column("invoices", sa.Column("currency", sa.String(3), server_default="ILS", nullable=False))
    op.add_column("payment_drafts", sa.Column("currency", sa.String(3), server_default="ILS", nullable=False))
    op.add_column("payment_drafts", sa.Column("callback_phone", sa.String(40), nullable=True))
    op.add_column("payment_drafts", sa.Column("callback_person", sa.String(120), nullable=True))
    op.add_column("payment_drafts", sa.Column("callback_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint("bank_change_needs_verification", "payment_drafts", type_="check")
    op.drop_column("payment_drafts", "bank_verification_note")
    op.create_check_constraint(
        "bank_change_needs_callback",
        "payment_drafts",
        "state <> 'APPROVED' OR NOT requires_bank_verification OR ("
        "btrim(coalesce(callback_phone, '')) <> '' AND btrim(coalesce(callback_person, '')) <> '' "
        "AND callback_at IS NOT NULL)",
    )
    op.create_check_constraint("draft_amount_positive", "payment_drafts", "amount IS NULL OR amount > 0")
    op.add_column(
        "simulated_payments", sa.Column("currency", sa.String(3), server_default="ILS", nullable=False)
    )
    op.create_check_constraint("payment_amount_positive", "simulated_payments", "amount > 0")
    op.add_column("audit_log", sa.Column("seq", sa.BigInteger(), nullable=False))
    op.create_unique_constraint("audit_log_seq_key", "audit_log", ["seq"])

    # ---- guards --------------------------------------------------------------------------------
    op.execute(
        """
        CREATE FUNCTION payment_drafts_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.state IN ('APPROVED', 'REJECTED') OR NEW.decided_by IS NOT NULL THEN
                    RAISE EXCEPTION 'payment_drafts: a draft is created undecided';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.state IN ('APPROVED', 'REJECTED') THEN
                RAISE EXCEPTION 'payment_drafts: a decided draft cannot change';
            END IF;
            IF (NEW.state IN ('APPROVED', 'REJECTED') OR NEW.decided_by IS NOT NULL)
                    AND current_user <> 'payables_api' THEN
                RAISE EXCEPTION 'payment_drafts: only payables_api records decisions, not %', current_user;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER payment_drafts_guard BEFORE INSERT OR UPDATE ON payment_drafts "
        "FOR EACH ROW EXECUTE FUNCTION payment_drafts_guard()"
    )
    op.execute(
        """
        CREATE FUNCTION simulated_payments_guard() RETURNS trigger AS $$
        DECLARE d payment_drafts%ROWTYPE;
        BEGIN
            SELECT * INTO d FROM payment_drafts WHERE id = NEW.draft_id;
            IF d.state IS DISTINCT FROM 'APPROVED' OR d.amount IS DISTINCT FROM NEW.amount
                    OR d.currency IS DISTINCT FROM NEW.currency THEN
                RAISE EXCEPTION 'simulated_payments: needs an approved draft with the same amount';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER simulated_payments_guard BEFORE INSERT ON simulated_payments "
        "FOR EACH ROW EXECUTE FUNCTION simulated_payments_guard()"
    )
    op.execute(
        """
        CREATE FUNCTION append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER simulated_payments_no_change BEFORE UPDATE OR DELETE ON simulated_payments "
        "FOR EACH ROW EXECUTE FUNCTION append_only()"
    )

    # ---- roles, ownership, grants --------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'payables_owner') THEN
                CREATE ROLE payables_owner NOLOGIN;
            END IF;
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'payables_api') THEN
                CREATE ROLE payables_api NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
            END IF;
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'payables_agent') THEN
                CREATE ROLE payables_agent NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
            END IF;
        END
        $$
        """
    )
    for table in (*TABLES, "alembic_version"):
        op.execute(f"ALTER TABLE {table} OWNER TO payables_owner")
    for fn in ("audit_log_append_only()", "append_only()", "payment_drafts_guard()", "simulated_payments_guard()"):
        op.execute(f"ALTER FUNCTION {fn} OWNER TO payables_owner")
    for enum in ("role", "draft_state"):
        op.execute(f"ALTER TYPE {enum} OWNER TO payables_owner")
    op.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    # The owner creates objects in later migrations, and foreign-key checks run as the table owner.
    op.execute("GRANT USAGE, CREATE ON SCHEMA public TO payables_owner")
    op.execute("GRANT USAGE ON SCHEMA public TO payables_api, payables_agent")

    # The API: everything a signed-in person can do, nothing structural.
    op.execute(
        "GRANT SELECT ON users, sessions, vendors, documents, invoices, payment_drafts, "
        "simulated_payments, audit_log TO payables_api"
    )
    op.execute(
        "GRANT INSERT ON sessions, vendors, documents, invoices, payment_drafts, simulated_payments, "
        "audit_log TO payables_api"
    )
    op.execute("GRANT UPDATE ON sessions, vendors, documents, payment_drafts TO payables_api")

    # The agent: reads what it compares against, writes documents, invoices, undecided drafts and
    # audit entries. No password hashes, no sessions, no payments, no decision columns.
    op.execute("GRANT SELECT (id, username, display_name, role, email) ON users TO payables_agent")
    op.execute("GRANT SELECT ON vendors, documents, invoices, payment_drafts, audit_log TO payables_agent")
    op.execute("GRANT INSERT ON documents, invoices, payment_drafts, audit_log TO payables_agent")
    op.execute("GRANT UPDATE (message_text) ON documents TO payables_agent")
    op.execute(
        "GRANT UPDATE (state, amount, currency, checks, flags, reasoning, requires_bank_verification, "
        "pay_to) ON payment_drafts TO payables_agent"
    )
    for table, roles in (
        ("sessions", "payables_api"),
        ("vendors", "payables_api"),
        ("documents", "payables_api, payables_agent"),
        ("invoices", "payables_api, payables_agent"),
        ("payment_drafts", "payables_api, payables_agent"),
        ("simulated_payments", "payables_api"),
        ("audit_log", "payables_api, payables_agent"),
    ):
        op.execute(f"GRANT USAGE ON SEQUENCE {table}_id_seq TO {roles}")


def downgrade() -> None:
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM payables_api, payables_agent")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM payables_api, payables_agent")
    op.execute("REVOKE USAGE ON SCHEMA public FROM payables_api, payables_agent")
    op.execute("REVOKE USAGE, CREATE ON SCHEMA public FROM payables_owner")
    op.execute("GRANT ALL ON SCHEMA public TO PUBLIC")
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} OWNER TO CURRENT_USER")
    op.execute("DROP TRIGGER simulated_payments_no_change ON simulated_payments")
    op.execute("DROP FUNCTION append_only()")
    op.execute("DROP TRIGGER simulated_payments_guard ON simulated_payments")
    op.execute("DROP FUNCTION simulated_payments_guard()")
    op.execute("DROP TRIGGER payment_drafts_guard ON payment_drafts")
    op.execute("DROP FUNCTION payment_drafts_guard()")
    op.drop_constraint("audit_log_seq_key", "audit_log", type_="unique")
    op.drop_column("audit_log", "seq")
    op.drop_constraint("payment_amount_positive", "simulated_payments", type_="check")
    op.drop_column("simulated_payments", "currency")
    op.drop_constraint("draft_amount_positive", "payment_drafts", type_="check")
    op.drop_constraint("bank_change_needs_callback", "payment_drafts", type_="check")
    op.add_column("payment_drafts", sa.Column("bank_verification_note", sa.Text(), nullable=True))
    op.create_check_constraint(
        "bank_change_needs_verification",
        "payment_drafts",
        "state <> 'APPROVED' OR NOT requires_bank_verification OR bank_verification_note IS NOT NULL",
    )
    for column in ("callback_at", "callback_person", "callback_phone", "currency"):
        op.drop_column("payment_drafts", column)
    op.drop_column("invoices", "currency")
    for column in ("submitted_by_email", "preview_png", "normalized", "original_sha256"):
        op.drop_column("documents", column)
    op.drop_column("vendors", "phones")
    op.drop_table("sessions")
    op.drop_constraint("users_email_key", "users", type_="unique")
    op.drop_column("users", "email")
