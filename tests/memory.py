"""What an InMemoryBackend holds as state, of every kind."""

# pylint: disable=protected-access  # tests look at the internals they check

from fastapi_alcazaba.backends.inmemory import InMemoryBackend, Value


def state(backend: InMemoryBackend) -> dict[str, Value]:
    """Its state entries by key, whatever their kind: the values themselves, to change them."""
    return {key: value for place in backend._state.values() for key, value in place.items()}
