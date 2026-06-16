import threading
import contextvars
import inspect
import dataclasses
from .cache import L1Cache

# Helper functions for dict API and Entity Mapping
def parse_dict_where(where_dict, dialect):
    fragments = []
    params = []
    placeholder = dialect.get_placeholders(1)
    for col_name, val in where_dict.items():
        quoted_col = dialect.quote_identifier(col_name)
        if isinstance(val, dict):
            for op, inner_val in val.items():
                op_upper = op.upper()
                if op_upper == "IN":
                    p_list = ",".join([placeholder] * len(inner_val))
                    fragments.append(f"{quoted_col} IN ({p_list})")
                    params.extend(inner_val)
                elif op_upper == "BETWEEN":
                    fragments.append(f"{quoted_col} BETWEEN {placeholder} AND {placeholder}")
                    params.extend(inner_val)
                elif inner_val is None:
                    if op_upper == "=" or op_upper == "IS":
                        fragments.append(f"{quoted_col} IS NULL")
                    elif op_upper == "<>" or op_upper == "!=" or op_upper == "IS NOT":
                        fragments.append(f"{quoted_col} IS NOT NULL")
                else:
                    actual_op = "<>" if op_upper == "!=" else op_upper
                    fragments.append(f"{quoted_col} {actual_op} {placeholder}")
                    params.append(inner_val)
        elif val is None:
            fragments.append(f"{quoted_col} IS NULL")
        else:
            fragments.append(f"{quoted_col} = {placeholder}")
            params.append(val)
    return fragments, params

def map_dict_to_class(data_dict, entity_class):
    if hasattr(entity_class, '_meta'):
        meta = entity_class._meta
        obj = entity_class()
        for k, v in data_dict.items():
            col = meta.get_column_by_db_name(k) or meta.get_column_by_field_name(k)
            if col:
                setattr(obj, col.field_name, v)
            else:
                setattr(obj, k, v)
        return obj
    if dataclasses.is_dataclass(entity_class):
        fields = {f.name for f in dataclasses.fields(entity_class)}
        init_args = {}
        for k, v in data_dict.items():
            if k in fields:
                init_args[k] = v
        return entity_class(**init_args)
    try:
        obj = entity_class()
        for k, v in data_dict.items():
            setattr(obj, k, v)
        return obj
    except TypeError:
        return entity_class(**data_dict)

def map_class_to_dict(obj):
    if isinstance(obj, dict):
        return obj
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if hasattr(obj, '__dict__'):
        return {k: v for k, v in obj.__dict__.items() if not k.startswith('_')}
    return {}


class Session:
    def __init__(self, connection, in_transaction=False, orm=None):
        self.connection = connection
        self._in_transaction = in_transaction
        self.l1_cache = L1Cache()
        self.orm = orm

    def is_in_transaction(self) -> bool:
        return self._in_transaction

    def close(self):
        self.l1_cache.clear()
        self.connection.close()

    def table(self, name_or_class):
        return TableHandler(self, name_or_class)


class AsyncSession:
    def __init__(self, connection, in_transaction=False, orm=None):
        self.connection = connection
        self._in_transaction = in_transaction
        self.l1_cache = L1Cache()
        self.orm = orm

    def is_in_transaction(self) -> bool:
        return self._in_transaction

    async def close(self):
        self.l1_cache.clear()
        await self.connection.close()

    def table(self, name_or_class):
        return AsyncTableHandler(self, name_or_class)


class SessionContext:
    _local = threading.local()

    @classmethod
    def get_current_session(cls) -> Session:
        return getattr(cls._local, 'session', None)

    @classmethod
    def set_current_session(cls, session: Session):
        cls._local.session = session

    @classmethod
    def clear(cls):
        if hasattr(cls._local, 'session'):
            del cls._local.session


class AsyncSessionContext:
    _current_session = contextvars.ContextVar('async_session', default=None)

    @classmethod
    def get_current_session(cls) -> AsyncSession:
        return cls._current_session.get()

    @classmethod
    def set_current_session(cls, session: AsyncSession):
        cls._current_session.set(session)

    @classmethod
    def clear(cls):
        cls._current_session.set(None)


class transaction:
    def __init__(self, orm):
        self.orm = orm
        self.session = None
        self.owns_session = False

    def __enter__(self):
        self.session = SessionContext.get_current_session()
        if self.session is None:
            conn = self.orm.get_connection()
            self.session = Session(conn, in_transaction=True, orm=self.orm)
            SessionContext.set_current_session(self.session)
            self.owns_session = True
        else:
            self.session._in_transaction = True
        return self.session

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.owns_session:
            try:
                if exc_type is not None:
                    self.session.connection.rollback()
                else:
                    self.session.connection.commit()
            finally:
                try:
                    self.session.close()
                except Exception:
                    pass
                SessionContext.clear()
        return False # Bubble exception up

    async def __aenter__(self):
        self.session = AsyncSessionContext.get_current_session()
        if self.session is None:
            conn = await self.orm.get_async_connection()
            self.session = AsyncSession(conn, in_transaction=True, orm=self.orm)
            AsyncSessionContext.set_current_session(self.session)
            self.owns_session = True
        else:
            self.session._in_transaction = True
        return self.session

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.owns_session:
            try:
                if exc_type is not None:
                    await self.session.connection.rollback()
                else:
                    await self.session.connection.commit()
            finally:
                try:
                    await self.session.close()
                except Exception:
                    pass
                AsyncSessionContext.clear()
        return False

    def __call__(self, func):
        if inspect.iscoroutinefunction(func):
            async def async_wrapper(*args, **kwargs):
                async with self:
                    return await func(self.session, *args, **kwargs)
            return async_wrapper
        else:
            def sync_wrapper(*args, **kwargs):
                with self:
                    return func(self.session, *args, **kwargs)
            return sync_wrapper


def transactional(orm):
    def decorator(func):
        def wrapper(*args, **kwargs):
            with transaction(orm):
                return func(*args, **kwargs)
        return wrapper
    return decorator


class TableHandler:
    def __init__(self, session, table_name_or_class):
        self.session = session
        self.orm = session.orm
        self.dialect = self.orm.get_dialect()
        
        if isinstance(table_name_or_class, str):
            self.table_name = table_name_or_class
            self.entity_class = None
            self.meta = self.orm.get_meta_for_table(table_name_or_class)
        else:
            self.entity_class = table_name_or_class
            self.meta = getattr(table_name_or_class, '_meta', None)
            if self.meta:
                self.table_name = self.meta.table_name
            elif hasattr(table_name_or_class, '__table_name__'):
                self.table_name = table_name_or_class.__table_name__
            else:
                self.table_name = table_name_or_class.__name__.lower() + "s"

    def select(self, where=None, order_by=None, limit=None, offset=None):
        return TableQuery(self, where, order_by, limit, offset)

    def all(self):
        return self.select().all()

    def one(self):
        return self.select().one()

    def count(self):
        return self.select().count()

    def insert(self, data):
        cache_provider = self.orm.get_cache_provider()
        if cache_provider:
            version = cache_provider.get("sys_table_versions", self.table_name) or 1
            cache_provider.put("sys_table_versions", self.table_name, version + 1)

        data_dict = map_class_to_dict(data)

        if self.meta and self.dialect.supports_procedures:
            sql = self.dialect.get_insert_call_statement(self.meta)
            id_col = self.meta.id_column
            params = []
            if id_col.generated:
                params.append(None)
            else:
                params.append(data_dict.get(id_col.field_name) or data_dict.get(id_col.name))
            
            for col in self.meta.columns:
                if col.primary_key:
                    continue
                val = data_dict.get(col.field_name) or data_dict.get(col.name)
                if col.encrypted and val is not None and self.orm.get_encryption_service() is not None:
                    val = self.orm.get_encryption_service().encrypt(str(val))
                params.append(val)
                
            cur = self.session.connection.cursor()
            try:
                cur.execute(sql, tuple(params))
                generated_id = None
                if id_col.generated:
                    if self.dialect.get_name() == "PostgreSQL":
                        row = cur.fetchone()
                        if row:
                            generated_id = row[0]
                    else:
                        generated_id = cur.lastrowid
                
                res_dict = dict(data_dict)
                if generated_id is not None:
                    res_dict[id_col.field_name] = generated_id
                    res_dict[id_col.name] = generated_id
                
                if self.entity_class:
                    return map_dict_to_class(res_dict, self.entity_class)
                return res_dict
            finally:
                cur.close()
        else:
            cols = []
            placeholders = []
            params = []
            for k, v in data_dict.items():
                col_name = k
                val = v
                if self.meta:
                    col = self.meta.get_column_by_field_name(k) or self.meta.get_column_by_db_name(k)
                    if col:
                        col_name = col.name
                        if col.encrypted and val is not None and self.orm.get_encryption_service() is not None:
                            val = self.orm.get_encryption_service().encrypt(str(val))
                cols.append(self.dialect.quote_identifier(col_name))
                placeholders.append(self.dialect.get_placeholders(1))
                params.append(val)
                
            sql = f"INSERT INTO {self.dialect.quote_identifier(self.table_name)} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})"
            cur = self.session.connection.cursor()
            try:
                cur.execute(sql, tuple(params))
                generated_id = cur.lastrowid
                res_dict = dict(data_dict)
                
                if self.meta and self.meta.id_column.generated and generated_id:
                    res_dict[self.meta.id_column.field_name] = generated_id
                    res_dict[self.meta.id_column.name] = generated_id
                elif generated_id:
                    res_dict['id'] = generated_id
                
                if self.entity_class:
                    return map_dict_to_class(res_dict, self.entity_class)
                return res_dict
            finally:
                cur.close()

    def update(self, data, where):
        cache_provider = self.orm.get_cache_provider()
        if cache_provider:
            version = cache_provider.get("sys_table_versions", self.table_name) or 1
            cache_provider.put("sys_table_versions", self.table_name, version + 1)

        data_dict = map_class_to_dict(data)

        is_by_pk = False
        pk_val = None
        if self.meta:
            pk_field = self.meta.id_column.field_name
            pk_name = self.meta.id_column.name
            if pk_field in where:
                pk_val = where[pk_field]
                is_by_pk = True
            elif pk_name in where:
                pk_val = where[pk_name]
                is_by_pk = True

        if self.meta and self.dialect.supports_procedures and is_by_pk and len(where) == 1:
            sql = self.dialect.get_update_call_statement(self.meta)
            params = [pk_val]
            for col in self.meta.columns:
                if col.primary_key:
                    continue
                val = data_dict.get(col.field_name) or data_dict.get(col.name)
                if col.encrypted and val is not None and self.orm.get_encryption_service() is not None:
                    val = self.orm.get_encryption_service().encrypt(str(val))
                params.append(val)
                
            cur = self.session.connection.cursor()
            try:
                cur.execute(sql, tuple(params))
                return True
            finally:
                cur.close()
        else:
            updates = []
            params = []
            placeholder = self.dialect.get_placeholders(1)
            for k, v in data_dict.items():
                col_name = k
                val = v
                if self.meta:
                    col = self.meta.get_column_by_field_name(k) or self.meta.get_column_by_db_name(k)
                    if col:
                        col_name = col.name
                        if col.encrypted and val is not None and self.orm.get_encryption_service() is not None:
                            val = self.orm.get_encryption_service().encrypt(str(val))
                updates.append(f"{self.dialect.quote_identifier(col_name)} = {placeholder}")
                params.append(val)
                
            where_fragments, where_params = parse_dict_where(where, self.dialect)
            sql = f"UPDATE {self.dialect.quote_identifier(self.table_name)} SET {', '.join(updates)} WHERE {' AND '.join(where_fragments)}"
            params.extend(where_params)
            
            cur = self.session.connection.cursor()
            try:
                cur.execute(sql, tuple(params))
                return True
            finally:
                cur.close()

    def delete(self, where):
        cache_provider = self.orm.get_cache_provider()
        if cache_provider:
            version = cache_provider.get("sys_table_versions", self.table_name) or 1
            cache_provider.put("sys_table_versions", self.table_name, version + 1)

        is_by_pk = False
        pk_val = None
        if self.meta:
            pk_field = self.meta.id_column.field_name
            pk_name = self.meta.id_column.name
            if pk_field in where:
                pk_val = where[pk_field]
                is_by_pk = True
            elif pk_name in where:
                pk_val = where[pk_name]
                is_by_pk = True

        if self.meta and self.dialect.supports_procedures and is_by_pk and len(where) == 1:
            sql = self.dialect.get_delete_call_statement(self.meta)
            cur = self.session.connection.cursor()
            try:
                cur.execute(sql, (pk_val,))
                return True
            finally:
                cur.close()
        else:
            where_fragments, where_params = parse_dict_where(where, self.dialect)
            sql = f"DELETE FROM {self.dialect.quote_identifier(self.table_name)} WHERE {' AND '.join(where_fragments)}"
            cur = self.session.connection.cursor()
            try:
                cur.execute(sql, tuple(where_params))
                return True
            finally:
                cur.close()


class TableQuery:
    def __init__(self, handler, where=None, order_by=None, limit_val=None, offset_val=None):
        self.handler = handler
        self.where_dict = where
        self.order_by_list = order_by
        self.limit_val = limit_val
        self.offset_val = offset_val
        self._use_cache = False
        self._cache_ttl = None

    def use_cache(self, enabled=True, ttl=None):
        self._use_cache = enabled
        self._cache_ttl = ttl
        return self

    def _build_sql(self):
        dialect = self.handler.dialect
        table_name = self.handler.table_name
        
        sb = [f"SELECT * FROM {dialect.quote_identifier(table_name)}"]
        params = []
        
        if self.where_dict:
            fragments, where_params = parse_dict_where(self.where_dict, dialect)
            if fragments:
                sb.append("WHERE " + " AND ".join(fragments))
                params.extend(where_params)
        
        if self.order_by_list:
            sb.append("ORDER BY")
            orders = []
            if isinstance(self.order_by_list, str):
                orders.append(self.order_by_list)
            elif isinstance(self.order_by_list, dict):
                for col, dir in self.order_by_list.items():
                    orders.append(f"{dialect.quote_identifier(col)} {dir}")
            elif isinstance(self.order_by_list, list):
                for item in self.order_by_list:
                    if isinstance(item, tuple) and len(item) == 2:
                        orders.append(f"{dialect.quote_identifier(item[0])} {item[1]}")
                    else:
                        orders.append(str(item))
            sb.append(", ".join(orders))
            
        sql = " ".join(sb)
        if self.limit_val is not None:
            offset = self.offset_val or 0
            sql = dialect.paginate(sql, self.limit_val, offset)
            
        return sql, tuple(params)

    def all(self):
        cache_provider = self.handler.orm.get_cache_provider()
        if self._use_cache and cache_provider:
            version = cache_provider.get("sys_table_versions", self.handler.table_name) or 1
            sql, params = self._build_sql()
            cache_key = f"v{version}:{sql}:{params}"
            cached = cache_provider.get(f"query_cache:{self.handler.table_name}", cache_key)
            if cached is not None:
                return cached
                
        sql, params = self._build_sql()
        cur = self.handler.session.connection.cursor()
        try:
            cur.execute(sql, params)
            colnames = [desc[0] for desc in cur.description] if cur.description else []
            rows = cur.fetchall()
            results = []
            for row in rows:
                record = {}
                for idx, colname in enumerate(colnames):
                    val = row[idx]
                    if self.handler.meta:
                        col = self.handler.meta.get_column_by_db_name(colname)
                        if col and col.encrypted and val is not None and self.handler.orm.get_encryption_service() is not None:
                            val = self.handler.orm.get_encryption_service().decrypt(val)
                    record[colname] = val
                
                if self.handler.entity_class:
                    record = map_dict_to_class(record, self.handler.entity_class)
                results.append(record)
                
            if self._use_cache and cache_provider:
                cache_provider.put(f"query_cache:{self.handler.table_name}", cache_key, results)
                
            return results
        finally:
            cur.close()

    def one(self):
        self.limit_val = 1
        res = self.all()
        return res[0] if res else None

    def count(self) -> int:
        dialect = self.handler.dialect
        sb = [f"SELECT COUNT(*) FROM {dialect.quote_identifier(self.handler.table_name)}"]
        params = []
        if self.where_dict:
            fragments, where_params = parse_dict_where(self.where_dict, dialect)
            if fragments:
                sb.append("WHERE " + " AND ".join(fragments))
                params.extend(where_params)
        sql = " ".join(sb)
        
        cur = self.handler.session.connection.cursor()
        try:
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
            return row[0] if row else 0
        finally:
            cur.close()


class AsyncTableHandler:
    def __init__(self, session, table_name_or_class):
        self.session = session
        self.orm = session.orm
        self.dialect = self.orm.get_dialect()
        
        if isinstance(table_name_or_class, str):
            self.table_name = table_name_or_class
            self.entity_class = None
            self.meta = self.orm.get_meta_for_table(table_name_or_class)
        else:
            self.entity_class = table_name_or_class
            self.meta = getattr(table_name_or_class, '_meta', None)
            if self.meta:
                self.table_name = self.meta.table_name
            elif hasattr(table_name_or_class, '__table_name__'):
                self.table_name = table_name_or_class.__table_name__
            else:
                self.table_name = table_name_or_class.__name__.lower() + "s"

    def select(self, where=None, order_by=None, limit=None, offset=None):
        return AsyncTableQuery(self, where, order_by, limit, offset)

    async def all(self):
        return await self.select().all()

    async def one(self):
        return await self.select().one()

    async def count(self):
        return await self.select().count()

    async def insert(self, data):
        cache_provider = self.orm.get_cache_provider()
        if cache_provider:
            if inspect.iscoroutinefunction(cache_provider.get):
                version = await cache_provider.get("sys_table_versions", self.table_name) or 1
                await cache_provider.put("sys_table_versions", self.table_name, version + 1)
            else:
                version = cache_provider.get("sys_table_versions", self.table_name) or 1
                cache_provider.put("sys_table_versions", self.table_name, version + 1)

        data_dict = map_class_to_dict(data)

        if self.meta and self.dialect.supports_procedures:
            sql = self.dialect.get_insert_call_statement(self.meta)
            id_col = self.meta.id_column
            params = []
            if id_col.generated:
                params.append(None)
            else:
                params.append(data_dict.get(id_col.field_name) or data_dict.get(id_col.name))
            
            for col in self.meta.columns:
                if col.primary_key:
                    continue
                val = data_dict.get(col.field_name) or data_dict.get(col.name)
                if col.encrypted and val is not None and self.orm.get_encryption_service() is not None:
                    val = self.orm.get_encryption_service().encrypt(str(val))
                params.append(val)
                
            cur = await self.session.connection.cursor()
            try:
                await cur.execute(sql, tuple(params))
                generated_id = None
                if id_col.generated:
                    if self.dialect.get_name() == "PostgreSQL":
                        row = await cur.fetchone()
                        if row:
                            generated_id = row[0]
                    else:
                        generated_id = cur.lastrowid
                
                res_dict = dict(data_dict)
                if generated_id is not None:
                    res_dict[id_col.field_name] = generated_id
                    res_dict[id_col.name] = generated_id
                
                if self.entity_class:
                    return map_dict_to_class(res_dict, self.entity_class)
                return res_dict
            finally:
                await cur.close()
        else:
            cols = []
            placeholders = []
            params = []
            for k, v in data_dict.items():
                col_name = k
                val = v
                if self.meta:
                    col = self.meta.get_column_by_field_name(k) or self.meta.get_column_by_db_name(k)
                    if col:
                        col_name = col.name
                        if col.encrypted and val is not None and self.orm.get_encryption_service() is not None:
                            val = self.orm.get_encryption_service().encrypt(str(val))
                cols.append(self.dialect.quote_identifier(col_name))
                placeholders.append(self.dialect.get_placeholders(1))
                params.append(val)
                
            sql = f"INSERT INTO {self.dialect.quote_identifier(self.table_name)} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})"
            cur = await self.session.connection.cursor()
            try:
                await cur.execute(sql, tuple(params))
                generated_id = cur.lastrowid
                res_dict = dict(data_dict)
                
                if self.meta and self.meta.id_column.generated and generated_id:
                    res_dict[self.meta.id_column.field_name] = generated_id
                    res_dict[self.meta.id_column.name] = generated_id
                elif generated_id:
                    res_dict['id'] = generated_id
                
                if self.entity_class:
                    return map_dict_to_class(res_dict, self.entity_class)
                return res_dict
            finally:
                await cur.close()

    async def update(self, data, where):
        cache_provider = self.orm.get_cache_provider()
        if cache_provider:
            if inspect.iscoroutinefunction(cache_provider.get):
                version = await cache_provider.get("sys_table_versions", self.table_name) or 1
                await cache_provider.put("sys_table_versions", self.table_name, version + 1)
            else:
                version = cache_provider.get("sys_table_versions", self.table_name) or 1
                cache_provider.put("sys_table_versions", self.table_name, version + 1)

        data_dict = map_class_to_dict(data)

        is_by_pk = False
        pk_val = None
        if self.meta:
            pk_field = self.meta.id_column.field_name
            pk_name = self.meta.id_column.name
            if pk_field in where:
                pk_val = where[pk_field]
                is_by_pk = True
            elif pk_name in where:
                pk_val = where[pk_name]
                is_by_pk = True

        if self.meta and self.dialect.supports_procedures and is_by_pk and len(where) == 1:
            sql = self.dialect.get_update_call_statement(self.meta)
            params = [pk_val]
            for col in self.meta.columns:
                if col.primary_key:
                    continue
                val = data_dict.get(col.field_name) or data_dict.get(col.name)
                if col.encrypted and val is not None and self.orm.get_encryption_service() is not None:
                    val = self.orm.get_encryption_service().encrypt(str(val))
                params.append(val)
                
            cur = await self.session.connection.cursor()
            try:
                await cur.execute(sql, tuple(params))
                return True
            finally:
                await cur.close()
        else:
            updates = []
            params = []
            placeholder = self.dialect.get_placeholders(1)
            for k, v in data_dict.items():
                col_name = k
                val = v
                if self.meta:
                    col = self.meta.get_column_by_field_name(k) or self.meta.get_column_by_db_name(k)
                    if col:
                        col_name = col.name
                        if col.encrypted and val is not None and self.orm.get_encryption_service() is not None:
                            val = self.orm.get_encryption_service().encrypt(str(val))
                updates.append(f"{self.dialect.quote_identifier(col_name)} = {placeholder}")
                params.append(val)
                
            where_fragments, where_params = parse_dict_where(where, self.dialect)
            sql = f"UPDATE {self.dialect.quote_identifier(self.table_name)} SET {', '.join(updates)} WHERE {' AND '.join(where_fragments)}"
            params.extend(where_params)
            
            cur = await self.session.connection.cursor()
            try:
                await cur.execute(sql, tuple(params))
                return True
            finally:
                await cur.close()

    async def delete(self, where):
        cache_provider = self.orm.get_cache_provider()
        if cache_provider:
            if inspect.iscoroutinefunction(cache_provider.get):
                version = await cache_provider.get("sys_table_versions", self.table_name) or 1
                await cache_provider.put("sys_table_versions", self.table_name, version + 1)
            else:
                version = cache_provider.get("sys_table_versions", self.table_name) or 1
                cache_provider.put("sys_table_versions", self.table_name, version + 1)

        is_by_pk = False
        pk_val = None
        if self.meta:
            pk_field = self.meta.id_column.field_name
            pk_name = self.meta.id_column.name
            if pk_field in where:
                pk_val = where[pk_field]
                is_by_pk = True
            elif pk_name in where:
                pk_val = where[pk_name]
                is_by_pk = True

        if self.meta and self.dialect.supports_procedures and is_by_pk and len(where) == 1:
            sql = self.dialect.get_delete_call_statement(self.meta)
            cur = await self.session.connection.cursor()
            try:
                await cur.execute(sql, (pk_val,))
                return True
            finally:
                await cur.close()
        else:
            where_fragments, where_params = parse_dict_where(where, self.dialect)
            sql = f"DELETE FROM {self.dialect.quote_identifier(self.table_name)} WHERE {' AND '.join(where_fragments)}"
            cur = await self.session.connection.cursor()
            try:
                await cur.execute(sql, tuple(where_params))
                return True
            finally:
                await cur.close()


class AsyncTableQuery:
    def __init__(self, handler, where=None, order_by=None, limit_val=None, offset_val=None):
        self.handler = handler
        self.where_dict = where
        self.order_by_list = order_by
        self.limit_val = limit_val
        self.offset_val = offset_val
        self._use_cache = False
        self._cache_ttl = None

    def use_cache(self, enabled=True, ttl=None):
        self._use_cache = enabled
        self._cache_ttl = ttl
        return self

    def _build_sql(self):
        return TableQuery._build_sql(self)

    async def all(self):
        cache_provider = self.handler.orm.get_cache_provider()
        if self._use_cache and cache_provider:
            if inspect.iscoroutinefunction(cache_provider.get):
                version = await cache_provider.get("sys_table_versions", self.handler.table_name) or 1
            else:
                version = cache_provider.get("sys_table_versions", self.handler.table_name) or 1
            sql, params = self._build_sql()
            cache_key = f"v{version}:{sql}:{params}"
            
            if inspect.iscoroutinefunction(cache_provider.get):
                cached = await cache_provider.get(f"query_cache:{self.handler.table_name}", cache_key)
            else:
                cached = cache_provider.get(f"query_cache:{self.handler.table_name}", cache_key)
            if cached is not None:
                return cached
                
        sql, params = self._build_sql()
        cur = await self.handler.session.connection.cursor()
        try:
            await cur.execute(sql, params)
            colnames = [desc[0] for desc in cur.description] if cur.description else []
            rows = await cur.fetchall()
            results = []
            for row in rows:
                record = {}
                for idx, colname in enumerate(colnames):
                    val = row[idx]
                    if self.handler.meta:
                        col = self.handler.meta.get_column_by_db_name(colname)
                        if col and col.encrypted and val is not None and self.handler.orm.get_encryption_service() is not None:
                            val = self.handler.orm.get_encryption_service().decrypt(val)
                    record[colname] = val
                
                if self.handler.entity_class:
                    record = map_dict_to_class(record, self.handler.entity_class)
                results.append(record)
                
            if self._use_cache and cache_provider:
                if inspect.iscoroutinefunction(cache_provider.put):
                    await cache_provider.put(f"query_cache:{self.handler.table_name}", cache_key, results)
                else:
                    cache_provider.put(f"query_cache:{self.handler.table_name}", cache_key, results)
                
            return results
        finally:
            await cur.close()

    async def one(self):
        self.limit_val = 1
        res = await self.all()
        return res[0] if res else None

    async def count(self) -> int:
        dialect = self.handler.dialect
        sb = [f"SELECT COUNT(*) FROM {dialect.quote_identifier(self.handler.table_name)}"]
        params = []
        if self.where_dict:
            fragments, where_params = parse_dict_where(self.where_dict, dialect)
            if fragments:
                sb.append("WHERE " + " AND ".join(fragments))
                params.extend(where_params)
        sql = " ".join(sb)
        
        cur = await self.handler.session.connection.cursor()
        try:
            await cur.execute(sql, tuple(params))
            row = await cur.fetchone()
            return row[0] if row else 0
        finally:
            await cur.close()
