import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import duplicates  # noqa: E402


def track(artist, name, id_=None):
    return {'id': id_ or f'{artist}-{name}', 'name': name, 'artists': [{'name': artist}]}


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
        return self._pages([{'track': t} for t in self.liked])

    def current_user_playlists(self):
        return self._pages([{'id': name, 'name': name, 'owner': {'id': owner}} for name, owner, _ in self.playlists] + [None])

    def playlist_items(self, playlist_id, additional_types):
        tracks = next(t for name, _, t in self.playlists if name == playlist_id)
        return self._pages([{'item': t} for t in tracks])


class DuplicatesTest(unittest.TestCase):
    def test_same_artist_and_title_are_duplicates_whatever_the_case_or_release(self):
        tracks = [track('Kino', 'Gruppa krovi', 'single'), None, track('KINO', 'gruppa krovi ', 'album'),
                  track('Kino', 'Kukushka'), track('Other', 'Gruppa krovi')]
        self.assertEqual(duplicates.find_duplicates(tracks), {('kino', 'gruppa krovi'): [1, 3]})

    def test_report_reads_liked_songs_and_only_own_playlists_across_pages(self):
        a, b = track('A', 'a'), track('B', 'b')
        spotify = FakeSpotify(liked=[a, b, track('A', 'a', 'other-release')],
                              playlists=[('Mine', 'me', [a, b, b, a, a]), ('Followed', 'someone', [a, a]),
                                         ('Clean', 'me', [a, b])])
        report = duplicates.make_report(duplicates.load_sections(spotify))
        self.assertIn('## Liked Songs: 1 songs repeated (3 tracks)', report)
        self.assertIn('| A — a | 2 | 1, 3 |', report)
        self.assertIn('## Mine: 2 songs repeated (5 tracks)', report)
        self.assertIn('| A — a | 3 | 1, 4, 5 |', report)
        self.assertNotIn('Followed', report)
        self.assertNotIn('Clean', report)

    def test_report_says_when_there_are_no_duplicates(self):
        report = duplicates.make_report([('Liked Songs', [track('A', 'a')]), ('P', [])])
        self.assertIn('No duplicates in 2 checked lists.', report)


if __name__ == '__main__':
    unittest.main()
