# Builtins stub used in check-narrowing test cases.
from _collections_abc import Sequence
from typing import Generic, Tuple, Type, TypeVar, Union


Tco = TypeVar('Tco', covariant=True)
KT = TypeVar("KT")
VT = TypeVar("VT")

class object:
    def __init__(self) -> None: pass

class type: pass
class tuple(Sequence[Tco], Generic[Tco]): pass
class function: pass
class ellipsis: pass
class int: pass
class str: pass
class dict(Generic[KT, VT]): pass

def isinstance(x: object, t: Union[Type[object], Tuple[Type[object], ...]]) -> bool: pass
