"""Guards the test harness: the db_url fixture yields a reachable, per-test database."""
import psycopg


def test_db_url_connects_to_generated_database(db_url):
    """db_url points at the freshly created t_* database, not the server default."""
    with psycopg.connect(db_url) as c:
        current = c.execute("SELECT current_database()").fetchone()[0]
    assert current.startswith("t_")
    assert current == psycopg.conninfo.conninfo_to_dict(db_url)["dbname"]


def test_shell_env_never_reaches_settings(settings):
    """No Settings env var (e.g. DATABASE_URL_UNPOOLED from `vercel env pull`) leaks into any test."""
    import os

    from statbrief.config import Settings

    leaked = [f.upper() for f in Settings.model_fields if f.upper() in os.environ]
    assert leaked == []
    assert settings.database_url_unpooled is None
    assert settings.bootstrap_database_url == settings.database_url
