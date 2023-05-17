import logging
import time

from ocs_ci.ocs.ui.base_ui import (
    PageNavigator,
)
from ocs_ci.ocs.ui.deployment_ui import DeploymentUI
from ocs_ci.ocs.ui.validation_ui import ValidationUI


logger = logging.getLogger(__name__)


class nfsUI(PageNavigator):
    """
    User Interface Selenium
    """

    def __init__(self):
        super().__init__()
        self.driver.implicitly_wait(5)

    def enable_nfs_from_ui(
        self,
    ):
        """
        To enable nfs from ui select nfs checkbox from storage cluster
        creation page
        """
        dep_obj = DeploymentUI()
        dep_obj.install_ocs_ui(enable_nfs=True)

    def nfs_page_available_at_webconsole(
        self,
    ):
        """
        To check nfs tab is available under “Storage > Data Foundation > Overview
        when nfs is enabled.

        """
        storage_system_details = (
            ValidationUI()
            .nav_odf_default_page()
            .nav_storage_systems_tab()
            .nav_storagecluster_storagesystem_details()
        )
        storage_system_details.nav_details_overview()
        logger.info(
            f"----Execute till here-----storage system details : {storage_system_details}"
        )
        nfs_tab_availability = storage_system_details.nav_network_file_system()
        return nfs_tab_availability

    def verify_nfs_share_details_ui(self, pvc_name, project_name):
        """
        Verifying PVC details via UI

        Args:
            pvc_size (str): the size of pvc (GB)
            access_mode (str): access mode
            vol_mode (str): volume mode type
            sc_name (str): storage class name
            pvc_name (str): the name of pvc
            project_name (str): name of test project
        """
        self.navigate_persistentvolumes_page()

        logger.info(f"Search and Select test project {project_name}")
        self.do_click(self.pvc_loc["pvc_project_selector"])
        self.do_send_keys(self.pvc_loc["search-project"], text=project_name)

        self.wait_for_namespace_selection(project_name=project_name)

        logger.info(f"Search for {pvc_name} inside test project {project_name}")
        self.do_send_keys(self.pvc_loc["search_pvc"], text=pvc_name)

        time.sleep(2)
        # Using sleep to avoid StaleElementReferenceException. Use of explict wait or refreshing the page didn't help.
