"""Centralized application workflow.

Single source of truth for the application state machine: statuses,
transitions, per-step permissions, editability rules, and the transition
function itself. The Application model, admin, forms and KPIs all delegate
here so the workflow logic lives in exactly one place.

This module deliberately does NOT import applications.models at module level
(no circular imports): it operates on an Application instance through its
attributes and its ``transitions`` related manager.
"""

from django.core.exceptions import PermissionDenied
from django.db import models
from django.utils import timezone

from .business_time import working_seconds_between


class ApplicationStatus(models.TextChoices):
    DRAFT = "draft", "مسودة"
    SUBMITTED = "submitted", "مؤكد"
    UNDER_PROCESSING = "under_processing", "قيد المعالجة"
    COMMITTEE_RECOMMENDATION = "committee_recommendation", "توصية اللجنة"
    UNDERSECRETARY_RECOMMENDATION = "undersecretary_recommendation", "توصية وكيل الوزارة"
    APPROVED = "approved", "معتمد"
    REJECTED = "rejected", "مرفوض"


class Recommendation(models.TextChoices):
    RECOMMENDED = "recommended", "موصى به"
    NOT_RECOMMENDED = "not_recommended", "غير موصى به"


# Allowed transitions: current -> {next}.
TRANSITIONS = {
    ApplicationStatus.DRAFT: {ApplicationStatus.SUBMITTED},
    ApplicationStatus.SUBMITTED: {ApplicationStatus.UNDER_PROCESSING},
    ApplicationStatus.UNDER_PROCESSING: {ApplicationStatus.COMMITTEE_RECOMMENDATION},
    ApplicationStatus.COMMITTEE_RECOMMENDATION: {
        ApplicationStatus.UNDERSECRETARY_RECOMMENDATION
    },
    ApplicationStatus.UNDERSECRETARY_RECOMMENDATION: {
        ApplicationStatus.APPROVED,
        ApplicationStatus.REJECTED,
    },
    ApplicationStatus.REJECTED: {ApplicationStatus.DRAFT},
}

# Custom workflow permissions declared on Application.Meta.permissions.
WORKFLOW_PERMISSIONS = [
    ("can_submit", "يمكنه تأكيد الطلبات"),
    ("can_review", "يمكنه مراجعة الطلبات (قيد المعالجة)"),
    ("can_committee_recommend", "يمكنه تقديم توصية اللجنة"),
    ("can_undersecretary_recommend", "يمكنه تقديم توصية وكيل الوزارة"),
    ("can_approve", "يمكنه اعتماد الطلبات"),
    ("can_reject", "يمكنه رفض الطلبات"),
]

WORKFLOW_PERMISSION_CODENAMES = [codename for codename, _label in WORKFLOW_PERMISSIONS]

# Statuses that count as "in review" for the KPI backlog.
REVIEW_STATUSES = (
    ApplicationStatus.UNDER_PROCESSING,
    ApplicationStatus.COMMITTEE_RECOMMENDATION,
    ApplicationStatus.UNDERSECRETARY_RECOMMENDATION,
)

# Statuses on which the "تأكيد الطلب" button is offered.
SUBMITTABLE_STATUSES = (ApplicationStatus.DRAFT, ApplicationStatus.REJECTED)

# The final decision (the minister's decision) states.
FINAL_DECISION_STATUSES = (ApplicationStatus.APPROVED, ApplicationStatus.REJECTED)

# Workflow data fields entered at the recommendation / final-decision stages.
ENTRY_FIELD_NAMES = (
    "committee_recommendation",
    "committee_recommendation_notes",
    "undersecretary_recommendation",
    "undersecretary_recommendation_notes",
    "minister_decision",
)


def is_editable(status):
    """Application data may only be changed while the application is a draft."""
    return status == ApplicationStatus.DRAFT


def is_readonly(status):
    """Anything past the draft is frozen (status moves only)."""
    return not is_editable(status)


def _permission_for(new_status):
    return {
        ApplicationStatus.SUBMITTED: "applications.can_submit",
        ApplicationStatus.UNDER_PROCESSING: "applications.can_review",
        ApplicationStatus.COMMITTEE_RECOMMENDATION: "applications.can_committee_recommend",
        ApplicationStatus.UNDERSECRETARY_RECOMMENDATION: (
            "applications.can_undersecretary_recommend"
        ),
        ApplicationStatus.APPROVED: "applications.can_approve",
        ApplicationStatus.REJECTED: "applications.can_reject",
    }.get(new_status)


def allowed_next_statuses_for(status, user):
    """Statuses `user` may move an application currently in `status` to
    (excluding the current one)."""
    allowed = TRANSITIONS.get(status, set())
    if user is not None and user.is_authenticated and user.is_superuser:
        return allowed
    if user is not None and user.is_authenticated:
        result = set()
        for target in allowed:
            perm = _permission_for(target)
            if perm is None or user.has_perm(perm):
                result.add(target)
        return result
    return {target for target in allowed if target == ApplicationStatus.DRAFT}


def can_transition(status, new_status, user):
    return new_status in allowed_next_statuses_for(status, user)


def entry_fields_for(status, user):
    """Workflow data fields that must be entered now, based on the current
    status and the user's allowed moves. Anything else is hidden (shown only
    when it should be entered)."""
    allowed = allowed_next_statuses_for(status, user)
    fields = []
    if (
        status == ApplicationStatus.UNDER_PROCESSING
        and ApplicationStatus.COMMITTEE_RECOMMENDATION in allowed
    ):
        fields += ["committee_recommendation", "committee_recommendation_notes"]
    if (
        status == ApplicationStatus.COMMITTEE_RECOMMENDATION
        and ApplicationStatus.UNDERSECRETARY_RECOMMENDATION in allowed
    ):
        fields += [
            "undersecretary_recommendation",
            "undersecretary_recommendation_notes",
        ]
    if (
        status == ApplicationStatus.UNDERSECRETARY_RECOMMENDATION
        and set(FINAL_DECISION_STATUSES) & allowed
    ):
        fields += ["minister_decision"]
    return fields


def _clear_recommendations(app):
    """Reset the decision cycle (used when a rejected application is moved
    back to DRAFT for resubmission)."""
    for prefix in ("committee", "undersecretary"):
        setattr(app, f"{prefix}_recommendation", None)
        setattr(app, f"{prefix}_recommendation_notes", "")
        setattr(app, f"{prefix}_recommended_by", None)
        setattr(app, f"{prefix}_recommended_at", None)
    app.minister_decision = ""


def transition_application(
    app, new_status, user, recommendation=None, notes="", minister_decision=""
):
    """Move an Application to new_status, enforcing the role-based workflow.

    Raises PermissionDenied on an invalid transition, a missing permission, or
    a missing mandatory entry (recommendation decision / minister's decision)
    when entering the corresponding stage. Records the transition-history row
    (with working-hours duration) and, on approval, logs one historical event
    on the agreement.
    """
    if new_status == app.status:
        return
    if new_status not in TRANSITIONS.get(app.status, set()):
        raise PermissionDenied(
            f"لا يمكن الانتقال من «{app.get_status_display()}» إلى "
            f"«{dict(ApplicationStatus.choices).get(new_status, new_status)}»."
        )
    if user is not None and not user.is_superuser:
        perm = _permission_for(new_status)
        if perm and not user.has_perm(perm):
            raise PermissionDenied(f"ليست لديك صلاحية تنفيذ هذا الإجراء ({perm}).")
    if new_status == ApplicationStatus.COMMITTEE_RECOMMENDATION and not recommendation:
        raise PermissionDenied("يجب تحديد توصية اللجنة (موصى به / غير موصى به).")
    if (
        new_status == ApplicationStatus.UNDERSECRETARY_RECOMMENDATION
        and not recommendation
    ):
        raise PermissionDenied("يجب تحديد توصية وكيل الوزارة (موصى به / غير موصى به).")
    if new_status in FINAL_DECISION_STATUSES and not minister_decision:
        raise PermissionDenied("يجب كتابة نص قرار الوزير.")

    old_status = app.status
    app.status = new_status
    now = timezone.now()
    if new_status == ApplicationStatus.SUBMITTED:
        app.submitted_at = now
        app.submitted_by = user
    elif new_status in FINAL_DECISION_STATUSES:
        app.reviewed_at = now
        app.reviewed_by = user
        app.minister_decision = minister_decision
    elif new_status == ApplicationStatus.COMMITTEE_RECOMMENDATION:
        app.committee_recommendation = recommendation
        app.committee_recommendation_notes = notes
        app.committee_recommended_by = user
        app.committee_recommended_at = now
    elif new_status == ApplicationStatus.UNDERSECRETARY_RECOMMENDATION:
        app.undersecretary_recommendation = recommendation
        app.undersecretary_recommendation_notes = notes
        app.undersecretary_recommended_by = user
        app.undersecretary_recommended_at = now
    elif new_status == ApplicationStatus.DRAFT:
        _clear_recommendations(app)

    if new_status == ApplicationStatus.APPROVED:
        log_approval_event(app)

    previous = app.transitions.order_by("-timestamp", "-id").first()
    duration_seconds = None
    if previous is not None and previous.timestamp is not None:
        duration_seconds = working_seconds_between(previous.timestamp, now)
    app.transitions.create(
        application=app,
        from_status=old_status,
        to_status=new_status,
        user=user,
        duration_seconds=duration_seconds,
    )


def anchor_creation(app):
    """Anchor the timeline: first transition entry (None -> draft) at creation."""
    app.transitions.create(application=app, from_status=None, to_status=app.status)


# ------------------------------------------------------------------ approval -> event


def _event_model_map():
    from companies.models import FinancialEvent, LegalEvent, TechnicalEvent

    return {
        "legal": LegalEvent,
        "financial": FinancialEvent,
        "technical": TechnicalEvent,
    }


def log_approval_event(app):
    """Record one historical event (legal/financial/technical) for an approved
    application. Idempotent: skips when an event is already linked to it."""
    at = app.app_type
    if not at.event_category or not at.event_label:
        return
    model_cls = _event_model_map().get(at.event_category)
    if model_cls is None:
        return
    if model_cls.objects.filter(application=app).exists():
        return
    label = at.event_label
    # Fall back to "other" when the label is not a valid choice value.
    valid_values = [v for v, _ in model_cls._meta.get_field("event_type").choices]
    if label not in valid_values:
        label = "other"
    description = f"طلب: {at.arabic_name}"
    if app.notes:
        description += f" — {app.notes}"
    model_cls.objects.create(
        agreement=app.agreement,
        event_type=label,
        date=timezone.localdate(),
        description=description,
        application=app,
    )
