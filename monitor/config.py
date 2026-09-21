"""配置加载。"""
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"


def load_config(path: Path | str | None = None) -> dict:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        example = BASE_DIR / "config.example.json"
        raise FileNotFoundError(
            f"找不到配置文件 {cfg_path}。请复制 config.example.json 为 config.json 并填写 QQ 机器人凭据。"
        )
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)
