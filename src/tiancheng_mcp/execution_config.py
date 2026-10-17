"""Local capture policy. No caller-controlled paths or unlimited budgets."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class CaptureSettings:
    mode: str = 'summary'
    storage_root: str | None = None
    summary_retention_days: int = 30
    summary_max_mib: int = 256
    detail_retention_days: int = 7
    detail_max_mib: int = 128
    detail_window_minutes: int = 30

    @classmethod
    def parse(cls, document: object = None) -> 'CaptureSettings':
        names = {'mode': 'mode', 'storageRoot': 'storage_root',
                 'summaryRetentionDays': 'summary_retention_days', 'summaryMaxMiB': 'summary_max_mib',
                 'detailRetentionDays': 'detail_retention_days', 'detailMaxMiB': 'detail_max_mib',
                 'detailWindowMinutes': 'detail_window_minutes'}
        if document is None:
            document = {}
        if not isinstance(document, dict) or set(document) - names.keys():
            raise ValueError('Invalid observability fields')
        settings = cls(**{names[k]: v for k, v in document.items()})
        if settings.mode not in ('off', 'summary', 'detail'):
            raise ValueError('Capture mode must be off/summary/detail')
        for name, low, high in (('summary_retention_days', 1, 365), ('summary_max_mib', 16, 2048),
                                ('detail_retention_days', 1, 30), ('detail_max_mib', 16, 1024),
                                ('detail_window_minutes', 1, 480)):
            value = getattr(settings, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError('Capture budget is outside its supported range')
        if settings.detail_retention_days > settings.summary_retention_days:
            raise ValueError('Detail retention cannot exceed summary retention')
        if settings.storage_root is not None:
            if (not isinstance(settings.storage_root, str) or not settings.storage_root
                    or not Path(settings.storage_root).is_absolute() or '\0' in settings.storage_root):
                raise ValueError('Capture storageRoot must be an absolute local path')
        return settings

    def document(self) -> dict:
        names = ('mode', 'storageRoot', 'summaryRetentionDays', 'summaryMaxMiB',
                 'detailRetentionDays', 'detailMaxMiB', 'detailWindowMinutes')
        return {k: v for k, v in zip(names, asdict(self).values()) if v is not None}

    def root(self, project_root: Path) -> Path:
        if self.storage_root is not None:
            return Path(self.storage_root)
        if os.name == 'nt':
            return project_root / 'state' / 'execution-records'
        home = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state')
        if not home.is_absolute():
            raise ValueError('State home must be absolute')
        return home / 'tiancheng-mcp' / 'execution-records'
