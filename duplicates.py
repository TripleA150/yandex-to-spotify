"""Report duplicate tracks in Liked Songs and in your own Spotify playlists. Changes nothing."""
import os
import sys
import argparse
from argparse import Namespace

import spotipy

from importer import make_auth_manager, make_session, handle_spotify_exception, iter_pages, entry_track, logger


def track_key(track):
    """Same main artist and title: catches a re-added track and a single next to its album version."""
    artist = track['artists'][0]['name'] if track.get('artists') else ''
    return artist.casefold().strip(), track['name'].casefold().strip()


def find_duplicates(tracks):
    """{key: 1-based positions} of songs that occur more than once; None entries (episodes, gaps) keep positions."""
    positions = {}
    for position, track in enumerate(tracks, 1):
        if track and track.get('name'):
            positions.setdefault(track_key(track), []).append(position)
    return {key: found for key, found in positions.items() if len(found) > 1}


def make_report(sections):
    """sections: (title, tracks in Spotify order) pairs."""
    cell = lambda text: text.replace('|', '&#124;')  # noqa: E731
    lines = ['# Duplicate tracks in Spotify', '',
             'Same main artist and title count as one song. Positions start at 1; in Liked Songs 1 is the latest like.']
    found_any = False
    for title, tracks in sections:
        duplicates = find_duplicates(tracks)
        if not duplicates:
            continue
        found_any = True
        lines += ['', f'## {cell(title)}: {len(duplicates)} songs repeated ({len(tracks)} tracks)', '',
                  '| Song | Times | Positions |', '|---|---|---|']
        for positions in duplicates.values():
            track = tracks[positions[0] - 1]
            artists = ', '.join(artist['name'] for artist in track['artists'])
            lines.append(f"| {cell(artists)} — {cell(track['name'])} | {len(positions)} | {', '.join(map(str, positions))} |")
    if not found_any:
        lines += ['', f'No duplicates in {len(sections)} checked lists.']
    return '\n'.join(lines) + '\n'


def load_sections(spotify):
    def tracks(first_page):
        return [entry_track(entry) for entry in iter_pages(spotify, first_page)]

    user = handle_spotify_exception(spotify.me)()['id']
    sections = [('Liked Songs', tracks(handle_spotify_exception(spotify.current_user_saved_tracks)(limit=50)))]
    for playlist in iter_pages(spotify, handle_spotify_exception(spotify.current_user_playlists)()):
        if playlist and playlist['owner']['id'] == user:
            logger.info(f'Reading playlist {playlist["name"]}...')
            first_page = handle_spotify_exception(spotify.playlist_items)(playlist['id'], additional_types=('track',))
            sections.append((playlist['name'], tracks(first_page)))
    return sections


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', help='Write the Markdown report to this file instead of printing it')
    arguments = parser.parse_args()

    auth = Namespace(id=os.environ.get('SPOTIFY_CLIENT_ID'), secret=os.environ.get('SPOTIFY_CLIENT_SECRET'), spotify=None,
                     refresh_token=os.environ.get('SPOTIFY_REFRESH_TOKEN'), auth_url=os.environ.get('SPOTIFY_AUTH_URL'))
    if not auth.id or not auth.secret:
        parser.error('Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET.')
    if not (auth.refresh_token or auth.auth_url or sys.stdin.isatty()):
        parser.error('Spotify login is missing: set SPOTIFY_AUTH_URL (the address after the login link from README) '
                     'or SPOTIFY_REFRESH_TOKEN.')

    try:
        spotify_client = spotipy.Spotify(auth_manager=make_auth_manager(auth), requests_session=make_session())
        report = make_report(load_sections(spotify_client))
        if arguments.report:
            with open(arguments.report, 'w', encoding='UTF-8') as file:
                file.write(report)
        else:
            print(report)
    except Exception:
        logger.exception('An unexpected error occurred')
        raise SystemExit(1)
