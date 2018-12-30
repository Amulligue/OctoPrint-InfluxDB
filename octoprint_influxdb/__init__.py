# coding=utf-8
from __future__ import absolute_import

import platform
import datetime

import octoprint.plugin
import octoprint.util
import influxdb
import requests.exceptions

__plugin_name__ = "InfluxDB Plugin"

def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = InfluxDBPlugin()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
	}

class InfluxDBPlugin(octoprint.plugin.EventHandlerPlugin,
                     octoprint.plugin.SettingsPlugin,
                     octoprint.plugin.StartupPlugin,
                     octoprint.plugin.TemplatePlugin):

	## our logic

	def __init__(self):
		self.influx_timer = None
		self.influx_db = None
		self.influx_kwargs = None
		self.influx_common_tags = {
			'host': platform.node(),
		}

	def influx_flash_exception(self, message):
		self._logger.exception(message)
		# FIXME flash something to the user, probably needs JS

	def influx_try_connect(self, kwargs):
		# create a safe copy we can dump out to the log, modify fields
		kwargs = kwargs.copy()
		kwargs_safe = kwargs.copy()
		for k in ['username', 'password']:
			if k in kwargs_safe:
				del kwargs_safe[k]
		kwargs_log = ", ".join("{}={!r}".format(*v) for v in sorted(kwargs_safe.items()))
		self._logger.info("connecting: {}".format(kwargs_log))

		dbname = 'octoprint'
		if 'database' in kwargs:
			dbname = kwargs.pop('database')

		try:
			db = influxdb.InfluxDBClient(**kwargs)
			db.ping()
		except Exception:
			# something went wrong connecting :(
			self.influx_flash_exception('Cannot connect to InfluxDB server.')
			return None
		try:
			db.create_database(dbname)
			db.switch_database(dbname)
		except Exception:
			# something went wrong making the database
			self.influx_flash_exception('Cannot create InfluxDB database.')
			return None
		return db

	def influx_reconnect(self):
		# stop the old timer, if we need to
		if self.influx_timer:
			self.influx_timer.cancel()
			self.influx_timer = None

		# build up some kwargs to pass to InfluxDBClient
		kwargs = {}
		def add_arg_if_exists(kwargsname, path, getter=self._settings.get):
			v = getter(path)
			if v:
				kwargs[kwargsname] = v

		add_arg_if_exists('host', ['host'])
		add_arg_if_exists('port', ['port'], self._settings.get_int)
		if self._settings.get_boolean(['authenticate']):
			add_arg_if_exists('username', ['username'])
			add_arg_if_exists('password', ['password'])
		add_arg_if_exists('database', ['database'])
		kwargs['ssl'] = self._settings.get_boolean(['ssl'])
		if kwargs['ssl']:
			kwargs['verify_ssl'] = self._settings.get_boolean(['verify_ssl'])
		kwargs['use_udp'] = self._settings.get_boolean(['udp'])
		if kwargs['use_udp'] and 'port' in kwargs:
			kwargs['udp_port'] = kwargs['port']
			del kwargs['port']

		if self.influx_db is None or kwargs != self.influx_kwargs:
			self.influx_db = self.influx_try_connect(kwargs)
			if self.influx_db:
				self.influx_kwargs = kwargs
				self.influx_prefix = self._settings.get(['prefix']) or ''

		# start a new timer
		if self.influx_db:
			interval = self._settings.get_float(['interval'], min=0)
			if not interval:
				interval = self.get_settings_defaults()['interval']
			self.influx_timer = octoprint.util.RepeatedTimer(interval, self.influx_gather)
			self.influx_timer.start()

	def influx_emit(self, measurement, fields, extra_tags={}):
		tags = self.influx_common_tags.copy()
		tags.update(extra_tags)
		# python doesn't put the Z at the end
		# because python cannot into timezones until Python 3
		time = datetime.datetime.utcnow().isoformat() + 'Z'
		point = {
			'measurement': self.influx_prefix + measurement,
			'tags': tags,
			'time': time,
			'fields': fields,
		}
		try:
			self.influx_db.write_points([point])
		except Exception:
			# we were dropped! try to reconnect
			self.influx_flash_exception("Disconnected from InfluxDB. Attempting to reconnect.")
			self.influx_db = None
			self.influx_reconnect()

	def influx_gather(self):
		# if we're not connected to a database, do nothing
		if not self.influx_db:
			return
		# if we're not connected to a printer, do nothing
		if not self._printer.is_operational():
			return

		temps = self._printer.get_current_temperatures()
		if not temps:
			return

		fields = {}
		for sensor in temps:
			for subfield in temps[sensor]:
				fields[sensor + '_' + subfield] = temps[sensor][subfield]

		self.influx_emit('temperature', fields)

	##~~ EventHandlerPlugin mixin

	# what events should we report to influx
	influx_events = set([
		'PrintStarted',
		'PrintFailed',
		'PrintDone',
		'PrintCancelled',
		'PrintPaused',
		'PrintResumed',
	])

	# what are bad names for tags that we should change
	influx_tag_blacklist = set([
		'time',
	])

	def on_event(self, event, payload):
		if not event in self.influx_events:
			return
		tags = payload.copy()
		for tag in list(tags.keys()):
			if tag in self.influx_tag_blacklist:
				tags[tag + '_'] = tags[tag]
				del tags[tag]
		self.influx_emit('events', {'type': event}, extra_tags=tags)

	##~~ SettingsPlugin mixin

	def get_settings_version(self):
		return 0

	def get_settings_defaults(self):
		return dict(
			host=None,
			port=None,
			authenticate=False,
			udp=False,
			ssl=False,
			verify_ssl=True,
			database='octoprint',
			prefix='',
			username=None,
			password=None,

			interval=1,
		)

	def get_settings_restricted_paths(self):
		return dict(admin=[
			['username'],
			['password'],
		])

	def on_settings_migrate(self, target, current):
		if current is None:
			current = 0
		# do migration here, incrementing current
		if target != current:
			raise RuntimeError("could not migrate InfluxDB settings")

	def on_settings_save(self, data):
		octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
		self.influx_reconnect()

	##~~ StartupPlugin mixin

	def on_after_startup(self):
		self.influx_reconnect()

	##~~ TemplatePlugin mixin

	def get_template_configs(self):
		return [
			dict(type="settings", custom_bindings=False),
		]

	##~~ Softwareupdate hook

	def get_update_information(self):
		return dict(
			influxdb=dict(
				displayName="InfluxDB Plugin",
				displayVersion=self._plugin_version,

				# version check: github repository
				type="github_release",
				user="agrif",
				repo="OctoPrint-InfluxDB",
				current=self._plugin_version,

				# update method: pip
				pip="https://github.com/agrif/OctoPrint-InfluxDB/archive/{target_version}.zip"
			)
		)
