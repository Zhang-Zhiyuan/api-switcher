from __future__ import annotations

import json

from models.profile import BrowserProfile, SSHProfile


def test_profile_integer_fields_tolerate_nonfinite_json_numbers():
    payload = json.loads(
        '{"port": Infinity, "launch_width": Infinity, "launch_height": -Infinity}'
    )

    ssh = SSHProfile.from_dict({"name": "remote", "host": "example.com", **payload})
    browser = BrowserProfile.from_dict(
        {
            "name": "browser",
            "browser_type": "chrome",
            "profile_mode": "managed",
            "user_data_dir": "profile",
            **payload,
        }
    )

    assert ssh.port == 22
    assert browser.launch_width == 1280
    assert browser.launch_height == 900
