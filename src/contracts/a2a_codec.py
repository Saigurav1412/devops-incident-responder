"""Convert between our Pydantic contract models and A2A v1.0 `Part` messages.

WHY this file exists: a2a-sdk 1.x types (Message, Part, Task) are *protobuf* messages, while our
contracts are *Pydantic* models (INTERFACES §2, "SDK gotcha"). Converting between them by hand in
two places is exactly the kind of thing that drifts, so both the diagnoser's A2A client and the
executor's A2A server go through these two functions.

HOW it works:
  model  --model_dump(mode="json")-->  plain dict  --ParseDict-->  protobuf Value  -->  Part(data=...)
  Part(data=...)  --MessageToDict-->  plain dict  --Model.model_validate-->  model

`mode="json"` matters: it turns datetimes into ISO strings and so on, because a protobuf
`Value` can only hold JSON types.
"""

from __future__ import annotations

from typing import Any

from a2a.types import Part
from google.protobuf import json_format, struct_pb2
from pydantic import BaseModel, TypeAdapter

JSON_MEDIA_TYPE = "application/json"


class CodecError(ValueError):
    """The part isn't a single JSON data part we can parse. The executor answers with REJECTED."""


def to_data_part(model: BaseModel) -> Part:
    """Wrap one contract model as an A2A `data` part (v1.0: no `kind`, the `data` field is the type)."""
    value = struct_pb2.Value()
    json_format.ParseDict(model.model_dump(mode="json"), value)
    return Part(data=value, media_type=JSON_MEDIA_TYPE)


def part_to_dict(part: Part) -> dict[str, Any]:
    """Extract the JSON object from a data part, or raise CodecError for any other kind of part."""
    if part.WhichOneof("content") != "data":
        raise CodecError("expected a data part")
    data = json_format.MessageToDict(part.data)
    if not isinstance(data, dict):
        raise CodecError("data part must hold a JSON object")
    return data


def from_data_part[M: BaseModel](part: Part, model: type[M]) -> M:
    """Parse a data part into `model`. Pydantic validation errors propagate unchanged."""
    return model.model_validate(part_to_dict(part))


def from_data_part_as(part: Part, type_: Any) -> Any:
    """Like from_data_part, for non-model types such as the `ExecutorRequest` discriminated union."""
    return TypeAdapter(type_).validate_python(part_to_dict(part))
