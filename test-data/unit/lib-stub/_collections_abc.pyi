from typing import (
    Any,
    Awaitable as Awaitable,
    Coroutine as Coroutine,
    Generator as Generator,
    Iterable as Iterable,
    Iterator as Iterator,
    Mapping as Mapping,
    TypeVar,
)

T_co = TypeVar('T_co', covariant=True)

class Sequence(Iterable[T_co]):
    def __getitem__(self, n: Any) -> T_co: pass
    def __len__(self) -> int: pass
