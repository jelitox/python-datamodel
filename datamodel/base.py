from __future__ import annotations
from typing import Any, Optional
import inspect
import logging
import types
# Dataclass
from dataclasses import (
    _FIELD,
    _MISSING_TYPE,
    MISSING,
    dataclass,
    is_dataclass,
    make_dataclass
)
from functools import partial
from enum import EnumMeta
from uuid import UUID
from orjson import OPT_INDENT_2
from datamodel.converters import parse_type, slugify_camelcase
from datamodel.fields import Field
from datamodel.types import JSON_TYPES, Text
from datamodel.validation import validator
from .exceptions import ValidationError
from .parsers.encoders import json_encoder
from .abstract import ModelMeta, Meta
from .models import ModelMixin


def _get_type_info(_type, name, title):
    if _type.__module__ == 'typing':
        if inspect.isfunction(_type):
            if hasattr(_type, '__supertype__'):
                return _type.__supertype__
            raise ValueError(
                f"You're using bare Functions to type hint on {name} for: {title}"
            )
        if _type._name == 'List':
            return 'array'
        if _type._name == 'Dict':
            return 'object'
        try:
            return _type.__args__[0].__name__
        except (AttributeError, ValueError):
            return 'string'
    elif hasattr(_type, '__supertype__'):
        if type(_type) == type(Text):
            return 'text'
        if isinstance(_type.__supertype__, (str, int)):
            return 'string' if isinstance(_type.__supertype__, str) else 'integer'
    return JSON_TYPES.get(_type, 'string')


def _get_ref_info(_type, field):
    if isinstance(_type, EnumMeta):
        return {
            "type": "array",
            "enum_type": {
                "type": "string",
                "enum": list(map(lambda c: c.value, _type))
            }
        }
    elif isinstance(_type, ModelMeta):
        return {
            "type": "object",
            "enum_type": None,
            "schema": _type.schema(as_dict=True),
            "ref": field.metadata.get('fk').split("|") if 'fk' in field.metadata else _type.schema(as_dict=True)['$id']
        }
    return None


class BaseModel(ModelMixin, metaclass=ModelMeta):
    """
    BaseModel.
    Base Model for all DataModels.
    """
    Meta = Meta

    def is_valid(self) -> bool:
        """is_valid.

        returns True when current Model is valid under datatype validations.
        Returns:
            bool: True if current model is valid.
        """
        return bool(self.__valid__)

    @classmethod
    def add_field(cls, name: str, value: Any = None) -> None:
        if cls.Meta.strict is True:
            raise TypeError(
                f'Cannot create a new field {name} on a Strict Model.'
            )
        if name != '__errors__':
            f = Field(required=False, default=value)
            f.name = name
            f.type = type(value)
            f._field_type = _FIELD
            cls.__columns__[name] = f
            cls.__dataclass_fields__[name] = f

    def create_field(self, name: str, value: Any) -> None:
        """create_field.
        create a new Field on Model (when strict is False).
        Args:
            name (str): name of the field
            value (Any): value to be assigned.
        Raises:
            TypeError: when try to create a new field on an Strict Model.
        """
        if self.Meta.strict is True:
            raise TypeError(
                f'Cannot create a new field {name} on a Strict Model.'
            )
        if name != '__errors__':
            f = Field(required=False, default=value)
            f.name = name
            f.type = type(value)
            f._field_type = _FIELD
            self.__columns__[name] = f
            self.__dataclass_fields__[name] = f
            setattr(self, name, value)

    def set(self, name: str, value: Any) -> None:
        """set.
        Alias for Create Field.
        Args:
            name (str): name of the field
            value (Any): value to be assigned.
        """
        if name not in self.__columns__:
            if name != '__errors__' and self.Meta.strict is False:
                self.create_field(name, value)
        else:
            setattr(self, name, value)

    def _calculate_value(self, key: str, value: Any, f: Field) -> None:
        encoder = f.metadata.get('encoder')

        # Check if the value is callable and the field has a default
        if hasattr(f, 'default') and self.is_callable(value):
            return

        # Factory Value handling
        if is_dataclass(f.type):  # f.type is a dataclass
            if hasattr(self.Meta, 'no_nesting'):
                new_val = value
            elif value is None:
                new_val = None
            elif isinstance(value, (dict, list)):
                new_val = f.type(**value) if isinstance(value, dict) else f.type(*value)
            else:
                new_val = value if isinstance(value, (int, str, UUID)) else f.type(value)
            setattr(self, key, new_val)
        else:
            # Handle typing extensions
            if f.type.__module__ == 'typing':
                new_val = parse_type(f.type, value, encoder)
                setattr(self, key, new_val)
                return

            # Handle list of dataclasses
            if isinstance(value, list):
                try:
                    sub_type = f.type.__args__[0]
                    if is_dataclass(sub_type):
                        items = [sub_type(**item) if isinstance(item, dict) else item for item in value]
                        setattr(self, key, items)
                        return
                except AttributeError:
                    setattr(self, key, value)

            # Handle empty values with defaults
            if self.is_empty(value):
                is_missing = isinstance(f.default, _MISSING_TYPE)
                setattr(self, key, f.default_factory if is_missing else f.default)
            else:
                try:
                    new_val = parse_type(f.type, value, encoder)
                    setattr(self, key, new_val)
                except (TypeError, ValueError) as ex:
                    raise ValueError(f"Wrong Type for {key}: {f.type}, error: {ex}") from ex

    def __post_init__(self) -> None:
        """
         Post init method.
        Fill fields with function-factory or calling validations
        """
        # checking if an attribute is already a dataclass:
        errors = {}
        for _, f in self.__columns__.items():
            value = getattr(self, f.name)
            name = f.name
            self._calculate_value(name, value, f)
            try:
                value = getattr(self, f.name)
                error = self._validation(name, value, f)
                if error:
                    errors[name] = error
            except RuntimeError as err:
                logging.exception(err)
        if errors:
            if self.Meta.strict is True:
                raise ValidationError(
                    f"""{self.modelName}: There are errors in your Model. Hint: please check the "payload" attribute in the exception.""",
                    payload=errors
                )
            self.__errors__ = errors
            object.__setattr__(self, "__valid__", False)
        else:
            object.__setattr__(self, "__valid__", True)

    def is_callable(self, value) -> bool:
        """is_callable.
            Check if current value is a callabled and not a _MISSING_TYPE or a partial function.
        Args:
            value (Any): value from field.

        Returns:
            bool: True if value is a real callable.
        """
        is_missing = (value == _MISSING_TYPE)
        is_function = isinstance(value, (types.BuiltinFunctionType, types.FunctionType, partial))
        return callable(value) if not is_missing and is_function else False

    def is_empty(self, value) -> bool:
        if isinstance(value, _MISSING_TYPE):
            return True
        elif (value == _MISSING_TYPE):
            return True
        elif value is None:
            return True
        elif str(value) == '':
            return True
        return False

    def _validation(self, name: str, value: Any, f: Field) -> Optional[Any]:
        """
        _validation.
        TODO: cover validations as length, not_null, required, max, min, etc
        """
        annotated_type = f.type
        val_type = type(value)
        # Fix values of Data based on Default factory
        if hasattr(f, 'default') and self.is_callable(value):
            try:
                if value.__module__ != 'typing':
                    try:
                        new_val = value()
                    except TypeError:
                        try:
                            new_val = f.default()
                        except TypeError:
                            new_val = None
                    setattr(self, name, new_val)
            except TypeError as ex:
                logging.warning(
                    f'{self.modelName}: Missing *Column* {f} with name {name}: {ex}'
                )
                setattr(self, name, None)
        if hasattr(f, 'default') and self.is_callable(f.default) and value is None:
            # set value based on default, if value is None
            try:
                new_val = f.default()
            except (AttributeError, RuntimeError):
                new_val = None
            setattr(self, name, new_val)
            value = new_val
        # first: check primary and required:
        if val_type == type or value == annotated_type or self.is_empty(value):
            try:
                if f.metadata['primary'] is True:
                    if 'db_default' in f.metadata:
                        pass
                    else:
                        raise ValueError(
                            f"::{self.modelName}:: Missing Primary Key *{name}*"
                        )
            except KeyError:
                pass
            try:
                if f.metadata["required"] is True and self.Meta.strict is True:
                    # if this has db_default:
                    if 'db_default' in f.metadata:
                        return
                    raise ValueError(
                        f"::{self.modelName}:: Missing Required Field *{name}*"
                    )
            except KeyError:
                pass
            try:
                if f.metadata["nullable"] is False and self.Meta.strict is True:
                    raise ValueError(
                        f"::{self.modelName}:: Cannot null *{name}*"
                    )
            except KeyError:
                pass
        else:
            # capturing other errors from validator:
            return validator(f, name, value, annotated_type)

    def get_errors(self):
        return self.__errors__

    @classmethod
    def make_model(cls, name: str, schema: str = "public", fields: list = None):
        parent = inspect.getmro(cls)
        obj = make_dataclass(name, fields, bases=(parent[0],))
        m = Meta()
        m.name = name
        m.schema = schema
        m.app_label = schema
        obj.Meta = m
        return obj

    @classmethod
    def from_json(cls, obj: str, **kwargs) -> dataclass:
        try:
            decoder = cls.__encoder__(**kwargs)
            decoded = decoder.loads(obj)
            return cls(**decoded)
        except ValueError as e:
            raise RuntimeError(
                "DataModel: Invalid string (JSON) data for decoding: {e}"
            ) from e

    @classmethod
    def from_dict(cls, obj: dict) -> dataclass:
        try:
            return cls(**obj)
        except ValueError as e:
            raise RuntimeError(
                "DataModel: Invalid Dictionary data for decoding: {e}"
            ) from e

    @classmethod
    def model(cls, dialect: str = "json", **kwargs) -> Any:
        """model.

        Return the json-version of current Model.
        Returns:
            str: string (json) version of model.
        """
        result = None
        clsname = cls.__name__
        schema = cls.Meta.schema
        table = cls.Meta.name if cls.Meta.name else clsname.lower()
        columns = cls.columns(cls).items()
        if dialect == 'json':
            cols = {}
            for _, field in columns:
                key = field.name
                _type = field.type
                if _type.__module__ == 'typing':
                    # TODO: discover real value of typing
                    if _type._name == 'List':
                        t = 'array'
                    elif _type._name == 'Dict':
                        t = 'object'
                    else:
                        try:
                            t = _type.__args__[0]
                            t = t.__name__
                        except (AttributeError, ValueError):
                            t = 'object'
                else:
                    try:
                        t = JSON_TYPES[_type]
                    except KeyError:
                        t = 'object'
                cols[key] = {"name": key, "type": t}
            doc = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": f"/schemas/{table}",
                "name": clsname,
                "description": cls.__doc__.strip("\n").strip(),
                "additionalProperties": False,
                "table": table,
                "schema": schema,
                "type": "object",
                "properties": cols,
            }
            encoder = cls.__encoder__(**kwargs)
            result = encoder.dumps(doc, option=OPT_INDENT_2)
        return result

    @classmethod
    def sample(cls) -> dict:
        """sample.

        Get a dict (JSON) sample of this datamodel, based on default values.

        Returns:
            dict: _description_
        """
        columns = cls.get_columns().items()
        _fields = {}
        required = []
        for name, f in columns:
            if f.repr is False:
                continue
            _fields[name] = f.default
            try:
                if f.metadata["required"] is True:
                    required.append(name)
            except KeyError:
                pass
        return {
            "properties": _fields,
            "required": required
        }

    @classmethod
    def schema(cls, as_dict=False):
        title = getattr(cls.Meta, 'title', cls.__name__)
        try:
            title = slugify_camelcase(title)
        except Exception:
            pass

        schema = cls.Meta.schema
        table = cls.Meta.name.lower() if cls.Meta.name else title.lower()
        columns = cls.get_columns().items()
        description = cls.Meta.description or cls.__doc__.strip("\n").strip()
        fields = {}
        required = []
        defs = {}

        for name, field in columns:
            _type = field.type
            type_info = _get_type_info(_type, name, title)
            ref_info = _get_ref_info(_type, field)
            minimum = field.metadata.get('min', None)
            maximum = field.metadata.get('max', None)
            secret = field.metadata.get('secret', None)
            label = field.metadata.get('label', None)

            if field.metadata.get('required', False):
                required.append(name)

            fields[name] = {
                "type": type_info,
                "nullable": field.metadata.get('nullable', False),
                "label": label,
                "attrs": {
                    "placeholder": field.metadata.get('description', None),
                    "format": field.metadata.get('format', None),
                },
                "readOnly": field.metadata.get('readonly', False),
                "writeOnly": False
            }

            if 'pattern' in field.metadata:
                fields[name]["attrs"]["pattern"] = field.metadata['pattern']

            ref = ref_info.get('ref') if ref_info else None
            if ref:
                fields[name]["$ref"] = ref

            if field.repr is False:
                fields[name]["attrs"]["visible"] = False

            fields[name]['default'] = field.default
            if secret is not None:
                fields[name]['secret'] = secret

            if type_info == 'string':
                if minimum:
                    fields[name]['minLength'] = minimum
                if maximum:
                    fields[name]['maxLength'] = maximum
            else:
                if minimum:
                    fields[name]['minimum'] = minimum
                if maximum:
                    fields[name]['maximum'] = maximum

        base_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"/schemas/{table}",
            "additionalProperties": cls.Meta.strict,
            "title": title,
            "description": description,
            "type": "object",
            "table": table,
            "schema": schema,
            "properties": fields,
            "required": required
        }

        if defs:
            base_schema["$defs"] = defs

        if as_dict:
            return base_schema
        else:
            return json_encoder(base_schema)
