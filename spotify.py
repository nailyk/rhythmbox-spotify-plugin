# -*- Mode: python; coding: utf-8; tab-width: 8; indent-tabs-mode: t; -*-
#
# Copyright (C) 2015 Kylian Deau
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2, or (at your option)
# any later version.
#
# The Rhythmbox authors hereby grant permission for non-GPL compatible
# GStreamer plugins to be used and distributed together with GStreamer
# and Rhythmbox. This permission is above and beyond the permissions granted
# by the GPL license by which Rhythmbox is covered. If you modify this code
# you may extend this exception to your version of the code, but you are not
# obligated to do so. If you do not wish to do so, delete this exception
# statement from your version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301  USA.

from gi.repository import Gtk, Gdk, GObject, Gio, GLib, Peas
from gi.repository import RB
import rb
import urllib.parse
import requests
import json
from datetime import datetime

import gettext
gettext.install('rhythmbox', RB.locale_dir())


class SpotifyEntryType(RB.RhythmDBEntryType):
	def __init__(self):
		RB.RhythmDBEntryType.__init__(self, name='spotify')

	def do_get_playback_uri(self, entry):
		uri = entry.get_string(RB.RhythmDBPropType.MOUNTPOINT)
		return uri

	def do_can_sync_metadata(self, entry):
		return False


class SpotifyPlugin(GObject.Object, Peas.Activatable):
	__gtype_name = 'SpotifyPlugin'
	object = GObject.property(type=GObject.GObject)

	def __init__(self):
		GObject.Object.__init__(self)

	def do_activate(self):
		shell = self.object

		rb.append_plugin_source_path(self, "icons")

		db = shell.props.db

		self.entry_type = SpotifyEntryType()
		db.register_entry_type(self.entry_type)

		model = RB.RhythmDBQueryModel.new_empty(db)
		self.source = GObject.new (SpotifySource,
					   shell=shell,
					   name=_("Spotify"),
					   plugin=self,
					   query_model=model,
					   entry_type=self.entry_type,
					   icon=Gio.ThemedIcon.new("spotify-symbolic"))
		shell.register_entry_type_for_source(self.source, self.entry_type)
		self.source.setup()
		group = RB.DisplayPageGroup.get_by_id ("shared")
		shell.append_display_page(self.source, group)

	def do_deactivate(self):
		self.source.delete_thyself()
		self.source = None

class SpotifySource(RB.StreamingSource):
	def __init__(self, **kwargs):
		super(SpotifySource, self).__init__(kwargs)
		self.loader = None

		self.search_count = 1
		self.search_types = {
			'tracks': {
				'label': _("Search tracks"),
				'placeholder': _("Search tracks on Spotify"),
				'title': "",	# container view is hidden
				'endpoint': '/v1/search?type=track',
				'containers': False
			},
			'artists': {
				'label': _("Search artists"),
				'placeholder': _("Search artists on Spotify"),
				'title': _("Spotify Artists"),
				'endpoint': '/v1/search?type=artist',
				'containers': True
			},
			'albums': {
				'label': _("Search albums"),
				'placeholder': _("Search albums on Spotify"),
				'title': _("Spotify albums"),
				'endpoint': '/v1/search?type=album',
				'containers': True
			},
		}

		self.container_types = {
			'artist',
			'album',
		}

	def hide_entry_cb(self, entry):
		shell = self.props.shell
		shell.props.db.entry_set(entry, RB.RhythmDBPropType.HIDDEN, True)

	def new_model(self):
		shell = self.props.shell
		plugin = self.props.plugin
		db = shell.props.db

		self.search_count = self.search_count + 1
		q = GLib.PtrArray()
		db.query_append_params(q, RB.RhythmDBQueryType.EQUALS, RB.RhythmDBPropType.TYPE, plugin.entry_type)
		db.query_append_params(q, RB.RhythmDBQueryType.EQUALS, RB.RhythmDBPropType.LAST_SEEN, self.search_count)
		model = RB.RhythmDBQueryModel.new_empty(db)

		db.do_full_query_async_parsed(model, q)
		self.props.query_model = model
		self.songs.set_model(model)

	def add_track(self, db, entry_type, item, cover=None):
		uri = item['uri']
		entry = db.entry_lookup_by_location(uri)
		if entry:
			db.entry_set(entry, RB.RhythmDBPropType.LAST_SEEN, self.search_count)
		else:
			entry = RB.RhythmDBEntry.new(db, entry_type, item['uri'])
			db.entry_set(entry, RB.RhythmDBPropType.MOUNTPOINT, item['preview_url'])
			artist_name = item['artists'][0]['name']
			if (len(item['artists']) > 1):
				for other_artist in item['artists'][1:]:
					artist_name += ', ' + other_artist['name']
			db.entry_set(entry, RB.RhythmDBPropType.ARTIST, artist_name)
			db.entry_set(entry, RB.RhythmDBPropType.TITLE, item['name'])
			db.entry_set(entry, RB.RhythmDBPropType.LAST_SEEN, self.search_count)
			db.entry_set(entry, RB.RhythmDBPropType.DURATION, 30)
			if cover is None:
				cover = item['album']['images'][2]['url']
			db.entry_set(entry, RB.RhythmDBPropType.MB_ALBUMID, cover)
				
		db.commit()

	def add_container(self, item):
		k = item['type']
		if k not in self.container_types:
			return
		self.containers.append([item['name'], item['type'], item['id'], item['external_urls']['spotify']])

	def search_tracks_api_cb(self, data, search_type, cover=None):
		if data is None:
			return

		shell = self.props.shell
		db = shell.props.db
		entry_type = self.props.entry_type
		data = data.decode('utf-8')
		stuff = json.loads(data)

		# with the spotify API, tracks json objects are not always at the same position depending on the query
		if search_type == 'tracks':
			tracks = stuff['tracks']['items']
		elif search_type == 'artists':
			tracks = stuff['tracks']
		elif search_type == 'albums':
			tracks = stuff['items']
		else:
			return

		for item in tracks:
			self.add_track(db, entry_type, item, cover)

	def search_cover_api(self, url):
		data = requests.get(url)
		stuff = data.json()
		return stuff['images'][2]['url']
	
	def search_containers_api_cb(self, data, search_type):
		if data is None:
			return
		entry_type = self.props.entry_type
		data = data.decode('utf-8')
		stuff = json.loads(data)

		for item in stuff[search_type]['items']:
			self.add_container(item)

	def cancel_request(self):
		if self.loader:
			self.loader.cancel()
			self.loader = None

	def search_popup_cb(self, widget):
		self.search_popup.popup(None, None, None, None, 3, Gtk.get_current_event_time())

	def search_type_action_cb(self, action, parameter):
		print(parameter.get_string() + " selected")
		self.search_type = parameter.get_string()

		if self.search_entry.searching():
			self.do_search()

		st = self.search_types[self.search_type]
		self.search_entry.set_placeholder(st['placeholder'])

	def search_entry_cb(self, widget, term):
		self.search_text = term
		self.do_search()

	def do_search(self):
		self.cancel_request()

		base = 'https://api.spotify.com'
		self.new_model()
		self.containers.clear()
		term = self.search_text

		if self.search_type not in self.search_types:
			print("not sure how to search for " + self.search_type)
			return

		print("searching for " + self.search_type + " matching " + term)
		st = self.search_types[self.search_type]
		self.container_view.get_column(0).set_title(st['title'])

		url = base + st['endpoint'] + '&q=' + urllib.parse.quote(term)
		self.loader = rb.Loader()
		if st['containers']:
			self.scrolled.show()
			self.loader.get_url(url, self.search_containers_api_cb, self.search_type)
		else:
			self.scrolled.hide()
			self.loader.get_url(url, self.search_tracks_api_cb, self.search_type)


	def selection_changed_cb(self, selection):
		self.new_model()
		self.cancel_request()
		self.build_sp_menu()

		base = 'https://api.spotify.com'
		(model, aiter) = selection.get_selected()
		if aiter is None:
			return
		[itemtype, _id] = model.get(aiter, 1, 2)
		if itemtype not in self.container_types:
			return
		print("loading %s with id %s" % (itemtype, _id))

		self.loader = rb.Loader()

		if itemtype == 'artist':
			tracksurl = base + '/v1/artists/' + _id + '/top-tracks?country=US'
			print("tracksurl : " + tracksurl)
			self.loader.get_url(tracksurl, self.search_tracks_api_cb, 'artists')

		elif itemtype == 'album':
			cover_url = base + '/v1/albums/' + _id
			print("cover_url : " + cover_url)
			cover = self.search_cover_api(cover_url)
			tracksurl = base + '/v1/albums/' + _id + '/tracks'
			print("tracksurl : " + tracksurl)
			self.loader.get_url(tracksurl, self.search_tracks_api_cb, 'albums', cover)

	def sort_order_changed_cb(self, obj, pspec):
		obj.resort_model()

	def songs_selection_changed_cb(self, songs):
		self.build_sp_menu()

	def playing_entry_changed_cb(self, player, entry):
		self.build_sp_menu()
		if not entry:
			return
		if entry.get_entry_type() != self.props.entry_type:
			return

		au = entry.get_string(RB.RhythmDBPropType.MB_ALBUMID)
		if au:
			key = RB.ExtDBKey.create_storage("title", entry.get_string(RB.RhythmDBPropType.TITLE))
			key.add_field("artist", entry.get_string(RB.RhythmDBPropType.ARTIST))
			self.art_store.store_uri(key, RB.ExtDBSourceType.EMBEDDED, au)

	def open_uri_action_cb(self, action, param):
		shell = self.props.shell
		window = shell.props.window
		screen = window.get_screen()

		uri = param.get_string()
		Gtk.show_uri(screen, uri, Gdk.CURRENT_TIME)

	def build_sp_menu(self):
		menu = {}

		# playing track
		shell = self.props.shell
		player = shell.props.shell_player
		entry = player.get_playing_entry()
		if entry is not None and entry.get_entry_type() == self.props.entry_type:
			url = entry.get_string(RB.RhythmDBPropType.LOCATION)
			menu[url] = _("View '%(title)s' on Spotify") % {'title': entry.get_string(RB.RhythmDBPropType.TITLE) }
			# artist too?


		# selected track
		if self.songs.have_selection():
			entry = self.songs.get_selected_entries()[0]
			url = entry.get_string(RB.RhythmDBPropType.LOCATION)
			menu[url] = _("View '%(title)s' on Spotify") % {'title': entry.get_string(RB.RhythmDBPropType.TITLE) }
			# artist too?

		# selected container
		selection = self.container_view.get_selection()
		(model, aiter) = selection.get_selected()
		if aiter is not None:
			[name, url] = model.get(aiter, 0, 3)
			menu[url] = _("View '%(container)s' on Spotify") % {'container': name}

		if len(menu) == 0:
			self.sp_button.set_menu_model(None)
			self.sp_button.set_sensitive(False)
			return None

		m = Gio.Menu()
		for u in menu:
			i = Gio.MenuItem()
			i.set_label(menu[u])
			i.set_action_and_target_value("win.spotify-open-uri", GLib.Variant.new_string(u))
			m.append_item(i)
		self.sp_button.set_menu_model(m)
		self.sp_button.set_sensitive(True)

	def setup(self):
		shell = self.props.shell

		builder = Gtk.Builder()
		builder.add_from_file(rb.find_plugin_file(self.props.plugin, "spotify.ui"))

		self.scrolled = builder.get_object("container-scrolled")
		self.scrolled.set_no_show_all(True)
		self.scrolled.hide()

		self.search_entry = RB.SearchEntry(spacing=6)
		self.search_entry.props.explicit_mode = True

		action = Gio.SimpleAction.new("spotify-search-type", GLib.VariantType.new('s'))
		action.connect("activate", self.search_type_action_cb)
		shell.props.window.add_action(action)

		m = Gio.Menu()
		for st in sorted(self.search_types):
			i = Gio.MenuItem()
			i.set_label(self.search_types[st]['label'])
			i.set_action_and_target_value("win.spotify-search-type", GLib.Variant.new_string(st))
			m.append_item(i)

		self.search_popup = Gtk.Menu.new_from_model(m)

		action.activate(GLib.Variant.new_string("tracks"))

		grid = builder.get_object("spotify-source")

		self.search_entry.connect("search", self.search_entry_cb)
		self.search_entry.connect("activate", self.search_entry_cb)
		self.search_entry.connect("show-popup", self.search_popup_cb)
		self.search_entry.set_size_request(400, -1)
		builder.get_object("search-box").pack_start(self.search_entry, False, True, 0)



		self.search_popup.attach_to_widget(self.search_entry, None)

		self.containers = builder.get_object("container-store")
		self.container_view = builder.get_object("containers")
		self.container_view.set_model(self.containers)

		action = Gio.SimpleAction.new("spotify-open-uri", GLib.VariantType.new('s'))
		action.connect("activate", self.open_uri_action_cb)
		shell.props.window.add_action(action)

		r = Gtk.CellRendererText()
		c = Gtk.TreeViewColumn("", r, text=0)
		self.container_view.append_column(c)

		self.container_view.get_selection().connect('changed', self.selection_changed_cb)

		self.songs = RB.EntryView(db=shell.props.db,
					  shell_player=shell.props.shell_player,
					  is_drag_source=True,
					  is_drag_dest=False,
					  shadow_type=Gtk.ShadowType.NONE)
		self.songs.append_column(RB.EntryViewColumn.TITLE, True)
		self.songs.append_column(RB.EntryViewColumn.ARTIST, True)
		self.songs.append_column(RB.EntryViewColumn.DURATION, True)
		self.songs.set_model(self.props.query_model)
		self.songs.connect("notify::sort-order", self.sort_order_changed_cb)
		self.songs.connect("selection-changed", self.songs_selection_changed_cb)

		paned = builder.get_object("paned")
		paned.pack2(self.songs)

		self.bind_settings(self.songs, paned, None, True)

		self.sp_button = Gtk.MenuButton()
		self.sp_button.set_relief(Gtk.ReliefStyle.NONE)
		img = Gtk.Image.new_from_file(rb.find_plugin_file(self.props.plugin, "listen-on-spotify.png"))
		self.sp_button.add(img)
		box = builder.get_object("spotify-button-box")
		box.pack_start(self.sp_button, True, True, 0)

		self.build_sp_menu()

		self.pack_start(grid, expand=True, fill=True, padding=0)
		grid.show_all()

		self.art_store = RB.ExtDB(name="album-art")
		player = shell.props.shell_player
		player.connect('playing-song-changed', self.playing_entry_changed_cb)

	def do_get_entry_view(self):
		return self.songs

	def do_get_playback_status(self, text, progress):
		return self.get_progress()

	def do_can_copy(self):
		return False

GObject.type_register(SpotifySource)
