from __future__ import annotations
from abc import ABC, abstractmethod

class SourceReader(ABC):
    @abstractmethod
    def read(self, spec: dict) -> list[dict]: ...
