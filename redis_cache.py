from django.core.exceptions import ImproperlyConfigured
from django.utils import importlib
from django.utils.encoding import smart_unicode, smart_str
from django.utils.datastructures import SortedDict

try:
    import cPickle as pickle
except ImportError:
    import pickle
 
try:
    import redis
except ImportError:
    print "Redis cache backend requires the 'redis-py' library"
from redis.connection import UnixDomainSocketConnection, Connection
from redis.connection import DefaultParser


class CacheKey(object):
    """
    A stub string class that we can use to check if a key was created already.
    """
    def __init__(self, key):
        self._key = key

    def __eq__(self, other):
        return self._key == other

    def __str__(self):
        return self.__unicode__()

    def __repr__(self):
        return self.__unicode__()

    def __unicode__(self):
        return smart_str(self._key)


class CacheConnectionPool(object):

    def __init__(self):
        self._connection_pools = {}

    def get_connection_pool(self, host='127.0.0.1', port=6379, db=1,
        password=None, parser_class=None,
        unix_socket_path=None):
        connection_identifier = (host, port, db, parser_class, unix_socket_path)
        if not self._connection_pools.get(connection_identifier):
            connection_class = (
                unix_socket_path and UnixDomainSocketConnection or Connection
            )
            kwargs = {
                'db': db,
                'password': password,
                'connection_class': connection_class,
                'parser_class': parser_class,
            }
            if unix_socket_path is None:
                kwargs.update({
                    'host': host,
                    'port': port,
                })
            else:
                kwargs['path'] = unix_socket_path
            self._connection_pools[connection_identifier] = redis.ConnectionPool(**kwargs)
        return self._connection_pools[connection_identifier]
pool = CacheConnectionPool()


class RedisCache():
    def __init__(self, server, params):
        """
        Connect to Redis, and set up cache backend.
        """
        self._init(server, params)

    def _init(self, server, params):
        #super(CacheClass, self).__init__(params)
        self._server = server
        self._params = params

        unix_socket_path = None
        if ':' in self.server:
            host, port = self.server.rsplit(':', 1)
            try:
                port = int(port)
            except (ValueError, TypeError):
                raise ImproperlyConfigured("port value must be an integer")
        else:
            host, port = None, None
            unix_socket_path = self.server

        kwargs = {
            'db': self.db,
            'password': self.password,
            'host': host,
            'port': port,
            'unix_socket_path': unix_socket_path,
        }
        connection_pool = pool.get_connection_pool(
            parser_class=self.parser_class,
            **kwargs
        )
        self._client = redis.Redis(
            connection_pool=connection_pool,
            **kwargs
        )

    @property
    def server(self):
        return self._server or "127.0.0.1:6379"

    @property
    def params(self):
        return self._params or {}

    @property
    def options(self):
        return self.params.get('OPTIONS', {})

    @property
    def db(self):
        _db = self.params.get('db', self.options.get('DB', 1))
        try:
            _db = int(_db)
        except (ValueError, TypeError):
            raise ImproperlyConfigured("db value must be an integer")
        return _db

    @property
    def password(self):
        return self.params.get('password', self.options.get('PASSWORD', None))

    @property
    def parser_class(self):
        cls = self.options.get('PARSER_CLASS', None)
        if cls is None:
            return DefaultParser
        mod_path, cls_name = cls.rsplit('.', 1)
        try:
            mod = importlib.import_module(mod_path)
            parser_class = getattr(mod, cls_name)
        except (AttributeError, ImportError):
            raise ImproperlyConfigured("Could not find parser class '%s'" % parser_class)
        return parser_class

    def __getstate__(self):
        return {'params': self._params, 'server': self._server}

    def __setstate__(self, state):
        self._init(**state)

    def make_key(self, key, version=None):
        """
        Returns the utf-8 encoded bytestring of the given key as a CacheKey
        instance to be able to check if it was "made" before.
        """
        if not isinstance(key, CacheKey):
            key = CacheKey(key)
        return key

    def add(self, key, value, timeout=None, version=None):
        """
        Add a value to the cache, failing if the key already exists.

        Returns ``True`` if the object was added, ``False`` if not.
        """
        key = self.make_key(key, version=version)
        if self._client.exists(key):
            return False
        return self.set(key, value, timeout)

    def get(self, key, default=None, version=None):
        """
        Retrieve a value from the cache.

        Returns unpickled value if key is found, the default if not.
        """
        key = self.make_key(key, version=version)
        value = self._client.get(key)
        if value is None:
            return default
        try:
            result = int(value)
        except (ValueError, TypeError):
            result = self.unpickle(value)
        return result

    def hget(self, name, key, default=None, version=None):
        """
        Retrieve a value from the cache as hash.

        Returns unpickled value if key is found, the default if not.
        """
        name = self.make_key(name, version=version)
        key = self.make_key(key, version=version)
        value = self._client.hget(name, key)
        if value is None:
            return default
        try:
            result = int(value)
        except (ValueError, TypeError):
            result = self.unpickle(value)
        return result

    def hget_all(self, name, default=None, version=None):
        """
        Retrieve all value from the cache as hash.

        Note it does not unpickle the values
        """
        name = self.make_key(name, version=version)
        value = self._client.hgetall(name)
        return value

    def _set(self, key, value, timeout, client):
        if timeout == 0:
            return client.set(key, value)
        elif timeout > 0:
            return client.setex(key, value, int(timeout))
        else:
            return False

    def _hset(self, name, key, value, timeout, client):
        """Set value in hash"""
        # Redis call
        status = client.hset( name, key, value)
        if timeout > 0:
            # Set expire time
            client.expire(name, timeout)
        return status

    def set(self, key, value, timeout=None, version=None, client=None):
        """
        Persist a value to the cache, and set an optional expiration time.
        """
        if not client:
            client = self._client
        key = self.make_key(key, version=version)
        if timeout is None:
            timeout = 24*60*60
        try:
            value = float(value)
            # If you lose precision from the typecast to str, then pickle value
            if int(value) != value:
                raise TypeError
        except (ValueError, TypeError):
            result = self._set(key, pickle.dumps(value), int(timeout), client)
        else:
            result = self._set(key, int(value), int(timeout), client)
        # result is a boolean
        return result

    def hset(self, name, key, value, timeout=None, version=None, client=None):
        """
        Sets field in the hash stored at key to value 
        and set an optional expiration time.
        """
        if not client:
            client = self._client
        name = self.make_key(name, version=version)
        key = self.make_key(key, version=version)
        if timeout is None:
            # To store it persistently
            timeout = 0
        try:
            value = float(value)
            # If you lose precision from the typecast to str, then pickle value
            if int(value) != value:
                raise TypeError
        except (ValueError, TypeError):
            result = self._hset(name, key, pickle.dumps(value), int(timeout), client)
        else:
            result = self._hset(name, key, int(value), int(timeout), client)
        # result is a boolean
        return result

    def delete(self, key, version=None):
        """
        Remove a key from the cache.
        """
        self._client.delete(self.make_key(key, version=version))

    def delete_many(self, keys, version=None):
        """
        Remove multiple keys at once.
        """
        if keys:
            keys = map(lambda key: self.make_key(key, version=version), keys)
            self._client.delete(*keys)

    def clear(self):
        """
        Flush all cache keys.
        """
        # TODO : potential data loss here, should we only delete keys based on the correct version ?
        self._client.flushdb()

    def unpickle(self, value):
        """
        Unpickles the given value.
        """
        value = smart_str(value)
        return pickle.loads(value)

    def get_many(self, keys, version=None):
        """
        Retrieve many keys.
        """
        if not keys:
            return {}
        recovered_data = SortedDict()
        new_keys = map(lambda key: self.make_key(key, version=version), keys)
        map_keys = dict(zip(new_keys, keys))
        results = self._client.mget(new_keys)
        for key, value in zip(new_keys, results):
            if value is None:
                continue
            try:
                value = int(value)
            except (ValueError, TypeError):
                value = self.unpickle(value)
            if isinstance(value, basestring):
                value = smart_unicode(value)
            recovered_data[map_keys[key]] = value
        return recovered_data

    def hget_many(self, name, keys, version=None):
        """
        Retrieve many keys from hash.
        Keys is the list of the key in the hash.
        Result is returned as key value pair dict. 
        """
        recovered_data = SortedDict()
        new_keys = map(lambda key: self.make_key(key, version=version), keys)
        map_keys = dict(zip(new_keys, keys))
        name = self.make_key(name, version=version)
        # Redis call
        results = self._client.hmget(name, new_keys)
        # Iterate to convert the result into proper format.
        for key, value in zip(new_keys, results):
            if value is None:
                continue
            try:
                value = int(value)
            except (ValueError, TypeError):
                value = self.unpickle(value)
            if isinstance(value, basestring):
                value = smart_unicode(value)
            recovered_data[map_keys[key]] = value
        return recovered_data

    def set_many(self, data, timeout=None, version=None):
        """
        Set a bunch of values in the cache at once from a dict of key/value
        pairs. This is much more efficient than calling set() multiple times.

        If timeout is given, that timeout will be used for the key; otherwise
        the default cache timeout will be used.
        """
        pipeline = self._client.pipeline()
        for key, value in data.iteritems():
            self.set(key, value, timeout, version=version, client=pipeline)
        pipeline.execute()


    def hset_many(self, name, mapping_dict, timeout=None, version=None):
        """
        Set a bunch of values in the cache at once from a dict of key/value
        pairs. This is much more efficient than calling hset() multiple times.

        If timeout is given, that timeout will be used for the key; otherwise 
        stored persistently
        """
        # Convert the keys to version format
        mapping_dict = dict( (self.make_key(key, version), value) for key, value in  mapping_dict.items() )
        name = self.make_key(name, version=version)
        # Iterate to convert the values to appropriate format.
        for key, value in mapping_dict.items():
            try:
                value = float(value)
                # If you lose precision from the typecast to str, then pickle value
                if int(value) != value:
                    raise TypeError
            except (ValueError, TypeError):
                value = pickle.dumps(value)
            else:
                value = int(value)
            mapping_dict[key] = value

        # Redis call
        result = self._client.hmset(name, mapping_dict)
        if timeout is not None: 
            # Set timeout of the name
            self._client.expire(name, timeout)
        return result

    def incr(self, key, delta=1, version=None):
        """
        Add delta to value in the cache. If the key does not exist, raise a
        ValueError exception.
        """
        key = self.make_key(key, version=version)
        exists = self._client.exists(key)
        if not exists:
            raise ValueError("Key '%s' not found" % key)
        try:
            value = self._client.incr(key, delta)
        except redis.ResponseError:
            value = self.get(key) + 1
            self.set(key, value)
        return value

    def hincr(self, name, key, delta=1, version=None):
        """
        Add delta to value in the cache of the hash. 
        If the key does not exist, raise a
        ValueError exception.
        """
        name = self.make_key(name, version=version)
        key = self.make_key(key, version=version)
        exists = self._client.hexists(name, key)
        if not exists:
            raise ValueError("Key '%s' not found" % key)
        try:
            # Redis call
            value = self._client.hincrby(name, key, delta)
        except redis.ResponseError:
            value = self.hget(name, key) + 1
            # Redis call
            self.hset(name, key, value)
        return value

    def has_hkey(self, name, key, version=None):
        """
        The name and key exist in the hash
        """
        name = self.make_key(name, version=version)
        key = self.make_key(key, version=version)
        return self._client.hexists(name, key)
    def sadd_list(self, name, value_list, expiry=86400):
        """
        The name and key exist in the hash
        """
        self._client.sadd(name, *value_list)
        self._client.expire(name, expiry)

    def set_str(self, key, value, timeout=86400, version=None, client=None):
        """
        Persist a value to the cache, and set an optional expiration time.
        """
        if not client:
            client = self._client
        key = self.make_key(key, version=version)
        if timeout is None:
            timeout = 24*60*60
        result = self._set(key, value, int(timeout), client)
        return result
     
    def get_many_mandatory(self, keys, version=None):
        """
        Retrieve many keys.
        """
        if not keys:
            return {}
        recovered_data = SortedDict()
        new_keys = map(lambda key: self.make_key(key, version=version), keys)
        map_keys = dict(zip(new_keys, keys))
        results = self._client.mget(new_keys)
        for key, value in zip(new_keys, results):
            if value is None:
                return None
            recovered_data[map_keys[key]] = value
        return recovered_data

    def set_many_str(self, data, timeout=None, version=None):
        """
        Set a bunch of values in the cache at once from a dict of key/value
        pairs. This is much more efficient than calling set() multiple times.

        If timeout is given, that timeout will be used for the key; otherwise
        the default cache timeout will be used.
        """
        pipeline = self._client.pipeline()
        for key, value in data.iteritems():
            self.set_str(key, value, timeout, version=version, client=pipeline)
        pipeline.execute()
#r = RedisCache(server='127.0.0.1:6379',params={'OPTIONS':{ 'DB': 1 }})
#r.set('key',{'a':1}, 60*60)
#print r.get('key')
