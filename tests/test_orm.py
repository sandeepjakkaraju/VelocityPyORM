import pytest
import sqlite3
from velocity_py_orm import (
    entity, table, Column, id, generated_value, encrypted,
    VelocityPyORM, SQLiteDialect, SimpleCacheProvider, EncryptionService,
    Direction, transaction, transactional
)

# 1. Define Test Entities
@entity
@table("users")
class User:
    id = Column(primary_key=True, generated=True, field_type=int)
    name = Column(nullable=False, unique=True, field_type=str)
    email = Column(encrypted=True, field_type=str)
    age = Column(nullable=True, field_type=int)


def test_orm_flow(sqlite_conn_factory):
    # Initialize Encryption Service
    secret_key = "my_super_secret_encryption_key_for_testing"
    encryption_service = EncryptionService(secret_key)
    
    # Initialize L2 Cache
    l2_cache = SimpleCacheProvider()

    # Build ORM
    orm = (VelocityPyORM.builder()
           .connection_factory(sqlite_conn_factory)
           .dialect(SQLiteDialect())
           .cache_provider(l2_cache)
           .encryption_service(encryption_service)
           .build())

    # Bootstrap Entity Tables
    orm.bootstrap([User])

    # Get Repository
    user_repo = orm.repository(User)

    # --- Test Create (Insert) ---
    new_user = User(name="Sandeep Jakkaraju", email="sandeep@velocityorm.com", age=30)
    saved_user = user_repo.save(new_user)

    assert saved_user.id is not None
    assert saved_user.name == "Sandeep Jakkaraju"
    assert saved_user.email == "sandeep@velocityorm.com"
    assert saved_user.age == 30

    # --- Test Caching & Retrieve ---
    # Find by ID (hits L1 cache)
    cached_user = user_repo.find_by_id(saved_user.id)
    assert cached_user is saved_user

    # Clear Session/L1 cache context to force database/L2 retrieval
    from velocity_py_orm.tx import SessionContext
    SessionContext.clear()

    # Retrieve from L2 cache
    l2_cached = user_repo.find_by_id(saved_user.id)
    assert l2_cached is not None
    assert l2_cached.name == "Sandeep Jakkaraju"
    
    # Clear both L1 and L2 cache to force raw database query
    SessionContext.clear()
    l2_cache.clear("users")

    db_user = user_repo.find_by_id(saved_user.id)
    assert db_user is not None
    assert db_user.id == saved_user.id
    assert db_user.name == "Sandeep Jakkaraju"
    assert db_user.email == "sandeep@velocityorm.com"

    # --- Verify Column Encryption in Database ---
    # Fetch raw row directly from sqlite to check if email is ciphertext
    conn = sqlite_conn_factory()
    cur = conn.cursor()
    cur.execute("SELECT email FROM users WHERE id = ?", (saved_user.id,))
    raw_email = cur.fetchone()[0]
    cur.close()
    conn.close()

    assert raw_email != "sandeep@velocityorm.com"
    # Decrypting raw ciphertext should return cleartext
    assert encryption_service.decrypt(raw_email) == "sandeep@velocityorm.com"

    # --- Test Update ---
    db_user.name = "Sandeep Kumar J"
    db_user.email = "sandeep_new@velocityorm.com"
    updated_user = user_repo.save(db_user)

    assert updated_user.name == "Sandeep Kumar J"
    assert updated_user.email == "sandeep_new@velocityorm.com"

    # Verify update in DB
    SessionContext.clear()
    l2_cache.clear("users")
    retrieved_updated = user_repo.find_by_id(saved_user.id)
    assert retrieved_updated.name == "Sandeep Kumar J"
    assert retrieved_updated.email == "sandeep_new@velocityorm.com"

    # --- Test Delete ---
    user_repo.delete(saved_user.id)
    assert user_repo.find_by_id(saved_user.id) is None


def test_query_builder(sqlite_conn_factory):
    orm = (VelocityPyORM.builder()
           .connection_factory(sqlite_conn_factory)
           .dialect(SQLiteDialect())
           .build())
    orm.bootstrap([User])
    user_repo = orm.repository(User)

    # Insert test data
    user_repo.save(User(name="Alice", email="alice@test.com", age=25))
    user_repo.save(User(name="Bob", email="bob@test.com", age=35))
    user_repo.save(User(name="Charlie", email="charlie@test.com", age=40))

    # Test where eq
    alice = user_repo.query().where(User.name).eq("Alice").one()
    assert alice is not None
    assert alice.name == "Alice"

    # Test count
    cnt = user_repo.query().count()
    assert cnt == 3

    # Test inequality and ordering
    users = user_repo.query().where(User.age).gt(30).order_by(User.age, Direction.DESC).list()
    assert len(users) == 2
    assert users[0].name == "Charlie"  # age 40 > 35
    assert users[1].name == "Bob"

    # Test in_ operator
    names = [u.name for u in user_repo.query().where("name").in_(["Alice", "Charlie"]).list()]
    assert "Alice" in names
    assert "Charlie" in names
    assert "Bob" not in names

    # Test between operator
    mid_age_users = user_repo.query().where(User.age).between(30, 45).list()
    assert len(mid_age_users) == 2


def test_transactions(sqlite_conn_factory):
    orm = (VelocityPyORM.builder()
           .connection_factory(sqlite_conn_factory)
           .dialect(SQLiteDialect())
           .build())
    orm.bootstrap([User])
    user_repo = orm.repository(User)

    # 1. Rollback test
    try:
        with transaction(orm):
            user_repo.save(User(name="Tx User 1", email="tx1@test.com", age=20))
            # Cause error to force rollback
            raise ValueError("Forced error")
    except ValueError:
        pass

    # Record should not exist due to rollback
    assert user_repo.query().where(User.name).eq("Tx User 1").one() is None

    # 2. Commit test
    with transaction(orm):
        user_repo.save(User(name="Tx User 2", email="tx2@test.com", age=21))

    # Record should exist due to commit
    assert user_repo.query().where(User.name).eq("Tx User 2").one() is not None


def test_batch_operations(sqlite_conn_factory):
    orm = (VelocityPyORM.builder()
           .connection_factory(sqlite_conn_factory)
           .dialect(SQLiteDialect())
           .build())
    orm.bootstrap([User])
    user_repo = orm.repository(User)

    # Batch Insert
    users = [
        User(name="Batch 1", email="b1@test.com", age=50),
        User(name="Batch 2", email="b2@test.com", age=60)
    ]
    user_repo.batch_insert(users)

    assert user_repo.query().count() == 2

    # Verify they were inserted
    b1 = user_repo.query().where(User.name).eq("Batch 1").one()
    b2 = user_repo.query().where(User.name).eq("Batch 2").one()
    assert b1 is not None
    assert b2 is not None

    # Batch Update
    b1.age = 51
    b2.age = 61
    user_repo.batch_update([b1, b2])

    # Retrieve and verify update
    from velocity_py_orm.tx import SessionContext
    SessionContext.clear()
    
    b1_updated = user_repo.query().where(User.name).eq("Batch 1").one()
    assert b1_updated.age == 51
