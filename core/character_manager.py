import json
from typing import List, Dict, Optional
from pathlib import Path

class CharacterManager:
    """キャラクター管理クラス（最大15キャラ対応）"""
    
    def __init__(self, config_path: str = "configs/characters.json"):
        self.config_path = Path(config_path)
        self.characters = {}
        self.load_characters()
    
    def load_characters(self):
        """JSONからキャラクターを読み込み"""
        if self.config_path.exists():
            with open(self.config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.characters = data.get("characters", {})
    
    def get_character(self, character_id: str) -> Optional[Dict]:
        """IDからキャラクターを取得"""
        return self.characters.get(character_id)
    
    def get_enabled_characters(self) -> List[Dict]:
        """有効なキャラクター一覧を取得"""
        return [
            char for char in self.characters.values()
            if char.get("enabled", False)
        ]
    
    def get_character_count(self) -> int:
        """現在のキャラクター数"""
        return len(self.characters)