"""What a source mapper did: tables written, rows, columns it could not map, and every assumption it made."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class MappingReport:
    source: str
    tables: Dict[str, int] = field(default_factory=dict)        # canonical table -> rows written
    unmapped_columns: Dict[str, List[str]] = field(default_factory=dict)   # input file -> columns not used
    assumptions: List[str] = field(default_factory=list)       # each judgment the mapping made, in plain words
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {"source": self.source, "tables": self.tables, "unmapped_columns": self.unmapped_columns,
                "assumptions": self.assumptions, "warnings": self.warnings}
