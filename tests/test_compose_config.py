import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class ComposeConfigurationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("docker"), "Docker Compose is not available")
    def test_unfilled_example_rejects_empty_database_password(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(root / "docker-compose.yaml", tmp)
            shutil.copy(root / ".env.example", Path(tmp, ".env"))
            env = os.environ.copy()
            env.pop("DB_PASSWORD", None)
            result = subprocess.run(
                ["docker", "compose", "config", "--format", "json"],
                cwd=tmp, env=env, capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("DB_PASSWORD", result.stderr)

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose is not available")
    def test_shell_database_overrides_match_in_both_services(self):
        compose = Path(__file__).resolve().parent.parent / "docker-compose.yaml"
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(compose, tmp)
            Path(tmp, ".env").write_text(
                "SECRET_KEY=example-key\nDB_NAME=file-db\nDB_USER=file-user\n"
                "DB_PASSWORD=file-password\nDB_HOST=db\nDB_PORT=5432\n"
            )
            env = os.environ.copy()
            env.update(
                DB_NAME="override-db", DB_USER="override-user",
                DB_PASSWORD="override-password", DB_HOST="override-host",
                DB_PORT="1234",
            )
            result = subprocess.run(
                ["docker", "compose", "config", "--format", "json"],
                cwd=tmp, env=env, capture_output=True, text=True, check=True,
            )
            services = json.loads(result.stdout)["services"]
            web = services["web"]["environment"]
            db = services["db"]["environment"]
            for name in ("NAME", "USER", "PASSWORD"):
                self.assertEqual(web[f"DB_{name}"], env[f"DB_{name}"])
                self.assertEqual(db[f"POSTGRES_{'DB' if name == 'NAME' else name}"], env[f"DB_{name}"])
            self.assertEqual(web["DB_HOST"], env["DB_HOST"])
            self.assertEqual(web["DB_PORT"], env["DB_PORT"])
