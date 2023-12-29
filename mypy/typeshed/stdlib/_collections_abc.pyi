import sys
from abc import abstractmethod
from types import MappingProxyType
from typing import (  # noqa: Y022,Y038,Y057
    AbstractSet as Set,
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
from typing_extensions import final

__all__ = [
    "Awaitable",
    "Coroutine",
    "AsyncIterable",
    "AsyncIterator",
    "AsyncGenerator",
    "Hashable",
    "Iterable",
    "Iterator",
    "Generator",
    "Reversible",
    "Sized",
    "Container",
    "Callable",
    "Collection",
    "Set",
    "MutableSet",
    "Mapping",
    "MutableMapping",
    "MappingView",
    "KeysView",
    "ItemsView",
    "ValuesView",
    "Sequence",
    "MutableSequence",
    "ByteString",
]
if sys.version_info >= (3, 12):
    __all__ += ["Buffer"]

_KT_co = TypeVar("_KT_co", covariant=True)  # Key type covariant containers.
_VT_co = TypeVar("_VT_co", covariant=True)  # Value type covariant containers.
_T_co = TypeVar("_T_co", covariant=True)  # Any type covariant containers.

class Sequence(Reversible[_T_co], Collection[_T_co]):
    @overload
    @abstractmethod
    def __getitem__(self, index: int) -> _T_co: ...
    @overload
    @abstractmethod
    def __getitem__(self, index: slice) -> Sequence[_T_co]: ...
    # Mixin methods
    def index(self, value: Any, start: int = 0, stop: int = ...) -> int: ...
    def count(self, value: Any) -> int: ...
    def __contains__(self, value: object) -> bool: ...
    def __iter__(self) -> Iterator[_T_co]: ...
    def __reversed__(self) -> Iterator[_T_co]: ...
    @abstractmethod
    def __len__(self) -> int: ...

@final
class dict_keys(KeysView[_KT_co], Generic[_KT_co, _VT_co]):  # undocumented
    def __eq__(self, __value: object) -> bool: ...
    if sys.version_info >= (3, 10):
        @property
        def mapping(self) -> MappingProxyType[_KT_co, _VT_co]: ...

@final
class dict_values(ValuesView[_VT_co], Generic[_KT_co, _VT_co]):  # undocumented
    if sys.version_info >= (3, 10):
        @property
        def mapping(self) -> MappingProxyType[_KT_co, _VT_co]: ...

@final
class dict_items(ItemsView[_KT_co, _VT_co]):  # undocumented
    __module__: str = "builtins"
    def __eq__(self, __value: object) -> bool: ...
    if sys.version_info >= (3, 10):
        @property
        def mapping(self) -> MappingProxyType[_KT_co, _VT_co]: ...

if sys.version_info >= (3, 12):
    @runtime_checkable
    class Buffer(Protocol):
        @abstractmethod
        def __buffer__(self, __flags: int) -> memoryview: ...
