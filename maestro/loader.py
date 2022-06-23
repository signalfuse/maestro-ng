# Copyright (C) 2015-2018 SignalFx, Inc.
#
# Docker container orchestration utility.

import jinja2
import os
import sys
import yaml
from yaml.constructor import ConstructorError, SafeConstructor

from . import exceptions


class MaestroYamlConstructor(SafeConstructor):
    """A PyYAML object constructor that errors on duplicate keys in YAML
    mappings. Because for some reason PyYAML doesn't do that since 3.x."""

    def construct_mapping(self, node, deep=False):
        if not isinstance(node, yaml.nodes.MappingNode):
            raise ConstructorError(
                None, None,
                "expected a mapping node, but found %s" % node.id,
                node.start_mark)
        keys = set([])
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in keys:
                raise ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    "found duplicate key (%s)" % key, key_node.start_mark)
            keys.add(key)
        return SafeConstructor.construct_mapping(self, node, deep)


try:
    # If possible, load the faster, C-based YAML Parser from _yaml.
    import _yaml

    class MaestroYamlLoader(_yaml.CParser, MaestroYamlConstructor,
                            yaml.resolver.Resolver):
        """A custom YAML Loader that uses the custom MaestroYamlConstructor."""

        def __init__(self, stream):
            _yaml.CParser.__init__(self, stream)
            MaestroYamlConstructor.__init__(self)
            yaml.resolver.Resolver.__init__(self)
except ImportError:
    # Fallback to the pure-Python implementation otherise.
    class MaestroYamlLoader(yaml.reader.Reader, yaml.scanner.Scanner,
                            yaml.parser.Parser, yaml.composer.Composer,
                            MaestroYamlConstructor, yaml.resolver.Resolver):
        """A custom YAML Loader that uses the custom MaestroYamlConstructor."""

        def __init__(self, stream):
            yaml.reader.Reader.__init__(self, stream)
            yaml.scanner.Scanner.__init__(self)
            yaml.parser.Parser.__init__(self)
            yaml.composer.Composer.__init__(self)
            MaestroYamlConstructor.__init__(self)
            yaml.resolver.Resolver.__init__(self)


def load(filename, filters=None, functions=None):
    """Load a config from the given file.

    Args:
        filename (string): Path to the YAML environment description
            configuration file to load. Use '-' for stdin.

    Returns:
        A python data structure corresponding to the YAML configuration.
    """
    base_dir = os.path.dirname(filename) if filename != '-' else os.getcwd()
    extensions = []
    if jinja2.__version__.split(".")[0] == "2":
        extensions = ['jinja2.ext.with_']
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(base_dir),
        auto_reload=False,
        extensions=extensions)
    if filters:
        env.filters.update(**filters)
    if functions:
        env.globals.update(**functions)
    try:
        if filename == '-':
            template = env.from_string(sys.stdin.read())
        else:
            template = env.get_template(os.path.basename(filename))
    except jinja2.exceptions.TemplateNotFound:
        raise exceptions.MaestroException(
            'Environment description file {} not found!'.format(filename))
    except Exception as e:
        raise exceptions.MaestroException(
            'Error reading environment description file {}: {}!'
            .format(filename, e))

    config = yaml.load(template.render(env=os.environ),
                       Loader=MaestroYamlLoader)
    if '__maestro' not in config:
        config['__maestro'] = {}
    config['__maestro']['base_dir'] = base_dir
    return config
