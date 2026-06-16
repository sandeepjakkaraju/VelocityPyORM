# VelocityPyORM  (..|o|..)

A parallel Python ORM matching the architecture, patterns, and design of Java **VelocityORM** as closely as possible.

## Features

- **Entity & Table Definitions**: Using python decorators (`@entity`, `@table`) and `Column` definitions.
- **Repository Pattern**: `BaseRepository` handling saving, fetching, querying, deleting, and batch operations.
- **Stored Procedure execution**: Postgres, MySQL, and Oracle dialect support calling generated stored procedures for insert/update/delete/get.
- **Built-in Query Builder**: Fluent query building API supporting filtering, order by, limit/offset, count.
- **Caching**: Thread-local L1 cache and plugin-based L2 cache.
- **AES Column-Level Encryption**: Fully compatible with Java's column-level encryption.

## Getting Started

Check out `tests/test_orm.py` to see complete examples of entities, CRUD operations, query building, transaction management, and encryption.
