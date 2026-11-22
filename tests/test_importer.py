import os
import sys
import tempfile
import unittest
from argparse import Namespace
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importer  # noqa: E402


class Track:  # class name matters: Importer derives the search type from it
    def __init__(self, id_, artist, title):
        self.id, self.title, self.artists = id_, title, [SimpleNamespace(name=artist)]


class FakeSpotify:
    def __init__(self, catalog, liked=(), playlists=None):
        self.catalog = catalog  # search query -> spotify id
        self.liked = set(liked)
        self.playlists = playlists or {}  # name -> list of track ids
        self.searches = 0
        self.created = []

    def me(self):
        return {'id': 'me'}

    def search(self, query, type):
        self.searches += 1
        found = self.catalog.get(query)
        return {f'{type}s': {'items': [{'id': found}] if found else []}}

    def current_user_saved_tracks_contains(self, ids):
        return [i in self.liked for i in ids]

    def current_user_saved_tracks_add(self, ids):
        assert not self.liked.intersection(ids), 'duplicate like'
        self.liked.update(ids)

    def current_user_playlists(self):
        return {'items': [{'id': name, 'name': name, 'owner': {'id': 'me'}} for name in self.playlists], 'next': None}

    def playlist_items(self, playlist_id, additional_types):
        return {'items': [{'item': {'id': i}} for i in self.playlists[playlist_id]], 'next': None}

    def current_user_playlist_create(self, name):
        self.created.append(name)
        self.playlists[name] = []
        return {'id': name}

    def playlist_add_items(self, playlist_id, ids):
        self.playlists[playlist_id].extend(ids)


class FakeYandex:
    def __init__(self, likes=(), playlists=()):
        self.likes = list(likes)
        self._playlists = playlists

    def users_likes_tracks(self):
        return SimpleNamespace(tracks=[SimpleNamespace(id=t.id, album_id=1) for t in self.likes])

    def tracks(self, ids):
        return list(self.likes)

    def users_playlists_list(self):
        return [SimpleNamespace(title=title, kind=1, collective=False, cover=SimpleNamespace(type='mosaic'),
                                fetch_tracks=lambda tracks=tracks: [SimpleNamespace(track=t) for t in tracks])
                for title, tracks in self._playlists]


A, B, C = Track(1, 'A', 'a'), Track(2, 'B', 'b'), Track(3, 'C', 'c')
CATALOG = {'A a': 'sa', 'B b': 'sb', 'C c': 'sc'}


def make(spotify, yandex, ignore=('playlists', 'albums', 'artists'), **kwargs):
    return importer.Importer(spotify, yandex, list(ignore), False, 40, **kwargs)


class ImporterTest(unittest.TestCase):
    def test_likes_skip_already_liked_and_duplicates(self):
        spotify = FakeSpotify(dict(CATALOG, **{'A again': 'sa'}), liked={'sb'})
        imp = make(spotify, FakeYandex(likes=[A, B, C, Track(4, 'A', 'again')]))
        imp.import_all()
        self.assertEqual(spotify.liked, {'sa', 'sb', 'sc'})
        self.assertEqual(imp.stats['Likes'], {'total': 4, 'found': 4, 'duplicates': 2, 'added': 2, 'not_found': 0})

    def test_dry_run_changes_nothing(self):
        spotify = FakeSpotify(CATALOG)
        imp = make(spotify, FakeYandex(likes=[A, B], playlists=[('New', [C])]), ignore=('albums', 'artists'), dry_run=True)
        imp.import_all()
        self.assertEqual(spotify.liked, set())
        self.assertEqual(spotify.created, [])
        self.assertEqual(imp.stats['New']['added'], 1)

    def test_existing_playlist_is_reused_and_only_missing_tracks_added(self):
        spotify = FakeSpotify(CATALOG, playlists={'Rock': ['sa']})
        imp = make(spotify, FakeYandex(playlists=[('Rock', [A, B]), ('Pop', [C])]), ignore=('likes', 'albums', 'artists'))
        imp.import_all()
        self.assertEqual(spotify.created, ['Pop'])
        self.assertEqual(spotify.playlists, {'Rock': ['sa', 'sb'], 'Pop': ['sc']})
        self.assertEqual(imp.stats['Rock']['duplicates'], 1)

    def test_cache_skips_search_on_rerun_and_report_lists_misses(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache, report = os.path.join(tmp, 'cache.json'), os.path.join(tmp, 'report.md')
            missing = Track(9, 'X', 'missing')
            make(FakeSpotify(CATALOG), FakeYandex(likes=[A, B, missing]), cache_path=cache).import_all()

            spotify = FakeSpotify(CATALOG)
            imp = make(spotify, FakeYandex(likes=[A, B, missing]), cache_path=cache)
            imp.import_all()
            imp.write_report(report)
            self.assertEqual(spotify.searches, 1)  # only the miss is searched again
            with open(report, encoding='UTF-8') as f:
                self.assertIn('- X - missing', f.read())

    def test_refresh_token_auth_is_non_interactive(self):
        args = Namespace(id='id', secret='secret', spotify=None, refresh_token='rt')
        auth = importer.make_auth_manager(args)
        with mock.patch.object(auth, 'refresh_access_token', return_value={'access_token': 'new', 'expires_at': 2**40}) as refresh, \
                mock.patch.object(auth, 'get_auth_response', side_effect=AssertionError('prompted')):
            self.assertEqual(auth.get_access_token(as_dict=False), 'new')
        refresh.assert_called_once_with('rt')


if __name__ == '__main__':
    unittest.main()
