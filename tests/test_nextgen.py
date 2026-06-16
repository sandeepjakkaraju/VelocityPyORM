import pytest
import asyncio
import aiosqlite
import sqlite3

@pytest.fixture
def anyio_backend():
    return "asyncio"
from dataclasses import dataclass
from unittest.mock import Mock, MagicMock

from velocity_py_orm import (
    entity, table, Column, VelocityPyORM, SQLiteDialect, PostgresDialect,
    SimpleCacheProvider, RedisCacheProvider, Placeholder, transaction,
    EncryptionService
)
from velocity_py_orm.tx import TableHandler

# 1. Test Entity
@entity
@table("users")
class User:
    id = Column(primary_key=True, generated=True, field_type=int)
    name = Column(nullable=False, unique=True, field_type=str)
    email = Column(encrypted=True, field_type=str)
    age = Column(nullable=True, field_type=int)


# Mock Redis client for testing RedisCacheProvider
class MockRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value
        return True

    def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)
        return len(keys)

    def keys(self, pattern):
        # Extremely basic pattern matching for clear
        prefix = pattern.replace("*", "")
        return [k for k in self.store.keys() if k.startswith(prefix)]


def test_redis_cache_provider_and_query_cache(sqlite_conn_factory):
    # Setup mock redis and cache provider
    mock_redis = MockRedis()
    l2_cache = RedisCacheProvider(redis_client=mock_redis)

    orm = (VelocityPyORM.builder()
           .connection_factory(sqlite_conn_factory)
           .dialect(SQLiteDialect())
           .cache_provider(l2_cache)
           .build())
    orm.bootstrap([User])

    user_repo = orm.repository(User)
    
    # Save a user
    user = User(name="Redis User", email="redis@test.com", age=45)
    user_repo.save(user)

    # 1. Query with cache enabled
    query = user_repo.query().where(User.name).eq("Redis User").use_cache(True)
    res = query.list()
    assert len(res) == 1
    assert res[0].name == "Redis User"

    # Verify L2 cache has the query cache key
    # Key looks like: velocity_cache:query_cache:users:v1:...
    cache_keys = list(mock_redis.store.keys())
    query_keys = [k for k in cache_keys if "query_cache:users" in k]
    assert len(query_keys) == 1

    # Verify table version key is present
    version_keys = [k for k in cache_keys if "sys_table_versions" in k]
    assert len(version_keys) == 1

    # 2. Modify table and verify automatic invalidation
    new_user = User(name="Another User", email="another@test.com", age=25)
    user_repo.save(new_user)

    # Verify table version has incremented (old cache keys will be ignored)
    # Let's run query again with cache enabled
    res2 = user_repo.query().where(User.name).eq("Redis User").use_cache(True).list()
    assert len(res2) == 1

    # Now there should be another query cache key under version 2
    cache_keys_after = list(mock_redis.store.keys())
    query_keys_after = [k for k in cache_keys_after if "query_cache:users" in k]
    # We should have a new key matching version 2
    assert len(query_keys_after) > len(query_keys)


@pytest.mark.anyio
async def test_async_transactions_and_queries():
    import os
    db_path = "test_async.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    try:
        # Setup async sqlite connection factory
        async def async_conn_factory():
            conn = await aiosqlite.connect(db_path)
            return conn

        # SQLite Dialect doesn't support procedures, which is perfect for general SQL testing
        orm = (VelocityPyORM.builder()
               .async_connection_factory(async_conn_factory)
               .dialect(SQLiteDialect())
               .build())

        # Create tables asynchronously for test
        async with orm.transaction() as tx:
            cur = await tx.connection.cursor()
            await cur.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT, age INTEGER)")
            await tx.connection.commit()

        # Bootstrap internally maps metadata, let's register User manually
        orm._table_metas["users"] = User._meta

        # Test insert and query inside async transaction
        async with orm.transaction() as tx:
            # Dictionary insert
            res = await tx.table("users").insert({"name": "Async User", "email": "async@test.com", "age": 28})
            assert res["id"] is not None
            assert res["name"] == "Async User"

            # Query select
            users = await tx.table("users").select(where={"name": "Async User"}).all()
            assert len(users) == 1
            assert users[0]["name"] == "Async User"
            assert users[0]["email"] == "async@test.com"

            # Update
            await tx.table("users").update({"age": 29}, where={"name": "Async User"})

            # Verify update
            user_updated = await tx.table("users").select(where={"name": "Async User"}).one()
            assert user_updated["age"] == 29

            # Delete
            await tx.table("users").delete(where={"name": "Async User"})
            
            # Verify count
            cnt = await tx.table("users").count()
            assert cnt == 0
    finally:
        if os.path.exists(db_path):
            try:
                os.remove(db_path)
            except Exception:
                pass



def test_stored_procedure_integration():
    # Setup mock PG dialect that supports procedures
    mock_dialect = PostgresDialect()
    
    orm = (VelocityPyORM.builder()
           .connection_factory(lambda: MagicMock())
           .dialect(mock_dialect)
           .build())
    
    orm._table_metas["users"] = User._meta

    # Create a mock session & transaction
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    
    from velocity_py_orm.tx import Session
    session = Session(mock_conn, in_transaction=True, orm=orm)

    # Calling tx.table("users").insert() should use sp_users_insert stored procedure
    handler = TableHandler(session, "users")
    
    # Define returning row for Postgres generated PK
    mock_cursor.fetchone.return_value = (99,)

    res = handler.insert({"name": "Proc User", "email": "proc@test.com", "age": 55})
    
    assert res["id"] == 99
    # Verify sp insert call statement was executed
    mock_cursor.execute.assert_called_once()
    sql_arg = mock_cursor.execute.call_args[0][0]
    assert "sp_users_insert" in sql_arg


def test_compile_time_query_generation(sqlite_conn_factory):
    orm = (VelocityPyORM.builder()
           .connection_factory(sqlite_conn_factory)
           .dialect(SQLiteDialect())
           .build())
    orm.bootstrap([User])

    # Precompile on entity mapping
    find_user = orm.precompile(User, lambda q: q.where(User.email).eq(Placeholder("email")))
    
    # Precompile on raw table dict
    find_user_dict = orm.precompile("users", lambda h: h.select(where={"email": Placeholder("email")}))

    # Verify SQL construction
    assert "email" in find_user.param_mappings
    assert "email" in find_user_dict.param_mappings

    # Populate data
    user_repo = orm.repository(User)
    user_repo.save(User(name="Compiled 1", email="c1@test.com", age=30))
    user_repo.save(User(name="Compiled 2", email="c2@test.com", age=31))

    with orm.transaction() as tx:
        # Execute compiled query
        res1 = find_user.execute(tx, email="c1@test.com")
        assert len(res1) == 1
        assert isinstance(res1[0], User)
        assert res1[0].name == "Compiled 1"

        # Execute compiled dict query
        res2 = find_user_dict.execute(tx, email="c2@test.com")
        assert len(res2) == 1
        assert isinstance(res2[0], dict)
        assert res2[0]["name"] == "Compiled 2"


@table("users")
@dataclass
class UserDC:
    id: int = None
    name: str = None
    email: str = None
    age: int = None


def test_entity_mapping_layer_dataclass(sqlite_conn_factory):
    orm = (VelocityPyORM.builder()
           .connection_factory(sqlite_conn_factory)
           .dialect(SQLiteDialect())
           .build())
    
    # We will bootstrap using User (registered entity DDL),
    # but query/insert using dataclass UserDC!
    orm.bootstrap([User])
    
    with orm.transaction() as tx:
        # Insert a dataclass directly!
        new_dc = UserDC(id=None, name="Dataclass User", email="dc@test.com", age=32)
        saved_dc = tx.table(UserDC).insert(new_dc)
        
        assert isinstance(saved_dc, UserDC)
        assert saved_dc.id is not None
        assert saved_dc.name == "Dataclass User"

        # Fetch using dataclass mapping
        fetched_dcs = tx.table(UserDC).select(where={"name": "Dataclass User"}).all()
        assert len(fetched_dcs) == 1
        assert isinstance(fetched_dcs[0], UserDC)
        assert fetched_dcs[0].email == "dc@test.com"
