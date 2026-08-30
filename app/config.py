"""
Environment configuration.

Responsibility:
- Load values from .env (DB_SERVER, DB_DATABASE, DB_USER, DB_PASSWORD,
  DB_DRIVER, GEMINI_API_KEY).
- Build the pyodbc SQL Server connection string.
- Expose a single settings object for the rest of the app to import.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    db_server: str
    db_database: str
    db_user: str
    db_password: str
    db_driver: str = "ODBC Driver 17 for SQL Server"
    gemini_api_key: str = ""

    @property
    def connection_string(self) -> str:
        return (
            f"DRIVER={{{self.db_driver}}};"
            f"SERVER={self.db_server};"
            f"DATABASE={self.db_database};"
            f"UID={self.db_user};"
            f"PWD={self.db_password};"
        )


settings = Settings()
