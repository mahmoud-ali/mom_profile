from django.core.management.base import BaseCommand

from applications.kpis import compute_kpis


class Command(BaseCommand):
    help = "Prints application transition KPIs (who moved applications, when, how long each stage took)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=None, help="Only consider applications created in the last N days.")

    def handle(self, *args, **options):
        kpis = compute_kpis(days=options["days"])
        window = f"last {options['days']} days" if options["days"] else "all time"
        self.stdout.write(f"=== Application transition KPIs ({window}) ===")
        self.stdout.write(f"Total applications: {kpis['total']}")
        self.stdout.write(f"By status: {kpis['by_status']}")
        self.stdout.write(
            f"Decided: {kpis['decided']} (approved {kpis['approved']}) — "
            f"approval rate {kpis['approval_rate']}%"
        )
        self.stdout.write(
            f"Backlog (قيد المعالجة): {kpis['backlog_count']} — "
            f"oldest {kpis['backlog_oldest_days'] or 0} days"
        )
        self.stdout.write("Stage durations (from ← to): count, avg_days, median_days, max_days")
        for pair, s in kpis["stage_durations"].items():
            self.stdout.write(
                f"  {pair[0]} ← {pair[1]}: {s['count']}, {s['avg_days']}, {s['median_days']}, {s['max_days']}"
            )
        self.stdout.write("Per user: count, decided, avg_decision_days")
        for user, info in kpis["per_user"].items():
            self.stdout.write(f"  {user}: {info['count']}, {info['decided']}, {info['avg_decision_days']}")
        self.stdout.write(f"Monthly transitions: {kpis['monthly']}")
