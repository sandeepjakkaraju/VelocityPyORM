import pytest
import sqlite3

@pytest.fixture(scope="function")
def sqlite_conn_factory():
    db_uri = "file:memdb_test?mode=memory&cache=shared"
    persist_conn = sqlite3.connect(db_uri, uri=True)
    
    def factory():
        conn = sqlite3.connect(db_uri, uri=True)
        return conn

    yield factory
    persist_conn.close()
