import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
XLSX = DATA / "ad_nprm.xlsx"
DB = DATA / "adc.db"
FLEET = DATA / "fleet.csv"
RULES_DIR = DATA / "rules"

MODEL_EXTRACT = os.getenv("ADC_MODEL_EXTRACT", "claude-sonnet-5-5")
MODEL_PARSE = os.getenv("ADC_MODEL_PARSE", "claude-haiku-5-5")
MODEL_ANSWER = os.getenv("ADC_MODEL_ANSWER", "claude-sonnet-5-5")

BM25_MIN_SCORE = float(os.getenv("ADC_BM25_MIN", "1.0"))
TOP_K = 6
