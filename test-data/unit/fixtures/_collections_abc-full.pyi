from abc import abstractmethod
from typing import (
    AbstractSet,
    Any,
    AsyncGenerator as AsyncGenerator,
    AsyncIterable as AsyncIterable,
    AsyncIterator as AsyncIterator,
    Awaitable as Awaitable,
    ByteString as ByteString,
    Callable as Callable,
    Collection as Collection,
    Container as Container,
    Coroutine as Coroutine,
    Generator as Generator,
    Generic,
    Hashable as Hashable,
    ItemsView as ItemsView,
    Iterable as Iterable,
    Iterator as Iterator,
    KeysView as KeysView,
    Mapping as Mapping,
    MappingView as MappingView,
    MutableMapping as MutableMapping,
    MutableSequence as MutableSequence,
    MutableSet as MutableSet,
    Protocol,
    Reversible as Reversible,
    Sized as Sized,
    TypeVar,
    ValuesView as ValuesView,
    overload,
    runtime_checkable,
)

T_co = TypeVar('T_co', covariant=True)

Set = AbstractSet

class Sequence(Iterable[T_co], Container[T_co]):
    @abstractmethod
    def __getitem__(self, n: Any) -> T_co: pass
