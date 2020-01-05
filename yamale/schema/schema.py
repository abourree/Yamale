import sys
from .datapath import DataPath
from .. import syntax, util
from .. import validators as val
import dpath.util

# Fix Python 2.x.
PY2 = sys.version_info[0] == 2


class Schema(object):
    """
    Makes a Schema object from a schema dict.
    Still acts like a dict.
    """
    def __init__(self, schema_dict, name='', validators=None, includes=None):
        self.validators = validators or val.DefaultValidators
        self.dict = schema_dict
        self.name = name
        self._schema = self._process_schema(DataPath(),
                                            schema_dict,
                                            self.validators)
        # if this schema is included it shares the includes with the top level
        # schema
        self.includes = {} if includes is None else includes

    def add_include(self, type_dict):
        for include_name, custom_type in type_dict.items():
            t = Schema(custom_type, name=include_name,
                       validators=self.validators, includes=self.includes)
            self.includes[include_name] = t

    def _process_schema(self, path, schema_data, validators):
        """
        Go through a schema and construct validators.
        """
        if util.is_map(schema_data) or util.is_list(schema_data):
            for key, data in util.get_iter(schema_data):
                schema_data[key] = self._process_schema(path + DataPath(key),
                                                        data,
                                                        validators)
        else:
            schema_data = self._parse_schema_item(path,
                                                  schema_data,
                                                  validators)
        return schema_data

    def _parse_schema_item(self, path, expression, validators):
        try:
            return syntax.parse(expression, validators)
        except SyntaxError as e:
            # Tack on some more context and rethrow.
            error = str(e) + ' at node \'%s\'' % str(path)
            raise SyntaxError(error)

    def validate(self, data, data_name, strict):
        path = DataPath()
        self._root_data = data
        errors = self._validate(self._schema, data, path, strict)

        if errors:
            header = '\nError validating data %s with schema %s' % (data_name,
                                                                    self.name)
            error_str = header + '\n\t' + '\n\t'.join(errors)
            if PY2:
                error_str = error_str.encode('utf-8')
            raise ValueError(error_str)

    def _validate_item(self, validator, data, path, strict, key):
        """
        Fetch item from data at the postion key and validate with validator.

        Returns an array of errors.
        """
        errors = []
        path = path + DataPath(key)
        try:  # Pull value out of data. Data can be a map or a list/sequence
            data_item = data[key]
        except (KeyError, IndexError):  # Oops, that field didn't exist.
            if not isinstance(validator, val.IncludeIf):
                # Optional? Who cares.
                if isinstance(validator, val.Validator) and validator.is_optional:
                    return errors
                # SHUT DOWN EVERTYHING
                errors.append('%s: Required field missing' % path)
                return errors
            data_item = None

        return self._validate(validator, data_item, path, strict)

    def _validate(self, validator, data, path, strict):
        """
        Validate data with validator.
        Special handling of non-primitive validators.

        Returns an array of errors.
        """

        if util.is_list(validator) or util.is_map(validator):
            return self._validate_static_map_list(validator,
                                                  data,
                                                  path,
                                                  strict)

        errors = []
        # Optional field with optional value? Who cares.
        if (data is None and
                validator.is_optional and
                validator.can_be_none and
                not isinstance(validator, val.IncludeIf)):
            return errors

        errors += self._validate_primitive(validator, data, path)

        if errors:
            return errors

        if isinstance(validator, val.Include):
            errors += self._validate_include(validator, data, path, strict)

        if isinstance(validator, val.IncludeIf):
            errors += self._validate_include_if(validator, data, path, strict)

        elif isinstance(validator, (val.Map, val.List)):
            errors += self._validate_map_list(validator, data, path, strict)

        elif isinstance(validator, val.Any):
            errors += self._validate_any(validator, data, path, strict)

        return errors

    def _validate_static_map_list(self, validator, data, path, strict):
        if util.is_map(validator) and not util.is_map(data):
            if data is None:
                return ["%s: Required field missing" % path]
            return ["%s : '%s' is not a map" % (path, data)]

        if util.is_list(validator) and not util.is_list(data):
            return ["%s : '%s' is not a list" % (path, data)]

        errors = []

        if strict:
            data_keys = set(util.get_keys(data))
            validator_keys = set(util.get_keys(validator))
            for key in data_keys - validator_keys:
                error_path = path + DataPath(key)
                errors += ['%s: Unexpected element' % error_path]

        for key, sub_validator in util.get_iter(validator):
            errors += self._validate_item(sub_validator,
                                          data,
                                          path,
                                          strict,
                                          key)
        return errors

    def _validate_map_list(self, validator, data, path, strict):
        errors = []

        if not validator.validators:
            return errors  # No validators, user just wanted a map.

        for key in util.get_keys(data):
            sub_errors = []
            for v in validator.validators:
                err = self._validate_item(v, data, path, strict, key)
                if err:
                    sub_errors.append(err)

            if len(sub_errors) == len(validator.validators):
                # All validators failed, add to errors
                for err in sub_errors:
                    errors += err

        return errors

    def _validate_include(self, validator, data, path, strict):
        include_schema = self.includes.get(validator.include_name)
        if not include_schema:
            return [('Include \'%s\' has not been defined.'
                     % validator.include_name)]
        strict = strict if validator.strict is None else validator.strict
        return include_schema._validate(include_schema._schema,
                                        data,
                                        path,
                                        strict)

    def _validate_include_if(self, validator, data, path, strict):
        strict = strict if validator.strict is None else validator.strict
        if_path = DataPath(validator.if_path.split('/'))
        try:
            if_data = dpath.util.get(self._root_data, validator.if_path)
        except KeyError:
            if strict:
                return [('path \'%s\' does not exist.' % validator.if_path)]
            if_data = {}
        if_schema = self.includes.get(validator.if_include_test)
        if not if_schema:
            return [('Include \'%s\' has not been defined.'
                     % validator.if_include_test)]
        # test if condition succeed
        errors = if_schema._validate(if_schema._schema, if_data, if_path, strict)
        if errors:
            # condition failed
            if validator.else_include is None:
                if strict and (not (data is None)):
                    return ['%s: Unexpected element' % path]
                return []
            include_name = validator.else_include
        else:
            include_name = validator.then_include
        include_schema = self.includes.get(include_name)
        if not include_schema:
            return [('Include \'%s\' has not been defined.'
                     % include_name)]
        return include_schema._validate(include_schema._schema,
                                        data,
                                        path,
                                        strict)

    def _validate_any(self, validator, data, path, strict):
        errors = []

        if not validator.validators:
            errors.append('No validators specified for "any".')
            return errors

        sub_errors = []
        for v in validator.validators:
            err = self._validate(v, data, path, strict)
            if err:
                sub_errors.append(err)

        if len(sub_errors) == len(validator.validators):
            # All validators failed, add to errors
            for err in sub_errors:
                errors += err

        return errors

    def _validate_primitive(self, validator, data, path):
        errors = validator.validate(data)

        for i, error in enumerate(errors):
            errors[i] = ('%s: ' % path) + error

        return errors
