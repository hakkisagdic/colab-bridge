"""
A Chrome of colab-bridge's own for the Colab tab, with Chrome's background throttling turned off.

Chrome slows down pages it considers hidden (a background tab, a minimized or covered window): after a few minutes
their timers fire at most once a minute and their renderer gets less of the CPU. A tab the bridge drives sits hidden
most of the time, so its cells answer late and fetch drops to a few KB/s. `colab-bridge open` opens the bridge's link
in a separate Chrome profile started without that throttling. The person signs in to Google in that profile once, and
it stays signed in; their everyday Chrome and its profile are not touched.
"""

import os
import shutil
import subprocess
import sys

DEFAULT_PROFILE = os.path.expanduser("~/.cache/colab-bridge/chrome")
MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PATH_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
UNTHROTTLED = ("--disable-background-timer-throttling", "--disable-renderer-backgrounding",
               "--disable-backgrounding-occluded-windows", "--disable-features=IntensiveWakeUpThrottling")


def find_chrome(explicit: str = None):
    """Google Chrome or Chromium: explicit, COLAB_BRIDGE_CHROME, the macOS app, or one on PATH; None when there is none."""
    for candidate in (explicit, os.environ.get("COLAB_BRIDGE_CHROME")):
        if candidate:
            return candidate
    if sys.platform == "darwin" and os.path.exists(MAC_CHROME):
        return MAC_CHROME
    return next((path for path in map(shutil.which, PATH_NAMES) if path), None)


def chrome_command(chrome: str, profile: str, link: str) -> list:
    """The command that opens link in a new window of the bridge's Chrome. A Chrome already running on that profile
    opens the window itself and keeps the switches it started with, which are these."""
    return [chrome, f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", *UNTHROTTLED,
            "--new-window", link]


def open_link(link: str, chrome: str = None, profile: str = None) -> list:
    """Opens link in the bridge's Chrome, detached from this process; returns the command. Raises FileNotFoundError
    when no Chrome is found."""
    chrome = find_chrome(chrome)
    if not chrome:
        raise FileNotFoundError("Google Chrome was not found: pass --chrome PATH, or set COLAB_BRIDGE_CHROME.")
    profile = os.path.expanduser(profile or DEFAULT_PROFILE)
    os.makedirs(profile, exist_ok=True)
    command = chrome_command(chrome, profile, link)
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return command
