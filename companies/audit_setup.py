import contextlib

from auditlog.models import LogEntry
from auditlog.registry import auditlog
from django.utils.html import format_html

AUDIT_ACTION_LABELS = {0: "إنشاء", 1: "تعديل", 2: "حذف"}


def audit_history_html(obj, limit=20):
    """Render a compact history table for an object's LogEntry rows."""
    if obj is None or not obj.pk:
        return "—"
    entries = LogEntry.objects.get_for_object(obj).select_related("actor")[:limit]
    if not entries:
        return "—"
    rows = []
    for entry in entries:
        actor = (
            (entry.actor.get_full_name() or entry.actor.username) if entry.actor else "—"
        )
        changes = entry.changes or {}
        if isinstance(changes, dict):
            bits = []
            for field, change in list(changes.items())[:4]:
                if isinstance(change, (list, tuple)) and len(change) == 2:
                    bits.append(f"{field}: {change[0] or '—'} ← {change[1] or '—'}")
                else:
                    bits.append(f"{field}: {change}")
            change_text = "؛ ".join(bits) or "—"
        else:
            change_text = str(changes)
        rows.append(
            f"<tr><td>{entry.timestamp:%Y-%m-%d %H:%M}</td>"
            f"<td>{actor}</td>"
            f"<td>{AUDIT_ACTION_LABELS.get(entry.action, entry.action)}</td>"
            f"<td>{change_text}</td></tr>"
        )
    body = "".join(rows)
    return format_html(
        '<table class="audit-history" style="width:100%;border-collapse:collapse">'
        "<thead><tr><th>الوقت</th><th>المستخدم</th><th>الإجراء</th><th>التغييرات</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )

# (model, register kwargs) — the models this project audits.
_AUDIT_REGISTRY = []


def setup_audit():
    """Register the audited models. Called once from companies.apps ready()."""
    from applications import models as am
    from companies import models as cm

    _register(cm.Company, m2m_fields=["nationalities"])
    _register(cm.Agreement, m2m_fields=["minerals"])
    _register(cm.FinancialPosition)
    _register(cm.TechnicalPosition)
    _register(cm.LegalEvent)
    _register(cm.FinancialEvent)
    _register(cm.TechnicalEvent)
    _register(am.Application)
    _register(am.ApplicationType)


def _register(model, **kwargs):
    _AUDIT_REGISTRY.append((model, kwargs))
    auditlog.register(model, **kwargs)


@contextlib.contextmanager
def suppress_auditlog():
    """Temporarily disable audit logging (e.g. for bulk backfills).

    Re-registers the same models (with their m2m_fields) afterwards.
    """
    for model, _kwargs in _AUDIT_REGISTRY:
        auditlog.unregister(model)
    try:
        yield
    finally:
        for model, kwargs in _AUDIT_REGISTRY:
            auditlog.register(model, **kwargs)


def registered_models():
    return [model for model, _kwargs in _AUDIT_REGISTRY]
