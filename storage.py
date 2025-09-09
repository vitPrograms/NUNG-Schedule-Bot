import json
import os
from typing import Dict, Any

SETTINGS_FILE = 'user_settings.json'

def load_settings() -> Dict[str, Any]:
    """Loads all user settings from the JSON file."""
    if not os.path.exists(SETTINGS_FILE):
        return {}
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def save_settings(settings: Dict[str, Any]) -> None:
    """Saves all user settings to the JSON file."""
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=4)
    except IOError as e:
        print(f"Error saving settings: {e}")

def get_user_setting(user_id: int, key: str, default: Any = None) -> Any:
    """Gets a specific setting for a user."""
    settings = load_settings()
    return settings.get(str(user_id), {}).get(key, default)

def set_user_setting(user_id: int, key: str, value: Any) -> None:
    """Sets a specific setting for a user."""
    settings = load_settings()
    user_id_str = str(user_id)
    if user_id_str not in settings:
        settings[user_id_str] = {}
    settings[user_id_str][key] = value
    save_settings(settings)
