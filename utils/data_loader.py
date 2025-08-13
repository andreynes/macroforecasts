# utils/data_loader.py
import os
import pandas as pd
from typing import Optional

class DataCache:
    def __init__(self, path: str, date_column: str, excel_sheet: Optional[str] = None):
        self.path = path
        self.date_column = date_column
        # Пустые строки считаем как «не задано»
        self.excel_sheet = excel_sheet.strip() if isinstance(excel_sheet, str) else excel_sheet
        if self.excel_sheet == "":
            self.excel_sheet = None

        self._last_mtime = None
        self._df = None

    def _load(self) -> pd.DataFrame:
        ext = os.path.splitext(self.path)[1].lower()
        if ext == ".xlsx":
            # Если лист не задан — не передаём sheet_name (по умолчанию 0 -> DataFrame)
            if self.excel_sheet is None:
                df = pd.read_excel(self.path)  # вернёт DataFrame первого листа
            else:
                df = pd.read_excel(self.path, sheet_name=self.excel_sheet)
        elif ext == ".csv":
            df = pd.read_csv(self.path)
        else:
            raise ValueError(f"Unsupported data file format: {ext}")

        # На случай, если кто-то всё же вернул dict (старые версии pandas)
        if isinstance(df, dict):
            # Берём первый лист по порядку
            first_key = next(iter(df))
            df = df[first_key]

        if self.date_column in df.columns:
            try:
                df[self.date_column] = pd.to_datetime(df[self.date_column])
            except Exception:
                pass
        return df

    def get_df(self) -> pd.DataFrame:
        if not os.path.exists(self.path):
            raise FileNotFoundError(f"Data file not found: {self.path}")

        mtime = os.path.getmtime(self.path)
        if self._df is None or self._last_mtime is None or mtime != self._last_mtime:
            self._df = self._load()
            self._last_mtime = mtime
        return self._df.copy()
