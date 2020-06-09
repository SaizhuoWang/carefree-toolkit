import math
import pprint
import random
import logging

import numpy as np

from typing import *
from abc import abstractmethod, ABCMeta

from ..utils import *
from ...misc import *
from ...dist import Parallel
from ...param_utils import *

hpo_dict: Dict[str, Type["HPOBase"]] = {}
pattern_creator_type = Callable[[np.ndarray, np.ndarray, Dict[str, Any]], pattern_type]


class HPOBase(LoggingMixin, metaclass=ABCMeta):
    def __init__(self,
                 pattern_creator: pattern_creator_type,
                 params: Dict[str, DataType],
                 *,
                 verbose_level: int = 2):
        self._caches = {}
        self._creator = pattern_creator
        self.param_generator = ParamsGenerator(params)
        self._verbose_level = verbose_level

    @property
    @abstractmethod
    def is_sequential(self) -> bool:
        pass

    @property
    def last_params(self) -> Dict[str, Any]:
        return self.param_mapping[self.last_code]

    @property
    def last_patterns(self) -> List[pattern_type]:
        return self.patterns[self.last_code]

    def _sample_params(self) -> Union[None, Dict[str, Any]]:
        if self.is_sequential:
            raise NotImplementedError
        return

    def _update_caches(self) -> None:
        if self.is_sequential:
            raise NotImplementedError
        return

    def search(self,
               x: np.ndarray,
               y: np.ndarray,
               estimators: List[Estimator],
               x_validation: np.ndarray = None,
               y_validation: np.ndarray = None,
               *,
               num_jobs: int = 4,
               num_retry: int = 5,
               num_search: Union[str, int, float] = 10,
               verbose_level: int = 3) -> "HPOBase":

        if x_validation is None or y_validation is None:
            x_validation, y_validation = x, y

        self.estimators = estimators
        self.x_validation, self.y_validation = x_validation, y_validation

        n_params = self.param_generator.n_params
        if isinstance(num_search, str):
            if num_search != "all":
                raise ValueError(f"num_search can only be 'all' when it is a string, '{num_search}' found")
            if n_params == math.inf:
                raise ValueError("num_search is 'all' but we have infinite params to search")
            num_search = n_params
        if num_search > n_params:
            self.log_msg(
                f"`n` is larger than total choices we've got ({n_params}), therefore only "
                f"{n_params} searches will be run", self.warning_prefix, msg_level=logging.WARNING
            )
            num_search = n_params
        num_jobs = min(num_search, num_jobs)

        def _core(params_, *, parallel_run=False) -> List[pattern_type]:
            range_list = list(range(num_retry))
            _task = lambda _=0: self._creator(x, y, params_)
            if parallel_run:
                parallel_ = Parallel(num_jobs, task_names=range_list)(_task, range_list)
                local_patterns = [parallel_.parallel_results[str(i_)] for i_ in range_list]
            else:
                local_patterns = []
                for _ in range_list:
                    local_patterns.append(_task())
            print(".", end="", flush=True)
            return local_patterns

        with timeit("Generating Patterns"):
            if self.is_sequential:
                counter = 0
                self.patterns, self.param_mapping = {}, {}
                while counter < num_search:
                    counter += 1
                    params = self._sample_params()
                    self.last_code = hash_code(str(params))
                    self.param_mapping[self.last_code] = params
                    self.patterns[self.last_code] = _core(params, parallel_run=True)
            else:
                if n_params == math.inf:
                    all_params = [self.param_generator.pop() for _ in range(num_search)]
                else:
                    all_params = []
                    all_indices = set(random.sample(list(range(num_search)), k=num_search))
                    for i, param in enumerate(self.param_generator.all()):
                        if i in all_indices:
                            all_params.append(param)
                        if len(all_params) == num_search:
                            break

                codes = list(map(hash_code, map(str, all_params)))
                self.param_mapping = dict(zip(codes, all_params))
                if num_jobs <= 1:
                    patterns = list(map(_core, all_params))
                else:
                    parallel = Parallel(num_jobs, task_names=list(range(num_search)))(_core, all_params)
                    patterns = [parallel.parallel_results[str(i)] for i in range(num_search)]
                self.patterns = dict(zip(codes, patterns))
                self.last_code = codes[-1]

        self.comparer = Comparer(self.patterns, estimators)
        self.comparer.compare(x_validation, y_validation, verbose_level=verbose_level)

        best_methods = self.comparer.best_methods
        self.best_params = {k: self.param_mapping[v] for k, v in best_methods.items()}
        param_msgs = {k: pprint.pformat(v) for k, v in self.best_params.items()}
        msg = "\n".join(
            sum([[
                "-" * 100,
                f"{k} ({self.comparer.final_scores[k][best_methods[k]]:8.6f})",
                "-" * 100,
                param_msgs[k]
            ] for k in sorted(param_msgs)], [])
            + ["-" * 100]
        )
        self.log_block_msg(msg, self.info_prefix, "Best Parameters", verbose_level - 1)

        return self

    @staticmethod
    def make(method: str, *args, **kwargs) -> "HPOBase":
        return hpo_dict[method](*args, **kwargs)

    @classmethod
    def register(cls, name):
        global hpo_dict
        return register_core(name, hpo_dict)


__all__ = ["HPOBase", "hpo_dict"]