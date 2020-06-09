from typing import *

number_type = Union[int, float]
generic_number_type = Union[number_type, Any]
nullable_number_type = Union[number_type, None]

nested_params_type = Dict[str, Union[Any, Dict[str, Any]]]
all_nested_params_type = Dict[str, Union[List[Any], Dict[str, List[Any]]]]
flattened_params_type = Dict[str, Any]
all_flattened_params_type = Dict[str, List[Any]]


__all__ = [
    "number_type", "generic_number_type", "nullable_number_type",
    "nested_params_type", "all_nested_params_type",
    "flattened_params_type", "all_flattened_params_type"
]