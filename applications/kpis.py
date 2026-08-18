import statistics
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone

from .models import Application, ApplicationTransition
from .workflow import ApplicationStatus, REVIEW_STATUSES

STATUS_LABELS = dict(ApplicationStatus.choices)


def format_duration(seconds):
    """Human-readable duration: 'X يوم Y ساعة' (or minutes/seconds)."""
    if not seconds:
        return "—"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} يوم {hours} ساعة"
    if hours:
        return f"{hours} ساعة {minutes} دقيقة"
    if minutes:
        return f"{minutes} دقيقة"
    return f"{seconds} ثانية"


def _summarize(values):
    if not values:
        return None
    seconds = [v.total_seconds() for v in values]
    return {
        "count": len(values),
        "avg_days": round(sum(seconds) / len(seconds) / 86400, 1),
        "median_days": round(statistics.median(seconds) / 86400, 1),
        "max_days": round(max(seconds) / 86400, 1),
    }


def compute_kpis(days=None):
    """Application transition KPIs: who moved applications, when, and how long
    each stage took."""
    apps = Application.objects.all()
    if days:
        cutoff = timezone.now() - timedelta(days=days)
        apps = apps.filter(created_at__gte=cutoff)

    transitions = list(
        ApplicationTransition.objects.filter(application__in=apps)
        .order_by("application_id", "timestamp", "id")
        .select_related("user")
    )

    # Group consecutive transitions per application for stage durations.
    by_app = {}
    for row in transitions:
        by_app.setdefault(row.application_id, []).append(row)

    stage = {}
    per_user = {}
    for app_id, rows in by_app.items():
        prev = None
        for row in rows:
            username = row.user.username if row.user else "—"
            info = per_user.setdefault(username, {"count": 0, "decided": 0, "decision_seconds": []})
            info["count"] += 1
            if row.to_status in (ApplicationStatus.APPROVED, ApplicationStatus.REJECTED):
                info["decided"] += 1
            if prev is not None and prev.timestamp <= row.timestamp:
                key = (prev.to_status, row.to_status)
                if row.duration_seconds is not None:
                    duration = timedelta(seconds=row.duration_seconds)
                else:
                    from .business_time import working_seconds_between

                    duration = timedelta(
                        seconds=working_seconds_between(prev.timestamp, row.timestamp)
                    )
                stage.setdefault(key, []).append(duration)
                if row.to_status in (ApplicationStatus.APPROVED, ApplicationStatus.REJECTED):
                    info["decision_seconds"].append(duration.total_seconds())
            prev = row

    stage_durations = {
        (
            "الإنشاء" if f is None else STATUS_LABELS.get(f, f),
            STATUS_LABELS.get(t, t),
        ): _summarize(values)
        for (f, t), values in sorted(stage.items())
    }

    per_user_summary = {
        user: {
            "count": info["count"],
            "decided": info["decided"],
            "avg_decision_days": (
                round(sum(info["decision_seconds"]) / len(info["decision_seconds"]) / 86400, 1)
                if info["decision_seconds"]
                else None
            ),
        }
        for user, info in sorted(per_user.items())
    }

    monthly = {}
    for row in transitions:
        month = row.timestamp.strftime("%Y-%m")
        monthly[month] = monthly.get(month, 0) + 1

    decided_qs = Application.objects.filter(
        status__in=(ApplicationStatus.APPROVED, ApplicationStatus.REJECTED)
    )
    approved = decided_qs.filter(status=ApplicationStatus.APPROVED).count()
    decided = decided_qs.count()
    backlog_qs = Application.objects.filter(status__in=REVIEW_STATUSES)
    oldest_backlog = None
    for app in backlog_qs:
        last = (
            app.transitions.filter(to_status__in=REVIEW_STATUSES)
            .order_by("-timestamp")
            .first()
        )
        if last:
            age = (timezone.now() - last.timestamp).days
            if oldest_backlog is None or age > oldest_backlog[1]:
                oldest_backlog = (app, age)

    return {
        "total": apps.count(),
        "by_status": {
            STATUS_LABELS.get(value, value): count
            for value, count in apps.values_list("status").annotate(n=Count("id")).order_by()
        },
        "decided": decided,
        "approved": approved,
        "approval_rate": round(approved / decided * 100, 1) if decided else 0,
        "backlog_count": backlog_qs.count(),
        "backlog_oldest_days": oldest_backlog[1] if oldest_backlog else None,
        "stage_durations": stage_durations,
        "per_user": per_user_summary,
        "monthly": dict(sorted(monthly.items())),
    }
