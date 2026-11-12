import os
import sys
import json
import time
import signal
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from bot import (
    CloudWorkerConfig,
    validate_startup_configuration,
    get_secret,
    log_event,
    handle_sigterm,
    DatabaseManager,
    SolvedRegistry,
    execute_batch,
    VerdictState,
    SubmissionVerdict,
    SHUTDOWN_REQUESTED,
)

@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_cloud.db"
    db = DatabaseManager(db_path=db_file, auto_migrate=False)
    old_db = SolvedRegistry._db
    SolvedRegistry.set_db(db)
    yield db
    SolvedRegistry.set_db(old_db)

class TestCloudWorkerConfiguration:
    def test_configuration_validation_success(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test_gemini_key")
        monkeypatch.setenv("GITHUB_USERNAME", "testuser")
        monkeypatch.setenv("GITHUB_TOKEN", "test_token")
        with patch("bot.GEMINI_API_KEY", "test_gemini_key"), \
             patch("bot.GITHUB_USERNAME", "testuser"), \
             patch("bot.GITHUB_TOKEN", "test_token"):
            cfg = validate_startup_configuration(mode="both", count=3)
            assert cfg.count == 3
            assert cfg.gemini_key_present is True
            assert cfg.github_auth_present is True

    def test_configuration_validation_missing_gemini_fails_in_production(self, monkeypatch):
        with patch("bot.GEMINI_API_KEY", ""), \
             patch("bot.DRY_RUN", False):
            with pytest.raises(ValueError, match="GEMINI_API_KEY .* is missing"):
                validate_startup_configuration(mode="both", dry_run=False, offline_mode=False)

    def test_configuration_validation_allows_missing_gemini_in_dry_run(self, monkeypatch):
        with patch("bot.GEMINI_API_KEY", ""), \
             patch("bot.DRY_RUN", False):
            cfg = validate_startup_configuration(mode="both", dry_run=True)
            assert cfg.dry_run is True
            assert cfg.gemini_key_present is True

    def test_secret_manager_lookup_and_fallback(self, monkeypatch):
        # 1. Plain env var
        monkeypatch.setenv("TEST_KEY", "my_secret_val")
        assert get_secret("TEST_KEY", "default") == "my_secret_val"

        # 2. Dry run fallback
        with patch("bot.DRY_RUN", True):
            assert get_secret("NONEXISTENT_KEY", "default") == "dry_run_nonexistent_key"

        # 3. Secret manager mock
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with patch.dict(os.environ, {"GCP_PROJECT_ID": "test-project"}, clear=False), \
             patch("bot.DRY_RUN", False):
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.payload.data.decode.return_value = "secret_from_gcp"
            mock_client.access_secret_version.return_value = mock_resp

            with patch.dict("sys.modules", {"google.cloud": MagicMock(), "google.cloud.secretmanager": MagicMock()}):
                import google.cloud.secretmanager as sm
                sm.SecretManagerServiceClient.return_value = mock_client
                res = get_secret("GEMINI_API_KEY", "fallback")
                assert res == "secret_from_gcp"

class TestStructuredCloudLogging:
    def test_structured_json_logging(self, capsys):
        with patch("bot.STRUCTURED_LOGGING", True):
            log_event("INFO", "Processing started", run_id=42, platform="leetcode")
            captured = capsys.readouterr()
            data = json.loads(captured.out.strip())
            assert data["severity"] == "INFO"
            assert data["message"] == "Processing started"
            assert data["component"] == "autocoder"
            assert data["run_id"] == 42
            assert data["platform"] == "leetcode"
            assert "timestamp" in data

    def test_structured_logging_redacts_secrets(self, capsys):
        with patch("bot.STRUCTURED_LOGGING", True):
            log_event("WARNING", "Failed with token=ghp_secretpassword12345678901234567890", extra="secret_token")
            captured = capsys.readouterr()
            out = captured.out or captured.err
            assert "ghp_secretpassword" not in out
            assert "[REDACTED_SECRET]" in out or "[REDACTED_GITHUB_TOKEN]" in out

class TestCrashRecoveryAndLeases:
    def test_recover_expired_leases(self, temp_db):
        now = time.time()
        # Insert 3 problems: 1 expired in_progress, 1 fresh in_progress, 1 accepted
        with temp_db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO problems (platform, canonical_id, url, status, claimed_by, claimed_at, created_at, updated_at)
                VALUES ('leetcode', 'p1', 'https://leetcode.com/p1', 'IN_PROGRESS', 'worker_dead', ?, '2026-01-01', '2026-01-01')
            """, (now - 1200,)) # 20 mins ago (expired)
            cur.execute("""
                INSERT INTO problems (platform, canonical_id, url, status, claimed_by, claimed_at, created_at, updated_at)
                VALUES ('leetcode', 'p2', 'https://leetcode.com/p2', 'IN_PROGRESS', 'worker_live', ?, '2026-01-01', '2026-01-01')
            """, (now - 60,)) # 1 min ago (fresh)
            cur.execute("""
                INSERT INTO problems (platform, canonical_id, url, status, created_at, updated_at)
                VALUES ('leetcode', 'p3', 'https://leetcode.com/p3', 'ACCEPTED', '2026-01-01', '2026-01-01')
            """)

        recovered = temp_db.recover_expired_leases(lease_timeout_sec=600.0)
        assert recovered == 1

        with temp_db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT status, claimed_by FROM problems WHERE canonical_id = 'p1'")
            row_p1 = cur.fetchone()
            assert row_p1[0] == "TRANSIENT_FAILURE"
            assert row_p1[1] is None

            cur.execute("SELECT status, claimed_by FROM problems WHERE canonical_id = 'p2'")
            row_p2 = cur.fetchone()
            assert row_p2[0] == "IN_PROGRESS"
            assert row_p2[1] == "worker_live"

            cur.execute("SELECT status FROM problems WHERE canonical_id = 'p3'")
            assert cur.fetchone()[0] == "ACCEPTED"

class TestDailyRunAtomicLocking:
    def test_prevent_overlapping_runs(self, temp_db):
        # Worker 1 acquires lock
        run_id1 = temp_db.acquire_daily_run_lock(
            platform="leetcode",
            target_quota=5,
            worker_id="worker_alpha",
            run_date="2026-11-12",
            lock_duration_sec=3600.0
        )
        assert run_id1 is not None

        # Worker 2 attempts concurrent lock -> rejected
        run_id2 = temp_db.acquire_daily_run_lock(
            platform="leetcode",
            target_quota=5,
            worker_id="worker_beta",
            run_date="2026-11-12",
            lock_duration_sec=3600.0
        )
        assert run_id2 is None

        # Worker 1 completes run with target quota met
        temp_db.increment_daily_run_counts(run_id1, accepted=5)
        temp_db.complete_daily_run(run_id1, status="COMPLETED")

        # Worker 3 attempts duplicate run after completion -> rejected
        run_id3 = temp_db.acquire_daily_run_lock(
            platform="leetcode",
            target_quota=5,
            worker_id="worker_gamma",
            run_date="2026-11-12"
        )
        assert run_id3 is None

    def test_reclaim_stale_lock(self, temp_db):
        now = time.time()
        # Create stale lock from 2 hours ago
        with temp_db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO daily_runs (run_date, platform, target_quota, started_at, status, locked_by, locked_at)
                VALUES ('2026-11-12', 'codeforces', 5, '2026-11-12T00:00:00', 'IN_PROGRESS', 'worker_dead', ?)
            """, (now - 7200,))

        # New worker reclaims stale lock
        run_id = temp_db.acquire_daily_run_lock(
            platform="codeforces",
            target_quota=5,
            worker_id="worker_alive",
            run_date="2026-11-12",
            lock_duration_sec=3600.0
        )
        assert run_id is not None

class TestGracefulShutdown:
    def test_sigterm_triggers_shutdown_flag(self):
        import bot
        bot.SHUTDOWN_REQUESTED = False
        handle_sigterm(signal.SIGTERM, None)
        assert bot.SHUTDOWN_REQUESTED is True
        bot.SHUTDOWN_REQUESTED = False

class TestPostgresqlDialectAdapter:
    def test_sql_placeholder_and_ddl_adaptation(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        db = DatabaseManager.__new__(DatabaseManager)
        db.is_postgres = True
        db.database_url = "postgresql://user:pass@localhost:5432/testdb"

        # 1. Placeholders
        db._execute(mock_conn, "SELECT * FROM problems WHERE platform = ? AND id = ?", ("leetcode", 1))
        mock_cursor.execute.assert_called_with("SELECT * FROM problems WHERE platform = %s AND id = %s", ["leetcode", 1])

        # 2. DDL AutoIncrement
        db._execute(mock_conn, "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, score REAL)")
        mock_cursor.execute.assert_called_with("CREATE TABLE t (id SERIAL PRIMARY KEY, score DOUBLE PRECISION)", [])

        # 3. Transaction control
        db._execute(mock_conn, "COMMIT")
        mock_conn.commit.assert_called_once()

@pytest.mark.asyncio
class TestOfflineDryRunEndToEnd:
    async def test_offline_dry_run_success(self, temp_db, tmp_path):
        solutions_dir = tmp_path / "solutions"
        with patch("bot.SOLUTIONS_DIR", solutions_dir):
            success = await execute_batch(
                platform="hackerearth",
                count=1,
                difficulty="easy",
                mode="submit",
                headless=True,
                dry_run=True,
                offline_mode=True
            )
            assert success is True
            assert SolvedRegistry.is_solved("hackerearth", "zoos-04664a78")
