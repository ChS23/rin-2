from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
import os

from ._utils import get_env


@dataclass
class VKApiSettings:
    token: str = field(default_factory=get_env("VK_TOKEN", ""))
    group_id: int = field(default_factory=get_env("VK_GROUP_ID", "0"))
    api_version: str = field(default_factory=get_env("VK_API_VERSION", "5.131"))


@dataclass
class Settings:
    vk: VKApiSettings = field(default_factory=VKApiSettings)

    @classmethod
    def from_env(cls, dotenv_filename: str = ".env") -> "Settings":

        env_file = Path(f"{os.curdir}/{dotenv_filename}")
        if env_file.is_file():
            from dotenv import load_dotenv

            load_dotenv(env_file, override=True)
        return Settings()


@lru_cache(maxsize=1, typed=True)
def get_settings() -> Settings:
    return Settings.from_env()
