from typing import Any, Type, get_type_hints, get_origin, get_args
from typing import Optional, Union, List, Dict
import inspect

def get_json_schema_type(type_hint: Any) -> dict:
    """Convert a Python type hint to JSON Schema type."""
    origin = get_origin(type_hint)
    args = get_args(type_hint)
    
    if origin is Union and type(None) in args:
        # This is an Optional type
        remaining_type = next(t for t in args if t != type(None))
        schema = get_json_schema_type(remaining_type)
        return schema
    
    if origin is List:
        return {
            "type": "array",
            "items": get_json_schema_type(args[0])
        }
    
    type_map = {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
        dict: {"type": "object"},
        list: {"type": "array"}
    }
    
    return type_map.get(type_hint, {"type": "string"})

def typed_dict_to_json_schema(cls: Type) -> dict:
    """Convert a TypedDict to JSON Schema format."""
    type_hints = get_type_hints(cls)
    required = []
    properties = {}
    
    # Get the docstring for the class if available
    class_doc = inspect.getdoc(cls) or ""
    
    # Check if any fields are required (this is a simplification)
    # In practice, you might need more sophisticated required field detection
    for field_name, field_type in type_hints.items():
        is_optional = get_origin(field_type) is Union and type(None) in get_args(field_type)
        if not is_optional:
            required.append(field_name)
        
        field_schema = get_json_schema_type(field_type)
        properties[field_name] = field_schema
    
    schema = {
        "type": "object",
        "properties": properties
    }
    
    if required:
        schema["required"] = required
        
    if class_doc:
        schema["description"] = class_doc
        
    return schema

def create_tool_schema(params_type: Type) -> dict:
    """Create a tool schema from a TypedDict."""
    return typed_dict_to_json_schema(params_type)