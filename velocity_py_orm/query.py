from enum import Enum
from .decorators import Column

class Direction(Enum):
    ASC = "ASC"
    DESC = "DESC"


class Criterion:
    def __init__(self, column_name: str, operator: str, value, is_and: bool):
        self.column_name = column_name
        self.operator = operator
        self.value = value
        self.is_and = is_and


class Order:
    def __init__(self, column_name: str, direction: Direction):
        self.column_name = column_name
        self.direction = direction


class Query:
    def __init__(self, meta, dialect, session, encryption_service=None, owns_session=False, cache_provider=None):
        self.meta = meta
        self.dialect = dialect
        self.session = session
        self.encryption_service = encryption_service
        self.owns_session = owns_session
        self.criteria = []
        self.order_by_list = []
        self.limit_val = None
        self.offset_val = None
        self.current_property = None
        self.is_and = True
        self._use_cache = False
        self._cache_ttl = None
        self._cache_provider = cache_provider or (getattr(session, 'orm', None) and session.orm.get_cache_provider())

    def use_cache(self, enabled=True, ttl=None):
        self._use_cache = enabled
        self._cache_ttl = ttl
        return self


    def where(self, property_or_name):
        if isinstance(property_or_name, Column):
            self.current_property = property_or_name.field_name
        else:
            self.current_property = property_or_name
        self.is_and = True
        return self

    def and_(self, property_or_name):
        return self.where(property_or_name)

    def or_(self, property_or_name):
        if isinstance(property_or_name, Column):
            self.current_property = property_or_name.field_name
        else:
            self.current_property = property_or_name
        self.is_and = False
        return self

    def _add_criterion(self, operator: str, value):
        if not self.current_property:
            raise ValueError("No property specified for criterion")
        col = self.meta.get_column_by_field_name(self.current_property)
        if not col:
            raise ValueError(f"Unknown property: {self.current_property}")
        
        if col.encrypted and value is not None and self.encryption_service is not None:
            if operator == "=":
                value = self.encryption_service.encrypt(str(value))
        
        self.criteria.append(Criterion(col.name, operator, value, self.is_and))
        self.current_property = None

    def eq(self, value):
        self._add_criterion("=", value)
        return self

    def ne(self, value):
        self._add_criterion("<>", value)
        return self

    def gt(self, value):
        self._add_criterion(">", value)
        return self

    def gte(self, value):
        self._add_criterion(">=", value)
        return self

    def lt(self, value):
        self._add_criterion("<", value)
        return self

    def lte(self, value):
        self._add_criterion("<=", value)
        return self

    def like(self, value):
        self._add_criterion("LIKE", value)
        return self

    def in_(self, values):
        self._add_criterion("IN", list(values))
        return self

    def between(self, val1, val2):
        self._add_criterion("BETWEEN", [val1, val2])
        return self

    def is_null(self):
        self._add_criterion("IS NULL", None)
        return self

    def is_not_null(self):
        self._add_criterion("IS NOT NULL", None)
        return self

    def order_by(self, property_or_name, direction: Direction = Direction.ASC):
        if isinstance(property_or_name, Column):
            prop = property_or_name.field_name
        else:
            prop = property_or_name
        col = self.meta.get_column_by_field_name(prop)
        if not col:
            raise ValueError(f"Unknown property: {prop}")
        self.order_by_list.append(Order(col.name, direction))
        return self

    def limit(self, limit: int):
        self.limit_val = limit
        return self

    def offset(self, offset: int):
        self.offset_val = offset
        return self

    def list(self) -> list:
        if self._use_cache and self._cache_provider:
            version = self._cache_provider.get("sys_table_versions", self.meta.table_name) or 1
            sql, params = self._build_select_sql()
            cache_key = f"v{version}:{sql}:{params}"
            cached = self._cache_provider.get(f"query_cache:{self.meta.table_name}", cache_key)
            if cached is not None:
                for entity in cached:
                    id_val = getattr(entity, self.meta.id_column.field_name)
                    self.session.l1_cache.put(self.meta.entity_class, id_val, entity)
                return list(cached)

        sql, params = self._build_select_sql()
        results = []
        cur = self.session.connection.cursor()
        try:
            cur.execute(sql, params)
            colnames = [desc[0] for desc in cur.description] if cur.description else []
            rows = cur.fetchall()
            for row in rows:
                entity = self._map_row(row, colnames)
                id_val = getattr(entity, self.meta.id_column.field_name)
                self.session.l1_cache.put(self.meta.entity_class, id_val, entity)
                results.append(entity)

            if self._use_cache and self._cache_provider:
                self._cache_provider.put(f"query_cache:{self.meta.table_name}", cache_key, results)
        finally:
            cur.close()
            if self.owns_session:
                try:
                    self.session.close()
                except Exception:
                    pass
        return results


    def one(self):
        self.limit(1)
        res = self.list()
        return res[0] if res else None

    def count(self) -> int:
        sql, params = self._build_count_sql()
        cur = self.session.connection.cursor()
        try:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else 0
        finally:
            cur.close()
            if self.owns_session:
                try:
                    self.session.close()
                except Exception:
                    pass

    def _build_select_sql(self) -> tuple:
        sb = [f"SELECT * FROM {self.dialect.quote_identifier(self.meta.table_name)}"]
        params = self._append_where_clause(sb)
        self._append_order_by_clause(sb)

        sql = " ".join(sb)
        if self.limit_val is not None and self.offset_val is not None:
            sql = self.dialect.paginate(sql, self.limit_val, self.offset_val)
        elif self.limit_val is not None:
            sql = self.dialect.paginate(sql, self.limit_val, 0)
        
        return sql, tuple(params)

    def _build_count_sql(self) -> tuple:
        sb = [f"SELECT COUNT(*) FROM {self.dialect.quote_identifier(self.meta.table_name)}"]
        params = self._append_where_clause(sb)
        return " ".join(sb), tuple(params)

    def _append_where_clause(self, sb: list) -> list:
        if not self.criteria:
            return []
        
        sb.append("WHERE")
        params = []
        placeholder = self.dialect.get_placeholders(1)

        for i, crit in enumerate(self.criteria):
            if i > 0:
                sb.append("AND" if crit.is_and else "OR")
            
            col_name = self.dialect.quote_identifier(crit.column_name)
            if crit.operator == "IN":
                p_list = ",".join([placeholder] * len(crit.value))
                sb.append(f"{col_name} IN ({p_list})")
                params.extend(crit.value)
            elif crit.operator == "BETWEEN":
                sb.append(f"{col_name} BETWEEN {placeholder} AND {placeholder}")
                params.extend(crit.value)
            elif crit.value is None:
                sb.append(f"{col_name} {crit.operator}")
            else:
                sb.append(f"{col_name} {crit.operator} {placeholder}")
                params.append(crit.value)
        return params

    def _append_order_by_clause(self, sb: list):
        if not self.order_by_list:
            return
        sb.append("ORDER BY")
        orders = []
        for o in self.order_by_list:
            orders.append(f"{self.dialect.quote_identifier(o.column_name)} {o.direction.value}")
        sb.append(", ".join(orders))

    def _map_row(self, row, colnames) -> object:
        entity = self.meta.entity_class()
        for col in self.meta.columns:
            if col.name in colnames:
                idx = colnames.index(col.name)
                val = row[idx]
                if col.encrypted and val is not None and self.encryption_service is not None:
                    val = self.encryption_service.decrypt(val)
                setattr(entity, col.field_name, val)
        return entity


class DummySession:
    def __init__(self, orm):
        self.orm = orm
        self.connection = None


class Placeholder:
    def __init__(self, name=None):
        self.name = name


class PrecompiledQuery:
    def __init__(self, orm, table_or_entity, sql, param_mappings):
        self.orm = orm
        self.table_or_entity = table_or_entity
        self.sql = sql
        self.param_mappings = param_mappings
        if isinstance(table_or_entity, str):
            self.table_name = table_or_entity
            self.entity_class = None
            self.meta = orm.get_meta_for_table(table_or_entity)
        else:
            self.entity_class = table_or_entity
            self.meta = getattr(table_or_entity, '_meta', None)
            self.table_name = self.meta.table_name if self.meta else table_or_entity.__name__.lower() + "s"

    def _bind_params(self, *args, **kwargs):
        params = []
        for mapping in self.param_mappings:
            if isinstance(mapping, int):
                if mapping < len(args):
                    params.append(args[mapping])
                else:
                    raise ValueError(f"Missing required positional argument at index {mapping}")
            else:
                if mapping in kwargs:
                    params.append(kwargs[mapping])
                elif len(args) == len(self.param_mappings):
                    params.append(args[self.param_mappings.index(mapping)])
                else:
                    raise ValueError(f"Missing required argument {mapping}")
        return tuple(params)

    def execute(self, tx, *args, **kwargs):
        from .tx import map_dict_to_class
        params = self._bind_params(*args, **kwargs)
        cur = tx.connection.cursor()
        try:
            cur.execute(self.sql, params)
            colnames = [desc[0] for desc in cur.description] if cur.description else []
            rows = cur.fetchall()
            results = []
            for row in rows:
                record = {}
                for idx, colname in enumerate(colnames):
                    val = row[idx]
                    if self.meta:
                        col = self.meta.get_column_by_db_name(colname)
                        if col and col.encrypted and val is not None and self.orm.get_encryption_service() is not None:
                            val = self.orm.get_encryption_service().encrypt(str(val))
                    record[colname] = val
                
                if self.entity_class:
                    record = map_dict_to_class(record, self.entity_class)
                results.append(record)
            return results
        finally:
            cur.close()

    async def execute_async(self, tx, *args, **kwargs):
        from .tx import map_dict_to_class
        params = self._bind_params(*args, **kwargs)
        cur = await tx.connection.cursor()
        try:
            await cur.execute(self.sql, params)
            colnames = [desc[0] for desc in cur.description] if cur.description else []
            rows = await cur.fetchall()
            results = []
            for row in rows:
                record = {}
                for idx, colname in enumerate(colnames):
                    val = row[idx]
                    if self.meta:
                        col = self.meta.get_column_by_db_name(colname)
                        if col and col.encrypted and val is not None and self.orm.get_encryption_service() is not None:
                            val = self.orm.get_encryption_service().encrypt(str(val))
                    record[colname] = val
                
                if self.entity_class:
                    record = map_dict_to_class(record, self.entity_class)
                results.append(record)
            return results
        finally:
            await cur.close()

