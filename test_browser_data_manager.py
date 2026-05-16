import sqlite3

from core.browser_data_manager import BrowserDataManager


def test_clear_cookies_db_uses_unique_temps_and_cleans_target_domains(tmp_path):
    default_dir = tmp_path / "Default"
    network_dir = default_dir / "Network"
    network_dir.mkdir(parents=True)
    cookies_path = network_dir / "Cookies"

    conn = sqlite3.connect(cookies_path)
    try:
        conn.execute("CREATE TABLE cookies (host_key TEXT)")
        conn.executemany(
            "INSERT INTO cookies (host_key) VALUES (?)",
            [
                ("chatgpt.com",),
                (".chatgpt.com",),
                ("example.com",),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    BrowserDataManager()._clear_cookies_db(default_dir, ["chatgpt.com"])

    conn = sqlite3.connect(cookies_path)
    try:
        rows = [row[0] for row in conn.execute("SELECT host_key FROM cookies ORDER BY host_key")]
    finally:
        conn.close()

    assert rows == ["example.com"]
    assert not list(network_dir.glob("Cookies.*"))
