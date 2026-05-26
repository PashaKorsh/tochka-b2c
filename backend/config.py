"""
Application configuration — loaded from environment variables.

B2B connection:
  B2B_BASE_URL     — base URL of the B2B service (default: http://localhost:8000)
  B2C_TO_B2B_KEY   — service key for X-Service-Key header (default: dev-service-key)
"""
import os

B2B_BASE_URL: str = os.getenv("B2B_BASE_URL", "http://localhost:8000")
B2C_TO_B2B_KEY: str = os.getenv("B2C_TO_B2B_KEY", "dev-service-key")
