"""Guards the test harness: the db_url fixture yields a reachable, per-test database."""
import psycopg


def test_db_url_connects_to_generated_database(db_url):
    """db_url points at the freshly created t_* database, not the server default."""
    with psycopg.connect(db_url) as c:
        current = c.execute("SELECT current_database()").fetchone()[0]
    assert current.startswith("t_")
    assert current == psycopg.conninfo.conninfo_to_dict(db_url)["dbname"]
