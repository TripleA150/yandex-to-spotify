import json
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
    def __init__(self, id_, artist, title, duration_ms=None, isrc=None):
        self.id, self.title, self.artists = id_, title, [SimpleNamespace(name=artist)]
        self.duration_ms, self.isrc = duration_ms, isrc


class FakeSpotify:
    def __init__(self, catalog, liked=(), playlists=None):
        self.catalog = catalog  # search query -> spotify id or list of result dicts
        self.liked = set(liked)
        self.playlists = playlists or {}  # name -> list of track ids
        self.searches = 0
        self.created = []

    def me(self):
        return {'id': 'me'}

    def search(self, query, type):
        self.searches += 1
        found = self.catalog.get(query)
        items = found if isinstance(found, list) else [{'id': found}] if found else []
        return {f'{type}s': {'items': items}}

    def current_user_saved_tracks_contains(self, ids):
        return [i in self.liked for i in ids]

    def current_user_saved_tracks_add(self, ids):
        assert not self.liked.intersection(ids), 'duplicate like'
        self.liked.update(ids)

    def current_user_playlists(self):
        return {'items': [{'id': name, 'name': name, 'owner': {'id': 'me'}} for name in self.playlists], 'next': None}

    def playlist_items(self, playlist_id, additional_types):
        return {'items': [{'item': {'id': i}} for i in self.playlists[playlist_id]], 'next': None}

    def current_user_playlist_create(self, name, public=True):
        self.created.append((name, public))
        self.playlists[name] = []
        return {'id': name}

    def playlist_add_items(self, playlist_id, ids):
        self.playlists[playlist_id].extend(ids)


class FakeYandex:
    def __init__(self, likes=(), playlists=()):
        self.likes = list(likes)
        self._playlists = playlists

    def users_likes_tracks(self):
        # the last like has no album, like a track uploaded by the user
        return SimpleNamespace(tracks=[SimpleNamespace(track_id=f'{t.id}:1' if i < len(self.likes) - 1 else str(t.id))
                                       for i, t in enumerate(self.likes)])

    def tracks(self, ids):
        by_id = {str(t.id): t for t in self.likes}
        return [by_id[i.split(':')[0]] for i in ids]

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
        self.assertEqual(spotify.created, [('Pop', False)])
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

    def test_isrc_is_searched_first(self):
        spotify = FakeSpotify({'isrc:RU1': 'exact', 'A a': 'fuzzy'})
        make(spotify, FakeYandex(likes=[Track(1, 'A', 'a', isrc='RU1')])).import_all()
        self.assertEqual(spotify.liked, {'exact'})

    def test_best_match_prefers_same_artist_then_close_duration(self):
        results = [
            {'id': 'cover', 'artists': [{'name': 'Someone'}], 'duration_ms': 200000},
            {'id': 'live', 'artists': [{'name': 'Kino'}], 'duration_ms': 260000},
            {'id': 'studio', 'artists': [{'name': 'KINO'}], 'duration_ms': 201500},
        ]
        self.assertEqual(importer.best_match(Track(1, 'Kino', 'x', duration_ms=200000), results)['id'], 'studio')
        # without a duration Spotify's ranking decides among the same artist
        self.assertEqual(importer.best_match(Track(1, 'Kino', 'x'), results)['id'], 'live')
        # nothing matches: keep Spotify's first result
        self.assertEqual(importer.best_match(Track(1, 'Nobody', 'x'), results)['id'], 'cover')

    def test_not_found_file_can_be_imported_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            not_found = os.path.join(tmp, 'not-found.json')
            missing = Track(9, 'X', 'missing')
            imp = make(FakeSpotify(CATALOG), FakeYandex(likes=[A, missing], playlists=[('P', [missing])]),
                       ignore=('albums', 'artists'))
            imp.import_all()
            imp.write_not_found(not_found)
            with open(not_found, encoding='UTF-8') as f:
                self.assertEqual(json.load(f), [{'artist': 'X', 'track': 'missing'}])

            spotify = FakeSpotify({'X missing': 'sx'})
            make(spotify, None, ignore=()).import_from_json(not_found)
            self.assertEqual(spotify.playlists, {'Imported from JSON': ['sx']})
            self.assertEqual(spotify.created, [('Imported from JSON', False)])

    def test_playlists_with_the_same_title_stay_apart(self):
        spotify = FakeSpotify(CATALOG, playlists={'New (2)': ['sb']})
        imp = make(spotify, FakeYandex(playlists=[('New', [A]), ('New', [B]), ('New', [C])]),
                   ignore=('likes', 'albums', 'artists'))
        imp.import_all()
        self.assertEqual(spotify.playlists, {'New': ['sa'], 'New (2)': ['sb'], 'New (3)': ['sc']})
        self.assertEqual([s['added'] for s in imp.stats.values()], [1, 0, 1])

    def test_rate_limit_waits_short_and_stops_on_long_bans(self):
        def limited(retry_after):
            calls = []

            def call():
                calls.append(1)
                if len(calls) == 1:
                    raise importer.SpotifyException(429, -1, 'limited', headers={'retry-after': retry_after})
                return 'ok'
            return call

        with mock.patch.object(importer, 'sleep') as sleep:
            self.assertEqual(importer.handle_spotify_exception(limited('2'))(), 'ok')
            sleep.assert_called_once_with(3)
            with self.assertRaisesRegex(RuntimeError, 'try again in 27 h'):
                importer.handle_spotify_exception(limited('99999'))()

        # 429 must reach handle_spotify_exception instead of being slept through inside urllib3
        retry = importer.make_session().get_adapter('https://api.spotify.com').max_retries
        self.assertFalse(retry.is_retry('GET', 429, has_retry_after=True))
        self.assertTrue(retry.is_retry('GET', 503, has_retry_after=True))

    def test_broken_json_entry_is_reported_before_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'tracks.json')
            with open(path, 'w', encoding='UTF-8') as f:
                json.dump([{'artist': 'A', 'track': 'a'}, {'artst': 'B', 'track': 'b'}], f)
            spotify = FakeSpotify(CATALOG)
            with self.assertRaisesRegex(ValueError, 'entry #2'):
                make(spotify, None, ignore=()).import_from_json(path)
            self.assertEqual((spotify.searches, spotify.created), (0, []))

    def test_refresh_token_auth_is_non_interactive(self):
        args = Namespace(id='id', secret='secret', spotify=None, refresh_token='rt', auth_url=None)
        auth = importer.make_auth_manager(args)
        with mock.patch.object(auth, 'refresh_access_token', return_value={'access_token': 'new', 'expires_at': 2**40}) as refresh, \
                mock.patch.object(auth, 'get_auth_response', side_effect=AssertionError('prompted')):
            self.assertEqual(auth.get_access_token(as_dict=False), 'new')
        refresh.assert_called_once_with('rt')

    def test_login_url_is_exchanged_for_a_token_without_prompt(self):
        args = Namespace(id='id', secret='secret', spotify=None, refresh_token=None,
                         auth_url='https://open.spotify.com/?code=abc')
        response = mock.Mock(status_code=200, ok=True)
        response.json.return_value = {'access_token': 'at', 'refresh_token': 'rt', 'expires_in': 3600,
                                      'token_type': 'Bearer', 'scope': 'user-library-read'}
        with mock.patch('requests.Session.post', return_value=response) as post, \
                mock.patch('builtins.input', side_effect=AssertionError('prompted')):
            auth = importer.make_auth_manager(args)
        self.assertEqual(post.call_args.kwargs['data']['code'], 'abc')
        self.assertEqual(auth.cache_handler.get_cached_token()['refresh_token'], 'rt')


if __name__ == '__main__':
    unittest.main()
