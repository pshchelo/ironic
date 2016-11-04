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

"""iPXE-related functionality"""

import abc

import six


@six.add_metaclass(abc.ABCMeta)
class DeployIPXEMixin(object):
    """Adds methods for dynamic iPXE to deploy interface."""

    @abc.abstractmethod
    def get_ipxe_extra_options(self, task):
        """Return deploy interface related options for dynamic iPXE.

        :param task: a task from TaskManager.
        :returns: an (empty) dict with deploy interface-related options
                  to render the boot config file template with.
        """


@six.add_metaclass(abc.ABCMeta)
class BootIPXEMixin(object):
    """Adds methods for dynamic iPXE to boot interface."""

    @abc.abstractmethod
    def get_ipxe_config(self, task, ramdisk_options):
        """Generate complete boot config for dynamic iPXE.

        :param task: a task from TaskManager
        :param ramdisk_options: a dict with extra pxe options obtained
                                from deploy interface
        :returns:  rendered iPXE boot config template as a string
        """
