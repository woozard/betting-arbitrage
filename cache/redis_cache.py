import redis
import json
from datetime import datetime, date
from decimal import Decimal


class RedisCache:
    def __init__(self, host='localhost', port=6379, db=0):
        self.client = redis.Redis(
            host=host,
            port=port,
            db=db,
            decode_responses=True
        )

    # ---------- CUSTOM JSON SERIALIZER ----------
    def _json_serializer(self, obj):
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()

        if isinstance(obj, Decimal):
            # ✅ Option 1: preserve precision (recommended for odds)
            return str(obj)

            # ❗ Option 2 (NOT recommended for money/odds):
            # return float(obj)

        raise TypeError(f"Type {type(obj)} not serializable")

    # ---------- BASIC ----------
    def set(self, key, value, ttl=None):
        if isinstance(value, (dict, list)):
            value = json.dumps(value, default=self._json_serializer)
        self.client.set(key, value, ex=ttl)

    def get(self, key):
        value = self.client.get(key)
        if not value:
            return None
        try:
            return json.loads(value)
        except:
            return value

    def delete(self, key):
        self.client.delete(key)

    def scan(self, pattern):
        return list(self.client.scan_iter(match=pattern))

    def pipeline(self):
        return self.client.pipeline()

    def lpush(self, key, value):
        if isinstance(value, (dict, list)):
            value = json.dumps(value, default=self._json_serializer)
        return self.client.lpush(key, value)

    def blpop(self, keys, timeout=0):
        """Block until one of keys has a value. timeout in seconds (float ok)."""
        if isinstance(keys, str):
            keys = [keys]
        result = self.client.blpop(keys, timeout=timeout)
        if not result:
            return None
        key, value = result
        try:
            return key, json.loads(value)
        except Exception:
            return key, value