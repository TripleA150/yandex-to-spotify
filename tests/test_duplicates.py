import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import duplicates  # noqa: E402


def track(artist, name, id_=None, added_at=None):
    return {'id': id_ or f'{artist}-{name}', 'name': name, 'artists': [{'name': artist}], 'added_at': added_at}


class FakeSpotify:
    def __init__(self, liked, playlists):
        self.liked, self.playlists = liked, playlists  # playlists: (name, owner, tracks)

    def me(self):
        return {'id': 'me'}

    @staticmethod
    def _pages(entries, size=2):
        """Split into pages linked by 'next', like Spotify does."""
        pages = [{'items': entries[i:i + size], 'next': None} for i in range(0, len(entries), size)] or [{'items': [], 'next': None}]
        for page, following in zip(pages, pages[1:]):
            page['next'] = following
        return pages[0]

    def next(self, page):
        return page['next']

    def current_user_saved_tracks(self, limit):
        return self._pages([{'track': dict(t, added_at=None), 'added_at': t['added_at']} for t in self.liked])

    def current_user_saved_tracks_delete(self, ids):
        assert len(ids) <= 40
        self.liked = [t for t in self.liked if t['id'] not in ids]

    def current_user_playlists(self):
        return self._pages([{'id': name, 'name': name, 'owner': {'id': owner}} for name, owner, _ in self.playlists] + [None])

    def _playlist(self, playlist_id):
        return next(t for name, _, t in self.playlists if name == playlist_id)

    def playlist_items(self, playlist_id, additional_types):
        return self._pages([{'item': t, 'added_at': t['added_at'] if t else None} for t in self._playlist(playlist_id)])

    def playlist_remove_all_occurrences_of_items(self, playlist_id, ids):
        # like Spotify: removal by id takes every copy
        tracks = self._playlist(playlist_id)
        tracks[:] = [t for t in tracks if t is None or t['id'] not in ids]

    def playlist_add_items(self, playlist_id, ids, position):
        self._playlist(playlist_id)[position:position] = [track('re', 'added', i, 'today') for i in ids]


def ids(tracks):
    return [t['id'] if t else None for t in tracks]


class DuplicatesTest(unittest.TestCase):
    def test_same_artist_and_title_are_duplicates_whatever_the_case_or_release(self):
        tracks = [track('Kino', 'Gruppa krovi', 'single'), None, track('KINO', 'gruppa krovi ', 'album'),
                  track('Kino', 'Kukushka'), track('Other', 'Gruppa krovi')]
        self.assertEqual(duplicates.find_duplicates(tracks), {('kino', 'gruppa krovi'): [1, 3]})

    def test_the_copy_added_first_goes_first_whatever_its_position(self):
        tracks = [track('A', 'a', 'new', '2025-05-01T00:00:00Z'), track('A', 'a', 'no-date'),
                  track('A', 'a', 'old', '2020-01-01T00:00:00Z')]
        self.assertEqual(duplicates.find_duplicates(tracks), {('a', 'a'): [3, 1, 2]})

    def test_report_reads_liked_songs_and_only_own_playlists_across_pages(self):
        a, b = track('A', 'a', added_at='1'), track('B', 'b', added_at='1')
        spotify = FakeSpotify(liked=[a, b, track('A', 'a', 'other-release', '0')],
                              playlists=[('Mine', 'me', [a, b, b, a, a]), ('Followed', 'someone', [a, a]),
                                         ('Clean', 'me', [a, b])])
        report = duplicates.make_report(duplicates.load_sections(spotify))
        self.assertIn('## Liked Songs: 1 songs repeated (3 tracks)', report)
        self.assertIn('| A — a | 2 | 1, **3** |', report)  # added_at comes from the library entry
        self.assertIn('## Mine: 2 songs repeated (5 tracks)', report)
        self.assertIn('| A — a | 3 | **1**, 4, 5 |', report)
        self.assertNotIn('Followed', report)
        self.assertNotIn('Clean', report)

    def test_report_says_when_there_are_no_duplicates(self):
        report = duplicates.make_report([('Liked Songs', None, [track('A', 'a')]), ('P', 'p', [])])
        self.assertIn('No duplicates in 2 checked lists.', report)

    def test_playlist_keeps_the_first_added_copy_in_its_place(self):
        k_old, k_new = track('K', 'k', 'k', '2020'), track('K', 'k', 'k', '2024')
        local = dict(track('K', 'k', added_at='2025'), id=None)  # local files have no id
        playlist = [track('K', 'k', 'single', '2023'), k_new, track('X', 'x', 'x', '2021'), None, k_old,
                    local, track('Y', 'y', 'y', '2022'), track('Y', 'y', 'y', '2021')]
        spotify = FakeSpotify(liked=[], playlists=[('P', 'me', playlist)])
        before = list(playlist)

        removed = duplicates.remove_duplicates(spotify, 'P', before)

        # k and y: every copy removed and the first-added one put back where it was; the local file can't be removed
        self.assertEqual(ids(playlist), ['x', None, 'k', None, 'y'])
        self.assertEqual(removed, (3, 2))
        report = duplicates.make_report([('P', 'P', before)], [removed])
        self.assertIn('Removed 3 extra copies. 2 tracks added several times', report)

    def test_failed_add_back_tells_what_to_restore_by_hand(self):
        playlist = [track('X', 'x', 'x', '1'), track('K', 'k', 'k', '1'), track('K', 'k', 'k', '2')]
        spotify = FakeSpotify(liked=[], playlists=[('P', 'me', playlist)])
        spotify.playlist_add_items = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('quota'))
        with self.assertLogs(duplicates.logger, 'ERROR') as logs, self.assertRaises(RuntimeError):
            duplicates.remove_duplicates(spotify, 'P', list(playlist))
        self.assertIn('Not added back: spotify:track:k at position 2', logs.output[0])

    def test_liked_songs_keep_the_first_like_of_each_song(self):
        spotify = FakeSpotify(liked=[track('A', 'a', 'album', '2024'), track('A', 'a', 'single', '2019'),
                                     track('B', 'b', 'b', '2020')], playlists=[])
        sections = duplicates.load_sections(spotify)
        self.assertEqual(duplicates.remove_duplicates(spotify, None, sections[0][2]), (1, 0))
        self.assertEqual(ids(spotify.liked), ['single', 'b'])


if __name__ == '__main__':
    unittest.main()
