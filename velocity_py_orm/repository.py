import abc
from typing import Optional, List, Collection
from .tx import Session, SessionContext
from .query import Query

class Repository(abc.ABC):
    @abc.abstractmethod
    def save(self, entity):
        pass

    @abc.abstractmethod
    def update(self, entity):
        pass

    @abc.abstractmethod
    def delete(self, id_val):
        pass

    @abc.abstractmethod
    def find_by_id(self, id_val) -> Optional[object]:
        pass

    @abc.abstractmethod
    def find_all(self) -> List[object]:
        pass

    @abc.abstractmethod
    def query(self) -> Query:
        pass

    @abc.abstractmethod
    def batch_insert(self, entities: Collection[object]):
        pass

    @abc.abstractmethod
    def batch_update(self, entities: Collection[object]):
        pass


class BaseRepository(Repository):
    def __init__(self, orm, meta):
        self.orm = orm
        self.meta = meta
        self.dialect = orm.get_dialect()
        self.l2_cache = orm.get_cache_provider()
        self.encryption = orm.get_encryption_service()

    def _get_session(self):
        session = SessionContext.get_current_session()
        if session is None:
            conn = self.orm.get_connection()
            session = Session(conn)
            SessionContext.set_current_session(session)
            return session, True
        return session, False

    def _release_session(self, session, created_new):
        if created_new:
            try:
                session.close()
            except Exception:
                pass
            SessionContext.clear()

    def _is_record_exists(self, conn, id_val) -> bool:
        ph = self.dialect.get_placeholders(1)
        if not self.dialect.supports_procedures:
            # SQLite named param
            id_name = self.meta.id_column.name.strip('`\"')
            sql = f"SELECT 1 FROM {self.dialect.quote_identifier(self.meta.table_name)} WHERE {self.dialect.quote_identifier(self.meta.id_column.name)} = :{id_name}"
            params = {id_name: id_val}
        else:
            sql = f"SELECT 1 FROM {self.dialect.quote_identifier(self.meta.table_name)} WHERE {self.dialect.quote_identifier(self.meta.id_column.name)} = {ph}"
            params = (id_val,)

        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            return cur.fetchone() is not None
        except Exception:
            return False
        finally:
            cur.close()

    def save(self, entity):
        session, created_new = self._get_session()
        try:
            id_val = getattr(entity, self.meta.id_column.field_name)
            if id_val is None or (self.meta.id_column.generated and not self._is_record_exists(session.connection, id_val)):
                res = self._insert(session, entity)
            else:
                res = self._update(session, entity)
            if created_new:
                session.connection.commit()
            return res
        except Exception as e:
            if created_new:
                try:
                    session.connection.rollback()
                except Exception:
                    pass
            raise e
        finally:
            self._release_session(session, created_new)

    def _insert(self, session, entity):
        conn = session.connection
        sql = self.dialect.get_insert_call_statement(self.meta)
        id_col = self.meta.id_column

        if not self.dialect.supports_procedures:
            # SQLite: dictionary parameters
            params = {}
            if not id_col.generated:
                params[id_col.name.strip('`\"')] = getattr(entity, id_col.field_name)
            for col in self.meta.columns:
                if col.primary_key:
                    continue
                params[col.name.strip('`\"')] = self._get_field_value(entity, col)
        else:
            # Postgres/MySQL call procedure with list params
            params = []
            if id_col.generated:
                params.append(None)
            else:
                params.append(getattr(entity, id_col.field_name))
            for col in self.meta.columns:
                if col.primary_key:
                    continue
                params.append(self._get_field_value(entity, col))
            params = tuple(params)

        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            if id_col.generated:
                if self.dialect.supports_procedures:
                    row = cur.fetchone()
                    if row:
                        generated_id = row[0]
                        setattr(entity, id_col.field_name, generated_id)
                else:
                    generated_id = cur.lastrowid
                    setattr(entity, id_col.field_name, generated_id)
            
            id_val = getattr(entity, id_col.field_name)
            session.l1_cache.put(self.meta.entity_class, id_val, entity)
            if self.l2_cache:
                self.l2_cache.put(self.meta.table_name, id_val, entity)
            return entity
        finally:
            cur.close()

    def update(self, entity):
        session, created_new = self._get_session()
        try:
            res = self._update(session, entity)
            if created_new:
                session.connection.commit()
            return res
        except Exception as e:
            if created_new:
                try:
                    session.connection.rollback()
                except Exception:
                    pass
            raise e
        finally:
            self._release_session(session, created_new)

    def _update(self, session, entity):
        conn = session.connection
        sql = self.dialect.get_update_call_statement(self.meta)
        id_col = self.meta.id_column

        if not self.dialect.supports_procedures:
            # SQLite: dictionary parameters
            params = {id_col.name.strip('`\"'): getattr(entity, id_col.field_name)}
            for col in self.meta.columns:
                if col.primary_key:
                    continue
                params[col.name.strip('`\"')] = self._get_field_value(entity, col)
        else:
            # Positional procedure params (p_id, p_col1, p_col2, ...)
            params = [getattr(entity, id_col.field_name)]
            for col in self.meta.columns:
                if col.primary_key:
                    continue
                params.append(self._get_field_value(entity, col))
            params = tuple(params)

        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            id_val = getattr(entity, id_col.field_name)
            session.l1_cache.put(self.meta.entity_class, id_val, entity)
            if self.l2_cache:
                self.l2_cache.put(self.meta.table_name, id_val, entity)
            return entity
        finally:
            cur.close()

    def delete(self, id_val):
        session, created_new = self._get_session()
        try:
            conn = session.connection
            sql = self.dialect.get_delete_call_statement(self.meta)
            
            if not self.dialect.supports_procedures:
                params = {self.meta.id_column.name.strip('`\"'): id_val}
            else:
                params = (id_val,)

            cur = conn.cursor()
            try:
                cur.execute(sql, params)
            finally:
                cur.close()

            if created_new:
                session.connection.commit()

            session.l1_cache.remove(self.meta.entity_class, id_val)
            if self.l2_cache:
                self.l2_cache.evict(self.meta.table_name, id_val)
        except Exception as e:
            if created_new:
                try:
                    session.connection.rollback()
                except Exception:
                    pass
            raise e
        finally:
            self._release_session(session, created_new)

    def find_by_id(self, id_val) -> Optional[object]:
        session, created_new = self._get_session()
        try:
            # 1. L1 Cache Check
            cached = session.l1_cache.get(self.meta.entity_class, id_val)
            if cached is not None:
                return cached

            # 2. L2 Cache Check
            if self.l2_cache:
                l2_cached = self.l2_cache.get(self.meta.table_name, id_val)
                if l2_cached is not None:
                    session.l1_cache.put(self.meta.entity_class, id_val, l2_cached)
                    return l2_cached

            # 3. Database fetch
            conn = session.connection
            sql = self.dialect.get_get_call_statement(self.meta)
            cur = conn.cursor()
            try:
                if self.dialect.get_name() == "PostgreSQL" and self.dialect.supports_procedures:
                    params = [id_val] + [None] * (len(self.meta.columns) - 1)
                    cur.execute(sql, tuple(params))
                    row = cur.fetchone()
                    if row and row[0] is not None:
                        colnames = [self.meta.id_column.name] + [c.name for c in self.meta.columns if not c.primary_key]
                        entity = self._map_row(row, colnames)
                        session.l1_cache.put(self.meta.entity_class, id_val, entity)
                        if self.l2_cache:
                            self.l2_cache.put(self.meta.table_name, id_val, entity)
                        return entity
                else:
                    if not self.dialect.supports_procedures:
                        params = {self.meta.id_column.name.strip('`\"'): id_val}
                    else:
                        params = (id_val,)
                        
                    cur.execute(sql, params)
                    colnames = [desc[0] for desc in cur.description] if cur.description else []
                    row = cur.fetchone()
                    if row:
                        entity = self._map_row(row, colnames)
                        session.l1_cache.put(self.meta.entity_class, id_val, entity)
                        if self.l2_cache:
                            self.l2_cache.put(self.meta.table_name, id_val, entity)
                        return entity
            finally:
                cur.close()
            return None
        finally:
            self._release_session(session, created_new)

    def find_all(self) -> List[object]:
        return self.query().list()

    def query(self) -> Query:
        session, created_new = self._get_session()
        if created_new:
            SessionContext.clear()
        return Query(self.meta, self.dialect, session, self.encryption, owns_session=created_new)

    def batch_insert(self, entities: Collection[object]):
        if not entities:
            return
        session, created_new = self._get_session()
        try:
            conn = session.connection
            cur = conn.cursor()
            
            if self.dialect.get_name() == "PostgreSQL" and self.dialect.supports_procedures:
                sql = self.dialect.get_batch_insert_call_statement(self.meta)
                params = []
                if not self.meta.id_column.generated:
                    ids = [getattr(e, self.meta.id_column.field_name) for e in entities]
                    params.append(ids)
                for col in self.meta.columns:
                    if col.primary_key:
                        continue
                    vals = [self._get_field_value(e, col) for e in entities]
                    params.append(vals)
                cur.execute(sql, tuple(params))
            else:
                sql = self.dialect.get_insert_call_statement(self.meta)
                params_list = []
                
                if not self.dialect.supports_procedures:
                    # SQLite dictionary list
                    for entity in entities:
                        params = {}
                        if not self.meta.id_column.generated:
                            params[self.meta.id_column.name.strip('`\"')] = getattr(entity, self.meta.id_column.field_name)
                        for col in self.meta.columns:
                            if col.primary_key:
                                continue
                            params[col.name.strip('`\"')] = self._get_field_value(entity, col)
                        params_list.append(params)
                else:
                    # Positional tuples list
                    for entity in entities:
                        params = []
                        if not self.meta.id_column.generated:
                            params.append(getattr(entity, self.meta.id_column.field_name))
                        for col in self.meta.columns:
                            if col.primary_key:
                                continue
                            params.append(self._get_field_value(entity, col))
                        params_list.append(tuple(params))
                
                cur.executemany(sql, params_list)

            if created_new:
                conn.commit()
        except Exception as e:
            if created_new:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise e
        finally:
            cur.close()
            self._release_session(session, created_new)

    def batch_update(self, entities: Collection[object]):
        if not entities:
            return
        session, created_new = self._get_session()
        try:
            conn = session.connection
            cur = conn.cursor()
            sql = self.dialect.get_update_call_statement(self.meta)
            params_list = []

            if not self.dialect.supports_procedures:
                # SQLite dictionaries list
                for entity in entities:
                    params = {self.meta.id_column.name.strip('`\"'): getattr(entity, self.meta.id_column.field_name)}
                    for col in self.meta.columns:
                        if col.primary_key:
                            continue
                        params[col.name.strip('`\"')] = self._get_field_value(entity, col)
                    params_list.append(params)
            else:
                # Positional tuples list
                for entity in entities:
                    params = [getattr(entity, self.meta.id_column.field_name)]
                    for col in self.meta.columns:
                        if col.primary_key:
                            continue
                        params.append(self._get_field_value(entity, col))
                    params_list.append(tuple(params))

            cur.executemany(sql, params_list)
            if created_new:
                conn.commit()
        except Exception as e:
            if created_new:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise e
        finally:
            cur.close()
            self._release_session(session, created_new)

    def _get_field_value(self, entity, col):
        val = getattr(entity, col.field_name, None)
        if col.encrypted and val is not None and self.encryption is not None:
            return self.encryption.encrypt(str(val))
        return val

    def _map_row(self, row, colnames) -> object:
        entity = self.meta.entity_class()
        for col in self.meta.columns:
            if col.name in colnames:
                idx = colnames.index(col.name)
                val = row[idx]
                if col.encrypted and val is not None and self.encryption is not None:
                    val = self.encryption.decrypt(val)
                setattr(entity, col.field_name, val)
        return entity
