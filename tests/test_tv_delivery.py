import sqlite3

import tv_delivery
import wsgi


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.ttl = {}

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return False
        self.store[key] = value
        if ex is not None:
            self.ttl[key] = ex
        return True

    def setex(self, key, ttl, value):
        return self.set(key, value, ex=ttl)

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)
        self.ttl.pop(key, None)
        return 1


def test_group_priority_round_trip_uses_camera_ids(tmp_path):
    db_path = tmp_path / "group_priorities.sqlite"
    original_db_file = wsgi.DB_FILE

    try:
        wsgi.DB_FILE = str(db_path)
        wsgi.init_db()

        tv_delivery.set_group_priority("driveway", ["cam-low", "cam-high"])

        assert tv_delivery.get_group_priority_ids("driveway") == ["cam-low", "cam-high"]

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT camera_id, camera_name, group_name, priority
                FROM camera_group_priorities
                ORDER BY priority ASC, created_at ASC, id ASC
                """
            ).fetchone()

        assert row["camera_id"] == "cam-low"
        assert row["camera_name"] is None
        assert row["group_name"] == "driveway"
        assert row["priority"] == 0
    finally:
        wsgi.DB_FILE = original_db_file


def test_higher_priority_camera_wins_group(tmp_path):
    db_path = tmp_path / "group_priority_winner.sqlite"
    original_db_file = wsgi.DB_FILE

    try:
        wsgi.DB_FILE = str(db_path)
        wsgi.init_db()

        tv_delivery.set_group_priority("driveway", ["high", "low"])

        winner = tv_delivery.resolve_group_winner(
            "driveway",
            [
                {"id": "low", "tv_push_enabled": 1, "tv_rtsp_url": "rtsp://low"},
                {"id": "high", "tv_push_enabled": 1, "tv_rtsp_url": "rtsp://high"},
            ],
        )

        assert winner["id"] == "high"
    finally:
        wsgi.DB_FILE = original_db_file


def test_group_winner_skips_camera_without_rtsp(tmp_path):
    db_path = tmp_path / "group_priority_rtsp.sqlite"
    original_db_file = wsgi.DB_FILE

    try:
        wsgi.DB_FILE = str(db_path)
        wsgi.init_db()

        tv_delivery.set_group_priority("driveway", ["high", "low"])

        winner = tv_delivery.resolve_group_winner(
            "driveway",
            [
                {"id": "high", "tv_push_enabled": 1, "tv_rtsp_url": None},
                {"id": "low", "tv_push_enabled": 1, "tv_rtsp_url": "rtsp://low"},
            ],
        )

        assert winner["id"] == "low"
    finally:
        wsgi.DB_FILE = original_db_file


def test_set_group_priority_succeeds_on_legacy_not_null_camera_name_schema(tmp_path):
    db_path = tmp_path / "legacy_group_priorities.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE camera_group_priorities (
                id TEXT PRIMARY KEY,
                camera_id TEXT,
                camera_name TEXT NOT NULL,
                group_name TEXT NOT NULL,
                priority INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    original_db_file = wsgi.DB_FILE
    try:
        wsgi.DB_FILE = str(db_path)
        wsgi.init_db()

        tv_delivery.set_group_priority("driveway", ["cam-low", "cam-high"])

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT camera_id, camera_name, group_name, priority
                FROM camera_group_priorities
                ORDER BY priority ASC, created_at ASC, id ASC
                """
            ).fetchall()

        assert [row["camera_id"] for row in rows] == ["cam-low", "cam-high"]
        assert [row["camera_name"] for row in rows] == ["cam-low", "cam-high"]
        assert [row["group_name"] for row in rows] == ["driveway", "driveway"]
    finally:
        wsgi.DB_FILE = original_db_file


def test_legacy_group_priority_row_without_camera_id_is_ignored(tmp_path):
    db_path = tmp_path / "legacy_unresolved_group_priority.sqlite"
    original_db_file = wsgi.DB_FILE

    try:
        wsgi.DB_FILE = str(db_path)
        wsgi.init_db()

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO camera_group_priorities (
                    id, camera_id, camera_name, group_name, priority, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("legacy-row", None, "legacy-name", "driveway", 0, "2026-01-01 00:00:00"),
            )
            conn.execute(
                """
                INSERT INTO camera_group_priorities (
                    id, camera_id, camera_name, group_name, priority, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("canonical-row", "cam-real", None, "driveway", 1, "2026-01-01 00:00:01"),
            )

        assert tv_delivery.get_group_priority_ids("driveway") == ["cam-real"]

        winner = tv_delivery.resolve_group_winner(
            "driveway",
            [
                {"id": "legacy-name", "tv_push_enabled": 1, "tv_rtsp_url": "rtsp://legacy"},
                {"id": "cam-real", "tv_push_enabled": 1, "tv_rtsp_url": "rtsp://real"},
            ],
        )

        assert winner["id"] == "cam-real"
    finally:
        wsgi.DB_FILE = original_db_file


def test_create_pairing_session_returns_token_and_manual_code(monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr(tv_delivery, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(tv_delivery.time, "time", lambda: 1000.0)

    result = tv_delivery.create_pairing_session(
        {
            "tv_name": "Living Room",
            "ip_address": "192.168.1.50",
            "port": 8080,
            "device_id": "tv-device-create",
        }
    )

    assert set(result) == {"pairing_token", "manual_code", "expires_at"}
    assert result["pairing_token"]
    assert result["manual_code"]
    assert result["expires_at"] == 1300.0

    stored = fake_redis.get(f"tv_pairing:{result['pairing_token']}")
    assert stored is not None
    code_token = fake_redis.get(f"tv_pairing_code:{result['manual_code']}")
    assert code_token == result["pairing_token"]


def test_dispatch_tv_alert_uses_base_url_for_mjpg_when_global_setting_blank(monkeypatch):
    captured = {}

    monkeypatch.setattr(tv_delivery, "BASE_URL", "http://hub.local:5000")
    monkeypatch.setattr(
        tv_delivery,
        "_load_target_tvs",
        lambda _camera_id, _camera_name: [{"id": "tv-1"}],
    )
    monkeypatch.setattr(
        tv_delivery,
        "send_to_many_tvs",
        lambda _tvs, payload: (
            captured.update({"payload": payload}) or {"delivered": ["tv-1"], "failed": []}
        ),
    )

    result = tv_delivery.dispatch_tv_alert(
        {
            "id": "cam-1",
            "name": "Driveway",
            "tv_stream_type": "mjpg",
            "bi_url": "http://blueiris.local",
            "tv_duration_seconds": 12,
            "tv_group": "driveway",
            "tv_mute_audio": 1,
            "request_id": "req-1",
        },
        "[Driveway][req-1]",
    )

    assert result["delivered"] == ["tv-1"]
    assert captured["payload"]["mjpg_url"] == "http://hub.local:5000/bi-mjpg/cam-1"
    assert captured["payload"]["rtsp_url"] is None


def test_dispatch_tv_alert_skips_mjpg_when_base_url_missing(monkeypatch):
    monkeypatch.setattr(tv_delivery, "BASE_URL", "")

    result = tv_delivery.dispatch_tv_alert(
        {
            "id": "cam-1",
            "name": "Driveway",
            "tv_stream_type": "mjpg",
            "bi_url": "http://blueiris.local",
            "tv_duration_seconds": 12,
            "tv_group": "driveway",
            "tv_mute_audio": 1,
            "request_id": "req-1",
        },
        "[Driveway][req-1]",
    )

    assert result["skipped"] is True
    assert result["payload"]["mjpg_url"] is None


def test_create_pairing_session_retries_manual_code_collision(monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr(tv_delivery, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(tv_delivery.time, "time", lambda: 1000.0)

    fake_redis.set("tv_pairing_code:A1B2C3", "reserved-token", ex=tv_delivery.PAIRING_TTL_SECONDS)
    manual_codes = iter(["a1b2c3", "a1b2c3", "d4e5f6"])
    monkeypatch.setattr(tv_delivery.secrets, "token_hex", lambda _: next(manual_codes))

    result = tv_delivery.create_pairing_session(
        {
            "tv_name": "Bedroom",
            "ip_address": "192.168.1.51",
            "port": 8081,
            "device_id": "tv-device-create-2",
        }
    )

    assert result["manual_code"] == "D4E5F6"
    assert fake_redis.get("tv_pairing_code:A1B2C3") == "reserved-token"
    assert fake_redis.get("tv_pairing_code:D4E5F6") == result["pairing_token"]


def test_finalize_pairing_persists_tv(tmp_path, monkeypatch):
    db_path = tmp_path / "paired_tvs.sqlite"
    original_db_file = wsgi.DB_FILE
    fake_redis = FakeRedis()
    monkeypatch.setattr(tv_delivery, "get_redis_client", lambda: fake_redis)

    try:
        wsgi.DB_FILE = str(db_path)
        wsgi.init_db()

        device_info = {
            "tv_name": "Living Room",
            "ip_address": "192.168.1.50",
            "port": 8080,
            "device_id": "tv-device-1",
        }
        pairing = tv_delivery.create_pairing_session(device_info)

        tv_id = tv_delivery.finalize_pairing(pairing["pairing_token"])

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT id, name, ip_address, port, shared_secret, device_token_id
                FROM paired_tvs
                WHERE id=?
                """,
                (tv_id,),
            ).fetchone()

        assert row["id"] == tv_id
        assert row["name"] == "Living Room"
        assert row["ip_address"] == "192.168.1.50"
        assert row["port"] == 8080
        assert row["shared_secret"]
        assert row["device_token_id"] == "tv-device-1"
        assert fake_redis.get(f"tv_pairing:{pairing['pairing_token']}") is None
    finally:
        wsgi.DB_FILE = original_db_file


def test_finalize_pairing_rejects_missing_required_device_id(tmp_path, monkeypatch):
    db_path = tmp_path / "missing_device_info.sqlite"
    original_db_file = wsgi.DB_FILE
    fake_redis = FakeRedis()
    monkeypatch.setattr(tv_delivery, "get_redis_client", lambda: fake_redis)

    try:
        wsgi.DB_FILE = str(db_path)
        wsgi.init_db()

        pairing = tv_delivery.create_pairing_session(
            {
                "tv_name": "Living Room",
                "ip_address": "192.168.1.62",
                "port": 8090,
            }
        )

        try:
            tv_delivery.finalize_pairing(pairing["pairing_token"])
            raise AssertionError("expected finalize_pairing to reject missing device_id")
        except ValueError as exc:
            assert "device_id" in str(exc)

        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM paired_tvs").fetchone()[0]

        assert count == 0
        assert fake_redis.get(f"tv_pairing:{pairing['pairing_token']}") is not None
        assert fake_redis.get(f"tv_pairing_code:{pairing['manual_code']}") == pairing["pairing_token"]
    finally:
        wsgi.DB_FILE = original_db_file


def test_finalize_pairing_rejects_missing_required_ip_address_without_consuming_session(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "missing_ip_address.sqlite"
    original_db_file = wsgi.DB_FILE
    fake_redis = FakeRedis()
    monkeypatch.setattr(tv_delivery, "get_redis_client", lambda: fake_redis)

    try:
        wsgi.DB_FILE = str(db_path)
        wsgi.init_db()

        pairing = tv_delivery.create_pairing_session(
            {
                "tv_name": "Living Room",
                "device_id": "tv-device-missing-ip",
            }
        )

        try:
            tv_delivery.finalize_pairing(pairing["pairing_token"])
            raise AssertionError("expected finalize_pairing to reject missing ip_address")
        except ValueError as exc:
            assert "ip_address" in str(exc)

        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM paired_tvs").fetchone()[0]

        assert count == 0
        assert fake_redis.get(f"tv_pairing:{pairing['pairing_token']}") is not None
        assert fake_redis.get(f"tv_pairing_code:{pairing['manual_code']}") == pairing["pairing_token"]
    finally:
        wsgi.DB_FILE = original_db_file


def test_finalize_pairing_uses_safe_rtsp_fallback_on_legacy_table(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy_paired_tvs.sqlite"
    original_db_file = wsgi.DB_FILE
    fake_redis = FakeRedis()
    monkeypatch.setattr(tv_delivery, "get_redis_client", lambda: fake_redis)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE paired_tvs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                rtsp_url TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    try:
        wsgi.DB_FILE = str(db_path)
        wsgi.init_db()

        pairing = tv_delivery.create_pairing_session(
            {
                "tv_name": "Kitchen",
                "ip_address": "192.168.1.60",
                "device_id": "tv-device-2",
            }
        )

        tv_id = tv_delivery.finalize_pairing(pairing["pairing_token"])

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, name, rtsp_url, device_token_id FROM paired_tvs WHERE id=?",
                (tv_id,),
            ).fetchone()

        assert row["name"] == "Kitchen"
        assert row["rtsp_url"] == ""
        assert row["device_token_id"] == "tv-device-2"
    finally:
        wsgi.DB_FILE = original_db_file


def test_finalize_pairing_returns_existing_row_for_duplicate_token(tmp_path, monkeypatch):
    db_path = tmp_path / "paired_tvs_idempotent.sqlite"
    original_db_file = wsgi.DB_FILE
    fake_redis = FakeRedis()
    monkeypatch.setattr(tv_delivery, "get_redis_client", lambda: fake_redis)

    try:
        wsgi.DB_FILE = str(db_path)
        wsgi.init_db()

        pairing = tv_delivery.create_pairing_session(
            {
                "tv_name": "Hallway",
                "ip_address": "192.168.1.61",
                "rtsp_url": "rtsp://192.168.1.61/live",
                "device_id": "tv-device-3",
            }
        )

        first_id = tv_delivery.finalize_pairing(pairing["pairing_token"])
        second_id = tv_delivery.finalize_pairing(pairing["pairing_token"])

        with sqlite3.connect(db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM paired_tvs WHERE device_token_id=?",
                ("tv-device-3",),
            ).fetchone()[0]

        assert second_id == first_id
        assert count == 1
    finally:
        wsgi.DB_FILE = original_db_file


def test_finalize_pairing_reuses_existing_row_for_same_device_with_new_session(tmp_path, monkeypatch):
    db_path = tmp_path / "paired_tvs_repair.sqlite"
    original_db_file = wsgi.DB_FILE
    fake_redis = FakeRedis()
    monkeypatch.setattr(tv_delivery, "get_redis_client", lambda: fake_redis)

    try:
        wsgi.DB_FILE = str(db_path)
        wsgi.init_db()

        first_pairing = tv_delivery.create_pairing_session(
            {
                "tv_name": "Living Room",
                "ip_address": "192.168.1.50",
                "port": 8080,
                "rtsp_url": "rtsp://192.168.1.50/old",
                "device_id": "tv-device-4",
            }
        )
        first_id = tv_delivery.finalize_pairing(first_pairing["pairing_token"])

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO camera_tv_targets (id, camera_id, camera_name, tv_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("target-1", "cam-front", "Front Door", first_id, "2026-01-01 00:00:01"),
            )
            initial_secret = conn.execute(
                "SELECT shared_secret FROM paired_tvs WHERE id=?",
                (first_id,),
            ).fetchone()[0]

        second_pairing = tv_delivery.create_pairing_session(
            {
                "tv_name": "Living Room XL",
                "ip_address": "192.168.1.75",
                "port": 9090,
                "rtsp_url": "rtsp://192.168.1.75/live",
                "device_id": "tv-device-4",
            }
        )
        second_id = tv_delivery.finalize_pairing(second_pairing["pairing_token"])

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT id, name, ip_address, port, rtsp_url, shared_secret, device_token_id
                FROM paired_tvs
                WHERE id=?
                """,
                (second_id,),
            ).fetchone()
            target = conn.execute(
                "SELECT tv_id FROM camera_tv_targets WHERE id=?",
                ("target-1",),
            ).fetchone()

        assert second_id == first_id
        assert row["name"] == "Living Room XL"
        assert row["ip_address"] == "192.168.1.75"
        assert row["port"] == 9090
        assert row["rtsp_url"] == "rtsp://192.168.1.75/live"
        assert row["shared_secret"] != initial_secret
        assert row["device_token_id"] == "tv-device-4"
        assert target["tv_id"] == first_id
    finally:
        wsgi.DB_FILE = original_db_file


def test_init_db_deduplicates_duplicate_device_token_rows(tmp_path):
    db_path = tmp_path / "duplicate_tokens.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE paired_tvs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                rtsp_url TEXT NOT NULL,
                device_token_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE camera_tv_targets (
                id TEXT PRIMARY KEY,
                camera_id TEXT,
                camera_name TEXT NOT NULL,
                tv_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO paired_tvs (id, name, rtsp_url, device_token_id, created_at) VALUES (?, ?, ?, ?, ?)",
            ("tv-a", "Alpha", "rtsp://alpha", "device-1", "2026-01-01 00:00:01"),
        )
        conn.execute(
            "INSERT INTO paired_tvs (id, name, rtsp_url, device_token_id, created_at) VALUES (?, ?, ?, ?, ?)",
            ("tv-b", "Beta", "rtsp://beta", "device-1", "2026-01-01 00:00:02"),
        )
        conn.execute(
            "INSERT INTO paired_tvs (id, name, rtsp_url, device_token_id, created_at) VALUES (?, ?, ?, ?, ?)",
            ("tv-c", "Gamma", "rtsp://gamma", "device-2", "2026-01-01 00:00:03"),
        )
        conn.execute(
            "INSERT INTO camera_tv_targets (id, camera_id, camera_name, tv_id, created_at) VALUES (?, ?, ?, ?, ?)",
            ("target-1", "cam-front", "Front Door", "tv-a", "2026-01-01 00:00:04"),
        )
        conn.execute(
            "INSERT INTO camera_tv_targets (id, camera_id, camera_name, tv_id, created_at) VALUES (?, ?, ?, ?, ?)",
            ("target-2", "cam-front", "Back Yard", "tv-b", "2026-01-01 00:00:05"),
        )

    original_db_file = wsgi.DB_FILE
    try:
        wsgi.DB_FILE = str(db_path)
        wsgi.init_db()

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, name, device_token_id FROM paired_tvs ORDER BY device_token_id, created_at, id"
            ).fetchall()
            targets = conn.execute(
                "SELECT camera_id, camera_name, tv_id FROM camera_tv_targets ORDER BY camera_id, tv_id"
            ).fetchall()
            token_indexes = conn.execute(
                "PRAGMA index_list(paired_tvs)"
            ).fetchall()
            target_indexes = conn.execute(
                "PRAGMA index_list(camera_tv_targets)"
            ).fetchall()

        assert len(rows) == 2
        assert [row["id"] for row in rows] == ["tv-b", "tv-c"]
        assert [row["device_token_id"] for row in rows] == ["device-1", "device-2"]
        assert len(targets) == 1
        assert [target["tv_id"] for target in targets] == ["tv-b"]
        assert [target["camera_id"] for target in targets] == ["cam-front"]
        assert any(index[1] == "idx_paired_tvs_device_token_id" for index in token_indexes)
        assert any(index[1] == "idx_camera_tv_targets_camera_id_tv_id" and index[2] for index in target_indexes)
    finally:
        wsgi.DB_FILE = original_db_file


def test_init_db_resolves_legacy_camera_names_without_blocking_canonical_uniqueness(tmp_path):
    db_path = tmp_path / "legacy_targets.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE configs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                chat_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("INSERT INTO configs (id, name) VALUES (?, ?)", ("cam-front", "Front Door"))
        conn.execute("INSERT INTO configs (id, name) VALUES (?, ?)", ("cam-shared-1", "Shared"))
        conn.execute("INSERT INTO configs (id, name) VALUES (?, ?)", ("cam-shared-2", "Shared"))
        conn.execute(
            """
            CREATE TABLE paired_tvs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                rtsp_url TEXT NOT NULL,
                device_token_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE camera_tv_targets (
                id TEXT PRIMARY KEY,
                camera_name TEXT NOT NULL,
                tv_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO paired_tvs (id, name, rtsp_url, device_token_id, created_at) VALUES (?, ?, ?, ?, ?)",
            ("tv-a", "Alpha", "rtsp://alpha", "device-1", "2026-01-01 00:00:01"),
        )
        conn.execute(
            "INSERT INTO camera_tv_targets (id, camera_name, tv_id, created_at) VALUES (?, ?, ?, ?)",
            ("target-1", "Front Door", "tv-a", "2026-01-01 00:00:04"),
        )
        conn.execute(
            "INSERT INTO camera_tv_targets (id, camera_name, tv_id, created_at) VALUES (?, ?, ?, ?)",
            ("target-2", "Front Door", "tv-a", "2026-01-01 00:00:05"),
        )
        conn.execute(
            "INSERT INTO camera_tv_targets (id, camera_name, tv_id, created_at) VALUES (?, ?, ?, ?)",
            ("target-3", "Shared", "tv-a", "2026-01-01 00:00:06"),
        )

    original_db_file = wsgi.DB_FILE
    try:
        wsgi.DB_FILE = str(db_path)
        wsgi.init_db()

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            targets = conn.execute(
                """
                SELECT camera_id, camera_name, tv_id
                FROM camera_tv_targets
                ORDER BY COALESCE(camera_id, ''), camera_name, tv_id, created_at, id
                """
            ).fetchall()
            indexes = conn.execute("PRAGMA index_list(camera_tv_targets)").fetchall()
            columns = {row[1] for row in conn.execute("PRAGMA table_info(camera_tv_targets)")}

        assert "camera_id" in columns
        assert any(index[1] == "idx_camera_tv_targets_camera_id_tv_id" and index[2] for index in indexes)
        assert len(targets) == 2
        assert {row["camera_id"] for row in targets} == {"cam-front", None}
        assert any(
            row["camera_id"] == "cam-front" and row["camera_name"] == "Front Door" and row["tv_id"] == "tv-a"
            for row in targets
        )
        assert any(
            row["camera_id"] is None and row["camera_name"] == "Shared" and row["tv_id"] == "tv-a"
            for row in targets
        )
    finally:
        wsgi.DB_FILE = original_db_file


def test_send_to_selected_tvs_retries_failed_device(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json, "timeout": timeout})

        class Response:
            status_code = 500 if len(calls) == 1 else 200
            text = "ok"

        return Response()

    monkeypatch.setattr(tv_delivery.requests, "post", fake_post)

    result = tv_delivery.send_to_tv_device(
        {
            "id": "tv-1",
            "ip_address": "192.168.1.80",
            "port": 7979,
            "shared_secret": "secret",
        },
        {"camera_name": "Driveway", "rtsp_url": "rtsp://cam/live", "duration": 20},
        attempts=2,
    )

    assert result["ok"] is True
    assert result["tv_id"] == "tv-1"
    assert len(calls) == 2
    assert calls[0]["url"] == "http://192.168.1.80:7979/notify"
    assert calls[0]["json"]["payload"]["camera_name"] == "Driveway"
    assert calls[0]["json"]["signing"]["algorithm"] == "hmac-sha256"
    assert calls[0]["json"]["signature"] == calls[1]["json"]["signature"]


def test_send_to_many_tvs_isolated_failures(monkeypatch):
    monkeypatch.setattr(
        tv_delivery,
        "send_to_tv_device",
        lambda tv, payload, attempts=2: {
            "tv_id": tv["id"],
            "ok": tv["id"] == "tv-ok",
        },
    )

    result = tv_delivery.send_to_many_tvs(
        [{"id": "tv-ok"}, {"id": "tv-fail"}],
        {"camera_name": "Driveway", "rtsp_url": "rtsp://cam/live", "duration": 20},
    )

    assert result["delivered"] == ["tv-ok"]
    assert result["failed"] == ["tv-fail"]


def test_pair_remote_tv_by_code_persists_paired_tv(tmp_path, monkeypatch):
    db_path = tmp_path / "remote_pairing.sqlite"
    original_db_file = wsgi.DB_FILE

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "tv_name": "Sony Lounge",
                "ip_address": "192.168.10.6",
                "port": 7979,
                "device_id": "sony-lounge-1",
            }

    posted = {}

    def fake_post(url, json=None, timeout=None):
        posted["url"] = url
        posted["json"] = json
        posted["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(tv_delivery.requests, "post", fake_post)

    try:
        wsgi.DB_FILE = str(db_path)
        wsgi.init_db()

        tv_id = tv_delivery.pair_remote_tv_by_code("192.168.10.6", "ABC123")

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT id, name, ip_address, port, shared_secret, device_token_id
                FROM paired_tvs
                WHERE id=?
                """,
                (tv_id,),
            ).fetchone()

        assert posted["url"] == "http://192.168.10.6:7979/pair/complete"
        assert posted["json"]["manual_code"] == "ABC123"
        assert posted["json"]["shared_secret"]
        assert posted["timeout"] == 5
        assert row["id"] == tv_id
        assert row["name"] == "Sony Lounge"
        assert row["ip_address"] == "192.168.10.6"
        assert row["port"] == 7979
        assert row["shared_secret"] == posted["json"]["shared_secret"]
        assert row["device_token_id"] == "sony-lounge-1"
    finally:
        wsgi.DB_FILE = original_db_file


def test_pair_remote_tv_by_code_rejects_public_ip(tmp_path):
    db_path = tmp_path / "remote_pairing_public_ip.sqlite"
    original_db_file = wsgi.DB_FILE

    try:
        wsgi.DB_FILE = str(db_path)
        wsgi.init_db()

        try:
            tv_delivery.pair_remote_tv_by_code("8.8.8.8", "ABC123")
            assert False, "expected ValueError for public IP"
        except ValueError as exc:
            assert str(exc) == "ip_address must be a private or loopback IP address"
    finally:
        wsgi.DB_FILE = original_db_file


def test_pair_remote_tv_by_code_rejects_hostname(tmp_path):
    db_path = tmp_path / "remote_pairing_hostname.sqlite"
    original_db_file = wsgi.DB_FILE

    try:
        wsgi.DB_FILE = str(db_path)
        wsgi.init_db()

        try:
            tv_delivery.pair_remote_tv_by_code("example.local", "ABC123")
            assert False, "expected ValueError for hostname"
        except ValueError as exc:
            assert str(exc) == "ip_address must be a valid IP address"
    finally:
        wsgi.DB_FILE = original_db_file
