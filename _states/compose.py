"""
Install and manage container compositions with ``podman``.

This module is intended to be used akin ``docker-compose``/
``podman-compose`` to deploy small stacks on a single server
easily and with rootless support (e.g. homelab). It takes care of

* generating podman systemd units from docker-compose.yml files
* managing their state

It exists because writing states that try to account for
several different configurations tend to get ugly fast
(pod vs separate containers, rootful vs rootless, reloading
after configuration change).

Configuration options:

compose.containers_base
    If composition/project is just a name, use this directory as the
    base to autodiscover projects. Defaults to ``/opt/containers``.
    Example: project=gitea -> ``/opt/containers/gitea/docker-compose.yml``

compose.default_to_dirowner
    If user is unspecified, try to guess the user running the project
    by looking up the dir owner. Defaults to True.

compose.default_pod_prefix
    By default, prefix service units for composition pods with this
    string. Defaults to empty (podman-compose still prefixes the
    pods themselves with ``pod_`` currently).

compose.default_container_prefix
    By default, prefix service units for containers with this
    string. Defaults to empty.

Todo:
    * import/export Kubernetes YAML files
"""
import logging
import re
import time
from pathlib import Path

from salt.exceptions import CommandExecutionError, SaltInvocationError
from salt.utils.args import get_function_argspec as _argspec

log = logging.getLogger(__name__)


def _get_valid_args(func, kwargs):
    valid_args = _argspec(func).args

    return {arg: kwargs[arg] for arg in valid_args if arg in kwargs}


def installed(
    name,
    update=True,
    project_name=None,
    create_pod=None,
    pod_args=None,
    pod_args_override_default=False,
    podman_create_args=None,
    remove_orphans=True,
    force_recreate=False,
    build=False,
    build_args=None,
    pull=False,
    user=None,
    ephemeral=True,
    restart_policy=None,
    restart_sec=None,
    stop_timeout=None,
    service_overrides=None,
    pod_wants=True,
    enable=True,
    pod_prefix=None,
    container_prefix=None,
    separator=None,
):
    """
    Make sure a container composition is installed.

    This will create the necessary resources and
    generate systemd units dedicated to managing their lifecycle.

    name
        Some reference about where to find the project definitions.
        Can be an absolute path to the composition definitions (``docker-compose.yml``),
        the name of a project with available containers or the name
        of a directory in ``compose.containers_base``.

    update
        If the definitions have changed, also update the services.
        This does not affect the images (they are not pulled automatically)
        and is unaffected by changes outside of the compose file (e.g. env_files).
        Defaults to True.

    project_name
        The name of the project. Defaults to the name of the parent directory
        of the composition file.

    create_pod
        Create a pod for the composition. Defaults to True, if ``podman-compose``
        supports it. Forced on 0.*, unavailable in 1.* <= 1.0.3.

    pod_args
        List of custom arguments to ``podman pod create`` when create_pod is True.
        To support ``systemd`` managed pods, an infra container is necessary. Otherwise,
        none of the namespaces are shared by default. In summary,
        ["infra", {"share": ""}] is default.

    pod_args_override_default
        If pod_args are specified, do not add them to the default values. Defaults to False.

    podman_create_args
        List of custom arguments to ``podman create``. This can be used to pass
        options that are not exposed by podman-compose by default, e.g. userns.
        See `man 'podman-create(1)' <https://docs.podman.io/en/latest/markdown/podman-create.1.html#options>`_

    remove_orphans
        Remove containers not defined in the composition. Defaults to True.

    force_recreate
        Always recreate containers, even if their configuration and images
        have not changed. Defaults to False.

    build
        Build images before starting containers. Defaults to False.

    build_args
        Set build-time variables for services.

    pull
        Whether to pull potential updated images before creating the container.
        Defaults to False.

    user
        Install a rootless containers under this user account instead
        of rootful ones. Defaults to the composition file parent dir owner
        (depending on ``compose.default_to_dirowner``)
        or Salt process user. By default, defaults to the parent dir owner.

    ephemeral
        Create new containers on service start, remove them on stop. Defaults to True.

    restart_policy
        Unit restart policy, defaults to ``on-failure``.
        This is not taken from the compose definition @TODO

    restart_sec
        Specify systemd RestartSec. Requires at least podman v4.3.

    stop_timeout
        Unit stop timeout, defaults to 10 [s].

    service_overrides
        Override generation parameters per unit. This should be a dictionary,
        mapping the service name as specified in the compose file
        to extra parameters (dict as well).

    pod_wants
        Ensure the pod dependencies for containers are enforced with ``Wants=``
        instead of ``Requires=``. This fixes issues when restarting a single
        container that is part of a pod, e.g. when auto-update is run.
        See https://github.com/containers/podman/issues/14546.

    enable
        Enable the service units after installation. Defaults to True.

    pod_prefix
        Unit name prefix for pods.
        Defaults to empty (podman-compose prefixes pod names with pod_ already).
        A different default can be set in ``compose.default_pod_prefix``.

    container_prefix:
        Unit name prefix for containers. Defaults to empty.
        A different default can be set in ``compose.default_container_prefix``.

    separator
        Unit name separator between prefix and name/id.
        Depending on the other prefixes, defaults to empty or dash.
    """

    ret = {"name": name, "changes": {}, "result": True, "comment": ""}

    try:
        # 1. see if project has been applied (list installed services?)
        # 2. check if there are changes (unit files, missing units)
        # 3. decide whether to apply those changes
        if not force_recreate:
            is_installed = __salt__["compose.list_installed_units"](
                name,
                status_only=True,
                project_name=project_name,
                container_prefix=container_prefix,
                pod_prefix=pod_prefix,
                separator=separator,
                user=user,
            )

            if is_installed and not update:
                ret["comment"] = f"Composition {name} is already installed."
                return ret

            has_changes = __salt__["compose.has_changes"](
                name,
                status_only=True,
                skip_removed=not remove_orphans,
                project_name=project_name,
                container_prefix=container_prefix,
                pod_prefix=pod_prefix,
                separator=separator,
                user=user,
            )

            try:
                wanted_units = __salt__["compose.install_units"](
                    name,
                    pod_prefix=pod_prefix,
                    container_prefix=container_prefix,
                    separator=separator,
                    user=user,
                    ephemeral=ephemeral,
                    restart_policy=restart_policy,
                    restart_sec=restart_sec,
                    stop_timeout=stop_timeout,
                    service_overrides=service_overrides,
                    pod_wants=pod_wants,
                    generate_only=True,
                )
            except SaltInvocationError as err:
                if "Could not find existing pod or containers" not in str(err):
                    raise
                wanted_units = {}

            units_changed = False

            service_dir = Path(__salt__["compose.service_dir"](user))
            for unit_name, unit_definitions in wanted_units.items():
                unit_file = str(service_dir / (unit_name + ".service"))
                if not __salt__["file.file_exists"](unit_file):
                    units_changed = True
                    break
                if __salt__["file.read"](unit_file) != unit_definitions:
                    units_changed = True
                    break

            if is_installed and not has_changes and not units_changed:
                ret[
                    "comment"
                ] = f"Composition {name} is already installed and in sync with the definitions."
                return ret
        else:
            # cheap out for now @TODO
            is_installed = True

        if __opts__["test"]:
            ret["result"] = None
            ret["comment"] = "Composition {} is set to be {}.".format(
                name, "installed" if not is_installed else "updated"
            )
            ret["changes"]["installed" if not is_installed else "updated"] = name
            return ret

        if has_changes and is_installed:
            # sometimes, podman-compose fails to remove the containers correctly:
            #   podman stop -t 10 container
            #   exit code: 0
            #   podman rm container
            #   Error: no container with name or ID "container" found: no such container
            #   exit code: 1
            #   podman pod rm pod_container
            #   exit code: 0
            #   recreating: done
            # this is intended to be a workaround
            log.debug("Updating composition.")
            log.debug("Removing composition to work around podman-compose up issues.")
            dead(
                name,
                project_name=project_name,
                pod_prefix=pod_prefix,
                container_prefix=container_prefix,
                separator=separator,
                user=user,
            )
            removed(
                name,
                volumes=False,
                project_name=project_name,
                pod_prefix=pod_prefix,
                container_prefix=container_prefix,
                separator=separator,
                user=user,
            )

        if units_changed and not has_changes:
            __salt__["compose.install_units"](
                name,
                pod_prefix=pod_prefix,
                container_prefix=container_prefix,
                separator=separator,
                user=user,
                ephemeral=ephemeral,
                restart_policy=restart_policy,
                restart_sec=restart_sec,
                stop_timeout=stop_timeout,
                service_overrides=service_overrides,
                pod_wants=pod_wants,
                enable_units=enable,
                now=False,
            )
            ret["comment"] = f"Unit files for composition {name} have been updated."
            ret["changes"]["updated"] = name
        elif __salt__["compose.install"](
            name,
            project_name=project_name,
            create_pod=create_pod,
            pod_args=pod_args,
            pod_args_override_default=pod_args_override_default,
            podman_create_args=podman_create_args,
            remove_orphans=remove_orphans,
            force_recreate=force_recreate,
            build=build,
            build_args=build_args,
            pull=pull,
            user=user,
            ephemeral=ephemeral,
            restart_policy=restart_policy,
            restart_sec=restart_sec,
            stop_timeout=stop_timeout,
            service_overrides=service_overrides,
            pod_wants=pod_wants,
            enable_units=enable,
            now=False,
            pod_prefix=pod_prefix,
            container_prefix=container_prefix,
            separator=separator,
        ):
            ret["comment"] = "Composition {} has been {}.".format(
                name, "installed" if not is_installed else "updated"
            )
            ret["changes"]["installed" if not is_installed else "updated"] = name
        else:
            raise CommandExecutionError(
                "Something went wrong while trying to {} composition {}. This should not happen.".format(
                    "install" if not is_installed else "update", name
                )
            )

        if __salt__["compose.list_missing_units"](
            name,
            status_only=True,
            project_name=project_name,
            container_prefix=container_prefix,
            pod_prefix=pod_prefix,
            should_have_pod=create_pod,
            separator=separator,
            user=user,
        ):
            ret["result"] = False
            ret[
                "comment"
            ] = "Tried to install the composition, but there are still some missing components."

    except (CommandExecutionError, SaltInvocationError) as e:
        ret["result"] = False
        ret["comment"] = str(e)

    return ret


def removed(
    name,
    volumes=False,
    project_name=None,
    container_prefix=None,
    pod_prefix=None,
    separator=None,
    user=None,
):
    """
    Make sure a container composition is not installed.

    name
        Some reference about where to find the project definitions.
        Can be an absolute path to the composition definitions (``docker-compose.yml``),
        the name of a project with available containers or the name
        of a directory in ``compose.containers_base``.

    volumes
        Also remove named volumes declared in the ``volumes`` section of the
        compose file and anonymous volumes attached to containers.
        Defaults to False.

    project_name
        The name of the project. Defaults to the name of the parent directory
        of the composition file.

    container_prefix:
        Unit name prefix for containers. Defaults to empty.
        A different default can be set in ``compose.default_container_prefix``.

    pod_prefix
        Unit name prefix for pods. Defaults to empty
        (podman-compose prefixes pod names with pod_ already).
        A different default can be set in ``compose.default_pod_prefix``.

    separator
        Unit name separator between prefix and name/id.
        Depending on the other prefixes, defaults to empty or dash.

    user
        Install a rootless containers under this user account instead
        of rootful ones. Defaults to the composition file parent dir owner
        (depending on ``compose.default_to_dirowner``)
        or Salt process user. By default, defaults to the parent dir owner.
    """

    ret = {"name": name, "changes": {}, "result": True, "comment": ""}

    try:
        if not __salt__["compose.find_compose_file"](
            name, user=user, raise_not_found_error=False
        ):
            ret[
                "comment"
            ] = f"Could not find compose file for composition {name}. Assuming it has been removed."
            return ret

        # @TODO this does not check for containers belonging to this composition
        # if the services are not ephemeral
        if not __salt__["compose.list_installed_units"](
            name,
            status_only=True,
            project_name=project_name,
            container_prefix=container_prefix,
            pod_prefix=pod_prefix,
            separator=separator,
            user=user,
        ):
            ret["comment"] = f"Composition {name} is already absent."
            return ret

        if __opts__["test"]:
            ret["result"] = None
            ret["comment"] = f"Composition {name} is set to be removed."
            if volumes:
                ret["comment"] += " Volumes are set to be removed as well."
            ret["changes"]["removed"] = name
            return ret

        if __salt__["compose.remove"](
            name,
            volumes=volumes,
            project_name=project_name,
            pod_prefix=pod_prefix,
            container_prefix=container_prefix,
            separator=separator,
            user=user,
        ):
            ret["comment"] = f"Composition {name} has been removed."
            if volumes:
                ret["comment"] += " Volumes have been removed as well."
            ret["changes"]["removed"] = name
        else:
            raise CommandExecutionError(
                f"Something went wrong while trying to remove composition {name}. "
                "This should not happen."
            )

        if __salt__["compose.list_installed_units"](
            name,
            status_only=True,
            project_name=project_name,
            container_prefix=container_prefix,
            pod_prefix=pod_prefix,
            separator=separator,
            user=user,
        ):
            ret["result"] = False
            ret[
                "comment"
            ] = "Tried to remove the composition, but some units are still installed."
            ret["changes"] = {}

    except (CommandExecutionError, SaltInvocationError) as e:
        ret["result"] = False
        ret["comment"] = str(e)

    return ret


def dead(
    name,
    project_name=None,
    pod_prefix=None,
    container_prefix=None,
    separator=None,
    user=None,
    timeout=10,
):
    """
    Make sure the installed units for a composition are dead.

    name
        Some reference about where to find the project definitions.
        Can be an absolute path to the composition definitions (``docker-compose.yml``),
        the name of a project with available containers or the name
        of a directory in ``compose.containers_base``.

    project_name
        The name of the project. Defaults to the name of the parent directory
        of the composition file.

    pod_prefix
        Unit name prefix for pods.
        Defaults to empty (podman-compose prefixes pod names with pod_ already).
        A different default can be set in ``compose.default_pod_prefix``.

    container_prefix:
        Unit name prefix for containers. Defaults to empty.
        A different default can be set in ``compose.default_container_prefix``.

    separator
        Unit name separator between prefix and name/id.
        Depending on the other prefixes, defaults to empty or dash.

    user
        The user account this composition has been applied to. Defaults to
        the composition file parent dir owner (depending on ``compose.default_to_dirowner``)
        or Salt process user. By default, defaults to the parent dir owner.

    timeout
        This state checks whether all services belonging to a composition are up.
        Since only the pod is started explicitly, there is a slight delay for the container
        services. Furthermore, many containers require some time to be reported
        as up. This configures the maximum wait time in seconds. Defaults to 10.
    """

    ret = {"name": name, "changes": {}, "result": True, "comment": ""}

    try:
        if not __salt__["compose.find_compose_file"](
            name, user=user, raise_not_found_error=False
        ):
            ret[
                "comment"
            ] = f"Could not find compose file for composition {name}. Assuming it has been removed."
            return ret

        if not __salt__["compose.list_installed_units"](
            name,
            status_only=True,
            project_name=project_name,
            pod_prefix=pod_prefix,
            container_prefix=container_prefix,
            separator=separator,
            user=user,
        ):
            ret[
                "comment"
            ] = f"Could not find any installed units for composition {name}."
            return ret

        if not __salt__["compose.is_running"](
            name,
            project_name=project_name,
            pod_prefix=pod_prefix,
            container_prefix=container_prefix,
            separator=separator,
            user=user,
        ):
            ret["comment"] = f"Service for {name} is already dead."
            return ret

        if __opts__["test"]:
            ret["result"] = None
            ret["comment"] = f"Service for {name} is set to be stopped."
            ret["changes"]["stopped"] = name
            return ret

        if __salt__["compose.stop"](
            name,
            project_name=project_name,
            pod_prefix=pod_prefix,
            container_prefix=container_prefix,
            separator=separator,
            user=user,
        ):
            ret["comment"] = f"Service for {name} has been stopped."
            ret["changes"]["stopped"] = name
        else:
            raise CommandExecutionError(
                f"Something went wrong while trying to stop service for {name}. "
                "This should not happen."
            )

        start_time = time.time()

        while not __salt__["compose.is_dead"](
            name,
            project_name=project_name,
            pod_prefix=pod_prefix,
            container_prefix=container_prefix,
            separator=separator,
            user=user,
        ):
            if time.time() - start_time > timeout:
                ret["result"] = False
                ret["comment"] = "Tried to stop the service, but it is still running."
                ret["changes"] = {}
                return ret
            time.sleep(0.25)

    except (CommandExecutionError, SaltInvocationError) as e:
        ret["result"] = False
        ret["comment"] = str(e)

    return ret


def disabled(
    name,
    project_name=None,
    pod_prefix=None,
    container_prefix=None,
    separator=None,
    user=None,
):
    """
    Make sure the installed units for a composition are disabled.

    name
        Some reference about where to find the project definitions.
        Can be an absolute path to the composition definitions (``docker-compose.yml``),
        the name of a project with available containers or the name
        of a directory in ``compose.containers_base``.

    project_name
        The name of the project. Defaults to the name of the parent directory
        of the composition file.

    pod_prefix
        Unit name prefix for pods.
        Defaults to empty (podman-compose prefixes pod names with pod_ already).
        A different default can be set in ``compose.default_pod_prefix``.

    container_prefix:
        Unit name prefix for containers. Defaults to empty.
        A different default can be set in ``compose.default_container_prefix``.

    separator
        Unit name separator between prefix and name/id.
        Depending on the other prefixes, defaults to empty or dash.

    user
        The user account this composition has been applied to. Defaults to
        the composition file parent dir owner (depending on ``compose.default_to_dirowner``)
        or Salt process user. By default, defaults to the parent dir owner.
    """

    ret = {"name": name, "changes": {}, "result": True, "comment": ""}

    try:
        if not __salt__["compose.find_compose_file"](
            name, user=user, raise_not_found_error=False
        ):
            ret[
                "comment"
            ] = f"Could not find compose file for composition {name}. Assuming it has been removed."
            return ret

        if not __salt__["compose.list_installed_units"](
            name,
            status_only=True,
            project_name=project_name,
            pod_prefix=pod_prefix,
            container_prefix=container_prefix,
            separator=separator,
            user=user,
        ):
            ret[
                "comment"
            ] = f"Could not find any installed units for composition {name}."
            return ret

        if not __salt__["compose.is_enabled"](
            name,
            project_name=project_name,
            pod_prefix=pod_prefix,
            container_prefix=container_prefix,
            separator=separator,
            user=user,
        ):
            ret["comment"] = f"Service for {name} is already disabled."
            return ret

        if __opts__["test"]:
            ret["result"] = None
            ret["comment"] = f"Service for {name} is set to be disabled."
            ret["changes"]["disabled"] = name
            return ret

        if __salt__["compose.disable"](
            name,
            project_name=project_name,
            pod_prefix=pod_prefix,
            container_prefix=container_prefix,
            separator=separator,
            user=user,
        ):
            ret["comment"] = f"Service for {name} has been disabled."
            ret["changes"]["disabled"] = name
        else:
            raise CommandExecutionError(
                f"Something went wrong while trying to stop service for {name}. "
                "This should not happen."
            )

        if not __salt__["compose.is_disabled"](
            name,
            project_name=project_name,
            pod_prefix=pod_prefix,
            container_prefix=container_prefix,
            separator=separator,
            user=user,
        ):
            ret["result"] = False
            ret[
                "comment"
            ] = "Tried to disable the service, but it is still reported as enabled."
            ret["changes"] = {}

    except (CommandExecutionError, SaltInvocationError) as e:
        ret["result"] = False
        ret["comment"] = str(e)

    return ret


def enabled(
    name,
    project_name=None,
    pod_prefix=None,
    container_prefix=None,
    separator=None,
    user=None,
):
    """
    Make sure the installed units for a composition are enabled.

    name
        Some reference about where to find the project definitions.
        Can be an absolute path to the composition definitions (``docker-compose.yml``),
        the name of a project with available containers or the name
        of a directory in ``compose.containers_base``.

    project_name
        The name of the project. Defaults to the name of the parent directory
        of the composition file.

    pod_prefix
        Unit name prefix for pods.
        Defaults to empty (podman-compose prefixes pod names with pod_ already).
        A different default can be set in ``compose.default_pod_prefix``.

    container_prefix:
        Unit name prefix for containers. Defaults to empty.
        A different default can be set in ``compose.default_container_prefix``.

    separator
        Unit name separator between prefix and name/id.
        Depending on the other prefixes, defaults to empty or dash.

    user
        The user account this composition has been applied to. Defaults to
        the composition file parent dir owner (depending on ``compose.default_to_dirowner``)
        or Salt process user. By default, defaults to the parent dir owner.
    """

    ret = {"name": name, "changes": {}, "result": True, "comment": ""}

    try:
        if __salt__["compose.is_enabled"](
            name,
            project_name=project_name,
            pod_prefix=pod_prefix,
            container_prefix=container_prefix,
            separator=separator,
            user=user,
        ):
            ret["comment"] = f"Service for {name} is already enabled."
            return ret

        if __opts__["test"]:
            ret["result"] = None
            ret["comment"] = f"Service for {name} is set to be enabled."
            ret["changes"]["enabled"] = name
            return ret

        if __salt__["compose.enable"](
            name,
            project_name=project_name,
            pod_prefix=pod_prefix,
            container_prefix=container_prefix,
            separator=separator,
            user=user,
        ):
            ret["comment"] = f"Service for {name} has been enabled."
            ret["changes"]["enabled"] = name
        else:
            raise CommandExecutionError(
                f"Something went wrong while trying to stop service for {name}. "
                "This should not happen."
            )

        if not __salt__["compose.is_enabled"](
            name,
            project_name=project_name,
            pod_prefix=pod_prefix,
            container_prefix=container_prefix,
            separator=separator,
            user=user,
        ):
            ret["result"] = False
            ret[
                "comment"
            ] = "Tried to enable the service, but it is reported as disabled."
            ret["changes"] = {}

    except (CommandExecutionError, SaltInvocationError) as e:
        ret["result"] = False
        ret["comment"] = str(e)

    return ret


def running(
    name,
    project_name=None,
    pod_prefix=None,
    container_prefix=None,
    separator=None,
    user=None,
    timeout=10,
):
    """
    Make sure the installed units for a composition are running.

    This state explicitly does not implement ``enable`` since any
    changes in this state would override ``mod_watch`` behavior,
    which is essential for this module to work correctly.

    name
        Some reference about where to find the project definitions.
        Can be an absolute path to the composition definitions (``docker-compose.yml``),
        the name of a project with available containers or the name
        of a directory in ``compose.containers_base``.

    project_name
        The name of the project. Defaults to the name of the parent directory
        of the composition file.

    pod_prefix
        Unit name prefix for pods.
        Defaults to empty (podman-compose prefixes pod names with pod_ already).
        A different default can be set in ``compose.default_pod_prefix``.

    container_prefix:
        Unit name prefix for containers. Defaults to empty.
        A different default can be set in ``compose.default_container_prefix``.

    separator
        Unit name separator between prefix and name/id.
        Depending on the other prefixes, defaults to empty or dash.

    user
        The user account this composition has been applied to. Defaults to
        the composition file parent dir owner (depending on ``compose.default_to_dirowner``)
        or Salt process user. By default, defaults to the parent dir owner.

    timeout
        This state checks whether all services belonging to a composition are up.
        Since only the pod is started explicitly, there is a slight delay for the container
        services. Furthermore, many containers require some time to be reported
        as up. This configures the maximum wait time in seconds. Defaults to 10.
    """

    ret = {"name": name, "changes": {}, "result": True, "comment": ""}

    try:
        if __salt__["compose.is_running"](
            name,
            project_name=project_name,
            pod_prefix=pod_prefix,
            container_prefix=container_prefix,
            separator=separator,
            user=user,
        ):
            ret["comment"] = f"Service for {name} is already running."
            return ret

        if __opts__["test"]:
            ret["result"] = None
            ret["comment"] = f"Service for {name} is set to be started."
            ret["changes"]["started"] = name
            return ret

        if __salt__["compose.start"](
            name,
            project_name=project_name,
            pod_prefix=pod_prefix,
            container_prefix=container_prefix,
            separator=separator,
            user=user,
        ):
            ret["comment"] = f"Service for {name} has been started."
            ret["changes"]["started"] = name
        else:
            raise CommandExecutionError(
                f"Something went wrong while trying to start service for {name}. "
                "This should not happen."
            )

        start_time = time.time()

        while not __salt__["compose.is_running"](
            name,
            project_name=project_name,
            pod_prefix=pod_prefix,
            container_prefix=container_prefix,
            separator=separator,
            user=user,
        ):
            if time.time() - start_time > timeout:
                ret["result"] = False
                ret[
                    "comment"
                ] = "Tried to start the service, but it is still not running."
                ret["changes"] = {}
                return ret
            time.sleep(0.25)

    except (CommandExecutionError, SaltInvocationError) as e:
        ret["result"] = False
        ret["comment"] = str(e)

    return ret


def lingering_managed(name, enable):
    """
    Manage lingering status for a user.
    Lingering is required to run rootless containers as
    general services.

    name
        The user to manage lingering for.

    enable
        Whether to enable or disable lingering.
    """

    ret = {"name": name, "changes": {}, "result": True, "comment": ""}

    try:
        user_info = __salt__["user.info"](name)
        if not user_info:
            if __opts__["test"]:
                ret["result"] = None
                ret[
                    "comment"
                ] = f"User {name} does not exist. If it is created by some state before this, this check will pass."
                return ret
            raise SaltInvocationError(f"User {name} does not exist.")

        if enable:
            func = __salt__["compose.lingering_enable"]
            verb = "enable"
        else:
            func = __salt__["compose.lingering_disable"]
            verb = "disable"

        if __salt__["compose.lingering_enabled"](name) is enable:
            ret["comment"] = f"Lingering for user {name} is already {verb}d."
            return ret
        if __opts__["test"]:
            ret["result"] = None
            ret["comment"] = f"Lingering for user {name} is set to be {verb}d."
            ret["changes"]["lingering"] = enable
        elif not func(name):
            raise CommandExecutionError(
                f"Something went wrong while trying to {verb} lingering for user {name}. "
                "This should not happen."
            )

        start = time.time()
        dbus_session_bus = Path(f"/run/user/{user_info['uid']}/bus")
        # The enabling lags a bit, which might make other states fail
        while start - time.time() < 10:
            if dbus_session_bus.exists() is enable:
                ret["comment"] = f"Lingering for user {name} has been {verb}d."
                ret["changes"]["lingering"] = enable
                return ret
            time.sleep(0.1)
        raise CommandExecutionError(
            "No errors encountered, but the reported state does not match the expected"
        )

    except (CommandExecutionError, SaltInvocationError) as e:
        ret["result"] = False
        ret["comment"] = str(e)

    return ret


def systemd_service_enabled(
    name,
    user=None,
):
    """
    Make sure a systemd unit is enabled. This is an extension to the
    official module, which allows to manage services for arbitrary user accounts.
    This does not support mod_watch behavior. Also, it should be a separate module @TODO

    name
        Name of the systemd unit.

    user
        User account the unit should be enabled for. Defaults to Salt process user.
    """

    ret = {"name": name, "changes": {}, "result": True, "comment": ""}

    try:
        if __salt__["compose.systemctl_is_enabled"](
            name,
            user=user,
        ):
            ret["comment"] = f"Service {name} is already enabled."
            return ret

        if __opts__["test"]:
            ret["result"] = None
            ret["comment"] = f"Service {name} is set to be enabled."
            ret["changes"]["enabled"] = name
            return ret

        if __salt__["compose.systemctl_enable"](
            name,
            user=user,
        ):
            ret["comment"] = f"Service {name} has been enabled."
            ret["changes"]["enabled"] = name
        else:
            raise CommandExecutionError(
                f"Something went wrong while trying to stop service {name}. This should not happen."
            )

        if not __salt__["compose.systemctl_is_enabled"](
            name,
            user=user,
        ):
            ret["result"] = False
            ret[
                "comment"
            ] = "Tried to enable the service, but it is reported as disabled."
            ret["changes"] = {}

    except (CommandExecutionError, SaltInvocationError) as e:
        ret["result"] = False
        ret["comment"] = str(e)

    return ret


def systemd_service_running(name, user=None, timeout=10):
    """
    Make sure a systemd unit is running. This is an extension to the
    official module, which allows to manage services for arbitrary user accounts.
    This does not support mod_watch behavior. Also, it should be a separate module @TODO

    name
        Name of the systemd unit.

    user
        User account the unit should be running for. Defaults to Salt process user.

    timeout
        This state checks whether the service was started successfully. This configures
        the maximum wait time in seconds. Defaults to 10.
    """

    ret = {"name": name, "changes": {}, "result": True, "comment": ""}

    try:
        if __salt__["compose.systemctl_is_running"](
            name,
            user=user,
        ):
            ret["comment"] = f"Service {name} is already running."
            return ret

        if __opts__["test"]:
            ret["result"] = None
            ret["comment"] = f"Service {name} is set to be started."
            ret["changes"]["started"] = name
            return ret

        if __salt__["compose.systemctl_start"](
            name,
            user=user,
        ):
            ret["comment"] = f"Service {name} has been started."
            ret["changes"]["started"] = name
        else:
            raise CommandExecutionError(
                f"Something went wrong while trying to start service {name}. This should not happen."
            )

        start_time = time.time()

        while not __salt__["compose.systemctl_is_running"](
            name,
            user=user,
        ):
            if time.time() - start_time > timeout:
                ret["result"] = False
                ret[
                    "comment"
                ] = "Tried to start the service, but it is still not running."
                ret["changes"] = {}
                return ret
            time.sleep(0.25)

    except (CommandExecutionError, SaltInvocationError) as e:
        ret["result"] = False
        ret["comment"] = str(e)

    return ret


def systemd_service_disabled(
    name,
    user=None,
):
    """
    Make sure a systemd unit is disabled. This is an extension to the
    official module, which allows to manage services for arbitrary user accounts.
    This does not support mod_watch behavior. Also, it should be a separate module @TODO

    name
        Name of the systemd unit.

    user
        User account the unit should be disabled for. Defaults to Salt process user.
    """

    ret = {"name": name, "changes": {}, "result": True, "comment": ""}

    try:
        if not __salt__["compose.systemctl_is_enabled"](
            name,
            user=user,
        ):
            ret["comment"] = f"Service {name} is already disabled."
            return ret

        if __opts__["test"]:
            ret["result"] = None
            ret["comment"] = f"Service {name} is set to be disabled."
            ret["changes"]["disabled"] = name
            return ret

        if __salt__["compose.systemctl_disable"](
            name,
            user=user,
        ):
            ret["comment"] = f"Service {name} has been disabled."
            ret["changes"]["disabled"] = name
        else:
            raise CommandExecutionError(
                f"Something went wrong while trying to stop service {name}. This should not happen."
            )

        if __salt__["compose.systemctl_is_enabled"](
            name,
            user=user,
        ):
            ret["result"] = False
            ret[
                "comment"
            ] = "Tried to disable the service, but it is reported as enabled."
            ret["changes"] = {}

    except (CommandExecutionError, SaltInvocationError) as e:
        ret["result"] = False
        ret["comment"] = str(e)

    return ret


def systemd_service_dead(name, user=None, timeout=10):
    """
    Make sure a systemd unit is dead. This is an extension to the
    official module, which allows to manage services for arbitrary user accounts.
    This does not support mod_watch behavior. Also, it should be a separate module @TODO

    name
        Name of the systemd unit.

    user
        User account the unit should be dead for. Defaults to Salt process user.

    timeout
        This state checks whether the service was stopped successfully. This configures
        the maximum wait time in seconds. Defaults to 10.
    """

    ret = {"name": name, "changes": {}, "result": True, "comment": ""}

    try:
        if not __salt__["compose.systemctl_is_running"](
            name,
            user=user,
        ):
            ret["comment"] = f"Service {name} is already dead."
            return ret

        if __opts__["test"]:
            ret["result"] = None
            ret["comment"] = f"Service {name} is set to be stopped."
            ret["changes"]["stopped"] = name
            return ret

        if __salt__["compose.systemctl_stop"](
            name,
            user=user,
        ):
            ret["comment"] = f"Service {name} has been stopped."
            ret["changes"]["stopped"] = name
        else:
            raise CommandExecutionError(
                f"Something went wrong while trying to stop service {name}. This should not happen."
            )

        start_time = time.time()

        while __salt__["compose.systemctl_is_running"](
            name,
            user=user,
        ):
            if time.time() - start_time > timeout:
                ret["result"] = False
                ret["comment"] = "Tried to stop the service, but it is still running."
                ret["changes"] = {}
                return ret
            time.sleep(0.25)

    except (CommandExecutionError, SaltInvocationError) as e:
        ret["result"] = False
        ret["comment"] = str(e)

    return ret


def mod_watch(name, sfun=None, **kwargs):
    ret = {"name": name, "changes": {}, "result": True, "comment": ""}
    target = "Service"
    pp_suffix = "ed"

    # all status functions have the same signature
    status_kwargs = _get_valid_args(__salt__["compose.is_running"], kwargs)

    try:
        if sfun in ["dead", "running"]:
            if "dead" == sfun:
                verb = "stop"
                pp_suffix = "ped"

                if __salt__["compose.is_running"](name, **status_kwargs):
                    func = __salt__["compose.stop"]
                    check_func = __salt__["compose.is_dead"]
                else:
                    ret["comment"] = "Service is already stopped."
                    return ret

            # "running" == sfun evidently
            else:
                check_func = __salt__["compose.is_running"]
                if __salt__["compose.is_running"](name, **status_kwargs):
                    verb = "restart"
                    func = __salt__["compose.restart"]
                else:
                    verb = "start"
                    func = __salt__["compose.start"]

        elif "installed" == sfun:
            # no need to check if installled since mod_watch only triggers
            # if there are no changes in the underlying state

            # podman-compose up has issues with systemd managed units though,
            # so stop the service before recreating
            if not __opts__["test"] and __salt__["compose.is_running"](
                name, **status_kwargs
            ):
                __salt__["compose.stop"](name, **status_kwargs)

            # force container recreation in case podman-compose does not detect changes
            kwargs["force_recreate"] = True
            # clash with function name (should have used enable_ and mapped the name @FIXME)
            if "enable" in kwargs:
                kwargs["enable_units"] = kwargs["enable"]

            target = "Composition"
            verb = "recreate"
            pp_suffix = "d"
            func = __salt__["compose.install"]
            check_func = False

        else:
            ret["comment"] = f"Unable to trigger watch for compose.{sfun}"
            ret["result"] = False
            return ret

        if __opts__["test"]:
            ret["result"] = None
            ret["comment"] = f"{target} is set to be {verb}{pp_suffix}."
            ret["changes"][verb + pp_suffix] = name
            return ret

        func_kwargs = _get_valid_args(func, kwargs)
        func(name, **func_kwargs)

        if check_func:
            timeout = kwargs.get("timeout", 10)
            start_time = time.time()

            while not check_func(name, **status_kwargs):
                if time.time() - start_time > timeout:
                    ret["result"] = False
                    ret[
                        "comment"
                    ] = f"Tried to {verb} the service, but it is still not {sfun}."
                    ret["changes"] = {}
                    return ret
                time.sleep(0.25)

    except (CommandExecutionError, SaltInvocationError) as e:
        ret["result"] = False
        ret["comment"] = str(e)
        return ret

    ret["comment"] = f"{target} was {verb}{pp_suffix}."
    ret["changes"][verb + pp_suffix] = name

    return ret


def file_copy(
    name,
    project,
    container_ref=None,
    fail_kwargs=None,
    **kwargs,
):
    """
    Wrapper for ``file.copy`` that allows to manage owners from the perspective
    of the container. This requires the container to be installed or running
    and ``user``/``group`` to be set using IDs instead of names (except ``root``,
    which is mapped to ``0``). If it is not running, only non-remapped IDs,
    ``userns=keep-id`` or ``uidmap``/``gidmap`` can be resolved.

    name
        Name of the directory to manage.

    project
        Either the absolute path to a composition file or a project name.

    container_ref
        Match specific project container by substring in a name.
        Optional.

    fail_kwargs
        Instead of failing when a container is not running, update the
        kwargs with this dictionary and still execute the state call.

    kwargs
        Other kwargs will be passed through to ``file.copy``.
    """
    return __states__["file.copy"](
        name, **_resolve_owner(project, container_ref, fail_kwargs, kwargs)
    )


def file_directory(
    name,
    project,
    container_ref=None,
    fail_kwargs=None,
    **kwargs,
):
    """
    Wrapper for ``file.directory`` that allows to manage owners from the perspective
    of the container. This requires the container to be installed or running
    and ``user``/``group`` to be set using IDs instead of names (except ``root``,
    which is mapped to ``0``). If it is not running, only non-remapped IDs,
    ``userns=keep-id`` or ``uidmap``/``gidmap`` can be resolved.

    name
        Name of the directory to manage.

    project
        Either the absolute path to a composition file or a project name.

    container_ref
        Match specific project container by substring in a name.
        Optional.

    fail_kwargs
        Instead of failing when a container is not running, update the
        kwargs with this dictionary and still execute the state call.

    kwargs
        Other kwargs will be passed through to ``file.directory``.
    """
    return __states__["file.directory"](
        name, **_resolve_owner(project, container_ref, fail_kwargs, kwargs)
    )


def file_managed(
    name,
    project,
    container_ref=None,
    fail_kwargs=None,
    **kwargs,
):
    """
    Wrapper for ``file.managed`` that allows to manage owners from the perspective
    of the container. This requires the container to be installed or running
    and ``user``/``group`` to be set using IDs instead of names (except ``root``,
    which is mapped to ``0``). If it is not running, only non-remapped IDs,
    ``userns=keep-id`` or ``uidmap``/``gidmap`` can be resolved.

    name
        Name of the directory to manage.

    project
        Either the absolute path to a composition file or a project name.

    container_ref
        Match specific project container by substring in a name.
        Optional.

    fail_kwargs
        Instead of failing when a container is not running, update the
        kwargs with this dictionary and still execute the state call.

    kwargs
        Other kwargs will be passed through to ``file.managed``.
    """
    return __states__["file.managed"](
        name, **_resolve_owner(project, container_ref, fail_kwargs, kwargs)
    )


def file_recurse(
    name,
    project,
    container_ref=None,
    fail_kwargs=None,
    **kwargs,
):
    """
    Wrapper for ``file.recurse`` that allows to manage owners from the perspective
    of the container. This requires the container to be installed or running
    and ``user``/``group`` to be set using IDs instead of names (except ``root``,
    which is mapped to ``0``). If it is not running, only non-remapped IDs,
    ``userns=keep-id`` or ``uidmap``/``gidmap`` can be resolved.

    name
        Name of the directory to manage.

    project
        Either the absolute path to a composition file or a project name.

    container_ref
        Match specific project container by substring in a name.
        Optional.

    fail_kwargs
        Instead of failing when a container is not running, update the
        kwargs with this dictionary and still execute the state call.

    kwargs
        Other kwargs will be passed through to ``file.recurse``.
    """
    return __states__["file.recurse"](
        name, **_resolve_owner(project, container_ref, fail_kwargs, kwargs)
    )


def file_serialize(
    name,
    project,
    container_ref=None,
    fail_kwargs=None,
    **kwargs,
):
    """
    Wrapper for ``file.serialize`` that allows to manage owners from the perspective
    of the container. This requires the container to be installed or running
    and ``user``/``group`` to be set using IDs instead of names (except ``root``,
    which is mapped to ``0``). If it is not running, only non-remapped IDs,
    ``userns=keep-id`` or ``uidmap``/``gidmap`` can be resolved.

    name
        Name of the directory to manage.

    project
        Either the absolute path to a composition file or a project name.

    container_ref
        Match specific project container by substring in a name.
        Optional.

    fail_kwargs
        Instead of failing when a container is not running, update the
        kwargs with this dictionary and still execute the state call.

    kwargs
        Other kwargs will be passed through to ``file.serialize``.
    """
    return __states__["file.serialize"](
        name, **_resolve_owner(project, container_ref, fail_kwargs, kwargs)
    )


def file_symlink(
    name,
    project,
    container_ref=None,
    fail_kwargs=None,
    **kwargs,
):
    """
    Wrapper for ``file.symlink`` that allows to manage owners from the perspective
    of the container. This requires the container to be installed or running
    and ``user``/``group`` to be set using IDs instead of names (except ``root``,
    which is mapped to ``0``). If it is not running, only non-remapped IDs,
    ``userns=keep-id`` or ``uidmap``/``gidmap`` can be resolved.

    name
        Name of the directory to manage.

    project
        Either the absolute path to a composition file or a project name.

    container_ref
        Match specific project container by substring in a name.
        Optional.

    fail_kwargs
        Instead of failing when a container is not running, update the
        kwargs with this dictionary and still execute the state call.

    kwargs
        Other kwargs will be passed through to ``file.symlink``.
    """
    return __states__["file.symlink"](
        name, **_resolve_owner(project, container_ref, fail_kwargs, kwargs)
    )


def _resolve_owner(project, container_ref, fail_kwargs, kwargs):
    """
    Helper that resolves ``file`` state module calls with owner
    parameters to container UID/GID.
    """

    def check_fail(msg=""):
        err_msg = msg or (
            f"Could not find any container with ref '{container_ref}' "
            f"belonging to project '{project}'"
        )
        if fail_kwargs is None:
            raise CommandExecutionError(err_msg)
        log.warning(err_msg)
        kwargs.update(fail_kwargs)
        return kwargs

    cnt_info = __salt__["compose.inspect"](project, name=container_ref)
    idmap_host = {"uid": None, "gid": None}
    if cnt_info:
        cnt_info = cnt_info[0]
        cnt_idmap = cnt_info["HostConfig"].get(
            "IDMappings", {"GidMap": [], "UidMap": []}
        )
    else:
        # This means the container is not running, we can try to
        # inspect the service units for expected ID remappings
        units = __salt__["compose.list_installed_units"](project)
        if not units:
            return check_fail()
        userns = ""
        uidmap_unit = []
        gidmap_unit = []
        if units.get("pods"):
            # If a pod is in use, it will carry the remap defs
            pod_info = __salt__["compose.inspect_unit"](
                units["pods"][next(iter(units["pods"]))]
            )
            if "userns" in pod_info["options"]:
                userns = pod_info["options"]["userns"]
            else:
                if "uidmap" in pod_info["options"]:
                    uidmap_unit = pod_info["options"]["uidmap"]
                if "gidmap" in pod_info["options"]:
                    gidmap_unit = pod_info["options"]["gidmap"]
        else:
            # Otherwise, look through the container unit definitions
            for cnt in units.get("containers", []):
                if container_ref is not None and container_ref not in cnt:
                    continue
                # `raw` because `inspect_unit` tries to render `podman.ps` output
                # for containers otherwise
                cnt_info = __salt__["compose.inspect_unit"](cnt, raw=True)
                if "userns" in cnt_info["options"]:
                    userns = cnt_info["options"]["userns"]
                else:
                    if "uidmap" in cnt_info["options"]:
                        uidmap_unit = cnt_info["options"]["uidmap"]
                    if "gidmap" in cnt_info["options"]:
                        gidmap_unit = cnt_info["options"]["gidmap"]
                break

        # Format possibly discovered remap defs as podman inspect would return
        if uidmap_unit or gidmap_unit:
            cnt_idmap = {"GidMap": gidmap_unit, "UidMap": uidmap_unit}
        elif userns.startswith("keep-id"):
            userns = userns[7:]
            cnt_user = None
            cnt_group = None
            if userns.startswith(":"):
                for defn in userns.split(","):
                    param, val = defn.split("=")
                    if param == "uid":
                        cnt_user = int(val)
                    elif param == "gid":
                        cnt_group = int(val)
            if cnt_user is None or cnt_group is None:
                # If uid/gid were not specified, we need to discover the
                # user's uid/gid to know which ID the user will receive
                # inside the container
                project_info = __salt__["compose.project_info"](project)
                if not project_info["user"]:
                    return check_fail(
                        f"Could not determine associated user from project '{project}'"
                    )
                user_info = __salt__["user.info"](project_info["user"])
                cnt_user = cnt_user if cnt_user is not None else user_info["uid"]
                cnt_group = cnt_group if cnt_group is not None else user_info["gid"]
            # We need the host maps to discover the number of IDs, save them for later
            idmap_host["uid"] = SubID.from_str(
                __salt__["compose.unshare"](project, "cat /proc/self/uid_map")
            )
            idmap_host["gid"] = SubID.from_str(
                __salt__["compose.unshare"](project, "cat /proc/self/gid_map")
            )
            # Calculate final internal idmap
            cnt_idmap = {
                "GidMap": [
                    f"0:1:{cnt_group}",
                    f"{cnt_group}:0:1",
                    f"{cnt_group+1}:{cnt_group+1}:{idmap_host['gid'].size() - cnt_group}",
                ],
                "UidMap": [
                    f"0:1:{cnt_user}",
                    f"{cnt_user}:0:1",
                    f"{cnt_user+1}:{cnt_user+1}:{idmap_host['uid'].size() - cnt_user}",
                ],
            }
        else:
            # Nothing was overridden (or we don't know about the `userns` value)
            cnt_idmap = {"GidMap": [], "UidMap": []}

    for ent in ("user", "group"):
        if ent in kwargs:
            if kwargs[ent] == "root":
                wanted_id = 0
            elif isinstance(kwargs[ent], str):
                raise SaltInvocationError(
                    f"Cannot resolve named {ent}s other than 'root' from outside "
                    f"the container, required: {ent} ID (integer)"
                )
            else:
                wanted_id = kwargs[ent]

            # This accounts for maps other than ``--userns=host``, e.g. ``keep-id``
            # or custom maps.
            int_id_map = SubID.from_inspect(cnt_idmap.get(f"{ent[0].upper()}idMap", []))
            id_map = idmap_host[f"{ent[0]}id"] or SubID.from_str(
                __salt__["compose.unshare"](project, f"cat /proc/self/{ent[0]}id_map")
            )
            eff_id = int_id_map.find_parent_id(wanted_id)
            log.debug(f"Found effective (internal) {ent} ID: {wanted_id} => {eff_id}")
            final_id = id_map.find_parent_id(eff_id)
            log.debug(f"Found final (host) {ent} ID: {eff_id} => {final_id}")
            kwargs[ent] = final_id
    return kwargs


class SubID:
    submap = None

    def __init__(self, submap):
        self.submap = submap

    @staticmethod
    def from_str(submap):
        """
        Create a SubID object from ``uid_map`` or ``gid_map`` output.
        """
        return SubID(
            tuple(
                tuple(map(int, re.split(r"[\s]+", line.strip())))
                for line in submap.splitlines()
                if line
            )
        )

    @staticmethod
    def from_inspect(submap):
        """
        Create a SubID object from ``podman inspect`` ``HostConfig:IDMappings:(U/G)idMap``
        output.
        """
        return SubID(tuple(tuple(map(int, line.split(":"))) for line in submap))

    def find_parent_id(self, fid):
        if not self.submap:
            return fid
        for pid_start, parent_start, cnt in self.submap:
            if fid in range(pid_start, pid_start + cnt):
                return parent_start + fid - pid_start
        raise SaltInvocationError("No such ID defined")

    def size(self):
        largest = 0
        for _, _, cnt in self.submap:
            if cnt > largest:
                largest = cnt
        return largest
