from typing import Iterable, TypeVar

T_co = TypeVar('T_co', covariant=True)

class Sequence(Iterable[T_co]): pass
