import csv
import datetime
from pathlib import Path

from auditlog.models import LogEntry

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import URLValidator, validate_email
from django.db import transaction

from companies.audit_setup import suppress_auditlog
from companies.models import (
    NATIONALITY_NAMES,
    Agreement,
    Block,
    Company,
    CompanyType,
    ContractType,
    Locality,
    Mineral,
    Nationality,
    State,
    contract_type_options,
    split_minerals,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

COMPANY_TYPE_MAP = {
    "emtiaz": CompanyType.EXPLORATION,
    "entaj": CompanyType.PRODUCTION,
    "mokhalfat": CompanyType.TAILINGS,
    "sageer": CompanyType.SMALL,
}

# agreement "نوع الشركة" -> contract_type (per the company-type rule)
COMPANY_KIND_TO_CONTRACT = {
    "امتياز استكشاف": ContractType.CONCESSION,
    "امتياز منتجة": ContractType.MINING,
    "مخلفات": ContractType.TAILINGS,
    "تعدين صغير": ContractType.SMALL,
}

VALIDITY_MAP = {
    "سارية": "active",
    "غير سارية": "expired",
    "ملغية": "cancelled",
    "مجمدة": "frozen",
    "تنازل": "cancelled",
}

STATE_NORMALIZE = {
    "النيل الازرق": "النيل الأزرق",
    "النيل الابيض": "النيل الأبيض",
}


def norm(value):
    return " ".join((value or "").strip().split())


def parse_date(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value[:10])
    except ValueError:
        return None


class Command(BaseCommand):
    help = "Imports companies and agreements from data/companies.csv and data/agreements.csv."

    def add_arguments(self, parser):
        parser.add_argument("--companies", default=str(PROJECT_ROOT / "data" / "companies.csv"))
        parser.add_argument("--agreements", default=str(PROJECT_ROOT / "data" / "agreements.csv"))
        parser.add_argument("--company-type", choices=[ct for ct, _ in CompanyType.choices])
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        self.dry_run = options["dry_run"]
        self.filter_type = options["company_type"]
        self.warnings = []
        stats = {"created": 0, "updated": 0, "skipped": 0, "failed": 0, "placeholders": 0}

        company_path = Path(options["companies"])
        agreement_path = Path(options["agreements"])
        if not company_path.exists() or not agreement_path.exists():
            raise CommandError("Both companies and agreements CSV files are required.")

        with open(company_path, encoding="utf-8-sig") as fh:
            company_rows = list(csv.DictReader(fh))
        with open(agreement_path, encoding="utf-8-sig") as fh:
            agreement_rows = list(csv.DictReader(fh))

        # Dry-run runs everything inside a transaction that is rolled back, so
        # validation sees fully saved state without persisting anything.
        # Row-level audit is suppressed (bulk backfill) and replaced by one
        # summary entry below.
        with transaction.atomic(), suppress_auditlog():
            self.stdout.write(f"Importing companies from {company_path.name} ...")
            company_lookup = {}
            for row in company_rows:
                self._import_company(row, company_lookup, stats)
            self.stdout.write(f"Importing agreements from {agreement_path.name} ...")
            for row in agreement_rows:
                self._import_agreement(row, company_lookup, stats)
            if self.dry_run:
                transaction.set_rollback(True)

        if not self.dry_run:
            from django.contrib.contenttypes.models import ContentType

            LogEntry.objects.create(
                content_type=ContentType.objects.get_for_model(Company),
                object_pk="",
                object_id=None,
                object_repr="Import run: companies.csv + agreements.csv",
                action=LogEntry.Action.UPDATE,
                changes={
                    "created": stats["created"],
                    "updated": stats["updated"],
                    "skipped": stats["skipped"],
                    "failed": stats["failed"],
                    "placeholders": stats["placeholders"],
                },
                actor=None,
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done: created={stats['created']} updated={stats['updated']} "
                f"skipped={stats['skipped']} failed={stats['failed']} "
                f"placeholder companies={stats['placeholders']}"
            )
        )
        if self.dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing was written."))
        if self.warnings:
            self.stdout.write(self.style.WARNING(f"{len(self.warnings)} warnings (first 10):"))
            for w in self.warnings[:10]:
                self.stdout.write(f"  - {w}")

    # ---------------------------------------------------------------- companies

    def _import_company(self, row, company_lookup, stats):
        name = norm(row.get("name_ar"))
        if not name:
            stats["skipped"] += 1
            return
        ct_value = COMPANY_TYPE_MAP.get((row.get("company_type") or "").strip())
        if self.filter_type and ct_value != self.filter_type:
            return
        if ct_value is None:
            self.warnings.append(f"Unknown company_type {row.get('company_type')!r} for {name}")
            stats["skipped"] += 1
            return
        website = norm(row.get("website"))
        if website and not website.lower().startswith(("http://", "https://")):
            website = "http://" + website
        if website:
            try:
                URLValidator()(website)
            except ValidationError:
                self.warnings.append(f"Company {name}: invalid website {website!r} — left blank")
                website = ""
        email = norm(row.get("email"))
        if email:
            try:
                validate_email(email)
            except ValidationError:
                self.warnings.append(f"Company {name}: invalid email {email!r} — left blank")
                email = ""
        defaults = {
            "name_en": norm(row.get("name_en")) or name,
            "company_type": ct_value,
            "registration_no": norm(row.get("code")),
            "address": norm(row.get("address")),
            "email": email,
            "website": website,
            "manager_name": norm(row.get("manager_name")),
            "manager_phone": norm(row.get("manager_phone")),
            "rep_name": norm(row.get("rep_name")),
            "rep_phone": norm(row.get("rep_phone")),
        }
        company = Company.objects.filter(name_ar=name).first()
        if company is None:
            company = Company(name_ar=name)
            stats["created"] += 1
        else:
            stats["updated"] += 1
        for key, value in defaults.items():
            setattr(company, key, value)
        try:
            company.full_clean()
            company.save()
        except ValidationError as exc:
            stats["failed"] += 1
            self.warnings.append(f"Company {name}: {exc.message_dict}")
            return
        # nationality -> Nationality lookup rows (comma-separated codes allowed)
        nat_codes = [
            part.strip()
            for part in norm(row.get("nationality")).split(",")
            if part.strip()
        ]
        nationalities = []
        for code in nat_codes:
            nationality, _ = Nationality.objects.get_or_create(
                code=code, defaults={"name_ar": NATIONALITY_NAMES.get(code, code)}
            )
            if nationality.name_ar != NATIONALITY_NAMES.get(code, nationality.name_ar):
                nationality.name_ar = NATIONALITY_NAMES[code]
                nationality.save()
            nationalities.append(nationality)
        company.nationalities.set(nationalities)
        company_lookup[name] = company

    # --------------------------------------------------------------- agreements

    def _import_agreement(self, row, company_lookup, stats):
        name = norm(row.get("شركة"))
        kind = norm(row.get("نوع الشركة"))
        company = company_lookup.get(name)
        if company is None:
            ct_value = COMPANY_KIND_TO_CONTRACT.get(kind)
            if self.filter_type and ct_value != self.filter_type:
                return
            company = self._placeholder_company(name, ct_value, stats)
            if company is None:
                stats["skipped"] += 1
                return

        state = self._get_state(norm(row.get("الولاية")))
        locality = None
        locality_name = norm(row.get("المحلية"))
        if locality_name and state is not None:
            locality, _ = Locality.objects.get_or_create(name_ar=locality_name, state=state)
        block = None
        block_code = norm(row.get("رقم المربع"))
        if block_code:
            block, _ = Block.objects.get_or_create(code=block_code, defaults={"name": block_code})
        minerals = [
            Mineral.objects.get_or_create(name_ar=name)[0]
            for name in split_minerals(row.get("المعدن"))
        ]

        agreement_no = norm(row.get("رقم الاتفاقية/العقد"))
        if not agreement_no:
            self.warnings.append(f"Agreement row for {name} has no رقم الاتفاقية/العقد")

        # The company CSV is the authoritative type; derive the contract type
        # from it so the company-type rule always holds. The agreement's
        # "نوع الشركة" column is used only for placeholder companies.
        allowed = contract_type_options(company.company_type)
        contract_type = allowed[0] if allowed else ContractType.SMALL

        validity = VALIDITY_MAP.get(norm(row.get("حالة الاتفاقية/العقد")), "active")
        notes_bits = []
        ag_type = norm(row.get("النوع"))
        if ag_type:
            notes_bits.append(f"النوع: {ag_type}")
        license_count = norm(row.get("عدد الرخص"))
        if license_count:
            notes_bits.append(f"عدد الرخص: {license_count}")

        defaults = {
            "contract_type": contract_type,
            "state": state,
            "locality": locality,
            "block": block,
            "start_date": parse_date(row.get("تاريح البداية")),
            "end_date": parse_date(row.get("تاريخ النهاية")),
            "validity_status": validity,
            "notes": "؛ ".join(notes_bits),
        }
        existing = Agreement.objects.filter(company=company, agreement_no=agreement_no).first()
        if existing is None:
            agreement = Agreement(company=company, agreement_no=agreement_no)
            stats["created"] += 1
        else:
            agreement = existing
            stats["updated"] += 1
        for key, value in defaults.items():
            setattr(agreement, key, value)
        try:
            agreement.full_clean()
            agreement.save()
        except ValidationError as exc:
            stats["failed"] += 1
            self.warnings.append(f"Agreement {agreement_no} / {name}: {exc.message_dict}")
            return
        agreement.minerals.set(minerals)

    def _placeholder_company(self, name, ct_value, stats):
        if ct_value is None:
            self.warnings.append(f"Cannot create placeholder for {name}: unknown نوع الشركة")
            return None
        company = Company.objects.filter(name_ar=name).first()
        if company is None:
            company = Company(
                name_ar=name,
                name_en=name,
                company_type=ct_value,
                notes="تم إنشاؤها تلقائياً من بيانات الاتفاقيات (غير موجودة في companies.csv).",
            )
            company.save()
            stats["created"] += 1
            stats["placeholders"] += 1
        return company

    def _get_state(self, state_name):
        if not state_name:
            return None
        state_name = STATE_NORMALIZE.get(state_name, state_name)
        state, _ = State.objects.get_or_create(name_ar=state_name, defaults={"name_en": state_name})
        return state
