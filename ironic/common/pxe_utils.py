#
# Copyright 2014 Rackspace, Inc
# All Rights Reserved
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os
import re

from ironic_lib import utils as ironic_utils
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import fileutils

from ironic.common import dhcp_factory
from ironic.common import exception
from ironic.common.i18n import _, _LW
from ironic.common import utils
from ironic.drivers.modules import deploy_utils

CONF = cfg.CONF

LOG = logging.getLogger(__name__)

PXE_CFG_DIR_NAME = 'pxelinux.cfg'


def get_root_dir():
    """Returns the directory where the config files and images will live."""
    if CONF.pxe.ipxe_enabled:
        return CONF.deploy.http_root
    else:
        return CONF.pxe.tftp_root


def _ensure_config_dirs_exist(node_uuid):
    """Ensure that the node's and PXE configuration directories exist.

    :param node_uuid: the UUID of the node.

    """
    root_dir = get_root_dir()
    fileutils.ensure_tree(os.path.join(root_dir, node_uuid))
    fileutils.ensure_tree(os.path.join(root_dir, PXE_CFG_DIR_NAME))


def _link_mac_pxe_configs(task):
    """Link each MAC address with the PXE configuration file.

    :param task: A TaskManager instance.

    """

    def create_link(mac_path):
        ironic_utils.unlink_without_raise(mac_path)
        relative_source_path = os.path.relpath(
            pxe_config_file_path, os.path.dirname(mac_path))
        utils.create_link_without_raise(relative_source_path, mac_path)

    pxe_config_file_path = get_pxe_config_file_path(task.node.uuid)
    for port in task.ports:
        client_id = port.extra.get('client-id')
        create_link(_get_pxe_mac_path(port.address, client_id=client_id))


def _link_ip_address_pxe_configs(task, hex_form):
    """Link each IP address with the PXE configuration file.

    :param task: A TaskManager instance.
    :param hex_form: Boolean value indicating if the conf file name should be
                     hexadecimal equivalent of supplied ipv4 address.
    :raises: FailedToGetIPAddressOnPort
    :raises: InvalidIPv4Address

    """
    pxe_config_file_path = get_pxe_config_file_path(task.node.uuid)

    api = dhcp_factory.DHCPFactory().provider
    ip_addrs = api.get_ip_addresses(task)
    if not ip_addrs:
        raise exception.FailedToGetIPAddressOnPort(_(
            "Failed to get IP address for any port on node %s.") %
            task.node.uuid)
    for port_ip_address in ip_addrs:
        ip_address_path = _get_pxe_ip_address_path(port_ip_address,
                                                   hex_form)
        ironic_utils.unlink_without_raise(ip_address_path)
        relative_source_path = os.path.relpath(
            pxe_config_file_path, os.path.dirname(ip_address_path))
        utils.create_link_without_raise(relative_source_path,
                                        ip_address_path)


def _get_pxe_mac_path(mac, delimiter='-', client_id=None):
    """Convert a MAC address into a PXE config file name.

    :param mac: A MAC address string in the format xx:xx:xx:xx:xx:xx.
    :param delimiter: The MAC address delimiter. Defaults to dash ('-').
    :param client_id: client_id indicate InfiniBand port.
                      Defaults is None (Ethernet)
    :returns: the path to the config file.

    """
    mac_file_name = mac.replace(':', delimiter).lower()
    if not CONF.pxe.ipxe_enabled:
        hw_type = '01-'
        if client_id:
            hw_type = '20-'
        mac_file_name = hw_type + mac_file_name

    return os.path.join(get_root_dir(), PXE_CFG_DIR_NAME, mac_file_name)


def _get_pxe_ip_address_path(ip_address, hex_form):
    """Convert an ipv4 address into a PXE config file name.

    :param ip_address: A valid IPv4 address string in the format 'n.n.n.n'.
    :param hex_form: Boolean value indicating if the conf file name should be
                     hexadecimal equivalent of supplied ipv4 address.
    :returns: the path to the config file.

    """
    # elilo bootloader needs hex based config file name.
    if hex_form:
        ip = ip_address.split('.')
        ip_address = '{0:02X}{1:02X}{2:02X}{3:02X}'.format(*map(int, ip))

    # grub2 bootloader needs ip based config file name.
    return os.path.join(
        CONF.pxe.tftp_root, ip_address + ".conf"
    )


def get_deploy_kr_info(node_uuid, driver_info):
    """Get href and tftp path for deploy kernel and ramdisk.

    Note: driver_info should be validated outside of this method.
    """
    root_dir = get_root_dir()
    image_info = {}
    for label in ('deploy_kernel', 'deploy_ramdisk'):
        image_info[label] = (
            str(driver_info[label]),
            os.path.join(root_dir, node_uuid, label)
        )
    return image_info


def get_pxe_config_file_path(node_uuid):
    """Generate the path for the node's PXE configuration file.

    :param node_uuid: the UUID of the node.
    :returns: The path to the node's PXE configuration file.

    """
    return os.path.join(get_root_dir(), node_uuid, 'config')


def create_pxe_config(task, pxe_options, template=None, on_disk=True):
    """Generate PXE configuration file and MAC address links for it.

    This method will generate the PXE configuration file for the task's
    node under a directory named with the UUID of that node. For each
    MAC address or DHCP IP address (port) of that node, a symlink for
    the configuration file will be created under the PXE configuration
    directory, so regardless of which port boots first they'll get the
    same PXE configuration.
    If elilo is the bootloader in use, then its configuration file will
    be created based on hex form of DHCP IP address.
    If grub2 bootloader is in use, then its configuration will be created
    based on DHCP IP address in the form nn.nn.nn.nn.

    :param task: A TaskManager instance.
    :param pxe_options: A dictionary with the PXE configuration
        parameters.
    :param template: The PXE configuration template. If no template is
        given the node specific template will be used.
    :param on_disk: if True, created config is saved as a file on disk.
    :returns: created config as a string
    """
    LOG.debug("Building PXE config for node %s", task.node.uuid)

    if template is None:
        template = deploy_utils.get_pxe_config_template(task.node)

    is_uefi_boot_mode = (deploy_utils.get_boot_mode_for_deploy(task.node) ==
                         'uefi')

    # grub bootloader panics with '{}' around any of its tags in its
    # config file. To overcome that 'ROOT' and 'DISK_IDENTIFIER' are enclosed
    # with '(' and ')' in uefi boot mode.
    # These changes do not have any impact on elilo bootloader.
    hex_form = True
    if is_uefi_boot_mode and utils.is_regex_string_in_file(template,
                                                           '^menuentry'):
        hex_form = False
        pxe_config_root_tag = '(( ROOT ))'
        pxe_config_disk_ident = '(( DISK_IDENTIFIER ))'
    else:
        # TODO(stendulker): We should use '(' ')' as the delimiters for all our
        # config files so that we do not need special handling for each of the
        # bootloaders. Should be removed once the Mitaka release starts.
        pxe_config_root_tag = '{{ ROOT }}'
        pxe_config_disk_ident = '{{ DISK_IDENTIFIER }}'

    params = {'pxe_options': pxe_options}
    if not CONF.pxe.ipxe_enabled:
        params.update({'ROOT': pxe_config_root_tag,
                       'DISK_IDENTIFIER': pxe_config_disk_ident})
        pxe_config = utils.render_template(template, params)
    else:
        pxe_config = _render_ipxe_template(template, params)

    if on_disk:
        _ensure_config_dirs_exist(task.node.uuid)
        pxe_config_file_path = get_pxe_config_file_path(task.node.uuid)
        utils.write_to_file(pxe_config_file_path, pxe_config)

        if is_uefi_boot_mode and not CONF.pxe.ipxe_enabled:
            _link_ip_address_pxe_configs(task, hex_form)
        else:
            _link_mac_pxe_configs(task)
    return pxe_config


def clean_up_pxe_config(task):
    """Clean up the TFTP environment for the task's node.

    :param task: A TaskManager instance.

    """
    LOG.debug("Cleaning up PXE config for node %s", task.node.uuid)

    is_uefi_boot_mode = (deploy_utils.get_boot_mode_for_deploy(task.node) ==
                         'uefi')
    if is_uefi_boot_mode and not CONF.pxe.ipxe_enabled:
        api = dhcp_factory.DHCPFactory().provider
        ip_addresses = api.get_ip_addresses(task)
        if not ip_addresses:
            return

        for port_ip_address in ip_addresses:
            try:
                # Get xx.xx.xx.xx based grub config file
                ip_address_path = _get_pxe_ip_address_path(port_ip_address,
                                                           False)
                # Get 0AOAOAOA based elilo config file
                hex_ip_path = _get_pxe_ip_address_path(port_ip_address,
                                                       True)
            except exception.InvalidIPv4Address:
                continue
            # Cleaning up config files created for grub2.
            ironic_utils.unlink_without_raise(ip_address_path)
            # Cleaning up config files created for elilo.
            ironic_utils.unlink_without_raise(hex_ip_path)
    else:
        for port in task.ports:
            client_id = port.extra.get('client-id')
            ironic_utils.unlink_without_raise(
                _get_pxe_mac_path(port.address, client_id=client_id))

    utils.rmtree_without_raise(os.path.join(get_root_dir(),
                                            task.node.uuid))


def dhcp_options_for_instance(task):
    """Retrieves the DHCP PXE boot options.

    :param task: A TaskManager instance.
    """
    dhcp_opts = []

    boot_file = deploy_utils.get_pxe_boot_file(task.node)

    if CONF.pxe.ipxe_enabled:
        script_name = os.path.basename(CONF.pxe.ipxe_boot_script)
        ipxe_script_url = '/'.join([CONF.deploy.http_url, script_name])
        dhcp_provider_name = dhcp_factory.CONF.dhcp.dhcp_provider
        # if the request comes from dumb firmware send them the iPXE
        # boot image.
        if dhcp_provider_name == 'neutron':
            # Neutron use dnsmasq as default DHCP agent, add extra config
            # to neutron "dhcp-match=set:ipxe,175" and use below option
            dhcp_opts.append({'opt_name': 'tag:!ipxe,bootfile-name',
                              'opt_value': boot_file})
            dhcp_opts.append({'opt_name': 'tag:ipxe,bootfile-name',
                              'opt_value': ipxe_script_url})
        else:
            # !175 == non-iPXE.
            # http://ipxe.org/howto/dhcpd#ipxe-specific_options
            dhcp_opts.append({'opt_name': '!175,bootfile-name',
                              'opt_value': boot_file})
            dhcp_opts.append({'opt_name': 'bootfile-name',
                              'opt_value': ipxe_script_url})
    else:
        dhcp_opts.append({'opt_name': 'bootfile-name',
                          'opt_value': boot_file})
        # 210 == tftp server path-prefix or tftp root, will be used to find
        # pxelinux.cfg directory. The pxelinux.0 loader infers this information
        # from it's own path, but Petitboot needs it to be specified by this
        # option since it doesn't use pxelinux.0 loader.
        dhcp_opts.append({'opt_name': '210',
                          'opt_value': get_tftp_path_prefix()})

    dhcp_opts.append({'opt_name': 'server-ip-address',
                      'opt_value': CONF.pxe.tftp_server})
    dhcp_opts.append({'opt_name': 'tftp-server',
                      'opt_value': CONF.pxe.tftp_server})

    # Append the IP version for all the configuration options
    for opt in dhcp_opts:
        opt.update({'ip_version': int(CONF.pxe.ip_version)})

    return dhcp_opts


def get_tftp_path_prefix():
    """Adds trailing slash (if needed) necessary for path-prefix

    :return: CONF.pxe.tftp_root ensured to have a trailing slash
    """
    return os.path.join(CONF.pxe.tftp_root, '')


def get_path_relative_to_tftp_root(file_path):
    """Return file relative path to CONF.pxe.tftp_root

    :param file_path: full file path to be made relative path.
    :returns: The path relative to CONF.pxe.tftp_root
    """
    return os.path.relpath(file_path, get_tftp_path_prefix())


def warn_on_legacy_ipxe_templates():
    msg = _LW("The specified iPXE boot config template %s is not a proper "
              "Jinja template and must be updated. "
              "It will be fixed in-memory. "
              "This is deprecated and will be impossible in Pike release.")
    if CONF.pxe.ipxe_enabled:
        templates = CONF.pxe.pxe_config_template_by_arch.values()
        templates.extend([CONF.pxe.uefi_pxe_config_template,
                          CONF.pxe.pxe_config_template])
        for path in set(templates):
            with open(path) as f:
                t = f.read()
            if _is_legacy_ipxe_template(t):
                LOG.warning(msg, path)


def _is_legacy_ipxe_template(template):
    is_proper_template = "goto {{ pxe_options" in template
    is_ipxe = template.split()[0].startswith("#!ipxe")
    return is_ipxe and not is_proper_template


def _render_ipxe_template(template_path, params):
    """Renders iPXE template with legacy template support.

    Template file is read and strings replaced if required.
    Template file on disk is not changed.

    :param template_path: path to the iPXE boot config template
    :param params: dict of parameters to render the template with
    :returns: rendered template as string
    """
    # TODO(pas-ha) this is legacy path for iPXE boot configs which
    # are not proper Jinja templates.
    # It should be removed once we stop supporting such
    # out-of-tree templates and replaced with simple
    # utils.render_template call
    with open(template_path) as f:
        template = f.read()
    if _is_legacy_ipxe_template(template):
        pattern = "^goto .*$"
        compiled = re.compile(pattern, re.M)
        replacement = 'goto {{ pxe_options.boot_target }}'
        template = compiled.sub(replacement, template)
    return utils.render_template(template, params, is_file=False)


def create_service_ipxe_config(node, pxe_options, root_uuid_or_disk_id,
                               on_disk=True):
    """Render iPXE boot config template for netboot-ed node

    :param node: Ironic Node object
    :param pxe_options: A dictionary with the PXE configuration
        parameters.
    :param root_uuid_or_disk_id: identifier of boot target on disk
    :param on_disk: if True, created config is saved as a file on disk.
    :returns: created config as a string
    """

    LOG.debug("Building service PXE config for node %s", node.uuid)
    iwdi = node.driver_internal_info.get('is_whole_disk_image')
    trusted_boot = deploy_utils.is_trusted_boot_requested(node)

    if iwdi:
        boot_disk_type = 'boot_whole_disk'
    elif trusted_boot:
        boot_disk_type = 'trusted_boot'
    else:
        boot_disk_type = 'boot_partition'

    pxe_options['boot_target'] = boot_disk_type
    disk_tag = 'DISK_IDENTIFIER' if iwdi else 'ROOT'
    template_options = {'pxe_options': pxe_options,
                        disk_tag: root_uuid_or_disk_id}
    template_path = deploy_utils.get_pxe_config_template(node)

    pxe_config = _render_ipxe_template(template_path, template_options)
    if on_disk:
        pxe_config_file_path = get_pxe_config_file_path(node.uuid)
        # NOTE(pas-ha) here we rely on the assumption that the proper
        # directories are already created, and proper MAC-based links are made
        # during provisioning, so that we just need to re-write existing file
        utils.write_to_file(pxe_config_file_path, pxe_config)
    return pxe_config
