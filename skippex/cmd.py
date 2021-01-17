import argparse
from functools import partial
import logging
import os
from pathlib import Path
import shelve
import sys
import tempfile
from typing import Optional
import webbrowser

from pid import PidFile, PidFileError
from plexapi.myplex import MyPlexAccount, MyPlexResource
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


# Note: Don't assume that the XDG paths are all different from each other (see
# Dockerfile).

if os.getenv('SK_DEV', '0') == '1':
    _DATABASE_PATH = xdg.xdg_data_home() / 'skippex_dev.db'
    _PID_NAME = 'skippex_dev.pid'
else:
    _DATABASE_PATH = xdg.xdg_data_home() / 'skippex.db'
    _PID_NAME = 'skippex.pid'

_APP_NAME = 'Skippex'
_APP_ARGV0 = __package__
_PID_DIR = xdg.xdg_runtime_dir() or Path(tempfile.gettempdir())
_PID_PATH = _PID_DIR / _PID_NAME


logger = logging.getLogger(__name__)


def cmd_debug_info(args: argparse.Namespace, db: Database, app: PlexApplication):
    from pprint import pprint

    print(f'PID path: {_PID_PATH}')
    print(f'Database path: {_DATABASE_PATH}')
    print()
    print(f'Database content:')
    pprint(db.content())


def cmd_auth(args: argparse.Namespace, db: Database, app: PlexApplication):
    plex_auth = PlexAuthClient(app)
    pin_id, pin_code = plex_auth.generate_pin()
    auth_url = plex_auth.generate_auth_url(pin_code)

    webbrowser.open_new_tab(auth_url)
    logger.info('Navigate to the following page to authorize this application:')
    logger.info(auth_url)

    logger.info('Waiting for successful authorization...')
    auth_token = plex_auth.wait_for_token(pin_id, pin_code)

    db.auth_token = auth_token
    logger.info('Authorization successful')


def _find_server(account: MyPlexAccount, server_name: Optional[str]) -> Optional[MyPlexResource]:
    for resource in account.resources():
        if 'server' not in resource.provides:
            continue
        if (server_name and resource.name == server_name) or not server_name:
            return resource
    return None


def cmd_run(args: argparse.Namespace, db: Database, app: PlexApplication) -> Optional[int]:
    try:
        auth_token = db.auth_token
    except KeyError:
        logger.error(
            "No credentials found. Please first run the 'auth' command to "
            "authorize the application."
        )
        return 1

    logger.info('Verifying token...')
    auth_client = PlexAuthClient(app)
    if not auth_client.is_token_valid(auth_token):
        logger.error("Token invalid. Please run the 'auth' command to reauthenticate yourself.")
        return 1

    logger.info('Connecting to Plex server...')
    account = MyPlexAccount(token=auth_token)
    server_resource = _find_server(account, args.server)

    if not server_resource:
        if args.server:
            logger.error(f"Could not find server '{args.server}' for this account.")
        else:
            logger.error(f"Could not find a server associated with this account.")
        return 1

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
    logger.info('Ready')
    notif_listener.run_forever()


def _main():
    db = Database(shelve.open(str(_DATABASE_PATH)))
    app = PlexApplication(name=_APP_NAME, identifier=db.app_id)

    parser = argparse.ArgumentParser(_APP_ARGV0, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--debug', help='enable debug logging', action='store_true')

    subparsers = parser.add_subparsers(title='subcommands', metavar='{auth,run}')
    subparsers.required = True

    parser_auth = subparsers.add_parser('auth', help='authorize this application to access your Plex account')
    parser_auth.set_defaults(func=partial(cmd_auth, db=db, app=app))

    parser_debug_info = subparsers.add_parser('debug-info')
    parser_debug_info.set_defaults(func=partial(cmd_debug_info, db=db, app=app))

    parser_run = subparsers.add_parser('run', help='monitor your shows and automatically skip intros')
    parser_run.set_defaults(func=partial(cmd_run, db=db, app=app))
    parser_run.add_argument('--server', help='name of your server (default: the first server Skippex finds)')

    args = parser.parse_args()

    if args.debug:
        log_level = logging.DEBUG
        log_format = '%(asctime)s - %(threadName)s - %(name)s - %(funcName)s - %(levelname)s - %(message)s'
        log_datefmt = None  # Use the default.
    else:
        log_level = logging.INFO
        log_format = '%(asctime)s - %(levelname)s - %(message)s'
        log_datefmt = '%Y-%m-%d %H:%M:%S'  # No milliseconds.

        # Disable logging from third-party packages.
        logger_name: str
        logger_inst: logging.Logger
        for logger_name, logger_inst in logging.root.manager.loggerDict.items():  # type: ignore
            if isinstance(logger_inst, logging.PlaceHolder):
                continue
            if not logger_name.startswith(__package__):
                logger_inst.addHandler(logging.NullHandler())
                logger_inst.propagate = False

    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt=log_datefmt,
    )

    sys.exit(args.func(args))


def main():
    try:
        with PidFile(piddir=_PID_DIR, pidname=_PID_NAME):
            _main()
    except PidFileError:
        logger.error(
            f'Another instance of {_APP_NAME} is already running.\n'
            f'Please terminate it before running this command.'
        )
        return 1
    except KeyboardInterrupt:
        logger.info('Bye')
        return 0
