from __future__ import annotations

from typing import Any, Dict, Type, TypeVar

from pydantic import BaseModel, TypeAdapter

ModelT = TypeVar("ModelT", bound=BaseModel)


def schema_for(model: Type[ModelT]) -> Dict[str, Any]:
    return model.model_json_schema()


def validate_json(model: Type[ModelT], payload: Any) -> ModelT:
    return TypeAdapter(model).validate_python(payload)


def dump_json(model: BaseModel) -> Dict[str, Any]:
    return model.model_dump(mode="json")

