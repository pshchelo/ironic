# Copyright 2016 Mirantis Inc
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""
iPXE Boot interface

This boot interface is designed to work with Ironic API serving
iPXE boot scripts and configs via 'v1/ipxe' endpoint.
"""

import os

from ironic_lib import metrics_utils
import jinja2
from oslo_log import log as logging
from oslo_utils import fileutils
from six.moves import urllib_parse as urlparse

from ironic.common import boot_devices
from ironic.common import dhcp_factory
from ironic.common import exception
from ironic.common.glance_service import service_utils
from ironic.common.i18n import _, _LE, _LW
from ironic.common import image_service as service
from ironic.common import pxe_utils
from ironic.common import states
from ironic.conf import CONF
from ironic.drivers import base
from ironic.drivers.modules import deploy_utils
from ironic.drivers.modules import image_cache
from ironic.drivers.modules import pxe
from ironic.drivers import utils as driver_utils
from ironic import objects

LOG = logging.getLogger(__name__)

METRICS = metrics_utils.get_metrics_logger(__name__)

REQUIRED_PROPERTIES = {
    'deploy_kernel': _("UUID (from Glance) of the deployment kernel. "
                       "Required."),
    'deploy_ramdisk': _("UUID (from Glance) of the ramdisk that is "
                        "mounted at boot time. Required."),
}
COMMON_PROPERTIES = REQUIRED_PROPERTIES

_IPXE_CONFIG_ALLOWED_STATES = {states.DEPLOYING, states.DEPLOYWAIT,
                               states.CLEANING, states.CLEANWAIT,
                               states.INSPECTING, states.ACTIVE}


def _build_pxe_config_options(task, pxe_info):
    """Build the PXE config options for a node

    This method builds the PXE boot options for a node,
    given all the required parameters.

    The options should then be passed to pxe_utils.create_pxe_config to
    create the actual config files.

    :param task: A TaskManager object
    :param pxe_info: a dict of values to set on the configuration file
    :returns: A dictionary of pxe options to be used in the pxe bootfile
        template.
    """
    pxe_options = pxe._get_ipxe_kernel_ramdisk(task, pxe_info)

    # These are dummy values to satisfy elilo.
    # image and initrd fields in elilo config cannot be blank.
    pxe_options.setdefault('aki_path', 'no_kernel')
    pxe_options.setdefault('ari_path', 'no_ramdisk')

    pxe_options.update({
        'pxe_append_params': CONF.pxe.pxe_append_params,
        'tftp_server': CONF.pxe.tftp_server,
        'ipxe_timeout': CONF.pxe.ipxe_timeout * 1000
    })

    return pxe_options


@image_cache.cleanup(priority=25)
class HTTPImageCache(image_cache.ImageCache):
    def __init__(self):
        super(HTTPImageCache, self).__init__(
            CONF.ipxe.http_master_path,
            # MiB -> B
            cache_size=CONF.ipxe.image_cache_size * 1024 * 1024,
            # min -> sec
            cache_ttl=CONF.ipxe.image_cache_ttl * 60)


def _cache_ramdisk_kernel(ctx, node, pxe_info):
    """Fetch the necessary kernels and ramdisks for the instance.

    Skip fetching of deploy images that iPXE can boot directly from if
    such support is enabled.
    """
    deploy_image_labels = ('deploy_kernel', 'deploy_ramdisk')
    # make a copy just in case
    cached_images = pxe_info.copy()
    for label in deploy_image_labels:
        if (label in cached_images and
            (CONF.ipxe.use_swift or
             service.image_is_ipxe_ready(cached_images[label][0]))):
                cached_images.pop(label)
    if not cached_images:
        return

    fileutils.ensure_tree(
        os.path.join(pxe_utils.get_root_dir(), node.uuid))
    LOG.debug("Fetching necessary kernel and ramdisk for node %s",
              node.uuid)
    deploy_utils.fetch_images(ctx, HTTPImageCache(),
                              list(cached_images.values()),
                              CONF.force_raw_images)


class IPXEBoot(pxe.PXEBoot):

    @METRICS.timer('IPXEBoot.validate')
    def validate(self, task):
        """Validate the PXE-specific info for booting deploy/instance images.

        This method validates the PXE-specific info for booting the
        ramdisk and instance on the node.  If invalid, raises an
        exception; otherwise returns None.

        :param task: a task from TaskManager.
        :returns: None
        :raises: InvalidParameterValue, if some parameters are invalid.
        :raises: MissingParameterValue, if some required parameters are
            missing.
        """
        node = task.node

        if not driver_utils.get_node_mac_addresses(task):
            raise exception.MissingParameterValue(
                _("Node %s does not have any port associated with it.")
                % node.uuid)

        # Get the boot_mode capability value.
        boot_mode = deploy_utils.get_boot_mode_for_deploy(node)

        if boot_mode == 'uefi':
            pxe.validate_boot_option_for_uefi(node)

        # Check the trusted_boot capabilities value.
        deploy_utils.validate_capabilities(node)
        if deploy_utils.is_trusted_boot_requested(node):
            # Check if 'boot_option' and boot mode is compatible with
            # trusted boot.
            pxe.validate_boot_parameters_for_trusted_boot(node)

        pxe._parse_driver_info(node)
        d_info = deploy_utils.get_image_instance_info(node)
        if (node.driver_internal_info.get('is_whole_disk_image') or
                deploy_utils.get_boot_option(node) == 'local'):
            props = []
        elif service_utils.is_glance_image(d_info['image_source']):
            props = ['kernel_id', 'ramdisk_id']
        else:
            props = ['kernel', 'ramdisk']
        deploy_utils.validate_image_properties(task.context, d_info, props)

    @METRICS.timer('IPXEBoot.prepare_ramdisk')
    def prepare_ramdisk(self, task, ramdisk_params):
        node = task.node

        dhcp_opts = pxe_utils.ipxe_dhcp_options_for_instance(task)
        provider = dhcp_factory.DHCPFactory()
        provider.update_dhcp(task, dhcp_opts)
        pxe_info = pxe._get_deploy_image_info(node)
        # NODE: Try to validate and fetch instance images only
        # if we are in DEPLOYING state.
        if node.provision_state == states.DEPLOYING:
            pxe_info.update(pxe._get_instance_image_info(node, task.context))
        deploy_utils.try_set_boot_device(task, boot_devices.PXE)
        _cache_ramdisk_kernel(task.context, node, pxe_info)

    @METRICS.timer('IPXEBoot.prepare_instance')
    def prepare_instance(self, task):
        """Prepares the boot of instance.

        This method prepares the boot of the instance after reading
        relevant information from the node's instance_info. In case of netboot,
        it updates the dhcp entries and switches the PXE config. In case of
        localboot, it cleans up the PXE config.

        :param task: a task from TaskManager.
        :returns: None
        """
        node = task.node
        boot_option = deploy_utils.get_boot_option(node)
        if boot_option != "local":
            # Make sure that the instance kernel/ramdisk is cached.
            # This is for the takeover scenario for active nodes.
            instance_image_info = pxe._get_instance_image_info(
                task.node, task.context)
            _cache_ramdisk_kernel(task.context, task.node,
                                  instance_image_info)
            dhcp_opts = pxe_utils.dhcp_options_for_instance(task)
            provider = dhcp_factory.DHCPFactory()
            provider.update_dhcp(task, dhcp_opts)

            iwdi = task.node.driver_internal_info.get('is_whole_disk_image')
            root_uuid_or_disk_id = task.node.driver_internal_info.get(
                'root_uuid_or_disk_id')
            if not root_uuid_or_disk_id:
                if not iwdi:
                    LOG.warning(
                        _LW("The UUID for the root partition can't be "
                            "found, unable to switch the pxe config from "
                            "deployment mode to service (boot) mode for "
                            "node %(node)s"), {"node": task.node.uuid})
                else:
                    LOG.warning(
                        _LW("The disk id for the whole disk image can't "
                            "be found, unable to switch the pxe config "
                            "from deployment mode to service (boot) mode "
                            "for node %(node)s"),
                        {"node": task.node.uuid})
            else:
                # This should go to boot_config vendor passthrough
                # or validate
                # pxe_config_path = pxe_utils.get_pxe_config_file_path(
                    # task.node.uuid)
                # In case boot mode changes from bios to uefi, boot device
                # order may get lost in some platforms. Better to re-apply
                # boot device.
                deploy_utils.try_set_boot_device(task, boot_devices.PXE)
        else:
            # If it's going to boot from the local disk, we don't need
            # PXE config files. They still need to be generated as part
            # of the prepare() because the deployment does PXE boot the
            # deploy ramdisk
            pxe_utils.clean_up_pxe_config(task)
            deploy_utils.try_set_boot_device(task, boot_devices.DISK)


class IPXEVendorPassthru(base.VendorPassthru):

    def get_properties(self):
        return {}

    def validate(self):
        pass

    @base.driver_passthru(['GET'], async=False, attach=True,
                          description="Serve iPXE boot script")
    def boot_script(self, context, **kwargs):
        if not set(kwargs).issubset({'mac', 'node_uuid'}):
            raise exception.NotFound()

        mac = kwargs.get('mac')
        node_uuid = kwargs.get('node_uuid')
        if not (mac or node_uuid):
            raise exception.NotFound()
        try:
            if node_uuid:
                node = objects.Node.get_by_uuid(context, node_uuid)
            else:
                node = objects.Node.get_by_port_addresses(context,
                                                          [mac])
        except exception.NotFound:
            raise exception.NotFound()

        LOG.debug('Building iPXE boot script for node %s' % node.uuid)

        if (CONF.ipxe.restrict_configs and
            node.provision_state not in _IPXE_CONFIG_ALLOWED_STATES):
                raise exception.NotFound()

        api_url = deploy_utils.get_ironic_api_url()
        endpoint = 'v1/nodes/{node_uuid}/vendor_passthru/boot_config'.format(
            node_uuid=node.uuid)
        ipxe_config_url = urlparse.urljoin(api_url, endpoint)

        tmpl_path, tmpl_file = os.path.split(CONF.ipxe.boot_script)
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(tmpl_path))
        template = env.get_template(tmpl_file)
        return template.render(ipxe_config_url=ipxe_config_url)

    @base.passthru(['GET'], async=False, attach=True,
                   description="Serve iPXE boot config")
    def boot_config(self, task):
        node = task.node
        if (CONF.ipxe.restrict_configs and
            node.provision_state not in _IPXE_CONFIG_ALLOWED_STATES):
                raise exception.NotFound()
        pxe_info = pxe._get_deploy_image_info(node)

        # NODE: Try to validate and fetch instance images only
        # if we are in DEPLOYING state.
        if node.provision_state == states.DEPLOYING:
            pxe_info.update(pxe._get_instance_image_info(node, task.context))

        pxe_options = _build_pxe_config_options(task, pxe_info)
        # TODO(pas-ha) fetch from deploy driver
        ramdisk_params = {}
        pxe_options.update(ramdisk_params)

        if deploy_utils.get_boot_mode_for_deploy(node) == 'uefi':
            pxe_config_template = CONF.ipxe.uefi_pxe_config_template
        else:
            pxe_config_template = CONF.ipxe.pxe_config_template
        # TODO(pas-ha) switch config
        # deploy_utils.switch_pxe_config(
            # pxe_config_path, root_uuid_or_disk_id,
            # deploy_utils.get_boot_mode_for_deploy(node),
            # iwdi, deploy_utils.is_trusted_boot_requested(node))

        return pxe_utils.create_pxe_config(task, pxe_options,
                                           pxe_config_template,
                                           dynamic=True)
