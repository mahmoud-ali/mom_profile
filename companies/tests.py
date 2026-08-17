import os
import tempfile
from django.core.exceptions import ValidationError
from django.test import TestCase

from .models import (
    Agreement,
    Block,
    Company,
    CompanyType,
    FinancialEvent,
    FinancialPosition,
    LegalEvent,
    Locality,
    Mineral,
    State,
    TechnicalPosition,
    contract_type_options,
)


class ProfileRoundTripTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.state = State.objects.create(name_ar="الخرطوم", name_en="Khartoum")
        cls.mineral = Mineral.objects.create(name_ar="الذهب")
        cls.block = Block.objects.create(code="BLK-T1", name="مربع اختبار")
        cls.company = Company.objects.create(
            name_ar="شركة الاختبار",
            name_en="Test Company",
            company_type=CompanyType.EXPLORATION,
            general_status="green",
        )
        cls.agreement = Agreement.objects.create(
            company=cls.company,
            block=cls.block,
            state=cls.state,
            start_date="2024-01-01",
            end_date="2026-12-31",
            signing_date="2023-06-01",
            initial_area_km2="1000.5000",
            current_area_km2="750.0000",
        )
        cls.agreement.minerals.add(cls.mineral)

    def test_profile_sections_attach_to_agreement(self):
        financial = FinancialPosition.objects.create(
            agreement=self.agreement,
            debt="150000.00",
            current_claim="50000.00",
            balance="-150000.00",
            satisfied=False,
        )
        technical = TechnicalPosition.objects.create(
            agreement=self.agreement, work_program="برنامج عمل", activity_summary="نشاط فني"
        )
        LegalEvent.objects.create(
            agreement=self.agreement, event_type="extension", date="2025-01-01", description="تمديد"
        )
        FinancialEvent.objects.create(
            agreement=self.agreement, event_type="payment", date="2025-02-01", amount="100000.00"
        )

        financial.refresh_from_db()
        technical.refresh_from_db()
        self.assertEqual(financial.agreement, self.agreement)
        self.assertEqual(financial.debt, 150000)
        self.assertFalse(financial.satisfied)
        self.assertEqual(technical.work_program, "برنامج عمل")
        self.assertEqual(self.agreement.financial_position, financial)
        self.assertEqual(self.agreement.technical_position, technical)
        self.assertEqual(self.agreement.legal_events.count(), 1)
        self.assertEqual(self.agreement.financial_events.count(), 1)
        # profile sections hang off the agreement, not the company
        self.assertFalse(hasattr(self.company, "financial_position"))
        self.assertEqual(str(self.company), "شركة الاختبار (استكشاف)")


class ContractTypeRuleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.exploration = Company.objects.create(
            name_ar="شركة استكشاف", company_type=CompanyType.EXPLORATION
        )
        cls.production = Company.objects.create(
            name_ar="شركة إنتاج", company_type=CompanyType.PRODUCTION
        )
        cls.tailings = Company.objects.create(
            name_ar="شركة مخلفات", company_type=CompanyType.TAILINGS
        )
        cls.small = Company.objects.create(name_ar="شركة صغيرة", company_type=CompanyType.SMALL)

    def test_mapping(self):
        expected = {
            CompanyType.EXPLORATION: ["concession"],
            CompanyType.PRODUCTION: ["mining", "two_minerals"],
            CompanyType.TAILINGS: ["tailings", "preprocessed_tailings"],
            CompanyType.SMALL: ["small"],
        }
        for ct_value, _ in CompanyType.choices:
            self.assertEqual(list(contract_type_options(ct_value)), expected[ct_value])

    def test_clean_validation(self):
        Agreement(company=self.exploration, contract_type="concession").full_clean()
        for company, bad_ct in [
            (self.exploration, "mining"),
            (self.production, "concession"),
            (self.tailings, "mining"),
            (self.small, "tailings"),
        ]:
            with self.assertRaises(ValidationError):
                Agreement(company=company, contract_type=bad_ct).full_clean()

    def test_admin_form_filters_choices(self):
        from companies.admin import AgreementAdminForm

        form = AgreementAdminForm(instance=Agreement(company=self.exploration))
        self.assertEqual([v for v, _ in form.fields["contract_type"].choices], ["concession"])
        form2 = AgreementAdminForm(instance=Agreement(company=self.production))
        self.assertEqual(
            sorted(v for v, _ in form2.fields["contract_type"].choices),
            ["mining", "two_minerals"],
        )

    def test_company_pages_render_with_inline_agreement_form(self):
        # Regression: the AgreementInline form must not touch the missing
        # 'company' form field (the parent FK is stripped by inline formsets).
        from django.contrib.auth.models import User
        from django.test import Client

        admin = User.objects.create_superuser("su-contract", "s@e.com", "x")
        client = Client()
        client.force_login(admin)
        response = client.get("/admin/companies/company/add/")
        self.assertEqual(response.status_code, 200)
        response = client.get(f"/admin/companies/company/{self.exploration.pk}/change/")
        self.assertEqual(response.status_code, 200)
        response = client.get("/admin/companies/agreement/add/")
        self.assertEqual(response.status_code, 200)

    def test_company_add_page_inline_carries_maps(self):
        # On the Company ADD page the inline agreement rows must carry the maps
        # so the JS can filter contract_type by #id_company_type and locality
        # by the row's state before the company exists server-side.
        from django.contrib.auth.models import User
        from django.test import Client

        admin = User.objects.create_superuser("su-cadd", "c@e.com", "x")
        client = Client()
        client.force_login(admin)
        response = client.get("/admin/companies/company/add/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn('<select name="company_type"', html)
        self.assertIn('name="agreements-0-contract_type"', html)
        self.assertIn("data-contract-map", html)
        self.assertIn('name="agreements-0-locality"', html)
        self.assertIn("data-locality-map", html)
        self.assertIn("agreement_contract_type.js", html)

    def test_standalone_add_page_carries_contract_map(self):
        # Regression: data-contract-map must render on the contract_type select
        # or the dependent JS sees {} and never filters.
        from django.contrib.auth.models import User
        from django.test import Client

        admin = User.objects.create_superuser("su-map", "m@e.com", "x")
        client = Client()
        client.force_login(admin)
        response = client.get("/admin/companies/agreement/add/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn('data-contract-map="{&quot;exploration&quot;', html)
        self.assertIn("data-companies", html)
        self.assertIn("data-locality-map", html)

    def test_inline_agreement_formset_filters_by_parent_company(self):
        # The agreement inline on the Company page must filter contract_type by
        # the parent company's type, for both the visible row and client clones.
        from django.contrib import admin as dj_admin
        from django.contrib.auth.models import User

        from companies.admin import AgreementInline

        class FakeRequest:
            user = None

        FakeRequest.user = User.objects.create_superuser("su-inline", "i@e.com", "x")
        from django.test import Client

        client = Client()
        client.force_login(FakeRequest.user)
        inline = AgreementInline(Company, dj_admin.site)
        formset = inline.get_formset(FakeRequest(), self.exploration)(instance=self.exploration)
        self.assertEqual(
            [v for v, _ in formset.forms[0].fields["contract_type"].choices], ["concession"]
        )
        self.assertEqual(
            [v for v, _ in formset.empty_form.fields["contract_type"].choices], ["concession"]
        )

        # The rendered Company change page shows only the allowed contract type.
        response = client.get(f"/admin/companies/company/{self.exploration.pk}/change/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        start = html.index('name="agreements-0-contract_type"')
        end = html.index("</select>", start)
        block = html[start:end]
        self.assertIn('value="concession"', block)
        self.assertNotIn('value="mining"', block)


class ImportCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data_dir = tempfile.mkdtemp()
        with open(os.path.join(cls.data_dir, "companies.csv"), "w", encoding="utf-8") as fh:
            fh.write(
                "id,company_type,code,name_ar,name_en,nationality,address,website,"
                "manager_name,manager_phone,rep_name,rep_phone,email,status\n"
                "1,emtiaz,C1,شركة ألف,A Co.,1,الخرطوم,http://a.com,مدير,123,ممثل,456,a@b.com,129\n"
                "2,mokhalfat,,شركة باء,,1,,,م2,,,,\n"
            )
        with open(os.path.join(cls.data_dir, "agreements.csv"), "w", encoding="utf-8") as fh:
            fh.write(
                "id,شركة,نوع الشركة,رقم الاتفاقية/العقد,النوع,تاريح البداية,تاريخ النهاية,"
                "عدد الرخص,الولاية,المحلية,رقم المربع,المعدن,حالة الاتفاقية/العقد\n"
                "1,شركة ألف,امتياز استكشاف,SK-1,إتفاقية,2021-10-20,2024-10-19,1,الخرطوم,بحري,SK-1,ذهب,سارية\n"
                "2,شركة غير موجودة,تعدين صغير,SM-1,عقد,2020-01-01,2023-01-01,1,نهر النيل,عطبرة,SM-1,ملح,ملغية\n"
                "3,شركة ألف,امتياز استكشاف,SK-2,إتفاقية,2022-01-01,2023-01-01,1,الخرطوم,بحري,SK-2,نحاس، ذهب,سارية\n"
            )

    def _run(self, dry_run=False):
        from django.core.management import call_command

        call_command(
            "import_profile_data",
            companies=os.path.join(self.data_dir, "companies.csv"),
            agreements=os.path.join(self.data_dir, "agreements.csv"),
            dry_run=dry_run,
            verbosity=0,
        )

    def test_imports_companies_and_agreements(self):
        self._run()
        company = Company.objects.get(name_ar="شركة ألف")
        self.assertEqual(company.company_type, CompanyType.EXPLORATION)
        self.assertEqual(company.registration_no, "C1")
        self.assertEqual(list(company.nationalities.values_list("code", flat=True)), ["1"])
        self.assertEqual(company.manager_name, "مدير")
        self.assertEqual(company.website, "http://a.com")
        self.assertFalse(hasattr(company, "status"))

        agreement = Agreement.objects.get(company=company, agreement_no="SK-1")
        self.assertEqual(agreement.contract_type, "concession")
        self.assertEqual(agreement.validity_status, "active")
        self.assertEqual(agreement.state.name_ar, "الخرطوم")
        self.assertEqual(agreement.locality.name_ar, "بحري")
        self.assertEqual(agreement.block.code, "SK-1")
        self.assertTrue(agreement.minerals.filter(name_ar="ذهب").exists())
        self.assertEqual(agreement.start_date.isoformat(), "2021-10-20")
        self.assertIn("النوع: إتفاقية", agreement.notes)

        # combined المعدن splits into individual minerals; ال-forms normalize
        combined = Agreement.objects.get(company=company, agreement_no="SK-2")
        self.assertEqual(
            set(combined.minerals.values_list("name_ar", flat=True)), {"نحاس", "ذهب"}
        )

        placeholder = Company.objects.get(name_ar="شركة غير موجودة")
        self.assertEqual(placeholder.company_type, CompanyType.SMALL)
        sm = Agreement.objects.get(company=placeholder, agreement_no="SM-1")
        self.assertEqual(sm.contract_type, "small")
        self.assertEqual(sm.validity_status, "cancelled")

    def test_idempotent(self):
        self._run()
        self._run()
        # 2 companies from CSV + 1 placeholder company = 3; 3 agreements.
        self.assertEqual(Company.objects.count(), 3)
        self.assertEqual(Agreement.objects.count(), 3)

    def test_dry_run_writes_nothing(self):
        self._run(dry_run=True)
        self.assertEqual(Company.objects.count(), 0)
        self.assertEqual(Agreement.objects.count(), 0)


class AuditTrailTests(TestCase):
    def test_create_update_delete_logged(self):
        from auditlog.models import LogEntry
        from django.contrib.contenttypes.models import ContentType

        company = Company.objects.create(
            name_ar="شركة تدقيق", company_type=CompanyType.EXPLORATION
        )
        entries = LogEntry.objects.get_for_object(company)
        self.assertEqual(entries.count(), 1)
        self.assertEqual(entries.first().action, LogEntry.Action.CREATE)

        company.name_ar = "شركة تدقيق معدلة"
        company.save()
        update = LogEntry.objects.get_for_object(company).filter(
            action=LogEntry.Action.UPDATE
        ).first()
        self.assertEqual(update.changes["name_ar"], ["شركة تدقيق", "شركة تدقيق معدلة"])

        pk = company.pk
        company.delete()
        deleted = LogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(Company),
            object_pk=str(pk),
            action=LogEntry.Action.DELETE,
        ).first()
        self.assertIsNotNone(deleted)

    def test_admin_save_records_actor(self):
        from auditlog.models import LogEntry
        from django.contrib.auth.models import User
        from django.test import Client

        user = User.objects.create_superuser("audit-admin", "a@e.com", "x")
        client = Client()
        client.force_login(user)
        response = client.post(
            "/admin/companies/company/add/",
            {
                "name_ar": "شركة عبر الأدمن",
                "company_type": "exploration",
                "general_status": "green",
                "agreements-TOTAL_FORMS": "1",
                "agreements-INITIAL_FORMS": "0",
                "agreements-MIN_NUM_FORMS": "0",
                "agreements-MAX_NUM_FORMS": "1000",
                "agreements-0-id": "",
                "agreements-0-agreement_no": "",
                "agreements-0-contract_type": "concession",
                "agreements-0-block": "",
                "agreements-0-state": "",
                "agreements-0-locality": "",
                "agreements-0-start_date": "",
                "agreements-0-end_date": "",
                "agreements-0-signing_date": "",
                "agreements-0-current_area_km2": "",
                "agreements-0-validity_status": "active",
                "_save": "حفظ",
            },
        )
        self.assertEqual(response.status_code, 302)
        company = Company.objects.get(name_ar="شركة عبر الأدمن")
        entry = LogEntry.objects.get_for_object(company).first()
        self.assertEqual(entry.actor, user)

    def test_import_suppresses_row_audit_and_writes_summary(self):
        import os
        import tempfile

        from auditlog.models import LogEntry
        from django.contrib.contenttypes.models import ContentType
        from django.core.management import call_command

        data_dir = tempfile.mkdtemp()
        with open(os.path.join(data_dir, "companies.csv"), "w", encoding="utf-8") as fh:
            fh.write(
                "id,company_type,code,name_ar,name_en,nationality,address,website,"
                "manager_name,manager_phone,rep_name,rep_phone,email,status\n"
                "1,emtiaz,C1,شركة ألف,,1,,,,,,,,,\n"
            )
        with open(os.path.join(data_dir, "agreements.csv"), "w", encoding="utf-8") as fh:
            fh.write(
                "id,شركة,نوع الشركة,رقم الاتفاقية/العقد,النوع,تاريح البداية,تاريخ النهاية,"
                "عدد الرخص,الولاية,المحلية,رقم المربع,المعدن,حالة الاتفاقية/العقد\n"
                "1,شركة ألف,امتياز استكشاف,SK-1,,2021-10-20,2024-10-19,1,الخرطوم,بحري,SK-1,ذهب,سارية\n"
            )
        call_command(
            "import_profile_data",
            companies=os.path.join(data_dir, "companies.csv"),
            agreements=os.path.join(data_dir, "agreements.csv"),
            verbosity=0,
        )
        # No per-row audit for the imported company/agreement...
        company = Company.objects.get(name_ar="شركة ألف")
        self.assertEqual(
            LogEntry.objects.filter(
                content_type=ContentType.objects.get_for_model(Company),
                object_pk=str(company.pk),
            ).count(),
            0,
        )
        # ...but one summary entry exists.
        self.assertTrue(LogEntry.objects.filter(object_repr__contains="Import run").exists())

    def test_m2m_minerals_logged(self):
        from auditlog.models import LogEntry

        company = Company.objects.create(
            name_ar="شركة معادن", company_type=CompanyType.EXPLORATION
        )
        agreement = Agreement.objects.create(company=company)
        mineral = Mineral.objects.create(name_ar="ذهب")
        agreement.minerals.add(mineral)
        self.assertTrue(
            any(
                "minerals" in (entry.changes or {})
                for entry in LogEntry.objects.get_for_object(agreement)
            )
        )


class LocalityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.khartoum = State.objects.create(name_ar="الخرطوم", name_en="Khartoum")
        cls.river_nile = State.objects.create(name_ar="نهر النيل", name_en="River Nile")
        cls.omdurman = Locality.objects.create(name_ar="أم درمان", state=cls.khartoum)
        cls.khartoum_city = Locality.objects.create(name_ar="الخرطوم", state=cls.khartoum)
        cls.atbara = Locality.objects.create(name_ar="عطبرة", state=cls.river_nile)
        cls.company = Company.objects.create(
            name_ar="شركة المحليات", company_type=CompanyType.EXPLORATION
        )

    def test_locality_belongs_to_state(self):
        self.assertEqual(self.omdurman.state, self.khartoum)
        self.assertEqual(list(self.khartoum.localities.order_by("name_ar")), [
            self.omdurman, self.khartoum_city,
        ])

    def test_clean_rejects_locality_from_other_state(self):
        agreement = Agreement(company=self.company, state=self.khartoum, locality=self.atbara)
        with self.assertRaises(ValidationError):
            agreement.full_clean()
        Agreement(company=self.company, state=self.khartoum, locality=self.omdurman).full_clean()

    def test_form_filters_locality_by_state(self):
        from companies.admin import AgreementAdminForm

        form = AgreementAdminForm(instance=Agreement(company=self.company, state=self.khartoum))
        self.assertEqual(
            set(form.fields["locality"].queryset.values_list("pk", flat=True)),
            {self.omdurman.pk, self.khartoum_city.pk},
        )
        form2 = AgreementAdminForm(instance=Agreement(company=self.company, state=self.river_nile))
        self.assertEqual(
            set(form2.fields["locality"].queryset.values_list("pk", flat=True)),
            {self.atbara.pk},
        )

    def test_add_page_carries_locality_state_map(self):
        from django.contrib.auth.models import User
        from django.test import Client

        admin = User.objects.create_superuser("su-locality", "l@e.com", "x")
        client = Client()
        client.force_login(admin)
        response = client.get("/admin/companies/agreement/add/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn("data-locality-map", html)
        self.assertIn("data-companies", html)
        self.assertIn("agreement_contract_type.js", html)
