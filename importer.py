import os
import json
import argparse
import logging
from base64 import b64encode
from time import sleep

import spotipy
import requests
from PIL import Image
from requests.adapters import HTTPAdapter
from requests.exceptions import ReadTimeout
from urllib3.util.retry import Retry
from spotipy.cache_handler import MemoryCacheHandler
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth
from yandex_music import Client, Artist

REDIRECT_URI = 'https://open.spotify.com'
MAX_REQUEST_RETRIES = 5
# a longer rate limit ban stops the run: progress is cached, so it is cheaper to run again later
MAX_RETRY_AFTER_S = 600
# read scopes are needed for duplicate checks (library/contains, own playlists)
SCOPE = ('playlist-modify-public,playlist-modify-private,playlist-read-private,user-library-modify,'
         'user-library-read,user-follow-modify,user-follow-read,ugc-image-upload')
DURATION_TOLERANCE_MS = 3000

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def chunks(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def encode_file_base64_jpeg(filename):
    img = Image.open(filename)
    if img.format != 'JPEG':
        img.convert('RGB').save(filename, 'JPEG')

    with open(filename, 'rb') as f:
        return b64encode(f.read())


def handle_spotify_exception(func):
    def wrapper(*args, **kwargs):
        retry = 1
        while True:
            try:
                return func(*args, **kwargs)
            except SpotifyException as exception:
                if exception.http_status != 429:
                    raise exception

                wait = int((exception.headers or {}).get('retry-after', 4)) + 1
                if wait > MAX_RETRY_AFTER_S:
                    raise RuntimeError(f'Spotify rate limit: try again in {wait // 3600} h {wait % 3600 // 60} min. '
                                       f'Found matches are cached, the next run continues from here.') from exception
                logger.info(f'Rate limited by Spotify, waiting {wait} s...')
                sleep(wait)
            except ReadTimeout as exception:
                logger.info(f'Read timed out. Retrying #{retry}...')

                if retry > MAX_REQUEST_RETRIES:
                    logger.info('Max retries reached.')
                    raise exception

                logger.info('Trying again...')
                retry += 1

    return wrapper


def make_session():
    """Like spotipy's own session, but 429 is left to handle_spotify_exception.

    spotipy's retry would sleep any Retry-After silently, even for hours.
    """
    session = requests.Session()
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3, read=False, backoff_factor=0.3, status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(['GET', 'POST', 'PUT', 'DELETE']), respect_retry_after_header=False,
    )))
    return session


def best_match(item, found_items):
    """Prefer results by the same artist, then with a close duration, then Spotify's own ranking."""
    if isinstance(item, (str, Artist)):
        return found_items[0]

    names = {artist.name.casefold() for artist in item.artists}
    duration = getattr(item, 'duration_ms', None)

    def key(found):
        same_artist = any(artist['name'].casefold() in names for artist in found.get('artists', []))
        close = duration is not None and found.get('duration_ms') is not None \
            and abs(found['duration_ms'] - duration) <= DURATION_TOLERANCE_MS
        return not same_artist, not close

    return min(found_items, key=key)  # min() keeps the first of equal results


def as_json_track(item):
    """Item in the --json-path format, or None for albums and artists."""
    if isinstance(item, dict):
        return item
    if type(item).__name__ == 'Track':
        return {'artist': ', '.join(artist.name for artist in item.artists), 'track': item.title}
    return None


class NotFoundException(SpotifyException):
    def __init__(self, item_name):
        self.item_name = item_name


class Importer:
    def __init__(self, spotify_client, yandex_client: Client, ignore_list, strict_search, chunk_size,
                 dry_run=False, cache_path=None, public=False):
        self.spotify_client = spotify_client
        self.yandex_client = yandex_client
        self.chunk_size = chunk_size
        self.dry_run = dry_run
        self.public = public

        self._importing_items = {
            'likes': self.import_likes,
            'playlists': self.import_playlists,
            'albums': self.import_albums,
            'artists': self.import_artists
        }

        for item in ignore_list:
            del self._importing_items[item]

        self._strict_search = strict_search

        self.user = handle_spotify_exception(spotify_client.me)()['id']
        logger.info(f'User ID: {self.user}')
        logger.info(f'Chunk size: {self.chunk_size}')
        if dry_run:
            logger.info('Dry run: nothing will be changed in Spotify')

        self.not_imported = {}
        self.not_found_tracks = []
        self.stats = {}

        # Yandex item -> Spotify ID, so reruns don't search the same items again
        self.cache_path = cache_path
        self.cache = {}
        if cache_path and os.path.exists(cache_path):
            with open(cache_path, 'r', encoding='UTF-8') as file:
                self.cache = json.load(file)
            logger.info(f'Loaded {len(self.cache)} cached matches from {cache_path}')

    def save_cache(self):
        if self.cache_path:
            with open(self.cache_path, 'w', encoding='UTF-8') as file:
                json.dump(self.cache, file, ensure_ascii=False)

    def _import_item(self, item):
        # if the item is a dict or a string, it is a query from the JSON file
        if isinstance(item, dict):
            item = f'{item["artist"]} {item["track"]}'
        if isinstance(item, str):
            query = item
            item_name = item
            type_ = 'track'  # Default type for string items
            artists = []  # Default artists for string items
            cache_key = f'query:{item}'
        # else it is an object from Yandex
        else:
            type_ = item.__class__.__name__.casefold()
            item_name = item.name if isinstance(item, Artist) else f'{", ".join([artist.name for artist in item.artists])} - {item.title}'
            artists = item.artists if not isinstance(item, Artist) else []  # Artists for Yandex items
            cache_key = f'{type_}:{item.id}'

            # A workaround for when track name is too long (100+ characters) there is an exception happening
            # because spotify API can not process it.
            if len(item_name) > 100:
                item_name = item_name[:100]
                logger.info('Name too long... Trimming to 100 characters. May affect search accuracy')

            query = item_name.replace('- ', '')

        if cache_key in self.cache:
            logger.info(f'Importing {type_}: {item_name}... (cached)')
            return self.cache[cache_key]

        logger.info(f'Importing {type_}: {item_name}...')
        found_items = []

        # ISRC identifies the exact recording, so try it before the text search
        isrc = getattr(item, 'isrc', None) if type_ == 'track' else None
        if isrc:
            found_items = handle_spotify_exception(self.spotify_client.search)(f'isrc:{isrc}', type='track')['tracks']['items']

        if not found_items:
            found_items = handle_spotify_exception(self.spotify_client.search)(query, type=type_)[f'{type_}s']['items']

        if not self._strict_search and not isinstance(item, Artist) and not len(found_items) and len(artists) > 1:
            query = f'{artists[0].name} {item.title}'
            found_items = handle_spotify_exception(self.spotify_client.search)(query, type=type_)[f'{type_}s']['items']

        logger.info(f'Searching "{query}"...')

        if not len(found_items):
            raise NotFoundException(item_name)

        spotify_id = best_match(item, found_items)['id']
        self.cache[cache_key] = spotify_id
        return spotify_id

    def _contains(self, check_method, ids):
        """Return the subset of ids that already exist (GET /me/library/contains, max 40 per request)."""
        existing = set()
        for chunk in chunks(ids, 40):
            flags = handle_spotify_exception(check_method)(chunk)
            existing.update(spotify_id for spotify_id, exists in zip(chunk, flags) if exists)
        return existing

    def _add_items_to_spotify(self, items, section, save_items_callback, existing_callback):
        not_imported_section = self.not_imported.setdefault(section, [])
        spotify_items = []

        items.reverse()
        for item in items:
            try:
                spotify_items.append(self._import_item(item))
                logger.info('OK')

            except SpotifyException as exception:  # NotFoundException included
                json_track = as_json_track(item)
                not_imported_section.append(getattr(exception, 'item_name', None)
                                            or (f'{json_track["artist"]} - {json_track["track"]}' if json_track else getattr(item, 'name', str(item))))
                if json_track:
                    self.not_found_tracks.append(json_track)
                logger.warning('NO')

        # different Yandex tracks often resolve to the same Spotify track
        unique_items = list(dict.fromkeys(spotify_items))
        existing = existing_callback(unique_items) if unique_items else set()
        new_items = [spotify_id for spotify_id in unique_items if spotify_id not in existing]

        self.stats[section] = {
            'total': len(items),
            'found': len(spotify_items),
            'duplicates': len(spotify_items) - len(new_items),
            'added': len(new_items),
            'not_found': len(not_imported_section),
        }
        logger.info(f'{section}: {len(new_items)} new, {len(spotify_items) - len(new_items)} already in Spotify or duplicated')

        if not new_items or self.dry_run:
            return

        for chunk in chunks(new_items, self.chunk_size):
            save_items_callback(self, chunk)

    def import_likes(self):
        likes_tracks = self.yandex_client.users_likes_tracks().tracks
        tracks = self.yandex_client.tracks([track.track_id for track in likes_tracks])
        logger.info('Importing liked tracks...')

        def save_tracks_callback(importer, spotify_tracks):
            logger.info(f'Saving {len(spotify_tracks)} tracks...')
            handle_spotify_exception(importer.spotify_client.current_user_saved_tracks_add)(spotify_tracks)
            logger.info('OK')

        self._add_items_to_spotify(tracks, 'Likes', save_tracks_callback,
                                   lambda ids: self._contains(self.spotify_client.current_user_saved_tracks_contains, ids))

    def _own_playlists(self):
        """Map name -> id of playlists owned by the current user."""
        playlists = {}
        page = handle_spotify_exception(self.spotify_client.current_user_playlists)()
        while page:
            for playlist in page['items']:
                if playlist and playlist['owner']['id'] == self.user:
                    playlists.setdefault(playlist['name'], playlist['id'])
            page = handle_spotify_exception(self.spotify_client.next)(page) if page['next'] else None
        return playlists

    def _playlist_track_ids(self, playlist_id):
        ids = set()
        page = handle_spotify_exception(self.spotify_client.playlist_items)(playlist_id, additional_types=('track',))
        while page:
            for entry in page['items']:
                # Spotify renamed 'track' to 'item' in 2026, accept both
                track = entry.get('item') or entry.get('track')
                if track and track.get('id'):
                    ids.add(track['id'])
            page = handle_spotify_exception(self.spotify_client.next)(page) if page['next'] else None
        return ids

    def import_playlists(self):
        own_playlists = self._own_playlists()
        playlists = self.yandex_client.users_playlists_list()
        used_titles = set()
        for playlist in playlists:
            # Yandex names every new playlist the same, so "Новый плейлист" x3 must not merge into one
            title, number = playlist.title, 1
            while title in used_titles:
                number += 1
                title = f'{playlist.title} ({number})'
            used_titles.add(title)
            logger.info(f'Importing playlist {title}...')

            spotify_playlist_id = own_playlists.get(title)
            if spotify_playlist_id:
                logger.info('Playlist already exists in Spotify, adding only missing tracks')
                existing_tracks = self._playlist_track_ids(spotify_playlist_id)
            else:
                existing_tracks = set()
                if not self.dry_run:
                    spotify_playlist_id = handle_spotify_exception(self.spotify_client.current_user_playlist_create)(title, public=self.public)['id']
                    own_playlists[title] = spotify_playlist_id

                    if playlist.cover.type == 'pic':
                        filename = f'{playlist.kind}-cover'
                        playlist.cover.download(filename, size='400x400')

                        handle_spotify_exception(self.spotify_client.playlist_upload_cover_image)(spotify_playlist_id, encode_file_base64_jpeg(filename))

            playlist_tracks = playlist.fetch_tracks()
            if not playlist.collective:
                tracks = [track.track for track in playlist_tracks]
            elif playlist.collective and playlist_tracks:
                tracks = self.yandex_client.tracks([track.track_id for track in playlist_tracks])
            else:
                tracks = []

            def save_tracks_callback(importer, spotify_tracks, playlist_id=spotify_playlist_id, title=title):
                logger.info(f'Saving {len(spotify_tracks)} tracks in playlist {title}...')
                handle_spotify_exception(importer.spotify_client.playlist_add_items)(playlist_id, spotify_tracks)
                logger.info('OK')

            self._add_items_to_spotify(tracks, title, save_tracks_callback,
                                       lambda ids, existing=existing_tracks: existing.intersection(ids))

    def import_albums(self):
        likes_albums = self.yandex_client.users_likes_albums()
        albums = [album.album for album in likes_albums]
        logger.info('Importing albums...')

        def save_albums_callback(importer, spotify_albums):
            logger.info(f'Saving {len(spotify_albums)} albums...')
            handle_spotify_exception(importer.spotify_client.current_user_saved_albums_add)(spotify_albums)
            logger.info('OK')

        self._add_items_to_spotify(albums, 'Albums', save_albums_callback,
                                   lambda ids: self._contains(self.spotify_client.current_user_saved_albums_contains, ids))

    def import_artists(self):
        likes_artists = self.yandex_client.users_likes_artists()
        artists = [artist.artist for artist in likes_artists]
        logger.info('Importing artists...')

        def save_artists_callback(importer, spotify_artists):
            logger.info(f'Saving {len(spotify_artists)} artists...')
            handle_spotify_exception(importer.spotify_client.user_follow_artists)(spotify_artists)
            logger.info('OK')

        self._add_items_to_spotify(artists, 'Artists', save_artists_callback,
                                   lambda ids: self._contains(self.spotify_client.current_user_following_artists, ids))

    def import_all(self):
        try:
            for item in self._importing_items.values():
                item()
        finally:
            # keep search results even if the run crashed or hit a rate limit
            self.save_cache()

        self.print_not_imported()

    def print_not_imported(self):
        logger.error('Not imported items:')
        for section, items in self.not_imported.items():
            logger.info(f'{section}:')
            for item in items:
                logger.info(item)

    def write_report(self, path):
        lines = ['# Yandex.Music → Spotify', '']
        if self.dry_run:
            lines += ['**Dry run** — nothing was changed in Spotify.', '']
        lines += ['| Section | In Yandex | Found | Skipped (already there / duplicate) | Added | Not found |',
                  '|---|---|---|---|---|---|']
        for section, s in self.stats.items():
            lines.append(f"| {section.replace('|', '&#124;')} | {s['total']} | {s['found']} | {s['duplicates']} | {s['added']} | {s['not_found']} |")
        for section, items in self.not_imported.items():
            if items:
                lines += ['', f'<details><summary>Not found in {section} ({len(items)})</summary>', '']
                lines += [f'- {item}' for item in items]
                lines += ['', '</details>']
        with open(path, 'w', encoding='UTF-8') as file:
            file.write('\n'.join(lines) + '\n')

    def write_not_found(self, path):
        """Not found tracks in the --json-path format: fix the names and import them again."""
        unique = {(track['artist'], track['track']): track for track in self.not_found_tracks}
        with open(path, 'w', encoding='UTF-8') as file:
            json.dump(list(unique.values()), file, ensure_ascii=False, indent=2)

    def import_from_json(self, file_path):
        with open(file_path, 'r', encoding='UTF-8') as file:
            tracks = json.load(file)

        # the file is meant to be edited by hand, so point at the broken entry instead of crashing mid-run
        if not isinstance(tracks, list):
            raise ValueError(f'{file_path}: expected a list of {{"artist": "...", "track": "..."}}')
        for number, track in enumerate(tracks, 1):
            if not (isinstance(track, dict) and isinstance(track.get('artist'), str) and isinstance(track.get('track'), str)):
                raise ValueError(f'{file_path}: entry #{number} must look like {{"artist": "...", "track": "..."}}, got {track!r}')

        playlist_name = 'Imported from JSON'
        playlist_id = self._own_playlists().get(playlist_name)
        existing = self._playlist_track_ids(playlist_id) if playlist_id else set()
        if not playlist_id and not self.dry_run:
            playlist_id = handle_spotify_exception(self.spotify_client.current_user_playlist_create)(playlist_name, public=self.public)['id']

        def save_tracks_callback(importer, chunk):
            logger.info(f'Saving {len(chunk)} tracks...')
            handle_spotify_exception(importer.spotify_client.playlist_add_items)(playlist_id, chunk)
            logger.info('OK')

        try:
            # reversed so that _add_items_to_spotify's reverse() keeps the file order
            self._add_items_to_spotify(list(reversed(tracks)), playlist_name, save_tracks_callback, existing.intersection)
        finally:
            self.save_cache()

        self.print_not_imported()


def make_auth_manager(arguments):
    cache_handler = None
    if arguments.refresh_token:
        # non-interactive mode (CI): start from a stored refresh token, spotipy refreshes it on the first request
        cache_handler = MemoryCacheHandler(token_info={
            'access_token': '', 'refresh_token': arguments.refresh_token, 'expires_at': 0,
        })

    auth_manager = SpotifyOAuth(
        client_id=arguments.id,
        client_secret=arguments.secret,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE,
        username=arguments.spotify,
        cache_handler=cache_handler,
        open_browser=not arguments.refresh_token,
    )
    if cache_handler:
        # spotipy discards cached tokens whose scope doesn't cover the requested one
        cache_handler.token_info['scope'] = auth_manager.scope
    return auth_manager


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Imports Yandex.Music library to Spotify')
    parser.add_argument('-u', '-s', '--spotify', help='Username at spotify.com (only used as a token cache key)')

    spotify_oauth = parser.add_argument_group('spotify_oauth')
    spotify_oauth.add_argument('--id', default=os.environ.get('SPOTIFY_CLIENT_ID'),
                               help='Client ID of your Spotify app (or SPOTIFY_CLIENT_ID env)')
    spotify_oauth.add_argument('--secret', default=os.environ.get('SPOTIFY_CLIENT_SECRET'),
                               help='Client Secret of your Spotify app (or SPOTIFY_CLIENT_SECRET env)')
    spotify_oauth.add_argument('--refresh-token', default=os.environ.get('SPOTIFY_REFRESH_TOKEN'),
                               help='Spotify refresh token for non-interactive runs (or SPOTIFY_REFRESH_TOKEN env)')
    spotify_oauth.add_argument('--print-refresh-token', action='store_true',
                               help='Authorize in the browser, print the refresh token and exit')

    parser.add_argument('-t', '--token', default=os.environ.get('YANDEX_TOKEN'),
                        help='Token from music.yandex.com account (or YANDEX_TOKEN env)')

    parser.add_argument('-i', '--ignore', nargs='+', help='Don\'t import some items',
                        choices=['likes', 'playlists', 'albums', 'artists'], default=[])

    parser.add_argument('-T', '--timeout', help='Request timeout for spotify', type=float, default=10)

    parser.add_argument('-S', '--strict-artists-search', help='Search for an exact match of all artists', action='store_true')

    parser.add_argument('-j', '--json-path', help='JSON file to import tracks from')

    parser.add_argument(
        '-c', '--chunk',
        help='Batch size for Spotify save/add requests (1..40)',
        type=int,
        default=40
    )

    parser.add_argument('-n', '--dry-run', action='store_true', help='Only search and report, change nothing in Spotify')
    parser.add_argument('--cache', help='JSON file to keep Yandex -> Spotify matches between runs')
    parser.add_argument('--report', help='Write a Markdown report to this file')
    parser.add_argument('--not-found', help='Write not found tracks to this file in the --json-path format')
    parser.add_argument('--public', action='store_true', help='Create public playlists (private by default)')

    arguments = parser.parse_args()

    if not 1 <= arguments.chunk <= 40:
        parser.error('The -c/--chunk argument must be between 1 and 40.')
    if not arguments.id or not arguments.secret:
        parser.error('Spotify --id and --secret are required.')

    try:
        auth_manager = make_auth_manager(arguments)
        spotify_client_ = spotipy.Spotify(auth_manager=auth_manager, requests_timeout=arguments.timeout,
                                          requests_session=make_session())

        if arguments.print_refresh_token:
            spotify_client_.me()
            print(f'SPOTIFY_REFRESH_TOKEN={auth_manager.cache_handler.get_cached_token()["refresh_token"]}')
            raise SystemExit(0)

        if arguments.token is None and arguments.json_path is None:
            raise ValueError('Either the -t (token) or -j (json_path) argument must be specified.')

        yandex_client_ = None

        if arguments.token and not arguments.json_path:
            yandex_client_ = Client(arguments.token)
            yandex_client_.init()

        importer_instance = Importer(spotify_client_, yandex_client_, arguments.ignore, arguments.strict_artists_search,
                                     arguments.chunk, arguments.dry_run, arguments.cache, arguments.public)

        # a refresh token issued before a scope was added still works, but fails later on writes with 403
        granted = auth_manager.cache_handler.get_cached_token() or {}
        if 'scope' in granted:
            missing = set(auth_manager.scope.split()) - set(granted['scope'].split())
            if missing:
                raise ValueError(f'Spotify token lacks scopes {", ".join(sorted(missing))}. '
                                 f'Get a new one with --print-refresh-token')

        try:
            if arguments.json_path:
                importer_instance.import_from_json(arguments.json_path)
            else:
                importer_instance.import_all()
        finally:
            if arguments.report:
                importer_instance.write_report(arguments.report)
            if arguments.not_found:
                importer_instance.write_not_found(arguments.not_found)
    except Exception:
        logger.exception('An unexpected error occurred')
        raise SystemExit(1)
