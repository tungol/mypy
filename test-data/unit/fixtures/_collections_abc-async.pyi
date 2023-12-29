from abc import abstractmethod
from typing import Any, Container, Iterable, TypeVar

T_co = TypeVar('T_co', covariant=True)

class Sequence(Iterable[T_co], Container[T_co]):
    @abstractmethod
    def __getitem__(self, n: Any) -> T_co: pass
