import json
from pathlib import Path

from ram_gobbler_app.core.config_repository import ConfigRepository
from ram_gobbler_app.core.models import AppConfig, TargetConfig


def test_load_missing_creates_default(tmp_path):
    path = tmp_path / "config.json"
    repo = ConfigRepository(path=str(path), backup_dir=str(tmp_path / "backups"))
    config = repo.load()
    assert isinstance(config, AppConfig)
    assert config.targets == ()


def test_corrupt_file_creates_backup(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not: json}")
    backup_dir = tmp_path / "backups"
    repo = ConfigRepository(path=str(path), backup_dir=str(backup_dir))
    config = repo.load()
    assert config.targets == ()
    backups = list(backup_dir.glob("config-corrupt-*.json"))
    assert backups, "corrupt configuration should be backed up"


def test_save_and_mutate(tmp_path):
    path = tmp_path / "config.json"
    repo = ConfigRepository(path=str(path), backup_dir=str(tmp_path / "backups"))
    config = repo.load()
    target = TargetConfig(key="demo", match_by="name", uid="uid1", active=True)

    def add_target(conf: AppConfig) -> AppConfig:
        return conf.with_targets([target])

    updated = repo.mutate(add_target)
    assert updated.targets[0].key == "demo"

    loaded_again = repo.load()
    assert loaded_again.targets[0].uid == "uid1"
