from abc import abstractmethod
from typing import (
    Any,
    AsyncGenerator as AsyncGenerator,
    AsyncIterable as AsyncIterable,
    AsyncIterator as AsyncIterator,
    Awaitable as Awaitable,
    Callable as Callable,
    Container as Container,
    Coroutine as Coroutine,
    Generator as Generator,
    Generic,
    Iterable as Iterable,
    Iterator as Iterator,
    Mapping as Mapping,
    MutableMapping as MutableMapping,
    Protocol,
    Sized as Sized,
    TypeVar,
    overload,
    runtime_checkable,
)

T_co = TypeVar('T_co', covariant=True)

class Sequence(Iterable[T_co], Container[T_co]):
    @abstractmethod
    def __getitem__(self, n: Any) -> T_co: pass
