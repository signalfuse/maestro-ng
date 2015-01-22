# Copyright (C) 2013-2014 SignalFuse, Inc.
#
# Docker container orchestration utility.

from __future__ import print_function

import functools
import inspect
import requests.exceptions
import sys

from . import audit
from . import entities
from . import exceptions
from . import plays
from . import shipproviders
from . import termoutput

AVAILABLE_MAESTRO_COMMANDS = ['status', 'start', 'stop', 'restart',
                              'pull', 'clean', 'logs', 'deptree']


class Conductor:
    """The Maestro; the Conductor.

    The conductor is in charge of parsing and analyzing the environment
    description and carrying out the orchestration plays to act on the services
    and containers described in the environment.
    """

    def __init__(self, config):
        self._config = config

        self.ships = (shipproviders.ShipsProviderFactory
                      .from_config(config).ships())

        # Register defined private Docker registries authentications
        self.registries = self._config.get('registries') or {}
        for name, registry in self.registries.items():
            if 'username' not in registry or 'password' not in registry:
                raise exceptions.OrchestrationException(
                    'Incomplete registry auth data for {}!'.format(name))

        # Build all the entities.
        self.services = {}
        self.containers = {}

        for kind, service in self._config.get('services', {}).items():
            self.services[kind] = entities.Service(kind, service['image'],
                                                   self.schema,
                                                   service.get('env', {}))

            for name, instance in service['instances'].items():
                self.containers[name] = \
                    entities.Container(name,
                                       self.ships[instance['ship']],
                                       self.services[kind],
                                       instance,
                                       self.schema,
                                       self._config['name'])

        # Resolve dependencies between services.
        for kind, service in self._config.get('services', {}).items():
            for dependency in service.get('requires', []):
                self.services[kind].add_dependency(self.services[dependency])
                self.services[dependency].add_dependent(self.services[kind])
            for wants_info in service.get('wants_info', []):
                self.services[kind].add_wants_info(self.services[wants_info])

        # Provide link environment variables to each container of each service
        # that requires it or wants it.
        for service in self.services.values():
            for container in service.containers:
                # Containers always know about their peers in the same service.
                container.env.update(service.get_link_variables(True))
                # Containers also get links from the service's dependencies.
                for dependency in service.requires.union(service.wants_info):
                    container.env.update(dependency.get_link_variables())

        # Instantiate audit bindings
        self.auditor = audit.AuditorFactory.from_config(
            self._config.get('audit', []))

    @property
    def schema(self):
        return self._config.get('__maestro', {'schema': 1})

    def _order_dependencies(self, pending=[], ordered=[], forward=True):
        """Order the given set of containers into an order respecting the
        service dependencies in the given direction.

        The list of containers to order should be passed in the pending
        parameter. The ordered list will be returned by the function (the
        ordered parameter is for internal recursion use only).

        The direction of the dependencies controls whether the ordering should
        be constructed for startup (dependencies first) or shutdown (dependents
        first).
        """
        wait = []
        for container in pending:
            deps = self._gather_dependencies([container], forward)
            if deps and not deps.issubset(set(ordered + [container])):
                wait.append(container)
            else:
                ordered.append(container)

        # If wait and pending are not empty and have the same length, it means
        # we were not able to order any container from the pending list (they
        # all went to the wait list). This means the dependency tree cannot be
        # resolved and an error should be raised.
        if wait and pending and len(wait) == len(pending):
            raise exceptions.DependencyException(
                'Cannot resolve dependencies for containers {}!'.format(
                    map(lambda x: x.name, wait)))

        # As long as 'wait' has elements, keep recursing to resolve
        # dependencies. Otherwise, returned the ordered list, which should now
        # be final.
        return wait and self._order_dependencies(wait, ordered, forward) \
            or ordered

    def _gather_dependencies(self, containers, forward=True):
        """Transitively gather all containers from the dependencies or
        dependents (depending on the value of the forward parameter) services
        that the services the given containers are members of."""
        result = set(containers or self.containers.values())
        for container in result:
            deps = container.service.requires if forward \
                else container.service.needed_for
            deps = functools.reduce(lambda x, y: x.union(y),
                                    [s.containers for s in deps], set([]))
            result = result.union(deps)
        return result

    def _to_containers(self, things):
        """Transform a list of "things", container names or service names, to
        an expended list of Container objects."""
        def parse_thing(s):
            if s in self.containers:
                return [self.containers[s]]
            elif s in self.services:
                return self.services[s].containers
            raise exceptions.OrchestrationException(
                '{} is neither a service nor a container!'.format(s))
        result = []
        for thing in things:
            result += parse_thing(thing)
        return sorted(set(result))

    def _to_services(self, things):
        """Transform a list of "things", container names or service names, to a
        list of Service objects with no duplicates."""
        def parse_thing(s):
            if s in self.containers:
                return self.containers[s].service
            if s in self.services:
                return self.services[s]
            raise exceptions.OrchestrationException(
                '{} is neither a service nor a container!'.format(s))
        return sorted(set(map(parse_thing, things)))

    def _ordered_containers(self, things, forward=True):
        """Return the ordered list of containers from the list of names passed
        to it (either container names or service names).

        Args:
            things (list<string>):
            forward (boolean): controls the direction of the dependency tree.
        """
        return self._order_dependencies(
            sorted(self._gather_dependencies(self._to_containers(things),
                                             forward)),
            forward=forward)

    def complete(self, tokens, **kwargs):
        """Completion handler; designed to work for shell auto-completion.

        Takens in a list of tokens (that may need to be split up into
        individual tokens) and assumes it is the start of a command the user
        tries to type. The method then returns a list of words that would
        complete the last token the user is currently trying to complete.
        """
        args = []
        for token in tokens:
            args += [x for x in token.split(' ') if not x.startswith('-')]

        choices = AVAILABLE_MAESTRO_COMMANDS if len(args) <= 2 \
            else self.services.keys() + self.containers.keys()
        prefix = ''

        if len(args) == 2:
            prefix = args[1]
            if prefix in choices:
                args.append('')

        if len(args) > 2:
            prefix = args.pop()

        print(' '.join(filter(lambda x: x.startswith(prefix), set(choices))))

    def _audit_play(self, play):
        """Run an orchestration play, wrapping its execution in audit trail
        calls.

        If auditors have been configured, they will be triggered before the
        orchestration play starts, and when it ends in either success or error
        situations.

        Args:
            play (plays.BaseOrchestrationPlay): An orchestration play instance
                ready to run.
        """
        action = inspect.stack()[1][3]
        things = [c.name for c in play.containers]
        self.auditor.action(things, action)
        try:
            play.run()
            self.auditor.success(things, action)
        except requests.exceptions.Timeout:
            e_type, e, tb = sys.exc_info()
            try:
                msg = e.args[0][1]
            except:
                # varies with the timeout exception
                msg = e.args[0][0]
            self.auditor.error(things, action, message=msg)
            raise e_type, e, tb
        except Exception as e:
            self.auditor.error(things, action, message=e)
            raise

    def status(self, things, full=False, with_dependencies=False,
               concurrency=None, **kwargs):
        """Display the status of the given services and containers, but only
        looking at the container's state, not the application availability.

        Args:
            things (set<string>): The list of things to start.
            full (boolean): Whether to display the full detailed status, with
                port states, for each container.
            with_dependencies (boolean): Whether to act on only the specified
                things, or their dependencies as well.
            concurrency (int): The maximum number of instances that can be
                acted on at the same time.
        """
        containers = self._ordered_containers(things) \
            if with_dependencies else self._to_containers(things)

        if full:
            plays.FullStatus(containers).run()
        else:
            plays.Status(containers, concurrency).run()

    def pull(self, things, with_dependencies=False,
             ignore_dependencies=False, concurrency=None, **kwargs):
        """Force an image pull to refresh images for the given services and
        containers. Dependencies of the requested containers and services are
        pulled first.

        Args:
            things (set<string>): The list of things to pull.
            with_dependencies (boolean): Whether to act on only the specified
                things, or their dependencies as well.
            ignore_dependencies (boolean): Whether dependency order should be
                respected.
            concurrency (int): The maximum number of instances that can be
                acted on at the same time.
        """
        containers = self._ordered_containers(things) \
            if with_dependencies else self._to_containers(things)

        self._audit_play(
            plays.Pull(containers, self.registries,
                       ignore_dependencies, concurrency))

    def start(self, things, refresh_images=False, with_dependencies=False,
              ignore_dependencies=False, concurrency=None, reuse=False,
              **kwargs):
        """Start the given container(s) and services(s). Dependencies of the
        requested containers and services are started first.

        Args:
            things (set<string>): The list of things to start.
            refresh_images (boolean): Whether to force an image pull for each
                container or not.
            with_dependencies (boolean): Whether to act on only the specified
                things, or their dependencies as well.
            ignore_dependencies (boolean): Whether dependency order should be
                respected.
            concurrency (int): The maximum number of instances that can be
                acted on at the same time.
            reuse (boolean): Restart the existing container instead of
                destroying/recreating a new one.
        """
        containers = self._ordered_containers(things) \
            if with_dependencies else self._to_containers(things)

        self._audit_play(
            plays.Start(containers, self.registries, refresh_images,
                        ignore_dependencies, concurrency, reuse))

    def restart(self, things, refresh_images=False, with_dependencies=False,
                ignore_dependencies=False, concurrency=None, step_delay=0,
                stop_start_delay=0, reuse=False, only_if_changed=False,
                **kwargs):
        """Restart the given container(s) and services(s). Dependencies of the
        requested containers and services are started first.

        Args:
            things (set<string>): The list of things to start.
            refresh_images (boolean): Whether to force an image pull for each
                container or not before starting it.
            with_dependencies (boolean): Whether to act on only the specified
                things, or their dependencies as well.
            ignore_dependencies (boolean): Whether dependency order should be
                respected.
            concurrency (int): The maximum number of instances that can be
                acted on at the same time.
            step_delay (int): Time, in seconds, to wait before restarting the
                next container.
            stop_start_delay (int): Time, in seconds, to wait between stopping
                and starting each container.
            reuse (boolean): Restart the existing container instead of
                destroying/recreating a new one.
            only_if_changed (boolean): Only restart the container if its
                underlying image was updated.
        """
        containers = self._ordered_containers(things, False) \
            if with_dependencies else self._to_containers(things)
        self._audit_play(
            plays.Restart(containers, self.registries, refresh_images,
                          ignore_dependencies, concurrency, step_delay,
                          stop_start_delay, reuse, only_if_changed))

    def stop(self, things, with_dependencies=False, ignore_dependencies=False,
             concurrency=None, **kwargs):
        """Stop the given container(s) and service(s).

        This one is a bit more tricky because we don't want to look at the
        dependencies of the containers and services we want to stop, but at
        which services depend on the containers and services we want to stop.
        Unless of course the only parameter is set to True.

        Args:
            things (set<string>): The list of things to stop.
            with_dependencies (boolean): Whether to act on only the specified
                things, or their dependencies as well.
            ignore_dependencies (boolean): Whether dependency order should be
                respected.
            concurrency (int): The maximum number of instances that can be
                acted on at the same time.
        """
        containers = self._ordered_containers(things, False) \
            if with_dependencies else self._to_containers(things)
        self._audit_play(
            plays.Stop(containers, ignore_dependencies, concurrency))

    def clean(self, things, with_dependencies=False, concurrency=None,
              **kwargs):
        """Remove the given stopped Docker containers.

        Args:
            things (set<string>): The list of things to stop.
            with_dependencies (boolean): Whether to act on only the specified
                things, or their dependencies as well.
            concurrency (int): The maximum number of instances that can be
                acted on at the same time.
        """
        containers = self._ordered_containers(things) \
            if with_dependencies else self._to_containers(things)
        self._audit_play(plays.Clean(containers, concurrency))

    def logs(self, things, follow, n, **kwargs):
        """Display the logs of the given container.

        Args:
            things (set<string>): A one-element set containing the name of the
                container to show logs from.
            follow (boolean): Whether to follow (tail) the log.
            n (int): Number of lines to display (when not following), from the
                bottom of the log.
        """
        containers = self._to_containers(things)
        if len(containers) != 1:
            raise exceptions.ParameterException(
                'Logs can only be shown for a single container!')

        container = containers[0]

        o = termoutput.OutputFormatter()
        o.pending('Inspecting container status...')
        status = container.status()
        if not status:
            return

        try:
            stream = follow and status['State']['Running']
            if stream:
                o.pending(
                    'Now streaming logs for {}. New output will appear below.'
                    .format(container.name))
                logs = container.ship.backend.attach(container.id, stream=True)
            else:
                o.pending(
                    'Requesting logs for {}. This may take a while...'
                    .format(container.name))
                logs = container.ship.backend.logs(container.id).split('\n')
                logs = logs[-int(n or len(logs)):]

            o.pending('\033[2K')
            for line in logs:
                print(line.rstrip())
        except:
            pass

    def deptree(self, things, recursive, **kwargs):
        """Display the dependency tree of the given services."""

        def treehelper(service, indent, shown):
            deps = sorted(service.dependencies) if recursive \
                else sorted(service.dependencies.difference(shown))
            shown.update(deps)
            for i, dep in enumerate(deps, 1):
                last = i == len(deps)
                print('{}{} {}'.format(indent,
                                       last and '\\-' or '+-',
                                       dep.name))
                treehelper(dep, indent + (last and '  ' or '|  '), shown)

        services = self._to_services(things or sorted(self.services))
        for i, service in enumerate(services, 1):
            print(service.name)
            treehelper(service, ' ', set([]))
            if i < len(services):
                print()
