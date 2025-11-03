"""
Simple Persistent Mapping Service
"""
import json
import os
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("simple_persistent_mapping")

class SimplePersistentMappingService:
    """Simple persistent mapping service for product mappings"""
    
    def __init__(self, mapping_file: str = "product_mappings.json"):
        self.mapping_file = mapping_file
        self.mappings = {}
        self.load_mappings()
    
    def load_mappings(self):
        """Load mappings from file"""
        try:
            if os.path.exists(self.mapping_file):
                with open(self.mapping_file, 'r') as f:
                    self.mappings = json.load(f)
                logger.info(f"Loaded {len(self.mappings)} mappings from {self.mapping_file}")
            else:
                logger.info(f"Mapping file {self.mapping_file} not found, starting with empty mappings")
        except Exception as e:
            logger.error(f"Error loading mappings: {e}")
            self.mappings = {}
    
    def save_mappings(self):
        """Save mappings to file"""
        try:
            with open(self.mapping_file, 'w') as f:
                json.dump(self.mappings, f, indent=2)
            logger.info(f"Saved {len(self.mappings)} mappings to {self.mapping_file}")
        except Exception as e:
            logger.error(f"Error saving mappings: {e}")
    
    def get_mapping(self, key: str) -> Optional[Dict[str, Any]]:
        """Get mapping by key"""
        return self.mappings.get(key)
    
    def set_mapping(self, key: str, value: Dict[str, Any]):
        """Set mapping for key"""
        self.mappings[key] = value
        self.save_mappings()
    
    def delete_mapping(self, key: str):
        """Delete mapping by key"""
        if key in self.mappings:
            del self.mappings[key]
            self.save_mappings()

# Global service instance
mapping_service = SimplePersistentMappingService()

async def initialize_simple_mapping_service():
    """Initialize the simple mapping service"""
    logger.info("Simple mapping service initialized")
    return mapping_service
