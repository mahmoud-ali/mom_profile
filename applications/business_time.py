"""Working-time calculations for stage durations.

Durations between transitions count only working hours on working days.
The applicable hours/days come from WorkingHoursSchedule (per date range, so
seasonal changes are supported); dates without a covering schedule fall back
to the DEFAULT_* constants below.
"""
from datetime import datetime, time as dtime, timedelta

from django.utils import timezone

# Fallback defaults (used when no WorkingHoursSchedule covers a date).
DEFAULT_WORKING_WEEKDAYS = [0, 1, 2, 3, 6]  # Sunday-Thursday
DEFAULT_WORK_START = dtime(8, 0)
DEFAULT_WORK_END = dtime(16, 0)


def _localize(dt):
    return dt.astimezone(timezone.get_current_timezone())


def _load_schedules():
    from .models import WorkingHoursSchedule

    return list(WorkingHoursSchedule.objects.order_by("-start_date"))


def _schedule_for_day(schedules, day):
    for schedule in schedules:
        if schedule.start_date <= day and (
            schedule.end_date is None or schedule.end_date >= day
        ):
            return schedule
    return None


def working_seconds_between(start, end):
    """Elapsed working seconds between two datetimes (0 if end <= start).

    Each day uses the working hours/days of the schedule covering it.
    """
    if end <= start:
        return 0
    start = _localize(start)
    end = _localize(end)
    schedules = _load_schedules()
    total = 0.0
    day = start.date()
    while day <= end.date():
        schedule = _schedule_for_day(schedules, day)
        if schedule is not None:
            weekdays = schedule.working_weekdays or DEFAULT_WORKING_WEEKDAYS
            work_start = schedule.work_start
            work_end = schedule.work_end
        else:
            weekdays = DEFAULT_WORKING_WEEKDAYS
            work_start = DEFAULT_WORK_START
            work_end = DEFAULT_WORK_END
        if day.weekday() in weekdays:
            day_start = datetime.combine(day, work_start, tzinfo=start.tzinfo)
            day_end = datetime.combine(day, work_end, tzinfo=start.tzinfo)
            seg_start = max(start, day_start)
            seg_end = min(end, day_end)
            if seg_end > seg_start:
                total += (seg_end - seg_start).total_seconds()
        day += timedelta(days=1)
    return int(total)
