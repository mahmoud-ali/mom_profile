import csv
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from applications.models import ApplicationType, EventCategory
from companies.models import CompanyType, contract_type_options

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

CSV_SOURCES = {
    CompanyType.EXPLORATION: "apps_emtiaz.csv",
    CompanyType.PRODUCTION: "apps_entaj.csv",
    CompanyType.TAILINGS: "apps_mokhalfat.csv",
    CompanyType.SMALL: "apps_sageer.csv",
}

# Field specs keyed by model_name: model -> {field label: {"type": ...}}.
FOREIGNER_PURPOSE_CHOICES = [
    "اذن تحرك",
    "كرت عمل",
    "تجديد كرت عمل",
    "تأشيرة دخول",
    "إقامة",
    "تجديد إقامة",
    "طلب تعيين اجنبي",
    "تأشيرة متعددة",
    "إذن عمل مبدئي",
]

FLOAT_SPEC = {"type": "float"}
INTEGER_SPEC = {"type": "integer"}

# Specs for MAIN form fields (form_fields), keyed by model_name.
DEFAULT_FIELD_SPECS = {
    "AppForeignerProcedure": {
        "الغرض من التصديق": {"type": "select", "choices": FOREIGNER_PURPOSE_CHOICES},
    },
    "AppAddArea": {"المساحة بالكم2": FLOAT_SPEC},
    "AppRemoveArea": {"المساحة بالكم2": FLOAT_SPEC, "نسبة التنازل": FLOAT_SPEC},
    "AppSendSamplesForAnalysis": {"تكلفة التحليل": FLOAT_SPEC},
    "AppExportGold": {
        "اجمالي انتاج الشركة بالجرام": FLOAT_SPEC,
        "اجمالي الوزن الصافي بالجرام": FLOAT_SPEC,
        "استلام الزكاة صافي بالجرام": FLOAT_SPEC,
        "استلام العوائد الجليلة صافي بالجرام": FLOAT_SPEC,
        "استلام ارباح اعمال صافي بالجرام (للمخلفات فقط)": FLOAT_SPEC,
        "الكمية المباعة لبنك السودان المركزي صافي بالجرام": FLOAT_SPEC,
        "الكمية المراد تصديرها صافي بالجرام": FLOAT_SPEC,
        "متبقي الذهب عند المصفاة": FLOAT_SPEC,
    },
    "AppExportGoldRaw": {"الكمية بالجرام": FLOAT_SPEC, "سعر البيع": FLOAT_SPEC},
}

# Specs for DETAIL columns (detail_fields), keyed by model_name.
DEFAULT_DETAIL_SPECS = {
    "AppSendSamplesForAnalysis": {"وزن العينة": FLOAT_SPEC},
    "AppRequirementsList": {"الكمية": INTEGER_SPEC},
    "AppFuelPermission": {"كمية الوقود": FLOAT_SPEC},
    "AppImportPermission": {"الكمية": FLOAT_SPEC},
    "AppBorrowMaterial": {"الكمية المستلفة": FLOAT_SPEC},
    "AppGoldProduction": {
        "وزن السبيكة": FLOAT_SPEC,
        "وزن المركز الجاف": FLOAT_SPEC,
        "وزن الخبث": FLOAT_SPEC,
        "وزن الذهب المضاف للصهر": FLOAT_SPEC,
        "وزن الذهب المتبقي": FLOAT_SPEC,
    },
    "AppVisibityStudy": {"Long/ East/ X": FLOAT_SPEC, "Lat/ West/ Y": FLOAT_SPEC},
}

# Field layouts keyed by model_name.
DEFAULT_FIELD_LAYOUTS = {
    "AppForeignerProcedure": {"columns": 2, "attachment_columns": 2},
}

# model_name -> (event_category, event_type_value)
DEFAULT_EVENT_MAPPING = {
    "AppTamdeed": (EventCategory.LEGAL, "extension"),
    "AppRenewalContract": (EventCategory.LEGAL, "extension"),
    "AppRemoveArea": (EventCategory.LEGAL, "relinquishment"),
    "AppTnazolShraka": (EventCategory.LEGAL, "relinquishment"),
    "AppTajeelTnazol": (EventCategory.LEGAL, "relinquishment"),
    "AppTakhali": (EventCategory.LEGAL, "cancellation"),
    "AppTajmeed": (EventCategory.LEGAL, "freeze"),
    "AppChangeCompanyName": (EventCategory.LEGAL, "name_change"),
    "AppAddArea": (EventCategory.LEGAL, "area_change"),
    "AppExplorationTime": (EventCategory.LEGAL, "area_change"),
    "AppChangeWorkProcedure": (EventCategory.LEGAL, "other"),
    "AppRestartActivity": (EventCategory.LEGAL, "other"),
    "AppTaaweed": (EventCategory.LEGAL, "other"),
    "AppMda": (EventCategory.LEGAL, "other"),
    "AppWorkPlan": (EventCategory.TECHNICAL, "work_plan"),
    "AppTechnicalFinancialReport": (EventCategory.TECHNICAL, "report"),
    "AppVisibityStudy": (EventCategory.TECHNICAL, "study"),
    "AppSendSamplesForAnalysis": (EventCategory.TECHNICAL, "sampling"),
    "AppGoldProduction": (EventCategory.TECHNICAL, "production"),
    "AppExportGold": (EventCategory.TECHNICAL, "export"),
    "AppExportGoldRaw": (EventCategory.TECHNICAL, "export"),
    "AppRequirementsList": (EventCategory.TECHNICAL, "other"),
    "AppCyanideCertificate": (EventCategory.TECHNICAL, "other"),
    "AppExplosivePermission": (EventCategory.TECHNICAL, "other"),
}


def clean_list(value):
    """Split a CSV cell on Arabic/regular commas, strip bidi/format chars and empties."""
    if not value:
        return []
    value = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", value)
    parts = re.split(r"[،,]", value)
    out = []
    for p in parts:
        p = p.strip()
        if p:
            out.append(p)
    return out


class Command(BaseCommand):
    help = "Loads application/procedure types from the four apps_*.csv catalogs into ApplicationType."

    def add_arguments(self, parser):
        parser.add_argument("--csv", help="Only load this CSV file (default: all four).")
        parser.add_argument(
            "--company-type",
            help="Only load this company type (exploration|production|tailings|small).",
        )

    def handle(self, *args, **options):
        requested_csv = options.get("csv")
        requested_type = options.get("company_type")
        sources = CSV_SOURCES.items()
        if requested_type:
            key = next((k for k in CSV_SOURCES if k == requested_type), None)
            if key is None:
                raise CommandError(f"Unknown company type: {requested_type}")
            sources = [(key, CSV_SOURCES[key])]
        if requested_csv:
            sources = [(ct, name) for ct, name in sources if name == requested_csv]
            if not sources:
                raise CommandError(f"No source CSV named {requested_csv}")

        total = 0
        for company_type, filename in sources:
            path = PROJECT_ROOT / filename
            if not path.exists():
                self.stderr.write(f"Missing CSV: {path}")
                continue
            count = 0
            with open(path, encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    model_name = (row.get("model_name") or "").strip()
                    if not model_name:
                        continue
                    category, label = DEFAULT_EVENT_MAPPING.get(model_name, (None, ""))
                    ApplicationType.objects.update_or_create(
                        company_type=company_type,
                        model_name=model_name,
                        defaults={
                            "verbose_name": (row.get("verbose_name") or "").strip(),
                            "arabic_name": (row.get("arabic_name") or "").strip(),
                            "attachments": clean_list(row.get("attachments_arabic")),
                            "form_fields": clean_list(row.get("form_fields_arabic")),
                            "detail_models": clean_list(row.get("detail_models_arabic")),
                            "detail_fields": clean_list(row.get("detail_fields_arabic")),
                            "field_specs": DEFAULT_FIELD_SPECS.get(model_name, {}),
                            "detail_specs": DEFAULT_DETAIL_SPECS.get(model_name, {}),
                            "field_layout": DEFAULT_FIELD_LAYOUTS.get(model_name, {}),
                            "contract_types": list(contract_type_options(company_type)),
                            "event_category": category,
                            "event_label": label or "",
                        },
                    )
                    count += 1
            total += count
            self.stdout.write(
                self.style.SUCCESS(
                    f"Loaded {count} types for {dict(CompanyType.choices)[company_type]} from {filename}."
                )
            )
        self.stdout.write(self.style.SUCCESS(f"Total ApplicationType rows ensured: {total}."))
