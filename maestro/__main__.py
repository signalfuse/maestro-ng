#!/usr/bin/env python

# Copyright (C) 2013-2014 SignalFuse, Inc.
# Copyright (C) 2015 SignalFx, Inc.
#
# Docker container orchestration utility.

from __future__ import print_function

import argparse
import jinja2
import logging
import os
import sys
import traceback
import yaml
try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader

from . import exceptions, maestro
from . import name, version

DEFAULT_MAESTRO_FILE = 'maestro.yaml'
DEFAULT_MAESTRO_COMMAND = 'status'


def load_config_from_file(filename):
    """Load a config from the given file.

    Args:
        filename (string): Path to the YAML environment description
            configuration file to load. Use '-' for stdin.

    Returns:
        A python data structure corresponding to the YAML configuration.
    """
    if filename == '-':
        template = jinja2.Template(sys.stdin.read())
    else:
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(os.path.dirname(filename)),
            extensions=['jinja2.ext.with_'])
        try:
            template = env.get_template(os.path.basename(filename))
        except jinja2.exceptions.TemplateNotFound:
            raise exceptions.MaestroException(
                'Environment description file {} not found!'.format(filename))
        except:
            raise exceptions.MaestroException(
                'Error reading environment description file {}!'.format(
                    filename))

    return yaml.load(template.render(env=os.environ), Loader=Loader)


def create_parser():
    """Create the Maestro argument parser."""
    parser = argparse.ArgumentParser(prog=name, description=(
        '{} v{}, Docker container orchestrator.'.format(
            name.title(), version)))
    parser.add_argument(
        '-f', '--file', metavar='FILE',
        default=DEFAULT_MAESTRO_FILE,
        help=('read environment description from FILE ' +
              '(use - for stdin, defaults to ./{})'
              .format(DEFAULT_MAESTRO_FILE)))
    parser.add_argument(
        '-v', '--version', action='version',
        version='{}-{}'.format(name, version),
        help='show program version and exit')

    subparsers = parser.add_subparsers(
        dest='command',
        metavar='{{{}}}'.format(','.join(maestro.AVAILABLE_MAESTRO_COMMANDS)))
    # Needed with Python >= 3.3 to force one of the subparsers to be found on
    # the command-line, otherwise the command and none of its arguments are
    # present in the options Namespace.
    subparsers.required = True

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        'things', nargs='*', metavar='thing',
        help='container(s) or service(s) to display')

    concurrent = argparse.ArgumentParser(add_help=False)
    concurrent.add_argument(
        '-c', '--concurrency', metavar='LIMIT', type=int, default=None,
        help='limit how many containers can be acted on at the same time')
    concurrent.add_argument(
        '-d', '--with-dependencies', action='store_true',
        help='include dependencies')
    concurrent.add_argument(
        '-i', '--ignore-dependencies', action='store_true',
        help='ignore dependency order')

    with_refresh = argparse.ArgumentParser(add_help=False)
    refresh_or_reuse_group = with_refresh.add_mutually_exclusive_group()
    refresh_or_reuse_group.add_argument(
        '-r', '--refresh-images', action='store_true',
        help='force refresh of container images from registry')
    refresh_or_reuse_group.add_argument(
        '--reuse', action='store_true',
        help='reuse existing container if it exists')

    # status
    subparser = subparsers.add_parser(
        parents=[common, concurrent],
        name='status',
        description='Display container status',
        help='display container status')
    subparser.add_argument(
        '-F', '--full', action='store_true',
        help='show full status with port state')

    # pull
    subparser = subparsers.add_parser(
        parents=[common, concurrent],
        name='pull',
        description='Pull container images from registry',
        help='pull container images from registry')

    # start
    subparser = subparsers.add_parser(
        parents=[common, concurrent, with_refresh],
        name='start',
        description='Start services and containers',
        help='start services and containers')

    # stop
    subparser = subparsers.add_parser(
        parents=[common, concurrent],
        name='stop',
        description='Stop services and containers',
        help='stop services and containers')

    # restart
    subparser = subparsers.add_parser(
        parents=[common, concurrent, with_refresh],
        name='restart',
        description='Restart services and containers',
        help='restart services and containers')
    subparser.add_argument(
        '--step-delay', type=int, default=0,
        help='delay, in seconds, between each container')
    subparser.add_argument(
        '--stop-start-delay', type=int, default=0,
        help='delay, in seconds, between stopping and starting each container')
    subparser.add_argument(
        '--only-if-changed', action='store_true',
        help='only restart if the container image was updated')

    # clean
    subparser = subparsers.add_parser(
        parents=[common, concurrent],
        name='clean',
        description='Cleanup and remove stopped containers',
        help='remove stopped containers')

    # logs
    subparser = subparsers.add_parser(
        parents=[common],
        name='logs',
        description='Show logs for a container',
        help='show logs from a container')
    subparser.add_argument(
        '-F', '--follow', action='store_true',
        help='follow logs as they are generated')
    subparser.add_argument(
        '-n', metavar='LINES', type=int,
        help='Only show the last LINES lines for logs')

    # deptree
    subparser = subparsers.add_parser(
        parents=[common],
        name='deptree',
        description='Display the service dependency tree',
        help='show the dependency tree')
    subparser.add_argument(
        '-r', '--recursive', action='store_true',
        help='show dependencies recursively (possible duplicates)')

    # complete
    subparser = subparsers.add_parser(
        name='complete',
        description='Auto-complete helper',
        help='shell auto-completion helper')
    subparser.add_argument(
        'tokens', nargs='*',
        help='command tokens')

    return parser


def execute(options, config):
    # Shutup urllib3, wherever it comes from.
    (logging.getLogger('requests.packages.urllib3.connectionpool')
            .setLevel(logging.WARN))
    (logging.getLogger('urllib3.connectionpool')
            .setLevel(logging.WARN))

    try:
        c = maestro.Conductor(config)
        if options.command != 'complete' and not options.things:
            options.things = [s.name for s in c.services.values()
                              if options.command == 'status' or not s.omit]
            options.with_dependencies = options.command == 'deptree' \
                or not options.ignore_dependencies
        getattr(c, options.command)(**vars(options))
        return 0
    except KeyboardInterrupt:
        pass
    except:
        traceback.print_exc()
    return 1


def parse_include_services(options, config):
    """Load services from seperate config files."""
    services = config.get('services', {})

    includes = config.pop('include_services', [])

    for path in includes:
        path = os.path.join(os.path.dirname(options.file), path)
        services.update(load_config_from_file(path))

    return config


def main(args=None, config=None):
    options = create_parser().parse_args(args)
    if config is None:
        config = load_config_from_file(options.file)
        parse_include_services(options, config)
    return execute(options, config)


if __name__ == '__main__':
    sys.exit(main())
