import base64
import datetime
import os
from binascii import unhexlify

import apsw
from pony import orm
from pony.orm import db_session
from six import text_type

from Tribler.Core.Modules.MetadataStore.OrmBindings.channel_node import LEGACY_ENTRY, NEW
from Tribler.Core.Modules.MetadataStore.serialization import REGULAR_TORRENT
from Tribler.Core.Modules.MetadataStore.store import MetadataStore
from Tribler.Core.Utilities.tracker_utils import get_uniformed_tracker_url
from Tribler.pyipv8.ipv8.database import database_blob
from Tribler.pyipv8.ipv8.keyvault.crypto import default_eccrypto

BATCH_SIZE = 10000

DISCOVERED_CONVERSION_STARTED = "discovered_conversion_started"
CHANNELS_CONVERSION_STARTED = "channels_conversion_started"
TRACKERS_CONVERSION_STARTED = "trackers_conversion_started"
PERSONAL_CONVERSION_STARTED = "personal_conversion_started"
CONVERSION_FINISHED = "conversion_finished"
CONVERSION_FROM_72 = "conversion_from_72"


def dispesy_cid_to_pk(dispersy_cid):
    return database_blob(unhexlify(("%X" % dispersy_cid).zfill(128)))


def pseudo_signature():
    return database_blob(os.urandom(32))


def final_timestamp():
    return 1 << 62


class DispersyToPonyMigration(object):
    select_channels_sql = "Select id, name, dispersy_cid, modified, nr_torrents, nr_favorite, nr_spam " \
                          + "FROM Channels " \
                          + "WHERE nr_torrents >= 3 " \
                          + "AND name not NULL;"

    select_trackers_sql = "select tracker_id, tracker, last_check, failures, is_alive from TrackerInfo"

    select_full = "SELECT" \
                  " (select ti.tracker from TorrentTrackerMapping ttm, TrackerInfo ti where ttm.torrent_id == t.torrent_id and ttm.tracker_id == ti.tracker_id and ti.tracker != 'DHT' and ti.tracker != 'http://retracker.local/announce' order by ti.is_alive asc, ti.failures desc, ti.last_check asc), " \
                  " ct.channel_id, ct.name, t.infohash, t.length, t.creation_date, t.torrent_id, t.category, t.num_seeders, t.num_leechers, t.last_tracker_check " \
                  "FROM _ChannelTorrents ct, Torrent t WHERE ct.name NOT NULL and t.length > 0 AND t.category NOT NULL AND ct.deleted_at IS NULL AND t.torrent_id == ct.torrent_id AND t.infohash NOT NULL "

    select_torrents_sql = " FROM _ChannelTorrents ct, Torrent t WHERE " + \
                          "ct.name NOT NULL and t.length>0 AND t.category NOT NULL AND ct.deleted_at IS NULL " + \
                          " AND t.torrent_id == ct.torrent_id AND t.infohash NOT NULL "

    def __init__(self, tribler_db, metadata_store, notifier_callback=None):
        self.notifier_callback = notifier_callback
        self.tribler_db = tribler_db
        self.mds = metadata_store

        self.personal_channel_id = None
        self.personal_channel_title = None

    def initialize(self):
        try:
            self.personal_channel_id, self.personal_channel_title = self.get_personal_channel_id_title()
            self.personal_channel_title = self.personal_channel_title[:200]  # limit the title size
        except:
            print ("No personal channel found")

    def get_old_channels(self):
        connection = apsw.Connection(self.tribler_db)
        cursor = connection.cursor()

        channels = []
        for id_, name, dispersy_cid, modified, nr_torrents, nr_favorite, nr_spam in cursor.execute(
                self.select_channels_sql):
            if nr_torrents and nr_torrents > 0:
                channels.append({"id_": 0,
                                 "infohash": database_blob(os.urandom(20)),
                                 "title": name or '',
                                 "public_key": dispesy_cid_to_pk(id_),
                                 "timestamp": final_timestamp(),
                                 "votes": int(nr_favorite or 0),
                                 "xxx": float(nr_spam or 0),
                                 "origin_id": 0,
                                 "signature": pseudo_signature(),
                                 "skip_key_check": True,
                                 "size": 0,
                                 "local_version": final_timestamp(),
                                 "subscribed": False,
                                 "status": LEGACY_ENTRY,
                                 "num_entries": int(nr_torrents or 0)})
        return channels

    def get_personal_channel_id_title(self):
        connection = apsw.Connection(self.tribler_db)
        cursor = connection.cursor()
        cursor.execute('SELECT id,name FROM Channels WHERE peer_id ISNULL LIMIT 1')
        return cursor.fetchone()

    def get_old_trackers(self):
        connection = apsw.Connection(self.tribler_db)
        cursor = connection.cursor()

        trackers = {}
        for tracker_id, tracker, last_check, failures, is_alive in cursor.execute(self.select_trackers_sql):
            try:
                tracker_url_sanitized = get_uniformed_tracker_url(tracker)
                if not tracker_url_sanitized:
                    continue
            except:
                # Skip malformed trackers
                continue
            trackers[tracker_url_sanitized] = ({
                "last_check": last_check,
                "failures": failures,
                "alive": is_alive})
        return trackers

    def get_old_torrents_count(self, personal_channel_only=False):
        personal_channel_filter = ""
        if self.personal_channel_id:
            personal_channel_filter = " AND ct.channel_id " + \
                                      (" == " if personal_channel_only else " != ") + \
                                      (" %i " % self.personal_channel_id)

        connection = apsw.Connection(self.tribler_db)
        cursor = connection.cursor()
        cursor.execute("SELECT COUNT(*) FROM (SELECT t.torrent_id " + self.select_torrents_sql + \
                       personal_channel_filter + "group by infohash )")
        return cursor.fetchone()[0]

    def get_personal_channel_torrents_count(self):
        connection = apsw.Connection(self.tribler_db)
        cursor = connection.cursor()
        cursor.execute("SELECT COUNT(*) FROM (SELECT t.torrent_id " + self.select_torrents_sql + \
                       (" AND ct.channel_id == %s " % self.personal_channel_id) + \
                       " group by infohash )")
        return cursor.fetchone()[0]

    def get_old_torrents(self, personal_channel_only=False, batch_size=BATCH_SIZE, offset=0,
                         sign=False):
        connection = apsw.Connection(self.tribler_db)
        cursor = connection.cursor()

        personal_channel_filter = ""
        if self.personal_channel_id:
            personal_channel_filter = " AND ct.channel_id " + \
                                      (" == " if personal_channel_only else " != ") + \
                                      (" %i " % self.personal_channel_id)

        torrents = []
        for tracker_url, channel_id, name, infohash, length, creation_date, torrent_id, category, num_seeders, num_leechers, last_tracker_check in cursor.execute(
                self.select_full + personal_channel_filter + " group by infohash" + (
                        " LIMIT " + str(batch_size) + " OFFSET " + str(offset))):
            # check if name is valid unicode data
            try:
                name = text_type(name)
            except UnicodeDecodeError:
                continue

            try:
                if len(base64.decodestring(infohash)) != 20:
                    continue
                infohash = base64.decodestring(infohash)

                torrent_dict = {
                    "status": NEW,
                    "infohash": infohash,
                    "size": int(length or 0),
                    "torrent_date": datetime.datetime.utcfromtimestamp(creation_date or 0),
                    "title": name or '',
                    "tags": category or '',
                    "id_": torrent_id or 0,
                    "origin_id": 0,
                    "tracker_info": tracker_url or '',
                    "xxx": int(category == u'xxx')}
                if not sign:
                    torrent_dict.update({
                        "timestamp": int(torrent_id or 0),
                        "status": LEGACY_ENTRY,
                        "public_key": dispesy_cid_to_pk(channel_id),
                        "signature": pseudo_signature(),
                        "skip_key_check": True})

                health_dict = {
                    "seeders": int(num_seeders or 0),
                    "leechers": int(num_leechers or 0),
                    "last_check": int(last_tracker_check or 0)}
                torrents.append((torrent_dict, health_dict))
            except:
                continue

        return torrents

    def convert_personal_channel(self):
        # Reflect conversion state
        with db_session:
            v = self.mds.MiscData.get(name=CONVERSION_FROM_72)
            if v:
                if v.value == PERSONAL_CONVERSION_STARTED:
                    # Just drop the entries from the previous try

                    my_channel = self.mds.ChannelMetadata.get_my_channel()
                    for g in my_channel.contents_list:
                        g.delete()
                    my_channel.delete()
                elif v.value == DISCOVERED_CONVERSION_STARTED:
                    v.set(value=PERSONAL_CONVERSION_STARTED)
                else:
                    return

            else:
                self.mds.MiscData(name=CONVERSION_FROM_72, value=PERSONAL_CONVERSION_STARTED)

        if not self.personal_channel_id or not self.get_personal_channel_torrents_count():
            return

        # Make sure there is nothing left of old personal channel, just in case
        if self.mds.ChannelMetadata.get_my_channel():
            return

        old_torrents = self.get_old_torrents(personal_channel_only=True, sign=True)
        with db_session:
            my_channel = self.mds.ChannelMetadata.create_channel(title=self.personal_channel_title, description='')
            for (t, h) in old_torrents:
                try:
                    md = self.mds.TorrentMetadata(**t)
                    md.parents.add(my_channel)
                except:
                    continue
            my_channel.commit_channel_torrent()

    def convert_discovered_torrents(self):

        offset = 0
        # Reflect conversion state
        with db_session:
            v = self.mds.MiscData.get(name=CONVERSION_FROM_72)
            if v:
                offset = orm.count(
                    g for g in self.mds.TorrentMetadata if g.status == LEGACY_ENTRY and g.metadata_type == REGULAR_TORRENT)
                v.set(value=DISCOVERED_CONVERSION_STARTED)
            else:
                self.mds.MiscData(name=CONVERSION_FROM_72, value=DISCOVERED_CONVERSION_STARTED)

        start = datetime.datetime.utcnow()
        x = 0 + offset
        batch_size = 1000
        total_to_convert = self.get_old_torrents_count()

        while True:
            old_torrents = self.get_old_torrents(batch_size=batch_size, offset=x)
            if not old_torrents:
                break
            with db_session:
                for (t, h) in old_torrents:
                    try:
                        self.mds.TorrentMetadata(**t)
                    except:
                        continue

            x += batch_size
            if self.notifier_callback:
                self.notifier_callback("%i/%i" % (x, total_to_convert))

        stop = datetime.datetime.utcnow()
        elapsed = (stop - start).total_seconds()

        if self.notifier_callback:
            self.notifier_callback("%i entries converted in %i seconds (%i e/s)" % (x, int(elapsed), int(x / elapsed)))

    def convert_discovered_channels(self):
        # Reflect conversion state
        with db_session:
            v = self.mds.MiscData.get(name=CONVERSION_FROM_72)
            if v:
                if v.value == CHANNELS_CONVERSION_STARTED:
                    # Just drop the entries from the previous try
                    orm.delete(g for g in self.mds.ChannelMetadata if g.status == LEGACY_ENTRY)
                else:
                    v.set(value=CHANNELS_CONVERSION_STARTED)
            else:
                self.mds.MiscData(name=CONVERSION_FROM_72, value=CHANNELS_CONVERSION_STARTED)



        with db_session:
            old_channels = self.get_old_channels()
            for c in old_channels:
                try:
                    self.mds.ChannelMetadata(**c)
                except:
                    continue

        with db_session:
            for c in self.mds.ChannelMetadata.select()[:]:
                c.num_entries = c.contents_len
                if c.num_entries == 0:
                    c.delete()

    def update_trackers_info(self):
        old_trackers = self.get_old_trackers()
        with db_session:
            trackers = self.mds.TrackerState.select()[:]
            for tracker in trackers:
                if tracker.url in old_trackers:
                    tracker.set(**old_trackers[tracker.url])

    def mark_conversion_finished(self):
        with db_session:
            v = self.mds.MiscData.get(name=CONVERSION_FROM_72)
            if v:
                v.set(value=CONVERSION_FINISHED)
            else:
                self.mds.MiscData(name=CONVERSION_FROM_72, value=CONVERSION_FINISHED)


if __name__ == "__main__":
    my_key = default_eccrypto.generate_key(u"curve25519")
    mds = MetadataStore("/tmp/metadata.db", "/tmp", my_key)
    d = DispersyToPonyMigration("/tmp/tribler.sdb", mds)

    d.initialize()
    d.convert_personal_channel()
    d.convert_discovered_channels()
    d.update_trackers_info()