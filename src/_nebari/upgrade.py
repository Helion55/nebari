"""
This file contains the upgrade logic for Nebari.
Each release of Nebari requires an upgrade step class (which is a child class of UpgradeStep) to be created.
When a user runs `nebari upgrade  -c nebari-config.yaml`, then the do_upgrade function will then run through all required upgrade steps to bring the config file up to date with the current version of Nebari.
"""

import json
import logging
import os
import re
import secrets
import string
import textwrap
from abc import ABC
from pathlib import Path
from typing import Any, ClassVar, Dict

import kubernetes.client
import kubernetes.config
import requests
import rich
from packaging.version import Version
from pydantic import ValidationError
from rich.prompt import Confirm, Prompt
from typing_extensions import override

from _nebari.config import backup_configuration
from _nebari.keycloak import get_keycloak_admin
from _nebari.utils import (
    get_k8s_version_prefix,
    get_provider_config_block_name,
    load_yaml,
    update_tfstate_file,
    yaml,
)
from _nebari.version import __version__, rounded_ver_parse
from nebari.schema import ProviderEnum, is_version_accepted, provider_enum_name_map

logger = logging.getLogger(__name__)

NEBARI_WORKFLOW_CONTROLLER_DOCS = (
    "https://www.nebari.dev/docs/how-tos/using-argo/#jupyterflow-override-beta"
)
ARGO_JUPYTER_SCHEDULER_REPO = "https://github.com/nebari-dev/argo-jupyter-scheduler"

UPGRADE_KUBERNETES_MESSAGE = "Please see the [green][link=https://www.nebari.dev/docs/how-tos/kubernetes-version-upgrade]Kubernetes upgrade docs[/link][/green] for more information."
DESTRUCTIVE_UPGRADE_WARNING = "-> This version upgrade will result in your cluster being completely torn down and redeployed.  Please ensure you have backed up any data you wish to keep before proceeding!!!"
TERRAFORM_REMOVE_TERRAFORM_STAGE_FILES_CONFIRMATION = (
    "Nebari needs to generate an updated set of Terraform scripts for your deployment and delete the old scripts.\n"
    "Do you want Nebari to remove your [green]stages[/green] directory automatically for you? It will be recreated the next time Nebari is run.\n"
    "[red]Warning:[/red] This will remove everything in the [green]stages[/green] directory.\n"
    "If you do not have Nebari do it automatically here, you will need to remove the [green]stages[/green] manually with a command"
    "like [green]rm -rf stages[/green]."
)
DESTROY_STAGE_FILES_WITH_TF_STATE_NOT_REMOTE = (
    "⚠️ CAUTION ⚠️\n"
    "Nebari would like to remove your old Terraform/Opentofu [green]stages[/green] files. Your [blue]terraform_state[/blue] configuration is not set to [blue]remote[/blue], so destroying your [green]stages[/green] files could potentially be very detructive.\n"
    "If you don't have active Terraform/Opentofu deployment state files contained within your [green]stages[/green] directory, you may proceed by entering [red]y[/red] at the prompt."
    "If you have an active Terraform/Opentofu deployment with active state files in your [green]stages[/green] folder, you will need to either bring Nebari down temporarily to redeploy or pursue some other means to upgrade. Enter [red]n[/red] at the prompt.\n\n"
    "Do you want to proceed by deleting your [green]stages[/green] directory and everything in it? ([red]POTENTIALLY VERY DESTRUCTIVE[/red])"
)


def do_upgrade(config_filename, attempt_fixes=False):
    """
    Perform an upgrade of the Nebari configuration file.

    This function loads the YAML configuration file, checks for deprecated keys,
    validates the current version, and if necessary, upgrades the configuration
    to the latest version of Nebari.

    Args:
    config_filename (str): The path to the configuration file.
    attempt_fixes (bool): Whether to attempt automatic fixes for validation errors.

    Returns:
    None
    """
    config = load_yaml(config_filename)
    if config.get("qhub_version"):
        rich.print(
            f"Your config file [purple]{config_filename}[/purple] uses the deprecated qhub_version key.  Please change qhub_version to nebari_version and re-run the upgrade command."
        )
        return

    try:
        from nebari.plugins import nebari_plugin_manager

        nebari_plugin_manager.read_config(config_filename)
        rich.print(
            f"Your config file [purple]{config_filename}[/purple] appears to be already up-to-date for Nebari version [green]{__version__}[/green]"
        )
        return
    except (ValidationError, ValueError) as e:
        if is_version_accepted(config.get("nebari_version", "")):
            # There is an unrelated validation problem
            rich.print(
                f"Your config file [purple]{config_filename}[/purple] appears to be already up-to-date for Nebari version [green]{__version__}[/green] but there is another validation error.\n"
            )
            raise e

    start_version = config.get("nebari_version", "")

    UpgradeStep.upgrade(
        config, start_version, __version__, config_filename, attempt_fixes
    )

    # Backup old file
    backup_configuration(config_filename, f".{start_version or 'old'}")

    with config_filename.open("wt") as f:
        yaml.dump(config, f)

    rich.print(
        f"Saving new config file [purple]{config_filename}[/purple] ready for Nebari version [green]{__version__}[/green]"
    )

    ci_cd = config.get("ci_cd", {}).get("type", "")
    if ci_cd in ("github-actions", "gitlab-ci"):
        rich.print(
            f"\nSince you are using ci_cd [green]{ci_cd}[/green] you also need to re-render the workflows and re-commit the files to your Git repo:\n"
            f"   nebari render -c [purple]{config_filename}[/purple]\n"
        )


class UpgradeStep(ABC):
    """
    Abstract base class representing an upgrade step.

    Attributes:
        _steps (ClassVar[Dict[str, Any]]): Class variable holding registered upgrade steps.
        version (ClassVar[str]): The version of the upgrade step.
    """

    _steps: ClassVar[Dict[str, Any]] = {}
    version: ClassVar[str] = ""

    def __init_subclass__(cls):
        """
        Initializes a subclass of UpgradeStep.

        This method validates the version string and registers the subclass
        in the _steps dictionary.
        """
        try:
            parsed_version = Version(cls.version)
        except ValueError as exc:
            raise ValueError(f"Invalid version string {cls.version}") from exc

        cls.parsed_version = parsed_version
        assert (
            rounded_ver_parse(cls.version) == parsed_version
        ), f"Invalid version {cls.version}: must be a full release version, not a dev/prerelease/postrelease version"
        assert (
            cls.version not in cls._steps
        ), f"Duplicate UpgradeStep version {cls.version}"
        cls._steps[cls.version] = cls

    @classmethod
    def clear_steps_registry(cls):
        """Clears the steps registry. Useful for testing."""
        cls._steps.clear()

    @classmethod
    def has_step(cls, version):
        """
        Checks if there is an upgrade step for a given version.

        Args:
            version (str): The version to check.

        Returns:
            bool: True if the step exists, False otherwise.
        """
        return version in cls._steps

    @classmethod
    def upgrade(
        cls, config, start_version, finish_version, config_filename, attempt_fixes=False
    ):
        """
        Runs through all required upgrade steps (i.e. relevant subclasses of UpgradeStep).
        Calls UpgradeStep.upgrade_step for each.

        Args:
            config (dict): The current configuration dictionary.
            start_version (str): The starting version of the configuration.
            finish_version (str): The target version for the configuration.
            config_filename (str): The path to the configuration file.
            attempt_fixes (bool): Whether to attempt automatic fixes for validation errors.

        Returns:
            dict: The updated configuration dictionary.
        """
        starting_ver = rounded_ver_parse(start_version or "0.0.0")
        finish_ver = rounded_ver_parse(finish_version)

        if finish_ver < starting_ver:
            raise ValueError(
                f"Your nebari-config.yaml already belongs to a later version ({start_version}) than the installed version of Nebari ({finish_version}).\n"
                "You should upgrade the installed nebari package (e.g. pip install --upgrade nebari) to work with your deployment."
            )

        step_versions = sorted(
            [
                v
                for v in cls._steps.keys()
                if rounded_ver_parse(v) > starting_ver
                and rounded_ver_parse(v) <= finish_ver
            ],
            key=rounded_ver_parse,
        )

        current_start_version = start_version
        for stepcls in [cls._steps[str(v)] for v in step_versions]:
            step = stepcls()
            config = step.upgrade_step(
                config,
                current_start_version,
                config_filename,
                attempt_fixes=attempt_fixes,
            )
            current_start_version = step.get_version()
            print("\n")

        return config

    @classmethod
    def _rm_rf_stages(cls, config_filename, dry_run: bool = False, verbose=False):
        """
        Remove stage files during and upgrade step

        Usually used when you need files in your `stages` directory to be
        removed in order to avoid resource conflicts

        Args:
            config_filename (str): The path to the configuration file.
        Returns:
            None
        """
        config_dir = Path(config_filename).resolve().parent

        if Path.is_dir(config_dir):
            stage_dir = config_dir / "stages"

            stage_filenames = [d for d in stage_dir.rglob("*") if d.is_file()]

            for stage_filename in stage_filenames:
                if dry_run and verbose:
                    rich.print(f"Dry run: Would remove {stage_filename}")
                else:
                    stage_filename.unlink(missing_ok=True)
                    if verbose:
                        rich.print(f"Removed {stage_filename}")

            stage_filedirs = sorted(
                (d for d in stage_dir.rglob("*") if d.is_dir()),
                reverse=True,
            )

            for stage_filedir in stage_filedirs:
                if dry_run and verbose:
                    rich.print(f"Dry run: Would remove {stage_filedir}")
                else:
                    stage_filedir.rmdir()
                    if verbose:
                        rich.print(f"Removed {stage_filedir}")

            if dry_run and verbose:
                rich.print(f"Dry run: Would remove {stage_dir}")
            elif stage_dir.is_dir():
                stage_dir.rmdir()
                if verbose:
                    rich.print(f"Removed {stage_dir}")

    def get_version(self):
        """
        Returns:
            str: The version of the upgrade step.
        """
        return self.version

    def requires_nebari_version_field(self):
        """
        Checks if the nebari_version field is required for this upgrade step.

        Returns:
            bool: True if the nebari_version field is required, False otherwise.
        """
        return rounded_ver_parse(self.version) > rounded_ver_parse("0.3.13")

    def upgrade_step(self, config, start_version, config_filename, *args, **kwargs):
        """
        Perform the upgrade from start_version to self.version.

        Generally, this will be in-place in config, but must also return config dict.

        config_filename may be useful to understand the file path for nebari-config.yaml, for example
        to output another file in the same location.

        The standard body here will take care of setting nebari_version and also updating the image tags.

        It should normally be left as-is for all upgrades. Use _version_specific_upgrade below
        for any actions that are only required for the particular upgrade you are creating.

        Args:
            config (dict): The current configuration dictionary.
            start_version (str): The starting version of the configuration.
            config_filename (str): The path to the configuration file.

        Returns:
            dict: The updated configuration dictionary.
        """
        finish_version = self.get_version()
        __rounded_finish_version__ = str(rounded_ver_parse(finish_version))
        rich.print(
            f"\n---> Starting upgrade from [green]{start_version or 'old version'}[/green] to [green]{finish_version}[/green]\n"
        )

        # Set the new version
        if start_version == "":
            assert "nebari_version" not in config
        assert self.version != start_version

        if self.requires_nebari_version_field():
            rich.print(f"Setting nebari_version to [green]{self.version}[/green]")
            config["nebari_version"] = self.version

        def contains_image_and_tag(s: str) -> bool:
            """
            Check if the string matches the Nebari image pattern.

            Args:
                s (str): The string to check.

            Returns:
                bool: True if the string matches the pattern, False otherwise.
            """
            pattern = r"^quay\.io\/nebari\/nebari-(jupyterhub|jupyterlab|dask-worker)(-gpu)?:\d{4}\.\d+\.\d+$"
            return bool(re.match(pattern, s))

        def replace_image_tag_legacy(
            image: str, start_version: str, new_version: str
        ) -> str:
            """
            Replace legacy image tags with the new version.

            Args:
                image (str): The current image string.
                start_version (str): The starting version of the image.
                new_version (str): The new version to replace with.

            Returns:
                str: The updated image string with the new version, or None if no match.
            """
            start_version_regex = start_version.replace(".", "\\.")
            if not start_version:
                start_version_regex = "0\\.[0-3]\\.[0-9]{1,2}"

            docker_image_regex = re.compile(
                f"^([A-Za-z0-9_-]+/[A-Za-z0-9_-]+):v{start_version_regex}$"
            )

            m = docker_image_regex.match(image)
            if m:
                return ":".join([m.groups()[0], f"v{new_version}"])
            return None

        def replace_image_tag(
            s: str, new_version: str, config_path: str, attempt_fixes: bool
        ) -> str:
            """
            Replace the image tag with the new version.

            Args:
                s (str): The current image string.
                new_version (str): The new version to replace with.
                config_path (str): The path to the configuration file.

            Returns:
                str: The updated image string with the new version, or the original string if no changes.
            """
            legacy_replacement = replace_image_tag_legacy(s, start_version, new_version)
            if legacy_replacement:
                return legacy_replacement

            if not contains_image_and_tag(s):
                return s
            image_name, current_tag = s.split(":")
            if current_tag == new_version:
                return s
            loc = f"{config_path}: {image_name}"
            response = attempt_fixes or Confirm.ask(
                f"\nDo you want to replace current tag [green]{current_tag}[/green] with [green]{new_version}[/green] for:\n[purple]{loc}[/purple]?",
                default=True,
            )
            if response:
                return s.replace(current_tag, new_version)
            else:
                return s

        def set_nested_item(config: dict, config_path: list, value: str):
            """
            Set a nested item in the configuration dictionary.

            Args:
                config (dict): The configuration dictionary.
                config_path (list): The path to the item to set.
                value (str): The value to set.

            Returns:
                None
            """
            config_path = config_path.split(".")
            for k in config_path[:-1]:
                try:
                    k = int(k)
                except ValueError:
                    pass
                config = config[k]
            try:
                config_path[-1] = int(config_path[-1])
            except ValueError:
                pass
            config[config_path[-1]] = value

        def update_image_tag(
            config: dict,
            config_path: str,
            current_image: str,
            new_version: str,
            attempt_fixes: bool,
        ) -> dict:
            """
            Update the image tag in the configuration.

            Args:
                config (dict): The configuration dictionary.
                config_path (str): The path to the item to update.
                current_image (str): The current image string.
                new_version (str): The new version to replace with.

            Returns:
                dict: The updated configuration dictionary.
            """
            new_image = replace_image_tag(
                current_image,
                new_version,
                config_path,
                attempt_fixes,
            )
            if new_image != current_image:
                set_nested_item(config, config_path, new_image)

            return config

        # update default_images
        for k, v in config.get("default_images", {}).items():
            config_path = f"default_images.{k}"
            config = update_image_tag(
                config,
                config_path,
                v,
                __rounded_finish_version__,
                kwargs.get("attempt_fixes", False),
            )

        # update profiles.jupyterlab images
        for i, v in enumerate(config.get("profiles", {}).get("jupyterlab", [])):
            current_image = v.get("kubespawner_override", {}).get("image", None)
            if current_image:
                config = update_image_tag(
                    config,
                    f"profiles.jupyterlab.{i}.kubespawner_override.image",
                    current_image,
                    __rounded_finish_version__,
                    kwargs.get("attempt_fixes", False),
                )

        # update profiles.dask_worker images
        for k, v in config.get("profiles", {}).get("dask_worker", {}).items():
            current_image = v.get("image", None)
            if current_image:
                config = update_image_tag(
                    config,
                    f"profiles.dask_worker.{k}.image",
                    current_image,
                    __rounded_finish_version__,
                    kwargs.get("attempt_fixes", False),
                )

        # Run any version-specific tasks
        return self._version_specific_upgrade(
            config,
            start_version,
            config_filename,
            *args,
            **kwargs,
        )

    def _version_specific_upgrade(
        self, config, start_version, config_filename, *args, **kwargs
    ):
        """
        Perform version-specific upgrade tasks.

        Override this method in subclasses if you need to do anything specific to your version.

        Args:
            config (dict): The current configuration dictionary.
            start_version (str): The starting version of the configuration.
            config_filename (str): The path to the configuration file.

        Returns:
            dict: The updated configuration dictionary.
        """
        return config


class Upgrade_0_3_12(UpgradeStep):
    version = "0.3.12"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename, *args, **kwargs
    ):
        """
        This version of Nebari requires a conda_store image for the first time.
        """
        if config.get("default_images", {}).get("conda_store", None) is None:
            newimage = "quansight/conda-store-server:v0.3.3"
            rich.print(
                f"Adding default_images: conda_store image as [green]{newimage}[/green]"
            )
            if "default_images" not in config:
                config["default_images"] = {}
            config["default_images"]["conda_store"] = newimage
        return config


class Upgrade_0_4_0(UpgradeStep):
    version = "0.4.0"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        """
        This version of Nebari introduces Keycloak for authentication, removes deprecated fields,
        and generates a default password for the Keycloak root user.
        """
        security = config.get("security", {})
        users = security.get("users", {})
        groups = security.get("groups", {})

        # Custom Authenticators are no longer allowed
        if (
            config.get("security", {}).get("authentication", {}).get("type", "")
            == "custom"
        ):
            customauth_warning = (
                f"Custom Authenticators are no longer supported in {self.version} because Keycloak "
                "manages all authentication.\nYou need to find a way to support your authentication "
                "requirements within Keycloak."
            )
            if not kwargs.get("attempt_fixes", False):
                raise ValueError(
                    f"{customauth_warning}\n\nRun `nebari upgrade --attempt-fixes` to switch to basic Keycloak authentication instead."
                )
            else:
                rich.print(f"\nWARNING: {customauth_warning}")
                rich.print(
                    "\nSwitching to basic Keycloak authentication instead since you specified --attempt-fixes."
                )
                config["security"]["authentication"] = {"type": "password"}

        # Create a group/user import file for Keycloak

        realm_import_filename = config_filename.parent / "nebari-users-import.json"

        realm = {"id": "nebari", "realm": "nebari"}
        realm["users"] = [
            {
                "username": k,
                "enabled": True,
                "groups": sorted(
                    list(
                        (
                            {v.get("primary_group", "")}
                            | set(v.get("secondary_groups", []))
                        )
                        - {""}
                    )
                ),
            }
            for k, v in users.items()
        ]
        realm["groups"] = [
            {"name": k, "path": f"/{k}"}
            for k, v in groups.items()
            if k not in {"users", "admin"}
        ]

        backup_configuration(realm_import_filename)

        with realm_import_filename.open("wt") as f:
            json.dump(realm, f, indent=2)

        rich.print(
            f"\nSaving user/group import file [purple]{realm_import_filename}[/purple].\n\n"
            "ACTION REQUIRED: You must import this file into the Keycloak admin webpage after you redeploy Nebari.\n"
            "Visit the URL path /auth/ and login as 'root'. Under Manage, click Import and select this file.\n\n"
            "Non-admin users will default to analyst group membership after the upgrade (no dask access), "
            "so you may wish to promote some users into the developer group.\n"
        )

        if "users" in security:
            del security["users"]
        if "groups" in security:
            if "users" in security["groups"]:
                # Ensure the users default group is added to Keycloak
                security["shared_users_group"] = True
            del security["groups"]

        if "terraform_modules" in config:
            del config["terraform_modules"]
            rich.print(
                "Removing terraform_modules field from config as it is no longer used.\n"
            )

        if "default_images" not in config:
            config["default_images"] = {}

        # Remove conda_store image from default_images
        if "conda_store" in config["default_images"]:
            del config["default_images"]["conda_store"]

        # Remove dask_gateway image from default_images
        if "dask_gateway" in config["default_images"]:
            del config["default_images"]["dask_gateway"]

        # Create root password
        default_password = "".join(
            secrets.choice(string.ascii_letters + string.digits) for i in range(16)
        )
        security.setdefault("keycloak", {})["initial_root_password"] = default_password

        rich.print(
            f"Generated default random password=[green]{default_password}[/green] for Keycloak root user (Please change at /auth/ URL path).\n"
        )

        # project was never needed in Azure - it remained as PLACEHOLDER in earlier nebari inits!
        if "azure" in config:
            if "project" in config["azure"]:
                del config["azure"]["project"]

        # "oauth_callback_url" and "scope" not required in nebari-config.yaml
        # for Auth0 and Github authentication
        auth_config = config["security"]["authentication"].get("config", None)
        if auth_config:
            if "oauth_callback_url" in auth_config:
                del auth_config["oauth_callback_url"]
            if "scope" in auth_config:
                del auth_config["scope"]

        # It is not safe to immediately redeploy without backing up data ready to restore data
        # since a new cluster will be created for the new version.
        # Setting the following flag will prevent deployment and display guidance to the user
        # which they can override if they are happy they understand the situation.
        config["prevent_deploy"] = True

        return config


class Upgrade_0_4_1(UpgradeStep):
    version = "0.4.1"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        """
        Upgrade jupyterlab profiles.
        """
        rich.print("\nUpgrading jupyterlab profiles in order to specify access type:\n")

        profiles_jupyterlab = config.get("profiles", {}).get("jupyterlab", [])
        for profile in profiles_jupyterlab:
            name = profile.get("display_name", "")

            if "groups" in profile or "users" in profile:
                profile["access"] = "yaml"
            else:
                profile["access"] = "all"

            rich.print(
                f"Setting access type of JupyterLab profile [green]{name}[/green] to [green]{profile['access']}[/green]"
            )
        return config


class Upgrade_2023_4_2(UpgradeStep):
    version = "2023.4.2"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        """
        Prompt users to delete Argo CRDs
        """
        argo_crds = [
            "clusterworkflowtemplates.argoproj.io",
            "cronworkflows.argoproj.io",
            "workfloweventbindings.argoproj.io",
            "workflows.argoproj.io",
            "workflowtasksets.argoproj.io",
            "workflowtemplates.argoproj.io",
        ]

        argo_sa = ["argo-admin", "argo-dev", "argo-view"]

        namespace = config.get("namespace", "default")

        if kwargs.get("attempt_fixes", False):
            try:
                kubernetes.config.load_kube_config()
            except kubernetes.config.config_exception.ConfigException:
                rich.print(
                    "[red bold]No default kube configuration file was found. Make sure to [link=https://www.nebari.dev/docs/how-tos/debug-nebari#generating-the-kubeconfig]have one pointing to your Nebari cluster[/link] before upgrading.[/red bold]"
                )
                exit()

            for crd in argo_crds:
                api_instance = kubernetes.client.ApiextensionsV1Api()
                try:
                    api_instance.delete_custom_resource_definition(
                        name=crd,
                    )
                except kubernetes.client.exceptions.ApiException as e:
                    if e.status == 404:
                        rich.print(f"CRD [yellow]{crd}[/yellow] not found. Ignoring.")
                    else:
                        raise e
                else:
                    rich.print(f"Successfully removed CRD [green]{crd}[/green]")

            for sa in argo_sa:
                api_instance = kubernetes.client.CoreV1Api()
                try:
                    api_instance.delete_namespaced_service_account(
                        sa,
                        namespace,
                    )
                except kubernetes.client.exceptions.ApiException as e:
                    if e.status == 404:
                        rich.print(
                            f"Service account [yellow]{sa}[/yellow] not found. Ignoring."
                        )
                    else:
                        raise e
                else:
                    rich.print(
                        f"Successfully removed service account [green]{sa}[/green]"
                    )
        else:
            kubectl_delete_argo_crds_cmd = " ".join(
                (
                    *("kubectl delete crds",),
                    *argo_crds,
                ),
            )
            kubectl_delete_argo_sa_cmd = " ".join(
                (
                    *(
                        "kubectl delete sa",
                        f"-n {namespace}",
                    ),
                    *argo_sa,
                ),
            )
            rich.print(
                f"\n\n[bold cyan]Note:[/] Upgrading requires a one-time manual deletion of the Argo Workflows Custom Resource Definitions (CRDs) and service accounts. \n\n[red bold]"
                f"Warning:  [link=https://{config['domain']}/argo/workflows]Workflows[/link] and [link=https://{config['domain']}/argo/workflows]CronWorkflows[/link] created before deleting the CRDs will be erased when the CRDs are deleted and will not be restored.[/red bold] \n\n"
                f"The updated CRDs will be installed during the next [cyan bold]nebari deploy[/cyan bold] step. Argo Workflows will not function after deleting the CRDs until the updated CRDs and service accounts are installed in the next nebari deploy. "
                f"You must delete the Argo Workflows CRDs and service accounts before upgrading to {self.version} (or later) or the deploy step will fail.  "
                f"Please delete them before proceeding by generating a kubeconfig (see [link=https://www.nebari.dev/docs/how-tos/debug-nebari/#generating-the-kubeconfig]docs[/link]), installing kubectl (see [link=https://www.nebari.dev/docs/how-tos/debug-nebari#installing-kubectl]docs[/link]), and running the following two commands:\n\n\t[cyan bold]{kubectl_delete_argo_crds_cmd} [/cyan bold]\n\n\t[cyan bold]{kubectl_delete_argo_sa_cmd} [/cyan bold]"
            )

            continue_ = Confirm.ask(
                "Have you deleted the Argo Workflows CRDs and service accounts?",
                default=False,
            )
            if not continue_:
                rich.print(
                    f"You must delete the Argo Workflows CRDs and service accounts before upgrading to [green]{self.version}[/green] (or later)."
                )
                exit()

        return config


class Upgrade_2023_7_1(UpgradeStep):
    version = "2023.7.1"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        provider = config["provider"]
        if provider == ProviderEnum.aws.value:
            rich.print("\n ⚠️  DANGER ⚠️")
            rich.print(
                DESTRUCTIVE_UPGRADE_WARNING,
                "The 'prevent_deploy' flag has been set in your config file and must be manually removed to deploy.",
            )
            config["prevent_deploy"] = True

        return config


class Upgrade_2023_7_2(UpgradeStep):
    version = "2023.7.2"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        argo = config.get("argo_workflows", {})
        if argo.get("enabled"):
            response = kwargs.get("attempt_fixes", False) or Confirm.ask(
                f"\nDo you want to enable the [green][link={NEBARI_WORKFLOW_CONTROLLER_DOCS}]Nebari Workflow Controller[/link][/green], required for [green][link={ARGO_JUPYTER_SCHEDULER_REPO}]Argo-Jupyter-Scheduler[/link][green]?",
                default=True,
            )
            if response:
                argo["nebari_workflow_controller"] = {"enabled": True}

        rich.print("\n ⚠️ Deprecation Warnings ⚠️")
        rich.print(
            f"-> [green]{self.version}[/green] is the last Nebari version that supports CDS Dashboards"
        )

        return config


class Upgrade_2023_10_1(UpgradeStep):
    """
    Upgrade step for Nebari version 2023.10.1

    Note:
        Upgrading to 2023.10.1 is considered high-risk because it includes a major refactor
        to introduce the extension mechanism system. This version introduces significant
        changes, including the support for third-party plugins, upgrades JupyterHub to version 3.1,
        and deprecates certain components such as CDS Dashboards, ClearML, Prefect, and kbatch.
    """

    version = "2023.10.1"
    # JupyterHub Helm chart 2.0.0 (app version 3.0.0) requires K8S Version >=1.23. (reference: https://z2jh.jupyter.org/en/stable/)
    # This released has been tested against 1.26
    min_k8s_version = 1.26

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        # Upgrading to 2023.10.1 is considered high-risk because it includes a major refacto
        # to introduce the extension mechanism system.
        rich.print("\n ⚠️  Warning ⚠️")
        rich.print(
            f"-> Nebari version [green]{self.version}[/green] includes a major refactor to introduce an extension mechanism that supports the development of third-party plugins."
        )
        rich.print(
            "-> Data should be backed up before performing this upgrade ([green][link=https://www.nebari.dev/docs/how-tos/manual-backup]see docs[/link][/green])  The 'prevent_deploy' flag has been set in your config file and must be manually removed to deploy."
        )

        # Setting the following flag will prevent deployment and display guidance to the user
        # which they can override if they are happy they understand the situation.
        config["prevent_deploy"] = True

        # Nebari version 2023.10.1 upgrades JupyterHub to 3.1.  CDS Dashboards are only compatible with
        # JupyterHub versions 1.X and so will be removed during upgrade.
        rich.print("\n ⚠️  Deprecation Warning ⚠️")
        rich.print(
            f"-> CDS dashboards are no longer supported in Nebari version [green]{self.version}[/green] and will be uninstalled."
        )
        if config.get("cdsdashboards"):
            rich.print("-> Removing cdsdashboards from config file.")
            del config["cdsdashboards"]

        # Deprecation Warning - ClearML, Prefect, kbatch
        rich.print("\n ⚠️  Deprecation Warning ⚠️")
        rich.print(
            "-> We will be removing and ending support for ClearML, Prefect and kbatch in the next release. The kbatch has been functionally replaced by Argo-Jupyter-Scheduler. We have seen little interest in ClearML and Prefect in recent years, and removing makes sense at this point. However if you wish to continue using them with Nebari we encourage you to [green][link=https://www.nebari.dev/docs/how-tos/nebari-extension-system/#developing-an-extension]write your own Nebari extension[/link][/green]."
        )

        # Kubernetes version check
        # JupyterHub Helm chart 2.0.0 (app version 3.0.0) requires K8S Version >=1.23. (reference: https://z2jh.jupyter.org/en/stable/)

        provider = config["provider"]
        provider_config_block = get_provider_config_block_name(provider)

        # Get current Kubernetes version if available in config.
        current_version = config.get(provider_config_block, {}).get(
            "kubernetes_version", None
        )

        # Convert to decimal prefix
        if provider in ["aws", "azure", "gcp", "do"]:
            current_version = get_k8s_version_prefix(current_version)

        # Try to convert known Kubernetes versions to float.
        if current_version is not None:
            try:
                current_version = float(current_version)
            except ValueError:
                current_version = None

        # Handle checks for when Kubernetes version should be detectable
        if provider in ["aws", "azure", "gcp", "do"]:
            # Kubernetes version not found in provider block
            if current_version is None:
                rich.print("\n ⚠️  Warning ⚠️")
                rich.print(
                    f"-> Unable to detect Kubernetes version for provider {provider}.  Nebari version [green]{self.version}[/green] requires Kubernetes version {str(self.min_k8s_version)}.  Please confirm your Kubernetes version is configured before upgrading."
                )

            # Kubernetes version less than required minimum
            if (
                isinstance(current_version, float)
                and current_version < self.min_k8s_version
            ):
                rich.print("\n ⚠️  Warning ⚠️")
                rich.print(
                    f"-> Nebari version [green]{self.version}[/green] requires Kubernetes version {str(self.min_k8s_version)}.  Your configured Kubernetes version is [red]{current_version}[/red]. {UPGRADE_KUBERNETES_MESSAGE}"
                )
                version_diff = round(self.min_k8s_version - current_version, 2)
                if version_diff > 0.01:
                    rich.print(
                        "-> The Kubernetes version is multiple minor versions behind the minimum required version. You will need to perform the upgrade one minor version at a time.  For example, if your current version is 1.24, you will need to upgrade to 1.25, and then 1.26."
                    )
                rich.print(
                    f"-> Update the value of [green]{provider_config_block}.kubernetes_version[/green] in your config file to a newer version of Kubernetes and redeploy."
                )

        else:
            rich.print("\n ⚠️  Warning ⚠️")
            rich.print(
                f"-> Unable to detect Kubernetes version for provider {provider}.  Nebari version [green]{self.version}[/green] requires Kubernetes version {str(self.min_k8s_version)} or greater."
            )
            rich.print(
                "-> Please ensure your Kubernetes version is up-to-date before proceeding."
            )

        if provider == "aws":
            rich.print("\n ⚠️  DANGER ⚠️")
            rich.print(DESTRUCTIVE_UPGRADE_WARNING)

        if kwargs.get("attempt_fixes", False) or Confirm.ask(
            TERRAFORM_REMOVE_TERRAFORM_STAGE_FILES_CONFIRMATION,
            default=False,
        ):
            if (
                (_terraform_state_config := config.get("terraform_state"))
                and (_terraform_state_config.get("type") != "remote")
                and not Confirm.ask(
                    DESTROY_STAGE_FILES_WITH_TF_STATE_NOT_REMOTE,
                    default=False,
                )
            ):
                exit()

            self._rm_rf_stages(
                config_filename,
                dry_run=kwargs.get("dry_run", False),
                verbose=True,
            )

        return config


class Upgrade_2023_11_1(UpgradeStep):
    """
    Upgrade step for Nebari version 2023.11.1

    Note:
        - ClearML, Prefect, and kbatch are no longer supported in this version.
    """

    version = "2023.11.1"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        rich.print("\n ⚠️  Deprecation Warning ⚠️")
        rich.print(
            f"-> ClearML, Prefect and kbatch are no longer supported in Nebari version [green]{self.version}[/green] and will be uninstalled."
        )

        if kwargs.get("attempt_fixes", False) or Confirm.ask(
            TERRAFORM_REMOVE_TERRAFORM_STAGE_FILES_CONFIRMATION,
            default=False,
        ):
            if (
                (_terraform_state_config := config.get("terraform_state"))
                and (_terraform_state_config.get("type") != "remote")
                and not Confirm.ask(
                    DESTROY_STAGE_FILES_WITH_TF_STATE_NOT_REMOTE,
                    default=False,
                )
            ):
                exit()

            self._rm_rf_stages(
                config_filename,
                dry_run=kwargs.get("dry_run", False),
                verbose=True,
            )

        return config


class Upgrade_2023_12_1(UpgradeStep):
    """
    Upgrade step for Nebari version 2023.12.1

    Note:
        - This is the last version that supports the jupyterlab-videochat extension.
    """

    version = "2023.12.1"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        rich.print("\n ⚠️  Deprecation Warning ⚠️")
        rich.print(
            f"-> [green]{self.version}[/green] is the last Nebari version that supports the jupyterlab-videochat extension."
        )
        rich.print()

        if kwargs.get("attempt_fixes", False) or Confirm.ask(
            TERRAFORM_REMOVE_TERRAFORM_STAGE_FILES_CONFIRMATION,
            default=False,
        ):
            if (
                (_terraform_state_config := config.get("terraform_state"))
                and (_terraform_state_config.get("type") != "remote")
                and not Confirm.ask(
                    DESTROY_STAGE_FILES_WITH_TF_STATE_NOT_REMOTE,
                    default=False,
                )
            ):
                exit()

            self._rm_rf_stages(
                config_filename,
                dry_run=kwargs.get("dry_run", False),
                verbose=True,
            )

        return config


class Upgrade_2024_1_1(UpgradeStep):
    """
    Upgrade step for Nebari version 2024.1.1

    Note:
        - jupyterlab-videochat, retrolab, jupyter-tensorboard, jupyterlab-conda-store, and jupyter-nvdashboard are no longer supported.
    """

    version = "2024.1.1"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        rich.print("\n ⚠️  Deprecation Warning ⚠️")
        rich.print(
            "-> jupyterlab-videochat, retrolab, jupyter-tensorboard, jupyterlab-conda-store and jupyter-nvdashboard",
            f"are no longer supported in Nebari version [green]{self.version}[/green] and will be uninstalled.",
        )
        rich.print()

        if kwargs.get("attempt_fixes", False) or Confirm.ask(
            TERRAFORM_REMOVE_TERRAFORM_STAGE_FILES_CONFIRMATION,
            default=False,
        ):
            if (
                (_terraform_state_config := config.get("terraform_state"))
                and (_terraform_state_config.get("type") != "remote")
                and not Confirm.ask(
                    DESTROY_STAGE_FILES_WITH_TF_STATE_NOT_REMOTE,
                    default=False,
                )
            ):
                exit()

            self._rm_rf_stages(
                config_filename,
                dry_run=kwargs.get("dry_run", False),
                verbose=True,
            )

        return config


class Upgrade_2024_3_1(UpgradeStep):
    version = "2024.3.1"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        rich.print("Ready to upgrade to Nebari version [green]2024.3.1[/green].")

        return config


class Upgrade_2024_3_2(UpgradeStep):
    version = "2024.3.2"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        rich.print("Ready to upgrade to Nebari version [green]2024.3.2[/green].")

        return config


class Upgrade_2024_3_3(UpgradeStep):
    version = "2024.3.3"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        rich.print("Ready to upgrade to Nebari version [green]2024.3.3[/green].")

        return config


class Upgrade_2024_4_1(UpgradeStep):
    """
    Upgrade step for Nebari version 2024.4.1

    Note:
        - Adds default configuration for node groups if not already defined.
    """

    version = "2024.4.1"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        # Default configuration for the node groups was added in this version. Therefore,
        # users upgrading who don't have any specific node groups defined on their config
        # file already, will be prompted and asked whether they want to include the default
        if provider := config.get("provider", ""):
            provider_full_name = provider_enum_name_map[provider]
            if provider_full_name in config and "node_groups" not in config.get(
                provider_full_name, {}
            ):
                try:
                    default_node_groups = schema.provider_enum_default_node_groups_map[
                        provider
                    ]
                    continue_ = kwargs.get("attempt_fixes", False) or Confirm.ask(
                        f"Would you like to include the default configuration for the node groups in [purple]{config_filename}[/purple]?",
                        default=False,
                    )
                    if continue_:
                        config[provider_full_name]["node_groups"] = default_node_groups
                except KeyError:
                    pass

        return config


class Upgrade_2024_5_1(UpgradeStep):
    version = "2024.5.1"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        rich.print("Ready to upgrade to Nebari version [green]2024.5.1[/green].")

        return config


class Upgrade_2024_6_1(UpgradeStep):
    """
    Upgrade step for version 2024.6.1

    This upgrade includes:
    - Manual updates for kube-prometheus-stack CRDs if monitoring is enabled.
    - Prompts to upgrade GCP node groups to more cost-efficient instances.
    """

    version = "2024.6.1"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        # Prompt users to manually update kube-prometheus-stack CRDs if monitoring is enabled
        if config.get("monitoring", {}).get("enabled", True):
            crd_urls = [
                "https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/v0.73.0/example/prometheus-operator-crd/monitoring.coreos.com_alertmanagerconfigs.yaml",
                "https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/v0.73.0/example/prometheus-operator-crd/monitoring.coreos.com_alertmanagers.yaml",
                "https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/v0.73.0/example/prometheus-operator-crd/monitoring.coreos.com_podmonitors.yaml",
                "https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/v0.73.0/example/prometheus-operator-crd/monitoring.coreos.com_probes.yaml",
                "https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/v0.73.0/example/prometheus-operator-crd/monitoring.coreos.com_prometheusagents.yaml",
                "https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/v0.73.0/example/prometheus-operator-crd/monitoring.coreos.com_prometheuses.yaml",
                "https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/v0.73.0/example/prometheus-operator-crd/monitoring.coreos.com_prometheusrules.yaml",
                "https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/v0.73.0/example/prometheus-operator-crd/monitoring.coreos.com_scrapeconfigs.yaml",
                "https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/v0.73.0/example/prometheus-operator-crd/monitoring.coreos.com_servicemonitors.yaml",
                "https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/v0.73.0/example/prometheus-operator-crd/monitoring.coreos.com_thanosrulers.yaml",
            ]
            daemonset_name = "prometheus-node-exporter"
            namespace = config.get("namespace", "default")

            # We're upgrading from version 30.1.0 to 58.4.0. This is a major upgrade and requires manual intervention.
            # See https://github.com/prometheus-community/helm-charts/blob/main/charts/kube-prometheus-stack/README.md#upgrading-chart
            # for more information on why the following commands are necessary.
            commands = "[cyan bold]"
            for url in crd_urls:
                commands += f"kubectl apply --server-side --force-conflicts -f {url}\n"
            commands += f"kubectl delete daemonset -l app={daemonset_name} --namespace {namespace}\n"
            commands += "[/cyan bold]"

            rich.print(
                "\n ⚠️  Warning ⚠️"
                "\n-> [red bold]Nebari version 2024.6.1 comes with a new version of Grafana. Any custom dashboards that you created will be deleted after upgrading Nebari. Make sure to [link=https://grafana.com/docs/grafana/latest/dashboards/share-dashboards-panels/#export-a-dashboard-as-json]export them as JSON[/link] so you can [link=https://grafana.com/docs/grafana/latest/dashboards/build-dashboards/import-dashboards/#import-a-dashboard]import them[/link] again afterwards.[/red bold]"
                f"\n-> [red bold]Before upgrading, kube-prometheus-stack CRDs need to be updated and the {daemonset_name} daemonset needs to be deleted.[/red bold]"
            )
            run_commands = kwargs.get("attempt_fixes", False) or Confirm.ask(
                "\nDo you want Nebari to update the kube-prometheus-stack CRDs and delete the prometheus-node-exporter for you? If not, you'll have to do it manually.",
                default=False,
            )

            # By default, rich wraps lines by splitting them into multiple lines. This is
            # far from ideal, as users copy-pasting the commands will get errors when running them.
            # To avoid this, we use a rich console with a larger width to print the entire commands
            # and let the terminal wrap them if needed.
            console = rich.console.Console(width=220)
            if run_commands:
                try:
                    kubernetes.config.load_kube_config()
                except kubernetes.config.config_exception.ConfigException:
                    rich.print(
                        "[red bold]No default kube configuration file was found. Make sure to [link=https://www.nebari.dev/docs/how-tos/debug-nebari#generating-the-kubeconfig]have one pointing to your Nebari cluster[/link] before upgrading.[/red bold]"
                    )
                    exit()
                current_kube_context = kubernetes.config.list_kube_config_contexts()[1]
                cluster_name = current_kube_context["context"]["cluster"]
                rich.print(
                    f"The following commands will be run for the [cyan bold]{cluster_name}[/cyan bold] cluster"
                )
                _ = kwargs.get("attempt_fixes", False) or Prompt.ask(
                    "Hit enter to show the commands"
                )
                console.print(commands)

                _ = kwargs.get("attempt_fixes", False) or Prompt.ask(
                    "Hit enter to continue"
                )
                # We need to add a special constructor to the yaml loader to handle a specific
                # tag as otherwise the kubernetes API will fail when updating the CRD.
                yaml.constructor.add_constructor(
                    "tag:yaml.org,2002:value", lambda loader, node: node.value
                )
                for url in crd_urls:
                    response = requests.get(url)
                    response.raise_for_status()
                    crd = yaml.load(response.text)
                    crd_name = crd["metadata"]["name"]
                    api_instance = kubernetes.client.ApiextensionsV1Api()
                    try:
                        api_response = api_instance.read_custom_resource_definition(
                            name=crd_name
                        )
                    except kubernetes.client.exceptions.ApiException:
                        api_response = api_instance.create_custom_resource_definition(
                            body=crd
                        )
                    else:
                        api_response = api_instance.patch_custom_resource_definition(
                            name=crd["metadata"]["name"], body=crd
                        )

                api_instance = kubernetes.client.AppsV1Api()
                api_response = api_instance.list_namespaced_daemon_set(
                    namespace=namespace, label_selector=f"app={daemonset_name}"
                )
                if api_response.items:
                    api_instance.delete_namespaced_daemon_set(
                        name=api_response.items[0].metadata.name,
                        namespace=namespace,
                    )

                rich.print(
                    f"The kube-prometheus-stack CRDs have been updated and the {daemonset_name} daemonset has been deleted."
                )
            else:
                rich.print(
                    "[red bold]Before upgrading, you need to manually delete the prometheus-node-exporter daemonset and update the kube-prometheus-stack CRDs. To do that, please run the following commands.[/red bold]"
                )
                _ = Prompt.ask("Hit enter to show the commands")
                console.print(commands)

                _ = Prompt.ask("Hit enter to continue")
                continue_ = Confirm.ask(
                    f"Have you backed up your custom dashboards (if necessary), deleted the {daemonset_name} daemonset and updated the kube-prometheus-stack CRDs?",
                    default=False,
                )
                if not continue_:
                    rich.print(
                        f"[red bold]You must back up your custom dashboards (if necessary), delete the {daemonset_name} daemonset and update the kube-prometheus-stack CRDs before upgrading to [green]{self.version}[/green] (or later).[/bold red]"
                    )
                    exit()

        # Prompt users to upgrade to the new default node groups for GCP
        if (provider := config.get("provider", "")) == ProviderEnum.gcp.value:
            provider_full_name = provider_enum_name_map[provider]
            if not config.get(provider_full_name, {}).get("node_groups", {}):
                try:
                    text = textwrap.dedent(
                        f"""
                        The default node groups for GCP have been changed to cost efficient e2 family nodes reducing the running cost of Nebari on GCP by ~50%.
                        This change will affect your current deployment, and will result in ~15 minutes of downtime during the upgrade step as the node groups are switched out, but shouldn't result in data loss.

                        [red bold]Note: If upgrading to the new node types, the upgrade process will take longer than usual. For this upgrade only, you'll likely see a timeout \
                        error and need to restart the deployment process afterwards in order to upgrade successfully.[/red bold]

                        As always, make sure to backup data before upgrading.  See https://www.nebari.dev/docs/how-tos/manual-backup for more information.

                        Would you like to upgrade to the cost effective node groups [purple]{config_filename}[/purple]?
                        If not, select "N" and the old default node groups will be added to the nebari config file.
                    """
                    )
                    continue_ = kwargs.get("attempt_fixes", False) or Confirm.ask(
                        text,
                        default=True,
                    )
                    if not continue_:
                        config[provider_full_name]["node_groups"] = {
                            "general": {
                                "instance": "n1-standard-8",
                                "min_nodes": 1,
                                "max_nodes": 1,
                            },
                            "user": {
                                "instance": "n1-standard-4",
                                "min_nodes": 0,
                                "max_nodes": 5,
                            },
                            "worker": {
                                "instance": "n1-standard-4",
                                "min_nodes": 0,
                                "max_nodes": 5,
                            },
                        }
                except KeyError:
                    pass
            else:
                text = textwrap.dedent(
                    """
                    The default node groups for GCP have been changed to cost efficient e2 family nodes reducing the running cost of Nebari on GCP by ~50%.
                    Consider upgrading your node group instance types to the new default configuration.

                    Upgrading your general node will result in ~15 minutes of downtime during the upgrade step as the node groups are switched out, but shouldn't result in data loss.

                    As always, make sure to backup data before upgrading.  See https://www.nebari.dev/docs/how-tos/manual-backup for more information.

                    The new default node groups instances are:
                """
                )
                text += json.dumps(
                    {
                        "general": {"instance": "e2-highmem-4"},
                        "user": {"instance": "e2-standard-4"},
                        "worker": {"instance": "e2-standard-4"},
                    },
                    indent=4,
                )
                rich.print(text)
                if not kwargs.get("attempt_fixes", False):
                    _ = Prompt.ask("\n\nHit enter to continue")
        return config


class Upgrade_2024_7_1(UpgradeStep):
    """
    Upgrade step for Nebari version 2024.7.1

    Note:
        - Digital Ocean deprecation warning.
    """

    version = "2024.7.1"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        if config.get("provider", "") == "do":
            rich.print("\n ⚠️  Deprecation Warning ⚠️")
            rich.print(
                "-> Digital Ocean support is currently being deprecated and will be removed in a future release.",
            )
            rich.print("")
        return config


class Upgrade_2024_9_1(UpgradeStep):
    """
    Upgrade step for Nebari version 2024.9.1

    """

    version = "2024.9.1"

    # Nebari version 2024.9.1 has been marked as broken, and will be skipped:
    # https://github.com/nebari-dev/nebari/issues/2798
    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        return config


class Upgrade_2024_11_1(UpgradeStep):
    """
    Upgrade step for Nebari version 2024.11.1
    """

    version = "2024.11.1"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        if config.get("provider", "") == ProviderEnum.azure.value:
            rich.print("\n ⚠️ Upgrade Warning ⚠️")
            rich.print(
                textwrap.dedent(
                    """
                -> Please ensure no users are currently logged in prior to deploying this update.  The node groups will be destroyed and recreated during the deployment process causing a downtime of approximately 15 minutes.

                Due to an upstream issue, Azure Nebari deployments may raise an error when deploying for the first time after this upgrade. Waiting for a few minutes and then re-running `nebari deploy` should resolve the issue.  More info can be found at [green][link=https://github.com/nebari-dev/nebari/issues/2640]issue #2640[/link][/green]."""
                ),
            )
            rich.print("")
        elif config.get("provider", "") == "do":
            rich.print("\n ⚠️  Deprecation Warning ⚠️")
            rich.print(
                "-> Digital Ocean support is currently being deprecated and will be removed in a future release.",
            )
            rich.print("")

        rich.print("\n ⚠️ Upgrade Warning ⚠️")

        text = textwrap.dedent(
            """
            Please ensure no users are currently logged in prior to deploying this
            update.

            This release introduces changes to how group directories are mounted in
            JupyterLab pods.

            Previously, every Keycloak group in the Nebari realm automatically created a
            shared directory at ~/shared/<group-name>, accessible to all group members
            in their JupyterLab pods.

            Moving forward, only groups assigned the JupyterHub client role
            [magenta]allow-group-directory-creation[/magenta] or its affiliated scope
            [magenta]write:shared-mount[/magenta] will have their directories mounted.

            By default, the admin, analyst, and developer groups will have this
            role assigned during the upgrade. For other groups, you'll now need to
            assign this role manually in the Keycloak UI to have their directories
            mounted.

            For more details check our [green][link=https://www.nebari.dev/docs/references/release/]release notes[/link][/green].
            """
        )
        rich.print(text)
        keycloak_admin = None

        # Prompt the user for role assignment (if yes, transforms the response into bool)
        # This needs to be monkeypatched and will be addressed in a future PR. Until then, this causes test failures.
        assign_roles = kwargs.get("attempt_fixes", False) or Confirm.ask(
            "[bold]Would you like Nebari to assign the corresponding role/scopes to all of your current groups automatically?[/bold]",
            default=False,
        )

        if assign_roles:
            # In case this is done with a local deployment
            import urllib3

            urllib3.disable_warnings()

            keycloak_username = os.environ.get("KEYCLOAK_ADMIN_USERNAME", "root")
            keycloak_password = os.environ.get(
                "KEYCLOAK_ADMIN_PASSWORD",
                config["security"]["keycloak"]["initial_root_password"],
            )

            try:
                # Quick test to connect to Keycloak
                keycloak_admin = get_keycloak_admin(
                    server_url=f"https://{config['domain']}/auth/",
                    username=keycloak_username,
                    password=keycloak_password,
                )
            except ValueError as e:
                if "invalid_grant" in str(e):
                    rich.print(
                        textwrap.dedent(
                            """
                            [red bold]Failed to connect to the Keycloak server.[/red bold]\n
                            [yellow]Please set the [bold]KEYCLOAK_ADMIN_USERNAME[/bold] and [bold]KEYCLOAK_ADMIN_PASSWORD[/bold]
                            environment variables with the Keycloak root credentials and try again.[/yellow]
                            """
                        )
                    )
                    exit()
                else:
                    # Handle other exceptions
                    rich.print(
                        f"[red bold]An unexpected error occurred: {repr(e)}[/red bold]"
                    )
                    exit()

            # Get client ID as role is bound to the JupyterHub client
            client_id = keycloak_admin.get_client_id("jupyterhub")
            role_name = "legacy-group-directory-creation-role"

            # Create role with shared scopes
            keycloak_admin.create_client_role(
                client_role_id=client_id,
                skip_exists=True,
                payload={
                    "name": role_name,
                    "attributes": {
                        "scopes": ["write:shared-mount"],
                        "component": ["shared-directory"],
                    },
                    "description": (
                        "Role to allow group directory creation, created as part of the "
                        "Nebari 2024.11.1 upgrade workflow."
                    ),
                },
            )

            role_id = keycloak_admin.get_client_role_id(
                client_id=client_id, role_name=role_name
            )

            role_representation = keycloak_admin.get_role_by_id(role_id=role_id)

            # Fetch all groups and groups with the role
            all_groups = keycloak_admin.get_groups()
            groups_with_role = keycloak_admin.get_client_role_groups(
                client_id=client_id, role_name=role_name
            )
            groups_with_role_ids = {group["id"] for group in groups_with_role}

            # Identify groups without the role
            groups_without_role = [
                group for group in all_groups if group["id"] not in groups_with_role_ids
            ]

            if groups_without_role:
                group_names = ", ".join(group["name"] for group in groups_without_role)
                rich.print(
                    f"\n[bold]Updating the following groups with the required permissions:[/bold] {group_names}\n"
                )
                for group in groups_without_role:
                    keycloak_admin.assign_group_client_roles(
                        group_id=group["id"],
                        client_id=client_id,
                        roles=[role_representation],
                    )
                rich.print(
                    "\n[green]Group permissions have been updated successfully.[/green]"
                )
            else:
                rich.print(
                    "\n[green]All groups already have the required permissions.[/green]"
                )
        return config


class Upgrade_2024_12_1(UpgradeStep):
    """
    Upgrade step for Nebari version 2024.12.1
    """

    version = "2024.12.1"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        if config.get("provider", "") == "do":
            rich.print(
                "\n[red bold]Error: DigitalOcean is no longer supported as a provider[/red bold].",
            )
            rich.print(
                "You can still deploy Nebari to a Kubernetes cluster on DigitalOcean by using 'existing' as the provider in the config file."
            )
            exit()

        rich.print("Ready to upgrade to Nebari version [green]2024.12.1[/green].")

        return config


class Upgrade_2025_2_1(UpgradeStep):
    version = "2025.2.1"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        rich.print("\n ⚠️ Upgrade Warning ⚠️")

        text = textwrap.dedent(
            """
            In this release, we have updated our maximum supported Kubernetes version from 1.29 to 1.31.
            Please note that Nebari will NOT automatically upgrade your running Kubernetes version as part of
            the redeployment process.

            After completing this upgrade step, we strongly recommend updating the Kubernetes version
            specified in your nebari-config YAML file and redeploying to apply the changes. Remember that
            Kubernetes minor versions must be upgraded incrementally (1.29 → 1.30 → 1.31).

            For more information on upgrading Kubernetes for your specific cloud provider, please visit:
            https://www.nebari.dev/docs/how-tos/kubernetes-version-upgrade
            """
        )
        rich.print(text)

        # If the Nebari provider is Azure, we must handle a major version upgrade
        # of the Azure Terraform provider (from 3.x to 4.x). This involves schema changes
        # that can cause validation issues. The following steps will attempt to migrate
        # your state file automatically. For details, see:
        # https://github.com/nebari-dev/nebari/issues/2964

        if config.get("provider", "") == "azure":
            rich.print("\n ⚠️ Azure Provider Upgrade Notice ⚠️")
            rich.print(
                textwrap.dedent(
                    """
                    In this Nebari release, the Azure Terraform provider has been upgraded
                    from version 3.97.1 to 4.7.0. This major update includes internal schema
                    changes for certain resources, most notably the `azurerm_storage_account`.

                    Nebari will attempt to update your Terraform state automatically to
                    accommodate these changes. However, if you skip this automatic migration,
                    you may encounter validation errors during redeployment.

                    For detailed information on the Azure provider 4.x changes, please visit:
                    https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/guides/4.0-upgrade-guide
                    """
                )
            )

            # Prompt user for confirmation
            continue_ = kwargs.get("attempt_fixes", False) or Confirm.ask(
                "Nebari can automatically apply the necessary state migrations. Continue?",
                default=False,
            )

            if not continue_:
                rich.print(
                    "You have chosen to skip the automatic state migration. This may lead "
                    "to validation errors during deployment.\n\nFor instructions on manually "
                    "updating your Terraform state, please refer to:\n"
                    "https://github.com/nebari-dev/nebari/issues/2964"
                )
                exit
            else:
                # In this case the full path in the tfstate file is
                # resources.instances.attributes.enable_https_traffic_only
                MIGRATION_STATE = {
                    "enable_https_traffic_only": "https_traffic_only_enabled"
                }
                state_filepath = (
                    config_filename.parent
                    / "stages/01-terraform-state/azure/terraform.tfstate"
                )

                # Perform the state file update
                update_tfstate_file(state_filepath, MIGRATION_STATE)

        rich.print("Ready to upgrade to Nebari version [green]2025.2.1[/green].")

        return config


class Upgrade_2025_3_1(UpgradeStep):
    """
    Upgrade step for Nebari version 2025.3.1
    """

    version = "2025.3.1"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):

        rich.print("Ready to upgrade to Nebari version [green]2025.3.1[/green].")

        return config


class Upgrade_2025_4_1(UpgradeStep):
    """
    Upgrade step for Nebari version 2025.4.1

    This upgrade adds node taints to non-general node groups by default.
    Node taints are Kubernetes mechanisms that restrict which pods can be scheduled
    on specific nodes, improving resource isolation and utilization.

    This upgrade step:
    1. Notifies users about upcoming automatic node taints
    2. Provides option to opt out by adding empty taint configurations
    3. Updates node group configurations if user chooses to opt out
    """

    version = "2025.4.1"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):
        # Check if provider is one of the cloud providers that support node taints
        provider = config.get("provider", "")
        if provider in [
            ProviderEnum.aws.value,
            ProviderEnum.azure.value,
            ProviderEnum.gcp.value,
        ]:
            rich.print("\n ⚠️  Node Taints Update ⚠️")

            text = textwrap.dedent(
                """
                Starting with Nebari version 2025.4.1, node taints will be automatically applied to all non-general node groups by default.
                Node taints help ensure that specific workloads run only on designated nodes,
                improving resource utilization and isolation. This change will include:
                - [green]user[/green] node groups (where JupyterLab servers and Argo Workflows run)
                - [green]worker[/green] node groups (where Dask workers run)
                - Any additional [green]custom node groups[/green] defined in your nebari config (e.g., GPU node groups)

                If you prefer not to use node taints, you can opt out by adding `taints: []`
                to each node group definition in your nebari-config.yaml file.
                """
            )
            rich.print(text)

            provider_full_name = provider_enum_name_map.get(provider)
            if provider_full_name and provider_full_name in config:
                # Ask if they want to opt out of taints regardless of whether node_groups is defined
                opt_out = kwargs.get("attempt_fixes", False) or Confirm.ask(
                    "Would you like to opt out of node taints by adding 'taints: []' to all node groups?",
                    default=False,
                )
                if opt_out:
                    rich.print("\nAdding 'taints: []' to all node groups:")
                    from nebari.plugins import nebari_plugin_manager

                    config_model = nebari_plugin_manager.config_schema(**config)
                    provider = getattr(config_model, provider_full_name)
                    node_groups = getattr(provider, "node_groups", None)

                    config[provider_full_name]["node_groups"] = {}
                    for node_group_name, node_group in node_groups.items():
                        node_group.taints = []
                        # Include a few fields, but exclude other node group fields set to the default value
                        config[provider_full_name]["node_groups"][node_group_name] = {
                            **node_group.model_dump(
                                include=["instance", "min_nodes", "max_nodes", "taints"]
                            ),
                            **node_group.model_dump(exclude_defaults=True),
                        }
                        rich.print(
                            f"  - Added taints: None to [green]{node_group_name}[/green] node group"
                        )

                    rich.print("\nNode taints have been disabled for all node groups.")
                else:
                    rich.print(
                        "\nNode taints will be applied by default. You can manually disable them later by adding 'taints: []' to specific node groups."
                    )

        rich.print("\nReady to upgrade to Nebari version [green]2025.4.1[/green].")

        return config


class Upgrade_2025_4_2(UpgradeStep):
    """
    Upgrade step for Nebari version 2025.4.2
    """

    version = "2025.4.2"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):

        rich.print("Ready to upgrade to Nebari version [green]2025.4.2[/green].")

        return config


class Upgrade_2025_6_1(UpgradeStep):
    """
    Upgrade step for Nebari version 2025.6.1
    """

    version = "2025.6.1"

    @override
    def _version_specific_upgrade(
        self, config, start_version, config_filename: Path, *args, **kwargs
    ):

        rich.print("Ready to upgrade to Nebari version [green]2025.6.1[/green].")

        return config


__rounded_version__ = str(rounded_ver_parse(__version__))

# Manually-added upgrade steps must go above this line
if not UpgradeStep.has_step(__rounded_version__):
    # Always have a way to upgrade to the latest full version number, even if no customizations
    # Don't let dev/prerelease versions cloud things
    class UpgradeLatest(UpgradeStep):
        """
        Upgrade step for the latest available version.

        This class ensures there is always an upgrade path to the latest version,
        even if no specific upgrade steps are defined for the current version.
        """

        version = __rounded_version__
