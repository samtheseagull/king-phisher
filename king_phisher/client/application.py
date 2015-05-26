#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  king_phisher/client/application.py
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are
#  met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following disclaimer
#    in the documentation and/or other materials provided with the
#    distribution.
#  * Neither the name of the project nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
#  "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
#  LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
#  A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
#  OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
#  SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
#  LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
#  DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
#  THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#

import copy
import ipaddress
import json
import logging
import os
import random
import shutil
import socket
import sys
import time
import uuid

from king_phisher import find
from king_phisher import utilities
from king_phisher import version
from king_phisher.client import client
from king_phisher.client import client_rpc
from king_phisher.client import dialogs
from king_phisher.client import graphs
from king_phisher.client import gui_utilities
from king_phisher.client import tools
from king_phisher.ssh_forward import SSHTCPForwarder
from king_phisher.third_party.AdvancedHTTPServer import AdvancedHTTPServerRPCError

from gi.repository import GLib
from gi.repository import GObject
from gi.repository import Gtk
import paramiko

CONFIG_FILE_PATH = '~/.king_phisher.json'
"""The default search location for the client configuration file."""

if isinstance(Gtk.Application, utilities.Mock):
	_Gtk_Application = type('Gtk.Application', (object,), {})
	_Gtk_Application.__module__ = ''
else:
	_Gtk_Application = Gtk.Application

class KingPhisherClientApplication(_Gtk_Application):
	"""
	This is the top level King Phisher client object. It contains the
	custom GObject signals, keeps all the GUI references, and manages
	the RPC client object. This is also the parent window for most
	GTK objects.

	:GObject Signals: :ref:`gobject-signals-application-label`
	"""
	__gsignals__ = {
		'campaign-set': (GObject.SIGNAL_RUN_FIRST, None, (str,)),
		'server-connected': (GObject.SIGNAL_RUN_FIRST, None, ())
	}
	def __init__(self, config_file=None):
		super(KingPhisherClientApplication, self).__init__()
		self.logger = logging.getLogger('KingPhisher.Client.Application')
		# log version information for debugging purposes
		self.logger.debug("gi.repository GLib version: {0}".format('.'.join(map(str, GLib.glib_version))))
		self.logger.debug("gi.repository GObject version: {0}".format('.'.join(map(str, GObject.pygobject_version))))
		self.logger.debug("gi.repository Gtk version: {0}.{1}.{2}".format(Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version()))
		if tools.has_vte:
			self.logger.debug("gi.repository VTE version: {0}".format(tools.Vte._version))
		if graphs.has_matplotlib:
			self.logger.debug("matplotlib version: {0}".format(graphs.matplotlib.__version__))
		self.set_property('application-id', 'org.king-phisher.client')
		self.set_property('register-session', True)
		self.config_file = (config_file or CONFIG_FILE_PATH)
		"""The file containing the King Phisher client configuration."""
		self.config = None
		"""The main King Phisher client configuration."""
		self.rpc = None
		self._ssh_forwarder = None
		"""The SSH forwarder responsible for tunneling RPC communications."""
		try:
			self.load_config(load_defaults=True)
		except IOError:
			self.logger.critical('failed to load the client configuration')
			raise

	def _create_ssh_forwarder(self, server, username, password):
		"""
		Create and set the
		:py:attr:`~.KingPhisherClientApplication._ssh_forwarder` attribute.

		:param tuple server: The server information as a host and port tuple.
		:param str username: The username to authenticate to the SSH server with.
		:param str password: The password to authenticate to the SSH server with.
		:rtype: int
		:return: The local port that is forwarded to the remote server or None if the connection failed.
		"""
		active_window = self.get_active_window()
		title_ssh_error = 'Failed To Connect To The SSH Service'
		server_remote_port = self.config['server_remote_port']
		local_port = random.randint(2000, 6000)

		try:
			self._ssh_forwarder = SSHTCPForwarder(server, username, password, local_port, ('127.0.0.1', server_remote_port), preferred_private_key=self.config['ssh_preferred_key'])
			self._ssh_forwarder.start()
			time.sleep(0.5)
			self.logger.info('started ssh port forwarding')
		except paramiko.AuthenticationException:
			self.logger.warning('failed to authenticate to the remote ssh server')
			gui_utilities.show_dialog_error(title_ssh_error, active_window, 'The server responded that the credentials are invalid.')
		except socket.error as error:
			gui_utilities.show_dialog_exc_socket_error(error, active_window, title=title_ssh_error)
		except Exception as error:
			self.logger.warning('failed to connect to the remote ssh server', exc_info=True)
			gui_utilities.show_dialog_error(title_ssh_error, active_window, "An {0}.{1} error occurred.".format(error.__class__.__module__, error.__class__.__name__))
		else:
			return local_port
		self.server_disconnect()
		return

	def exception_hook(self, exc_type, exc_value, exc_traceback):
		if isinstance(exc_value, KeyboardInterrupt):
			self.logger.warning('received a KeyboardInterrupt exception')
			return
		exc_info = (exc_type, exc_value, exc_traceback)
		error_uid = str(uuid.uuid4())
		self.logger.error("error uid: {0} an unhandled exception was thrown".format(error_uid), exc_info=exc_info)
		dialogs.ExceptionDialog(self.config, self.get_active_window(), exc_info=exc_info, error_uid=error_uid).interact()

	def do_activate(self):
		Gtk.Application.do_activate(self)
		sys.excepthook = self.exception_hook

		win = client.KingPhisherClient(self.config, self)
		win.set_position(Gtk.WindowPosition.CENTER)
		win.show()

	def do_campaign_set(self, campaign_id):
		self.rpc.cache_clear()
		self.logger.info("campaign set to {0} (id: {1})".format(self.config['campaign_name'], self.config['campaign_id']))

	def do_server_connected(self):
		self.load_server_config()
		campaign_id = self.config.get('campaign_id')
		if campaign_id == None:
			if not self.show_campaign_selection():
				self.logger.debug('no campaign selected, disconnecting and exiting')
				self.emit('exit')
				return True
		campaign_info = self.rpc.remote_table_row('campaigns', self.config['campaign_id'], cache=True)
		if campaign_info == None:
			if not self.show_campaign_selection():
				self.logger.debug('no campaign selected, disconnecting and exiting')
				self.emit('exit')
				return True
			campaign_info = self.rpc.remote_table_row('campaigns', self.config['campaign_id'], cache=True, refresh=True)
		self.config['campaign_name'] = campaign_info.name
		self.emit('campaign-set', self.config['campaign_id'])
		return

	def do_shutdown(self):
		Gtk.Application.do_shutdown(self)
		sys.excepthook = sys.__excepthook__
		self.save_config()

	def load_config(self, load_defaults=False):
		"""
		Load the client configuration from disk and set the
		:py:attr:`~.KingPhisherClientApplication.config` attribute.

		:param bool load_defaults: Load missing options from the template configuration file.
		"""
		self.logger.info('loading the config from disk')
		config_file = os.path.expanduser(self.config_file)
		client_template = find.find_data_file('client_config.json')
		if not (os.path.isfile(config_file) and os.stat(config_file).st_size):
			shutil.copy(client_template, config_file)
		with open(config_file, 'r') as tmp_file:
			self.config = json.load(tmp_file)
		if load_defaults:
			with open(client_template, 'r') as tmp_file:
				client_template = json.load(tmp_file)
			for key, value in client_template.items():
				if not key in self.config:
					self.config[key] = value

	def load_server_config(self):
		"""Load the necessary values from the server's configuration."""
		self.config['server_config'] = self.rpc('config/get', ['server.require_id', 'server.secret_id', 'server.tracking_image', 'server.web_root'])
		return

	def save_config(self):
		"""Write the client configuration to disk."""
		self.logger.info('writing the client configuration to disk')
		config = copy.copy(self.config)
		for key in self.config.keys():
			if 'password' in key or key == 'server_config':
				del config[key]
		config_file = os.path.expanduser(self.config_file)
		config_file_h = open(config_file, 'w')
		json.dump(config, config_file_h, sort_keys=True, indent=2, separators=(',', ': '))

	def server_connect(self):
		server_version_info = None
		title_rpc_error = 'Failed To Connect To The King Phisher RPC Service'
		active_window = self.get_active_window()

		server = utilities.server_parse(self.config['server'], 22)
		username = self.config['server_username']
		password = self.config['server_password']
		if server[0] == 'localhost' or (utilities.is_valid_ip_address(server[0]) and ipaddress.ip_address(server[0]).is_loopback):
			local_port = self.config['server_remote_port']
			self.logger.info("connecting to local king-phisher instance")
		else:
			local_port = self._create_ssh_forwarder(server, username, password)
		if not local_port:
			return

		self.rpc = client_rpc.KingPhisherRPCClient(('localhost', local_port), username=username, password=password, use_ssl=self.config.get('server_use_ssl'))
		if self.config.get('rpc.serializer'):
			try:
				self.rpc.set_serializer(self.config['rpc.serializer'])
			except ValueError as error:
				self.logger.error("failed to set the rpc serializer, error: '{0}'".format(error.message))

		connection_failed = True
		try:
			assert self.rpc('client/initialize')
			server_version_info = self.rpc('version')
			assert server_version_info != None
		except AdvancedHTTPServerRPCError as err:
			if err.status == 401:
				self.logger.warning('failed to authenticate to the remote king phisher service')
				gui_utilities.show_dialog_error(title_rpc_error, active_window, 'The server responded that the credentials are invalid.')
			else:
				self.logger.warning('failed to connect to the remote rpc server with http status: ' + str(err.status))
				gui_utilities.show_dialog_error(title_rpc_error, active_window, 'The server responded with HTTP status: ' + str(err.status))
		except socket.error as error:
			gui_utilities.show_dialog_exc_socket_error(error, active_window)
		except Exception as error:
			self.logger.warning('failed to connect to the remote rpc service', exc_info=True)
			gui_utilities.show_dialog_error(title_rpc_error, active_window, 'Ensure that the King Phisher Server is currently running.')
		else:
			connection_failed = False
		finally:
			if connection_failed:
				self.rpc = None
				self.server_disconnect()
				return

		server_rpc_api_version = server_version_info.get('rpc_api_version', -1)
		if isinstance(server_rpc_api_version, int):
			# compatibility with pre-0.2.0 version
			server_rpc_api_version = (server_rpc_api_version, 0)
		self.logger.info("successfully connected to the king phisher server (version: {0} rpc api version: {1}.{2})".format(server_version_info['version'], server_rpc_api_version[0], server_rpc_api_version[1]))

		error_text = None
		if server_rpc_api_version[0] < version.rpc_api_version.major or (server_rpc_api_version[0] == version.rpc_api_version.major and server_rpc_api_version[1] < version.rpc_api_version.minor):
			error_text = 'The server is running an old and incompatible version.'
			error_text += '\nPlease update the remote server installation.'
		elif server_rpc_api_version[0] > version.rpc_api_version.major:
			error_text = 'The client is running an old and incompatible version.'
			error_text += '\nPlease update the local client installation.'
		if error_text:
			gui_utilities.show_dialog_error('The RPC API Versions Are Incompatible', active_window, error_text)
			self.server_disconnect()
			return
		self.emit('server-connected')
		return

	def server_disconnect(self):
		"""Clean up the SSH TCP connections and disconnect from the server."""
		if self._ssh_forwarder:
			self._ssh_forwarder.stop()
			self._ssh_forwarder = None
			self.logger.info('stopped ssh port forwarding')
		self.rpc = None
		return

	def show_campaign_selection(self):
		"""
		Display the campaign selection dialog in a new
		:py:class:`.CampaignSelectionDialog` instance.

		:return: The status of the dialog.
		:rtype: bool
		"""
		dialog = dialogs.CampaignSelectionDialog(self.config, self.get_active_window())
		return dialog.interact() == Gtk.ResponseType.APPLY
