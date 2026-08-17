from django.core.management.base import BaseCommand

from applications.models import WorkingHoursSchedule

from companies.models import Locality, Mineral, Nationality, State, NATIONALITY_NAMES

SUDAN_STATES = [
    ("الخرطوم", "Khartoum"),
    ("الجزيرة", "Al Jazirah"),
    ("سنار", "Sennar"),
    ("النيل الأبيض", "White Nile"),
    ("النيل الأزرق", "Blue Nile"),
    ("القضارف", "Gedaref"),
    ("كسلا", "Kassala"),
    ("البحر الأحمر", "Red Sea"),
    ("نهر النيل", "River Nile"),
    ("الشمالية", "Northern"),
    ("شمال كردفان", "North Kordofan"),
    ("جنوب كردفان", "South Kordofan"),
    ("غرب كردفان", "West Kordofan"),
    ("شمال دارفور", "North Darfur"),
    ("جنوب دارفور", "South Darfur"),
    ("غرب دارفور", "West Darfur"),
    ("شرق دارفور", "East Darfur"),
    ("وسط دارفور", "Central Darfur"),
]

MINERALS = [
    "ذهب",
    "فضة",
    "نحاس",
    "كروم",
    "حديد",
    "منجنيز",
    "رخام",
    "جرانيت",
    "جبس",
    "ملح",
]

# Example localities per state (state Arabic name -> locality names).
# The Locality lookup is managed via the admin; this is a starter set.
EXAMPLE_LOCALITIES = {
    "الخرطوم": ["الخرطوم", "أم درمان", "بحري", "شرق النيل"],
    "الجزيرة": ["ود مدني", "الحصاحيصا", "رفاعة"],
    "نهر النيل": ["الدامر", "عطبرة", "شندي"],
    "البحر الأحمر": ["بورتسودان", "سواكن", "حلايب"],
    "الشمالية": ["دنقلا", "وادي حلفا", "مروي"],
    "كسلا": ["كسلا", "أروما", "خشم القربة"],
}


class Command(BaseCommand):
    help = "Seeds reference data: Sudan states and common minerals."

    def handle(self, *args, **options):
        created_states = 0
        for ar, en in SUDAN_STATES:
            _, created = State.objects.get_or_create(name_ar=ar, defaults={"name_en": en})
            created_states += int(created)
        created_minerals = 0
        for name in MINERALS:
            _, created = Mineral.objects.get_or_create(name_ar=name)
            created_minerals += int(created)
        created_localities = 0
        for state_ar, localities in EXAMPLE_LOCALITIES.items():
            state = State.objects.filter(name_ar=state_ar).first()
            if state is None:
                continue
            for name in localities:
                _, created = Locality.objects.get_or_create(name_ar=name, state=state)
                created_localities += int(created)
        updated_nationalities = 0
        for code, name in NATIONALITY_NAMES.items():
            nationality, created = Nationality.objects.update_or_create(
                code=code, defaults={"name_ar": name}
            )
            updated_nationalities += int(created)
        # Default working-hours schedule (open-ended) unless one already exists.
        from datetime import date, time

        if not WorkingHoursSchedule.objects.exists():
            WorkingHoursSchedule.objects.create(
                name="افتراضي",
                start_date=date(2000, 1, 1),
                work_start=time(8, 0),
                work_end=time(16, 0),
                working_weekdays=[0, 1, 2, 3, 6],
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded states ({len(SUDAN_STATES)} total, {created_states} new), "
                f"minerals ({len(MINERALS)} total, {created_minerals} new), "
                f"localities (example set, {created_localities} new) and "
                f"nationalities ({len(NATIONALITY_NAMES)} total, {updated_nationalities} new)."
            )
        )
        self.stdout.write(
            self.style.SUCCESS("Working-hours schedule: default (Sun-Thu 08:00-16:00) ensured.")
        )
