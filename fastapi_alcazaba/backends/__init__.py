from fastapi_alcazaba.backends import inmemory, tiered
from fastapi_alcazaba.backends.base import Backend

__all__ = ["Backend", "inmemory", "tiered"]

# import each backend in turn and add to __all__. This syntax
# is explicitly supported by type checkers, while more dynamic
# syntax would not be recognised.
try:
    from fastapi_alcazaba.backends import dynamodb
except ImportError:
    pass
else:
    __all__ += ["dynamodb"]

try:
    from fastapi_alcazaba.backends import memcached
except ImportError:
    pass
else:
    __all__ += ["memcached"]

try:
    from fastapi_alcazaba.backends import redis
except ImportError:
    pass
else:
    __all__ += ["redis"]

try:
    from fastapi_alcazaba.backends import valkey
except ImportError:
    pass
else:
    __all__ += ["valkey"]

try:
    from fastapi_alcazaba.backends import glide
except ImportError:
    pass
else:
    __all__ += ["glide"]
