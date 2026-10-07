"""Idempotent role provisioning, including databases on existing volumes."""

import os

import psycopg
from psycopg import sql
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

# Only entities used by the capability compiler and its access subqueries.
TABLES = [
    "api_project",
    "api_financialrecord",
    "api_commitment",
    "api_salestarget",
    "api_team",
    "api_teammembership",
    "api_clientprojectaccess",
    "api_userprofile",
    "api_chataccess",
    "api_crmprojectsnapshot",
]


class Command(BaseCommand):
    help = "Provision the analytics SELECT role. Requires a separate database administrator connection."

    def handle(self, *args, **options):
        role = os.getenv("ANALYTICS_DB_USER", "mazory_analytics")
        password = os.getenv("ANALYTICS_DB_PASSWORD")
        cfg = connections["default"].settings_dict
        admin_user = os.getenv("ANALYTICS_ADMIN_USER")
        admin_password = os.getenv("ANALYTICS_ADMIN_PASSWORD")
        if (
            not password
            or not admin_user
            or not admin_password
            or role in [cfg["USER"], admin_user]
        ):
            raise CommandError(
                "Set distinct ANALYTICS_DB credentials and ANALYTICS_ADMIN_USER/PASSWORD."
            )
        try:
            with psycopg.connect(
                host=cfg["HOST"],
                port=cfg["PORT"],
                dbname=cfg["NAME"],
                user=admin_user,
                password=admin_password,
            ) as conn:
                with conn.cursor() as c:
                    c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", [role])
                    if c.fetchone() is None:
                        c.execute(
                            sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role))
                        )
                    c.execute(
                        sql.SQL(
                            "ALTER ROLE {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD {}"
                        ).format(sql.Identifier(role), sql.Literal(password))
                    )
                    c.execute(
                        "SELECT parent.rolname FROM pg_auth_members m JOIN pg_roles parent ON parent.oid=m.roleid JOIN pg_roles child ON child.oid=m.member WHERE child.rolname=%s",
                        [role],
                    )
                    for (parent,) in c.fetchall():
                        c.execute(
                            sql.SQL("REVOKE {} FROM {}").format(
                                sql.Identifier(parent), sql.Identifier(role)
                            )
                        )
                    c.execute(
                        sql.SQL(
                            "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}"
                        ).format(sql.Identifier(role))
                    )
                    c.execute(
                        sql.SQL(
                            "REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {}"
                        ).format(sql.Identifier(role))
                    )
                    c.execute(
                        sql.SQL("REVOKE ALL ON SCHEMA public FROM {}").format(
                            sql.Identifier(role)
                        )
                    )
                    c.execute(
                        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                            sql.Identifier(cfg["NAME"]), sql.Identifier(role)
                        )
                    )
                    c.execute(
                        sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                            sql.Identifier(role)
                        )
                    )
                    for table in TABLES:
                        c.execute(
                            sql.SQL("GRANT SELECT ON {} TO {}").format(
                                sql.Identifier(table), sql.Identifier(role)
                            )
                        )
                    c.execute(
                        sql.SQL(
                            "GRANT SELECT (id, is_active, is_superuser) ON auth_user TO {}"
                        ).format(sql.Identifier(role))
                    )
                    c.execute(
                        sql.SQL(
                            "GRANT SELECT (id, config_id, source, project_id, team_id, timestamp, sender_name, content, session_name, chat_id, message_id, source_revision, sent_at_known, processing_state) ON api_rawmessage TO {}"
                        ).format(sql.Identifier(role))
                    )
                    c.execute(
                        sql.SQL(
                            "GRANT SELECT (id, is_active, team_id) ON api_whatsappconfig TO {}"
                        ).format(sql.Identifier(role))
                    )
                    c.execute(
                        sql.SQL(
                            "ALTER ROLE {} SET default_transaction_read_only=on"
                        ).format(sql.Identifier(role))
                    )
                    c.execute(
                        sql.SQL("ALTER ROLE {} SET statement_timeout=5000").format(
                            sql.Identifier(role)
                        )
                    )
                    # Public grants can defeat the intended role isolation: fail closed
                    # rather than revoke privileges from unrelated application users.
                    c.execute(
                        "SELECT tablename FROM pg_tables WHERE schemaname='public' AND has_table_privilege(%s, quote_ident(schemaname)||'.'||quote_ident(tablename), 'INSERT,UPDATE,DELETE,TRUNCATE')",
                        [role],
                    )
                    if c.fetchall():
                        raise CommandError(
                            "PUBLIC grants permit writes; remove those grants with the database administrator first."
                        )
                    c.execute(
                        "SELECT has_schema_privilege(%s, 'public', 'CREATE')", [role]
                    )
                    if c.fetchone()[0]:
                        raise CommandError(
                            "PUBLIC schema CREATE is enabled; restrict that grant before provisioning."
                        )
                    c.execute(
                        "SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prosecdef AND has_function_privilege(%s, p.oid, 'EXECUTE')",
                        [role],
                    )
                    if c.fetchone():
                        raise CommandError(
                            "Executable public SECURITY DEFINER function requires administrator review."
                        )
        except psycopg.Error:
            raise CommandError(
                "Analytics role provisioning failed; verify administrator permissions."
            ) from None
        self.stdout.write(self.style.SUCCESS("Analytics read-only role provisioned."))
