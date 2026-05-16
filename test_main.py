from __future__ import annotations

import main
from ui.startup_splash import SPLASH_ARG, StartupSplash


def test_parse_args_defaults_to_splash_enabled():
    args = main.parse_args([])

    assert args.start_minimized is False
    assert args.no_splash is False


def test_parse_args_supports_no_splash_and_minimized_aliases():
    args = main.parse_args(["--tray", "--no-splash", "--ignored"])

    assert args.start_minimized is True
    assert args.no_splash is True
    assert args.splash_child is False


def test_parse_args_supports_hidden_splash_child_mode():
    args = main.parse_args([SPLASH_ARG])

    assert args.splash_child is True


def test_disabled_startup_splash_is_noop():
    splash = StartupSplash(enabled=False)

    assert splash.visible is False
    splash.pulse("ignored")
    splash.keep_visible_for(0)
    splash.close()
    assert splash.visible is False
