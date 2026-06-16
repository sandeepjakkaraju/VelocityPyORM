__author__ = "sandeepkumarjakkaraju"

import abc
from collections import defaultdict

class L1Cache:
    def __init__(self):
        self._cache = defaultdict(dict)

    def put(self, clazz, id_val, entity):
        self._cache[clazz][id_val] = entity

    def get(self, clazz, id_val):
        return self._cache[clazz].get(id_val, None)

    def remove(self, clazz, id_val):
        if id_val in self._cache[clazz]:
            del self._cache[clazz][id_val]

    def clear(self):
        self._cache.clear()


class CacheProvider(abc.ABC):
    @abc.abstractmethod
    def put(self, cache_name: str, key, value):
        pass

    @abc.abstractmethod
    def get(self, cache_name: str, key):
        pass

    @abc.abstractmethod
    def evict(self, cache_name: str, key):
        pass

    @abc.abstractmethod
    def clear(self, cache_name: str):
        pass


class SimpleCacheProvider(CacheProvider):
    def __init__(self):
        self._store = defaultdict(dict)

    def put(self, cache_name: str, key, value):
        self._store[cache_name][key] = value

    def get(self, cache_name: str, key):
        return self._store[cache_name].get(key, None)

    def evict(self, cache_name: str, key):
        if key in self._store[cache_name]:
            del self._store[cache_name][key]

    def clear(self, cache_name: str):
        self._store[cache_name].clear()


class RedisCacheProvider(CacheProvider):
    def __init__(self, redis_client=None, host="localhost", port=6379, db=0, password=None, ssl=False):
        if redis_client is not None:
            self.client = redis_client
        else:
            import redis
            self.client = redis.Redis(host=host, port=port, db=db, password=password, ssl=ssl)

    def _get_key(self, cache_name: str, key) -> str:
        return f"velocity_cache:{cache_name}:{key}"

    def put(self, cache_name: str, key, value):
        import pickle
        serialized = pickle.dumps(value)
        self.client.set(self._get_key(cache_name, key), serialized)

    def get(self, cache_name: str, key):
        import pickle
        data = self.client.get(self._get_key(cache_name, key))
        if data is not None:
            try:
                return pickle.loads(data)
            except Exception:
                try:
                    return int(data)
                except ValueError:
                    try:
                        return data.decode('utf-8')
                    except Exception:
                        return data
        return None

    def evict(self, cache_name: str, key):
        self.client.delete(self._get_key(cache_name, key))

    def clear(self, cache_name: str):
        pattern = f"velocity_cache:{cache_name}:*"
        keys = self.client.keys(pattern)
        if keys:
            self.client.delete(*keys)

