import argparse
from functools import partial
import logging
import shelve
import sys
from typing import Optional
import webbrowser

from pid import PidFile, PidFileError
from plexapi.myplex import MyPlexAccount
import pychromecast
import xdg
import zeroconf

from .auth import PlexApplication, PlexAuthClient
from .core import AutoSkipper
from .notifications import NotificationListener
from .seekables import (
    ChromecastMonitor,
    ChromecastSeekableProvider,
    PlexSeekableProvider,
    SeekableProviderChain
)
from .sessions import SessionDiscovery, SessionDispatcher, SessionProvider
from .stores import Database


_APP_NAME = 'Skippex'
_DATABASE_PATH = xdg.xdg_data_home() / 'skippex.db'
_PID_DIR = xdg.xdg_runtime_dir()
_PID_NAME = 'skippex.pid'


def _print_stderr(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def cmd_auth(args: argparse.Namespace, db: Database, app: PlexApplication):
    plex_auth = PlexAuthClient(app)
    pin_id, pin_code = plex_auth.generate_pin()
    auth_url = plex_auth.generate_auth_url(pin_code)

    webbrowser.open_new_tab(auth_url)
    _print_stderr('Navigate to the following page to authorize this application:')
    _print_stderr(auth_url)
    _print_stderr()

    _print_stderr('Waiting for successful authorization...')
    auth_token = plex_auth.wait_for_token(pin_id, pin_code)

    db.auth_token = auth_token
    _print_stderr('Authorization successful')


def cmd_default(args: argparse.Namespace, db: Database, app: PlexApplication) -> Optional[int]:
    try:
        auth_token = db.auth_token
    except KeyError:
        _print_stderr(
            "No credentials found. Please first run the 'auth' command to "
            "authorize the application."
        )
        return 1

    auth_client = PlexAuthClient(app)
    if not auth_client.is_token_valid(auth_token):
        _print_stderr("Token invalid. Please run the 'auth' command to reauthenticate yourself.")
        return 1

    account = MyPlexAccount(token=auth_token)

    try:
        server_resource = next(r for r in account.resources() if 'server' in r.provides)
    except StopIteration:
        _print_stderr("Couldn't find a Plex server for this account.")
        return 1
    else:
        # TODO: Ensure we try HTTP only if HTTPS fails.
        server = server_resource.connect()

    # Build the object hierarchy.

    cc_listener = pychromecast.discovery.CastListener()
    zconf = zeroconf.Zeroconf()
    cc_monitor = ChromecastMonitor(cc_listener, zconf)

    cc_listener.add_callback = cc_monitor.add_callback
    cc_listener.update_callback = cc_monitor.update_callback
    cc_listener.remove_callback = cc_monitor.remove_callback
    cc_browser = pychromecast.discovery.start_discovery(cc_listener, zconf)

    seekable_provider = SeekableProviderChain([
        PlexSeekableProvider(server),
        ChromecastSeekableProvider(cc_monitor),
    ])

    session_provider = SessionProvider(server)
    auto_skipper = AutoSkipper(seekable_provider)
    dispatcher = SessionDispatcher(listener=auto_skipper)

    discovery = SessionDiscovery(
        server=server,
        provider=session_provider,
        dispatcher=dispatcher,
        extrapolator=auto_skipper,
    )

    notif_listener = NotificationListener(server, discovery.alert_callback)
    notif_listener.run_forever()


def _main():
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(threadName)s - %(name)s - %(funcName)s - %(levelname)s - %(message)s',
    )

    db = Database(shelve.open(str(_DATABASE_PATH)))
    app = PlexApplication(name=_APP_NAME, identifier=db.app_id)

    parser = argparse.ArgumentParser()
    parser.set_defaults(func=partial(cmd_default, db=db, app=app))

    subparsers = parser.add_subparsers(title='subcommands')
    parser_auth = subparsers.add_parser('auth', help='authorize this application to access your Plex account')
    parser_auth.set_defaults(func=partial(cmd_auth, db=db, app=app))

    args = parser.parse_args()
    sys.exit(args.func(args))


def main():
    try:
        with PidFile(piddir=_PID_DIR, pidname=_PID_NAME):
            _main()
    except PidFileError:
        _print_stderr(
            f'Another instance of {_APP_NAME} is already running.\n'
            f'Please terminate it before running this command.'
        )
        return 1
