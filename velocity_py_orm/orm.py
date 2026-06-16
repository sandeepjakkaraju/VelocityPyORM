from .repository import BaseRepository

class VelocityPyORM:
    def __init__(self, builder):
        self._connection_factory = builder._connection_factory
        self._dialect = builder._dialect
        self._cache_provider = builder._cache_provider
        self._encryption_service = builder._encryption_service
        self._repository_cache = {}

    def get_connection(self):
        return self._connection_factory()

    def get_dialect(self):
        return self._dialect

    def get_cache_provider(self):
        return self._cache_provider

    def get_encryption_service(self):
        return self._encryption_service

    def repository(self, entity_class):
        if entity_class not in self._repository_cache:
            if not hasattr(entity_class, '_meta'):
                raise ValueError(
                    f"Class {entity_class.__name__} is not registered as an entity. "
                    "Did you forget the @entity decorator?"
                )
            self._repository_cache[entity_class] = BaseRepository(self, entity_class._meta)
        return self._repository_cache[entity_class]

    def bootstrap(self, entity_classes):
        from .migration import SchemaGenerator, ProcedureGenerator
        conn = self.get_connection()
        try:
            metas = [cls._meta for cls in entity_classes if hasattr(cls, '_meta')]
            
            # 1. Run Schema Table DDL
            sg = SchemaGenerator(self._dialect)
            sg.generate_schema(conn, metas)
            
            # 2. Run Stored Procedure DDL
            pg = ProcedureGenerator(self._dialect)
            pg.generate_procedures(conn, metas)
            
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            raise e
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def builder():
        return Builder()


class Builder:
    def __init__(self):
        self._connection_factory = None
        self._dialect = None
        self._cache_provider = None
        self._encryption_service = None

    def connection_factory(self, factory):
        self._connection_factory = factory
        return self

    def dialect(self, dialect):
        self._dialect = dialect
        return self

    def cache_provider(self, provider):
        self._cache_provider = provider
        return self

    def encryption_service(self, service):
        self._encryption_service = service
        return self

    def build(self) -> VelocityPyORM:
        if self._connection_factory is None:
            raise ValueError("connection_factory must be configured")
        if self._dialect is None:
            raise ValueError("dialect must be configured")
        return VelocityPyORM(self)
