"""
All provider client operator upgrades implemented here

"""

import functools
import logging
import threading

from ocs_ci.deployment.hub_spoke import HostedODF, HostedClients
from ocs_ci.ocs.dr_upgrade import DRUpgrade
from ocs_ci.framework import config
from ocs_ci.ocs import ocs_upgrade
from ocs_ci.ocs.ocs_upgrade import OCSUpgrade, prune_old_df_repo_idms
from ocs_ci.ocs import constants
from ocs_ci.ocs.ocp import OCP
from ocs_ci.deployment.metallb import MetalLBInstaller
from ocs_ci.deployment.cnv import CNVInstaller
from ocs_ci.deployment.deployment import Deployment
from ocs_ci.ocs.acm_upgrade import ACMUpgrade
from ocs_ci.ocs.resources.install_plan import wait_for_install_plan_and_approve
from ocs_ci.framework.testlib import (
    skipif_ocs_version,
    skipif_ocp_version,
    skipif_managed_service,
    runs_on_provider,
    skipif_external_mode,
)
from ocs_ci.deployment.helpers.lso_helpers import lso_upgrade
from ocs_ci.utility.prometheus import (
    PrometheusAPI,
    wait_for_alert_firing,
    wait_for_alert_cleared,
)

log = logging.getLogger(__name__)


def get_ocs_client_operator_subscription_channel(
    namespace=constants.OPENSHIFT_STORAGE_CLIENT_NAMESPACE,
    cluster_kubeconfig=None,
):
    """
    Return the current channel of the ocs-client-operator Subscription.

    Args:
        namespace (str): Namespace where the Subscription lives.
        cluster_kubeconfig (str): Path to kubeconfig; uses active context when None.

    Returns:
        str: Current subscription channel value.
    """
    kwargs = dict(
        resource_name=constants.OCS_CLIENT_OPERATOR,
        kind="subscription.operators.coreos.com",
        namespace=namespace,
    )
    if cluster_kubeconfig:
        kwargs["cluster_kubeconfig"] = cluster_kubeconfig
    subscription = OCP(**kwargs)
    channel = subscription.data["spec"]["channel"]
    log.info(f"ocs-client-operator subscription channel: {channel}")
    return channel


def verify_client_subscription_channel_unchanged(
    expected_channel,
    namespace=constants.OPENSHIFT_STORAGE_CLIENT_NAMESPACE,
    cluster_kubeconfig=None,
):
    """
    Assert that the ocs-client-operator subscription channel has not changed.

    Args:
        expected_channel (str): The channel value that must still be set.
        namespace (str): Namespace where the Subscription lives.
        cluster_kubeconfig (str): Path to kubeconfig; uses active context when None.

    Raises:
        AssertionError: If the channel differs from *expected_channel*.
    """
    current = get_ocs_client_operator_subscription_channel(
        namespace=namespace, cluster_kubeconfig=cluster_kubeconfig
    )
    assert current == expected_channel, (
        f"ocs-client-operator subscription channel changed unexpectedly: "
        f"expected '{expected_channel}', got '{current}'"
    )
    log.info("ocs-client-operator subscription channel is unchanged as expected")


def approve_ocs_client_operator_upgrade(
    new_channel,
    namespace=constants.OPENSHIFT_STORAGE_CLIENT_NAMESPACE,
    cluster_kubeconfig=None,
    install_plan_timeout=960,
):
    """
    Manually update the ocs-client-operator subscription channel and approve
    the resulting InstallPlan, simulating admin-approved upgrade.

    Args:
        new_channel (str): Target subscription channel (e.g. "stable-5.1").
        namespace (str): Namespace where the Subscription lives.
        cluster_kubeconfig (str): Path to kubeconfig; uses active context when None.
        install_plan_timeout (int): Seconds to wait for an InstallPlan to approve.
    """
    log.info(f"Patching ocs-client-operator subscription to channel '{new_channel}'")
    kwargs = dict(
        resource_name=constants.OCS_CLIENT_OPERATOR,
        kind="subscription.operators.coreos.com",
        namespace=namespace,
    )
    if cluster_kubeconfig:
        kwargs["cluster_kubeconfig"] = cluster_kubeconfig
    subscription = OCP(**kwargs)
    patch_cmd = (
        f"patch subscription.operators.coreos.com {constants.OCS_CLIENT_OPERATOR} "
        f"-n {namespace} --type merge "
        f'-p \'{{"spec":{{"channel": "{new_channel}"}}}}\''
    )
    subscription.exec_oc_cmd(patch_cmd, out_yaml_format=False)
    log.info("Waiting for InstallPlan and approving upgrade")
    wait_for_install_plan_and_approve(namespace=namespace, timeout=install_plan_timeout)


def verify_incompatible_operator_version_alert(
    timeout=1200,
    expected_severity="warning",
    expected_message_substr=None,
    min_count=1,
    cluster_context=None,
):
    """
    Wait for StorageClientIncompatibleOperatorVersion to fire and validate it.

    This alert fires on BOTH the provider and the affected client cluster.

    Args:
        timeout (int): Seconds to wait for the alert to reach firing state.
        expected_severity (str): Expected severity label value.
        expected_message_substr (str | None): Optional substring to verify in
            the alert message.
        min_count (int): Minimum number of firing instances to expect.
        cluster_context: Callable passed to PrometheusAPI to control which
            cluster's Prometheus is queried. Defaults to
            RunWithProviderConfigContextIfAvailable. Pass
            ``functools.partial(config.RunWithConfigContext, idx)`` to target
            a specific client cluster.

    Returns:
        list[dict]: Fired alert records.
    """
    if cluster_context is None:
        cluster_context = config.RunWithProviderConfigContextIfAvailable
    alert_name = constants.ALERT_STORAGECLIENTINCOMPATIBLEOPERATORVERSION
    log.info(f"Waiting for alert '{alert_name}' to fire (timeout={timeout}s)")
    api = PrometheusAPI(
        threading_lock=threading.RLock(), cluster_context=cluster_context
    )
    alerts = wait_for_alert_firing(
        api=api,
        alert_name=alert_name,
        timeout=timeout,
        expected_severity=expected_severity,
        expected_message_substr=expected_message_substr,
        min_count=min_count,
    )
    return alerts


def verify_incompatible_operator_version_alert_cleared(
    timeout=600, cluster_context=None
):
    """
    Wait for StorageClientIncompatibleOperatorVersion to disappear.

    Args:
        timeout (int): Seconds to wait for the alert to clear.
        cluster_context: Callable passed to PrometheusAPI to control which
            cluster's Prometheus is queried. Defaults to
            RunWithProviderConfigContextIfAvailable. Pass
            ``functools.partial(config.RunWithConfigContext, idx)`` to target
            a specific client cluster.
    """
    if cluster_context is None:
        cluster_context = config.RunWithProviderConfigContextIfAvailable
    alert_name = constants.ALERT_STORAGECLIENTINCOMPATIBLEOPERATORVERSION
    log.info(f"Waiting for alert '{alert_name}' to clear (timeout={timeout}s)")
    api = PrometheusAPI(
        threading_lock=threading.RLock(), cluster_context=cluster_context
    )
    wait_for_alert_cleared(api=api, alert_name=alert_name, timeout=timeout)


def verify_client_upgrade_required_alert(
    timeout=1200,
    expected_severity="warning",
    expected_message_substr=None,
    min_count=1,
    cluster_context=None,
):
    """
    Wait for StorageClientUpgradeRequired to fire on the specified client
    cluster and validate its properties.

    This alert fires ONLY on the client cluster. Always pass
    ``functools.partial(config.RunWithConfigContext, idx)`` as
    ``cluster_context`` so PrometheusAPI queries the correct client's
    Prometheus endpoint.

    Args:
        timeout (int): Seconds to wait for the alert to reach firing state.
        expected_severity (str): Expected severity label value.
        expected_message_substr (str | None): Optional substring to verify in
            the alert message.
        min_count (int): Minimum number of firing instances to expect.
        cluster_context: Callable passed to PrometheusAPI to control which
            cluster's Prometheus is queried. Defaults to
            RunWithProviderConfigContextIfAvailable.

    Returns:
        list[dict]: Fired alert records.
    """
    if cluster_context is None:
        cluster_context = config.RunWithProviderConfigContextIfAvailable
    alert_name = constants.ALERT_STORAGECLIENTUPGRADEREQUIRED
    log.info(f"Waiting for alert '{alert_name}' to fire (timeout={timeout}s)")
    api = PrometheusAPI(
        threading_lock=threading.RLock(), cluster_context=cluster_context
    )
    alerts = wait_for_alert_firing(
        api=api,
        alert_name=alert_name,
        timeout=timeout,
        expected_severity=expected_severity,
        expected_message_substr=expected_message_substr,
        min_count=min_count,
    )
    return alerts


def verify_client_upgrade_required_alert_cleared(timeout=600, cluster_context=None):
    """
    Wait for StorageClientUpgradeRequired to disappear on the specified client
    cluster.

    Args:
        timeout (int): Seconds to wait for the alert to clear.
        cluster_context: Callable passed to PrometheusAPI to control which
            cluster's Prometheus is queried. Defaults to
            RunWithProviderConfigContextIfAvailable.
    """
    if cluster_context is None:
        cluster_context = config.RunWithProviderConfigContextIfAvailable
    alert_name = constants.ALERT_STORAGECLIENTUPGRADEREQUIRED
    log.info(f"Waiting for alert '{alert_name}' to clear (timeout={timeout}s)")
    api = PrometheusAPI(
        threading_lock=threading.RLock(), cluster_context=cluster_context
    )
    wait_for_alert_cleared(api=api, alert_name=alert_name, timeout=timeout)


@skipif_ocs_version("<4.15")
@skipif_ocp_version("<4.15")
@skipif_external_mode
@skipif_managed_service
@runs_on_provider
class ProviderUpgrade(OCSUpgrade):
    """
    Base class for all provider operator upgrades

    """

    def __init__(
        self,
        namespace=constants.OPENSHIFT_OPERATORS,
        version_before_upgrade=None,
        ocs_registry_image=None,
        upgrade_in_current_source=config.UPGRADE.get(
            "upgrade_in_current_source", False
        ),
        resource_name=None,
    ):
        if not version_before_upgrade:
            if config.PREUPGRADE_CONFIG.get("ENV_DATA").get("ocs_version", ""):
                version_before_upgrade = config.PREUPGRADE_CONFIG["ENV_DATA"].get(
                    "ocs_version"
                )
            else:
                version_before_upgrade = config.ENV_DATA.get("ocs_version")
        if not ocs_registry_image:
            ocs_registry_image = config.UPGRADE.get("upgrade_ocs_registry_image")
        self.external_cluster = None
        self.operator_name = None
        self.subscription_name = None
        self.pre_upgrade_data = dict()
        self.post_upgrade_data = dict()
        self.namespace = namespace
        # Upgraded phases [pre_upgrade, post_upgrade]
        self.upgrade_phase = "pre_upgrade"
        if resource_name:
            self.resource_name = resource_name

        super().__init__(
            namespace,
            version_before_upgrade,
            ocs_registry_image,
            upgrade_in_current_source,
        )
        self.upgrade_version = self.get_upgrade_version()


class OperatorUpgrade(ProviderUpgrade):
    """
    A class to handle installed operators on provider upgrades

    """

    def __init__(self):
        super().__init__()
        self.drupgrade_obj = DRUpgrade()
        self.metallb_installer_obj = MetalLBInstaller()
        self.cnv_installer_obj = CNVInstaller()
        self.acm_hub_upgrade_obj = ACMUpgrade()

    def run_acm_operator_upgrade(self):
        """
        This method is for acm operator upgrade
        """
        if not Deployment().acm_operator_installed():
            log.info("ACM operator is unavailable")
            log.info("Upgrade mce operator")
        try:
            self.acm_hub_upgrade_obj.run_upgrade()
        except Exception as e:
            log.error(f"ACM Operator upgrade failed: {e}")

    def run_operators_upgrade(self):
        """
        This method is for upgrade of all operators required for provider clusters,
        ACM, Metallb, Cnv, lso

        To do: MCE

        """
        try:
            if not self.metallb_installer_obj.upgrade_metallb():
                log.error("Failed to upgrade Metallb operator")
            else:
                log.info("Upgrade successful")
        except Exception as e:
            log.error(f"Failed to upgrade Metallb operator: {e}")

        try:
            if not self.cnv_installer_obj.upgrade_cnv():
                raise Exception("CNV Operator upgrade failed")
        except Exception as e:
            log.error(f"Failed to upgrade CNV operator: {e}")

        try:
            self.run_acm_operator_upgrade()
        except Exception as e:
            log.error(f"Failed to upgrade ACM operator: {e}")

        try:
            if not lso_upgrade():
                log.error("Failed to upgrade lso operator")
            else:
                log.info("Upgrade successful")
        except Exception as e:
            log.error(f"Failed to upgrade lso operator: {e}")

    def bump_ocs_version_on_clients(self, cluster_names=None):
        """
        Bump the OCS version to the latest version available in the registry

        This only updates the CatalogSource image on each client cluster so
        that the new operator bundle is discoverable. It does NOT change the
        ocs-client-operator subscription channel — that is an explicit admin
        action required since ODF 5.1 (RHSTOR-8681).

        Args:
            cluster_names (list): List of cluster names to upgrade. If None, all clusters will be upgraded.

        """
        log.info(
            "Bumping OCS version to the latest available in the registry of client clusters"
        )

        if not cluster_names:
            cluster_names = list(config.ENV_DATA.get("clusters").keys())

        for cluster_name in cluster_names:
            log.info(
                f"Validate ODF client operator installed on hosted OCP cluster '{cluster_name}'"
            )
            try:
                hosted_odf = HostedODF(cluster_name)
                if not hosted_odf.odf_client_installed():
                    log.info(
                        f"ODF client operator not installed on HCP cluster '{cluster_name}', skipping this client"
                    )
                    continue
                hosted_odf.create_catalog_source(
                    reapply=True,
                    odf_version_tag=f"latest-stable-{self.upgrade_version}",
                )
            except Exception as e:
                # we don't want to abort the upgrade process if one client upgrade fails because:
                # It will be easier to address issue manually with one client
                # It allows to run negative tests, when one client fails to upgrade
                log.error(
                    f"Failed to bump ODF client operator version on hosted OCP cluster '{cluster_name}': {e}"
                )

    def validate_and_upgrade_clients(self):
        """
        Validate alert state and approve ocs-client-operator upgrade on every
        consumer cluster, in a staged sequence.

        This is a no-op for upgrades targeting versions earlier than 5.1,
        where OLM managed the channel automatically (RHSTOR-8681).

        For ODF 5.1+ the sequence is:

        1. Verify subscription channel on each client is still at the old
           value (not auto-changed by the controller).
        2. Verify StorageClientIncompatibleOperatorVersion fires on provider.
        3. For each client:
           a. Verify both alerts fire (IncompatibleOperatorVersion +
              UpgradeRequired).
           b. Patch subscription channel and approve the InstallPlan.
           c. Verify both alerts clear on that client.
        4. Verify StorageClientIncompatibleOperatorVersion clears on provider.
        """
        from packaging.version import Version

        if Version(str(self.upgrade_version)) < Version("5.1"):
            log.info(
                f"upgrade_version={self.upgrade_version} is earlier than 5.1 — "
                f"client subscription channel is managed automatically, skipping"
            )
            return

        upgrade_channel = f"stable-{self.upgrade_version}"
        pre_upgrade_channel = f"stable-{self.version_before_upgrade}"

        consumer_indexes = config.get_consumer_indexes_list(raise_exception=False)
        if not consumer_indexes:
            log.info("No consumer clusters found, skipping client upgrade")
            return

        log.info(
            f"Upgrading ocs-client-operator to '{upgrade_channel}' on "
            f"{len(consumer_indexes)} consumer cluster(s)"
        )

        # Step 1: subscription channel must be unchanged on all clients
        log.info("Step 1: Verify subscription channel is unchanged on all clients")
        for idx in consumer_indexes:
            cluster_name = config.get_cluster_name_by_index(idx)
            with config.RunWithConfigContext(idx):
                verify_client_subscription_channel_unchanged(
                    expected_channel=pre_upgrade_channel
                )
            log.info(
                f"Subscription channel is still '{pre_upgrade_channel}' on '{cluster_name}'"
            )

        # Step 2: IncompatibleOperatorVersion must fire on the provider
        log.info(
            "Step 2: Verify StorageClientIncompatibleOperatorVersion fires on provider"
        )
        verify_incompatible_operator_version_alert(
            timeout=1200, expected_severity="warning"
        )

        # Step 3: per-client staged upgrade with alert validation
        for idx in consumer_indexes:
            cluster_name = config.get_cluster_name_by_index(idx)
            client_ctx = functools.partial(config.RunWithConfigContext, idx)

            log.info(
                f"Step 3a [{cluster_name}]: Verify both alerts fire before upgrade"
            )
            # StorageClientIncompatibleOperatorVersion fires on both provider and client;
            # StorageClientUpgradeRequired fires on the client only.
            # Pass client_ctx so PrometheusAPI queries this client's Prometheus endpoint.
            verify_incompatible_operator_version_alert(
                timeout=600, expected_severity="warning", cluster_context=client_ctx
            )
            verify_client_upgrade_required_alert(
                timeout=600, expected_severity="warning", cluster_context=client_ctx
            )

            log.info(
                f"Step 3b [{cluster_name}]: Approve upgrade to '{upgrade_channel}'"
            )
            with config.RunWithConfigContext(idx):
                approve_ocs_client_operator_upgrade(new_channel=upgrade_channel)

            log.info(
                f"Step 3c [{cluster_name}]: Verify both alerts cleared after upgrade"
            )
            verify_incompatible_operator_version_alert_cleared(
                timeout=900, cluster_context=client_ctx
            )
            verify_client_upgrade_required_alert_cleared(
                timeout=900, cluster_context=client_ctx
            )
            log.info(f"Client '{cluster_name}' upgraded successfully")

        # Step 4: IncompatibleOperatorVersion must clear on the provider
        log.info(
            "Step 4: Verify StorageClientIncompatibleOperatorVersion cleared on provider"
        )
        verify_incompatible_operator_version_alert_cleared(timeout=900)
        log.info("All clients upgraded — provider alert cleared")


class KubevirtClusterUpgrade(ProviderUpgrade):
    """
    A class to handle Kubevirt Cluster(s) upgrade

    """

    def run_upgrade_ocp_on_kubevirt_clusters(self):
        hosted_clients = HostedClients()
        hosted_clients.upgrade_ocp_on_kubevirt_clusters()


class ProviderClusterOperatorUpgrade(ProviderUpgrade):
    """
    A class to handle Provider Cluster operator upgrades

    """

    def __init__(self):
        super().__init__(namespace=config.ENV_DATA["cluster_namespace"])

    def run_provider_upgrade(self):
        """
        This method is for running the upgrade of ocs, metallb, acm and cnv opertaors
        """
        try:
            log.info("Starting the operator upgrade process...")
            operator_upgrade = OperatorUpgrade()
            # delete old idms, if exist from the previous upgrades
            prune_old_df_repo_idms(force_delete_pods=True)
            # Bump OCS version on clients (catalog source only; channel unchanged per RHSTOR-8681)
            operator_upgrade.bump_ocs_version_on_clients()
            ocs_upgrade.run_ocs_upgrade()

            hosted_clients = HostedClients()
            hosted_clients.apply_idms_to_hosted_clusters()

            operator_upgrade.run_operators_upgrade()

            # For ODF 5.1+: validate version-mismatch alerts then approve the
            # ocs-client-operator channel change on every consumer cluster.
            operator_upgrade.validate_and_upgrade_clients()

            log.info("Operator upgrade completed successfully.")
        except Exception as e:
            log.error(f"Operator upgrade failed: {e}")
            raise
