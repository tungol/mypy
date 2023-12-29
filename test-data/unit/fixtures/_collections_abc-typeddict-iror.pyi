from typing import Any, Iterable, TypeVar

T_co = TypeVar('T_co', covariant=True)

class Sequence(Iterable[T_co]):
    # misc is for explicit Any.
    def __getitem__(self, n: Any) -> T_co: pass # type: ignore[misc]
