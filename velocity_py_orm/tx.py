import threading
from .cache import L1Cache

class Session:
    def __init__(self, connection, in_transaction=False):
        self.connection = connection
        self._in_transaction = in_transaction
        self.l1_cache = L1Cache()

    def is_in_transaction(self) -> bool:
        return self._in_transaction

    def close(self):
        self.l1_cache.clear()
        self.connection.close()


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


class transaction:
    def __init__(self, orm):
        self.orm = orm
        self.session = None
        self.owns_session = False

    def __enter__(self):
        self.session = SessionContext.get_current_session()
        if self.session is None:
            conn = self.orm.get_connection()
            # DB-API starts transactions implicitly on modifications.
            self.session = Session(conn, in_transaction=True)
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


def transactional(orm):
    def decorator(func):
        def wrapper(*args, **kwargs):
            with transaction(orm):
                return func(*args, **kwargs)
        return wrapper
    return decorator
