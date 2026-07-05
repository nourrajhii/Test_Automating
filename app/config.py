import os

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# Modele texte pour l'analyse HTML, la generation de scenarios et de scripts
TEXT_MODEL = os.getenv("TEXT_MODEL", "llama3.2:3b")

REPORTS_DIR = "reports"
UPLOAD_DIR  = "uploads"

# Timeouts (en secondes)
TEXT_TIMEOUT      = int(os.getenv("TEXT_TIMEOUT",      "360"))
EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", "120"))

# Nombre max de scenarios a generer par interface
MAX_SCENARIOS = int(os.getenv("MAX_SCENARIOS", "6"))