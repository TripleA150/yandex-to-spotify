"""Report duplicate tracks in Liked Songs and in your own Spotify playlists; --remove keeps only the first-added copy."""
import os
import sys
import argparse
from argparse import Namespace

import spotipy

from importer import chunks, make_auth_manager, make_session, handle_spotify_exception, iter_pages, entry_track, logger


def track_key(track):
    """Same main artist and title: catches a re-added track and a single next to its album version."""
    artist = track['artists'][0]['name'] if track.get('artists') else ''
    return artist.casefold().strip(), track['name'].casefold().strip()


def find_duplicates(tracks):
    """{key: 1-based positions} of songs that occur more than once, the copy added first goes first.

    None entries (episodes, gaps) keep positions. Copies without added_at count as added last.
    """
    positions = {}
    for position, track in enumerate(tracks, 1):
        if track and track.get('name'):
            positions.setdefault(track_key(track), []).append(position)

    def added_first(position):
        added_at = tracks[position - 1].get('added_at')
        return added_at is None, added_at or '', position

    return {key: sorted(found, key=added_first) for key, found in positions.items() if len(found) > 1}


def plan_removal(tracks):
    """Ids to remove and (id, 0-based index) pairs to add back, so that only the first-added copy of each song stays.

    Spotify removes a track by id, every copy at once. So a track added several times is removed
    completely and put back once where its first-added copy was.
    """
    remove, kept_repeats = set(), []
    for positions in find_duplicates(tracks).values():
        kept_id = tracks[positions[0] - 1]['id']
        extra_ids = {tracks[p - 1]['id'] for p in positions[1:]} - {None}  # local files can't be removed via the API
        remove |= extra_ids
        if kept_id in extra_ids:
            kept_repeats.append(positions[0])

    restore = []
    for position in sorted(kept_repeats):
        # what stays in front of it, plus the copies already put back in front of it
        index = sum(1 for track in tracks[:position - 1] if track is None or track['id'] not in remove) + len(restore)
        restore.append((tracks[position - 1]['id'], index))
    return remove, restore


def remove_duplicates(spotify, playlist_id, tracks):
    """Leave the first-added copy of every song; playlist_id None means Liked Songs. Returns (removed, re-added)."""
    remove, restore = plan_removal(tracks)
    if playlist_id is None:
        delete, size = spotify.current_user_saved_tracks_delete, 40
    else:
        delete, size = (lambda ids: spotify.playlist_remove_all_occurrences_of_items(playlist_id, ids)), 100
    for chunk in chunks(sorted(remove), size):
        handle_spotify_exception(delete)(chunk)

    pending = list(restore)
    try:
        while pending:
            track_id, index = pending[0]
            handle_spotify_exception(spotify.playlist_add_items)(playlist_id, [track_id], position=index)
            pending.pop(0)
    finally:
        for track_id, index in pending:
            logger.error(f'Not added back: spotify:track:{track_id} at position {index + 1}, add it by hand')

    removed = sum(1 for track in tracks if track and track['id'] in remove) - len(restore)
    return removed, len(restore)


def make_report(sections, removed=None):
    """sections: (title, playlist id or None, tracks in Spotify order); removed: remove_duplicates() results so far."""
    cell = lambda text: text.replace('|', '&#124;')  # noqa: E731
    lines = ['# Duplicate tracks in Spotify', '',
             'Same main artist and title count as one song. Positions start at 1; in Liked Songs 1 is the latest like. '
             'The bold position is the copy added first, it is the one that stays.']
    found_any = False
    for number, (title, _, tracks) in enumerate(sections):
        duplicates = find_duplicates(tracks)
        if not duplicates:
            continue
        found_any = True
        lines += ['', f'## {cell(title)}: {len(duplicates)} songs repeated ({len(tracks)} tracks)', '',
                  '| Song | Times | Positions |', '|---|---|---|']
        for positions in duplicates.values():
            track = tracks[positions[0] - 1]
            artists = ', '.join(artist['name'] for artist in track['artists'])
            shown = ', '.join(f'**{p}**' if p == positions[0] else str(p) for p in sorted(positions))
            lines.append(f"| {cell(artists)} — {cell(track['name'])} | {len(positions)} | {shown} |")
        if removed is not None and number < len(removed):
            count, readded = removed[number]
            lines += ['', f'Removed {count} extra copies.']
            if readded:
                lines[-1] += (f' {readded} tracks added several times were removed and added back once at the position '
                              f'of the first copy: Spotify removes every copy at once, so their added date is now today.')
    if not found_any:
        lines += ['', f'No duplicates in {len(sections)} checked lists.']
    return '\n'.join(lines) + '\n'


def load_sections(spotify):
    def tracks(first_page):
        entries = iter_pages(spotify, first_page)
        return [dict(track, added_at=entry.get('added_at')) if (track := entry_track(entry)) else None for entry in entries]

    user = handle_spotify_exception(spotify.me)()['id']
    sections = [('Liked Songs', None, tracks(handle_spotify_exception(spotify.current_user_saved_tracks)(limit=50)))]
    for playlist in iter_pages(spotify, handle_spotify_exception(spotify.current_user_playlists)()):
        if playlist and playlist['owner']['id'] == user:
            logger.info(f'Reading playlist {playlist["name"]}...')
            first_page = handle_spotify_exception(spotify.playlist_items)(playlist['id'], additional_types=('track',))
            sections.append((playlist['name'], playlist['id'], tracks(first_page)))
    return sections


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', help='Write the Markdown report to this file instead of printing it')
    parser.add_argument('--remove', action='store_true', help='Remove duplicates, keep the copy added first')
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
        sections = load_sections(spotify_client)
        removed = [] if arguments.remove else None
        try:
            for title, playlist_id, tracks in sections if arguments.remove else []:
                logger.info(f'Removing duplicates in {title}...')
                removed.append(remove_duplicates(spotify_client, playlist_id, tracks))
        finally:
            # also after a failure, so the report shows what was already removed
            report = make_report(sections, removed)
            if arguments.report:
                with open(arguments.report, 'w', encoding='UTF-8') as file:
                    file.write(report)
            else:
                print(report)
    except Exception:
        logger.exception('An unexpected error occurred')
        raise SystemExit(1)
