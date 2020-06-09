import math

from ...types import *
from ..bases import SingleNormalizer


@SingleNormalizer.register("cube")
class CubeNormalizer(SingleNormalizer):
    @property
    def bounds(self) -> bounds_type:
        return 0., 1.

    def normalize(self, value: generic_number_type) -> float:
        if self._choices is not None:
            idx = self._choices.index(value)
            return idx / (len(self._choices) - 1)
        if self._is_exponential:
            value = math.log(value, self.dist.base)
        return (value - self._lower) / self._diff

    def recover(self, value: float) -> generic_number_type:
        if self._choices is not None:
            idx = int(round(value * (len(self._choices) - 1)))
            idx = max(0, min(len(self._choices) - 1, idx))
            return self._choices[idx]
        value = max(0., min(1., value))
        value = value * self._diff + self._lower
        if self._is_exponential:
            value = math.pow(self.dist.base, value)
        return value


__all__ = ["CubeNormalizer"]
