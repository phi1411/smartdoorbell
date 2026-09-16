from smart_doorbell.config import AppConfig
from smart_doorbell.database import Database
from smart_doorbell.repositories.setting_repository import SettingRepository


if __name__ == "__main__":
    config = AppConfig.from_env()
    config.ensure_directories()
    database = Database(config.database_path)
    database.initialize()
    SettingRepository(database, config.defaults).seed_defaults()
    print(f"Initialized: {config.database_path}")

