from abc import ABC, abstractmethod

from typing import Any, Dict, Tuple, Optional


class Verifier(ABC):
    """
    Base class for a Verifier.
    """

    @abstractmethod
    def process_row(self, solution: Any) -> Any:
        """
        Verify a solution.
        """
        pass
