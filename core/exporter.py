"""
exporter.py
~~~~~~~~~~~
Exports batch metadata results to CSV files formatted for specific stock markets.
"""
import csv
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from core.stock_markets import MarketRules

logger = logging.getLogger(__name__)


class MetadataExporter:
    """Writes per-market CSV export files."""

    def export_csv(
        self,
        records: List[Dict],
        market: MarketRules,
        output_path: str,
    ) -> bool:
        """
        Write records to a market-specific CSV.

        Each record dict must have keys: filename, title, description, keywords (list).
        Returns True on success.
        """
        try:
            with open(output_path, "w", newline="", encoding="utf-8-sig") as fh:
                # utf-8-sig writes BOM so Excel opens it correctly
                writer = csv.writer(fh)
                writer.writerow(market.csv_columns)

                for rec in records:
                    row = self._build_row(rec, market)
                    writer.writerow(row)

            logger.info("Exported %d records to %s", len(records), output_path)
            return True

        except OSError as exc:
            logger.error("Export failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_row(self, rec: Dict, market: MarketRules) -> List[str]:
        """Map a metadata record to the correct column order for this market."""
        filename    = Path(rec.get("filename", "")).name
        title       = rec.get("title", "")
        description = rec.get("description", "")
        keywords_list: List[str] = rec.get("keywords", [])
        keywords_str = ", ".join(keywords_list)

        row_map = {
            "Filename":      filename,
            "Title":         title,
            "Description":   description,
            "Keywords":      keywords_str,
            # Adobe-specific
            "Category":      rec.get("category", ""),
            "Editorial":     rec.get("editorial", "No"),
            # Shutterstock-specific (uses Description as title field)
            "Categories":    rec.get("categories", ""),
            "Mature Content": rec.get("mature_content", "No"),
            # Getty-specific
            "Country":       rec.get("country", ""),
            "Date Taken":    rec.get("date_taken", ""),
        }

        return [row_map.get(col, "") for col in market.csv_columns]