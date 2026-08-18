from django import forms as django_forms
from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

from companies.models import Agreement, Company, CompanyType, LegalEvent, TechnicalEvent

from django.utils import timezone

from .forms import build_application_form
from .kpis import compute_kpis
from .models import (
    Application,
    ApplicationDetail,
    ApplicationDetailField,
    ApplicationField,
    ApplicationStatus,
    ApplicationType,
)


def assign_role(user, role_name, company_types=None):
    """Put a user into one managed role (group + scope profile)."""
    from roles.roles import apply_role_to_user

    apply_role_to_user(user, role_name, company_types=company_types)


class LoadAppTypesTests(TestCase):
    def test_all_catalogs_loaded_and_idempotent(self):
        call_command("load_app_types")
        expected = {
            CompanyType.EXPLORATION: 23,
            CompanyType.PRODUCTION: 24,
            CompanyType.TAILINGS: 22,
            CompanyType.SMALL: 29,
        }
        for company_type, count in expected.items():
            self.assertEqual(
                ApplicationType.objects.filter(company_type=company_type).count(), count
            )
        self.assertEqual(ApplicationType.objects.count(), 98)

        samples = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppSendSamplesForAnalysis"
        )
        self.assertIn("نوع العينة", samples.detail_fields)
        self.assertEqual(
            samples.attachments,
            ["فاتورة مبدئية", "استمارة وصف العينات", "نتيجة التحليل السابق ان وجدت"],
        )

        work_plan = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppWorkPlan"
        )
        self.assertEqual(work_plan.event_category, "technical")
        self.assertEqual(work_plan.event_label, "work_plan")
        whom = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppWhomConcern"
        )
        self.assertIsNone(whom.event_category)

        call_command("load_app_types")  # idempotent
        self.assertEqual(ApplicationType.objects.count(), 98)


class WorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_app_types")
        call_command("create_roles")
        cls.entry = User.objects.create_user(username="entry1", password="x")
        assign_role(cls.entry, "technical_data_entry", company_types=["exploration"])
        cls.manager = User.objects.create_user(username="manager1", password="x")
        cls.manager.groups.add(Group.objects.get(name="manager"))
        cls.company = Company.objects.create(
            name_ar="شركة الاختبار", company_type=CompanyType.EXPLORATION
        )
        cls.agreement = Agreement.objects.create(company=cls.company)
        cls.work_plan = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppWorkPlan"
        )

    def _make_app(self, app_type=None):
        return Application.objects.create(
            agreement=self.agreement, app_type=app_type or self.work_plan
        )

    def test_full_workflow_and_approval_logs_one_event_on_agreement(self):
        app = self._make_app()
        self.assertEqual(app.status, ApplicationStatus.DRAFT)

        app.transition_status(ApplicationStatus.SUBMITTED, self.entry)
        self.assertEqual(app.status, ApplicationStatus.SUBMITTED)
        self.assertEqual(app.submitted_by, self.entry)

        with self.assertRaises(PermissionDenied):
            app.transition_status(ApplicationStatus.APPROVED, self.manager)

        app.transition_status(ApplicationStatus.UNDER_PROCESSING, self.manager)
        app.transition_status(ApplicationStatus.APPROVED, self.manager)
        self.assertEqual(app.status, ApplicationStatus.APPROVED)
        events = TechnicalEvent.objects.filter(application=app)
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().event_type, "work_plan")
        self.assertEqual(events.first().agreement, self.agreement)

        app.transition_status(ApplicationStatus.APPROVED, self.manager)  # idempotent
        self.assertEqual(TechnicalEvent.objects.filter(application=app).count(), 1)

    def test_technical_data_entry_cannot_approve(self):
        app = self._make_app()
        with self.assertRaises(PermissionDenied):
            app.transition_status(ApplicationStatus.APPROVED, self.entry)
        self.assertEqual(TechnicalEvent.objects.filter(application=app).count(), 0)

    def test_rejection_logs_no_event_and_allows_resubmit(self):
        app = self._make_app()
        app.transition_status(ApplicationStatus.SUBMITTED, self.entry)
        app.transition_status(ApplicationStatus.UNDER_PROCESSING, self.manager)
        app.transition_status(ApplicationStatus.REJECTED, self.manager)
        self.assertEqual(app.status, ApplicationStatus.REJECTED)
        self.assertEqual(TechnicalEvent.objects.filter(application=app).count(), 0)
        app.transition_status(ApplicationStatus.DRAFT, self.entry)
        self.assertEqual(app.status, ApplicationStatus.DRAFT)

    def test_invalid_transition_blocked(self):
        app = self._make_app()
        with self.assertRaises(PermissionDenied):
            app.transition_status(ApplicationStatus.UNDER_PROCESSING, self.manager)

    def test_legal_application_logs_legal_event(self):
        tamdeed = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppTamdeed"
        )
        app = self._make_app(tamdeed)
        app.transition_status(ApplicationStatus.SUBMITTED, self.entry)
        app.transition_status(ApplicationStatus.UNDER_PROCESSING, self.manager)
        app.transition_status(ApplicationStatus.APPROVED, self.manager)
        event = LegalEvent.objects.get(application=app)
        self.assertEqual(event.event_type, "extension")
        self.assertEqual(event.agreement, self.agreement)

    def test_cross_type_mismatch_rejected(self):
        production_type = ApplicationType.objects.get(
            company_type=CompanyType.PRODUCTION, model_name="AppGoldProduction"
        )
        app = Application(agreement=self.agreement, app_type=production_type)
        with self.assertRaises(ValidationError):
            app.full_clean()


class DynamicFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_app_types")
        call_command("create_roles")
        cls.entry = User.objects.create_user(username="entry1", password="x", is_staff=True)
        assign_role(cls.entry, "technical_data_entry", company_types=["exploration"])
        cls.company = Company.objects.create(
            name_ar="شركة الاستكشاف", company_type=CompanyType.EXPLORATION
        )
        cls.agreement = Agreement.objects.create(company=cls.company)
        cls.work_plan = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppWorkPlan"
        )
        cls.samples = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppSendSamplesForAnalysis"
        )
        cls.client = Client()

    def _create_draft(self, app_type):
        self.client.force_login(self.entry)
        add_url = reverse("admin:applications_application_add")
        response = self.client.post(
            add_url,
            {
                "agreement": self.agreement.pk,
                "app_type": app_type.pk,
                "status": ApplicationStatus.DRAFT,
                "notes": "",
                "_save": "حفظ",
            },
        )
        return Application.objects.get(app_type=app_type)

    def test_form_fields_match_type(self):
        Form = build_application_form(self.work_plan, instance=None)
        self.assertIn("f_0", Form.base_fields)
        self.assertIn("f_1", Form.base_fields)
        self.assertIn("f_2", Form.base_fields)
        self.assertEqual(Form.base_fields["f_0"].label, "بداية الخطة")
        self.assertEqual(Form.base_fields["f_1"].label, "نهاية الخطة")
        self.assertEqual(Form.base_fields["f_2"].label, "تعليق على الخطة")
        # work plan has two attachments: خطاب رسمي من الشركة، ملف خطة العمل
        self.assertEqual(len([k for k in Form.base_fields if k.startswith("a_")]), 2)
        self.assertEqual(Form.base_fields["a_0"].label, "خطاب رسمي من الشركة")

    def test_change_page_renders_type_form_and_saves_locked_labels(self):
        app = self._create_draft(self.work_plan)
        change_url = reverse("admin:applications_application_change", args=[app.pk])

        response = self.client.get(change_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "بداية الخطة")
        self.assertContains(response, "استمارة الطلب")

        response = self.client.post(
            change_url,
            {
                "agreement": self.agreement.pk,
                "app_type": self.work_plan.pk,
                "status": ApplicationStatus.DRAFT,
                "notes": "ملاحظة",
                "f_0": "2025-01-01",
                "f_1": "2025-12-31",
                "f_2": "تعليق",
                "_save": "حفظ",
            },
        )
        self.assertEqual(response.status_code, 302)
        app.refresh_from_db()
        values = {f.label: f.value for f in app.fields.all()}
        self.assertEqual(values, {"بداية الخطة": "2025-01-01", "نهاية الخطة": "2025-12-31", "تعليق على الخطة": "تعليق"})

    def test_submit_button_transitions_to_submitted(self):
        app = self._create_draft(self.work_plan)
        change_url = reverse("admin:applications_application_change", args=[app.pk])
        response = self.client.post(
            change_url,
            {
                "agreement": self.agreement.pk,
                "app_type": self.work_plan.pk,
                "status": ApplicationStatus.DRAFT,
                "notes": "",
                "f_0": "2025-01-01",
                "f_1": "2025-12-31",
                "f_2": "",
                "_submit_app": "تأكيد",
            },
        )
        self.assertEqual(response.status_code, 302)
        app.refresh_from_db()
        self.assertEqual(app.status, ApplicationStatus.SUBMITTED)
        self.assertEqual(app.submitted_by, self.entry)

    def test_manager_approves_via_admin_logs_event(self):
        # Regression: the admin form pre-applies the posted status, so the state
        # machine must detect the change against the DB status for the event to fire.
        from django.contrib.auth.models import User as AuthUser

        manager = AuthUser.objects.create_user(username="manager2", password="x", is_staff=True)
        manager.groups.add(Group.objects.get(name="manager"))
        app = self._create_draft(self.work_plan)
        change_url = reverse("admin:applications_application_change", args=[app.pk])
        base = {
            "agreement": self.agreement.pk,
            "app_type": self.work_plan.pk,
            "notes": "",
            "f_0": "2025-01-01",
            "f_1": "2025-12-31",
            "f_2": "تنفيذ",
        }
        # data entry submits
        self.client.post(change_url, {**base, "status": ApplicationStatus.DRAFT, "_submit_app": "تأكيد"})
        app.refresh_from_db()
        self.assertEqual(app.status, ApplicationStatus.SUBMITTED)
        # manager processes then approves
        client = Client()
        client.force_login(manager)
        client.post(change_url, {**base, "status": ApplicationStatus.UNDER_PROCESSING, "_save": "حفظ"})
        app.refresh_from_db()
        self.assertEqual(app.status, ApplicationStatus.UNDER_PROCESSING)
        response = client.post(change_url, {**base, "status": ApplicationStatus.APPROVED, "_save": "حفظ"})
        self.assertEqual(response.status_code, 302)
        app.refresh_from_db()
        self.assertEqual(app.status, ApplicationStatus.APPROVED)
        events = TechnicalEvent.objects.filter(application=app)
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().agreement, self.agreement)

    def test_detail_rows_round_trip(self):
        app = self._create_draft(self.samples)
        change_url = reverse("admin:applications_application_change", args=[app.pk])

        self.client.post(
            change_url,
            {
                "agreement": self.agreement.pk,
                "app_type": self.samples.pk,
                "status": ApplicationStatus.DRAFT,
                "notes": "",
                "f_0": "السودان",
                "f_1": "الخرطوم",
                "f_2": "شارع النيل",
                "f_3": "500",
                "dt_0_0": "تربة",
                "dt_0_1": "2.5",
                "dt_0_2": "كيس محكم",
                "dt_0_3": "كيميائي",
                "dt_0_4": "تحديد التركيز",
                "_save": "حفظ",
            },
        )
        app.refresh_from_db()
        detail = app.details.get()
        self.assertEqual(detail.category, "")
        cells = {f.label: f.value for f in detail.fields.all()}
        self.assertEqual(cells["نوع العينة"], "تربة")
        self.assertEqual(cells["وزن العينة"], "2.5")
        self.assertEqual(cells["الغرض من التحليل"], "تحديد التركيز")

        # second row appears after first save
        response = self.client.get(change_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "dt_1_0")
        self.assertContains(response, "نوع العينة")


class NumericFieldTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_app_types")
        call_command("create_roles")
        cls.entry = User.objects.create_user(username="nentry", password="x", is_staff=True)
        assign_role(cls.entry, "technical_data_entry", company_types=["exploration"])
        cls.company = Company.objects.create(
            name_ar="شركة الأرقام", company_type=CompanyType.EXPLORATION
        )
        cls.agreement = Agreement.objects.create(company=cls.company)
        cls.client = Client()

    def _draft(self, model_name, ct=CompanyType.EXPLORATION):
        at = ApplicationType.objects.get(company_type=ct, model_name=model_name)
        self.client.force_login(self.entry)
        add_url = reverse("admin:applications_application_add")
        self.client.post(
            add_url,
            {
                "agreement": self.agreement.pk,
                "app_type": at.pk,
                "status": ApplicationStatus.DRAFT,
                "notes": "",
                "_save": "حفظ",
            },
        )
        return at, Application.objects.get(app_type=at)

    def test_builder_field_types(self):
        at, _ = self._draft("AppAddArea")
        Form = build_application_form(at)
        self.assertIsInstance(Form.base_fields["f_0"], django_forms.FloatField)
        self.assertIsInstance(Form.base_fields["f_1"], django_forms.CharField)

        at2 = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppRequirementsList"
        )
        Form2 = build_application_form(at2)
        self.assertIsInstance(Form2.base_fields["dt_0_2"], django_forms.IntegerField)

        at3 = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppWorkPlan"
        )
        Form3 = build_application_form(at3)
        self.assertIsInstance(Form3.base_fields["f_2"], django_forms.CharField)

    def test_float_save_round_trip(self):
        at, app = self._draft("AppAddArea")
        change_url = reverse("admin:applications_application_change", args=[app.pk])
        response = self.client.post(
            change_url,
            {
                "agreement": self.agreement.pk,
                "app_type": at.pk,
                "status": ApplicationStatus.DRAFT,
                "notes": "",
                "f_0": "1000.5",
                "f_1": "توسعة منطقة الشمال",
                "_save": "حفظ",
            },
        )
        self.assertEqual(response.status_code, 302)
        field = app.fields.get(label="المساحة بالكم2")
        self.assertEqual(field.value, "1000.5")
        self.assertEqual(app.fields.get(label="اسباب الاضافة").value, "توسعة منطقة الشمال")

    def test_integer_detail_round_trip(self):
        at, app = self._draft("AppRequirementsList")
        change_url = reverse("admin:applications_application_change", args=[app.pk])
        response = self.client.post(
            change_url,
            {
                "agreement": self.agreement.pk,
                "app_type": at.pk,
                "status": ApplicationStatus.DRAFT,
                "notes": "",
                "dt_0_cat": "معدات المناجم",
                "dt_0_0": "مطرقة هيدروليكية",
                "dt_0_1": "حفارة صغيرة",
                "dt_0_2": "10",
                "_save": "حفظ",
            },
        )
        self.assertEqual(response.status_code, 302)
        detail = app.details.get()
        self.assertEqual(detail.category, "معدات المناجم")
        cells = {f.label: f.value for f in detail.fields.all()}
        self.assertEqual(cells, {"البند": "مطرقة هيدروليكية", "الوصف": "حفارة صغيرة", "الكمية": "10"})

    def test_invalid_numeric_input_rejected(self):
        at, app = self._draft("AppAddArea")
        change_url = reverse("admin:applications_application_change", args=[app.pk])
        response = self.client.post(
            change_url,
            {
                "agreement": self.agreement.pk,
                "app_type": at.pk,
                "status": ApplicationStatus.DRAFT,
                "notes": "",
                "f_0": "abc",
                "f_1": "",
                "_save": "حفظ",
            },
        )
        self.assertEqual(response.status_code, 200)  # validation error, not saved
        field = app.fields.get(label="المساحة بالكم2")
        self.assertEqual(field.value, "")  # empty placeholder, invalid value not stored

    def test_numeric_spec_on_all_catalogs(self):
        for ct_value in (
            CompanyType.EXPLORATION,
            CompanyType.PRODUCTION,
            CompanyType.TAILINGS,
            CompanyType.SMALL,
        ):
            at = ApplicationType.objects.get(
                company_type=ct_value, model_name="AppRequirementsList"
            )
            self.assertEqual(at.detail_specs["الكمية"]["type"], "integer")

    def test_detail_specs_separate_from_field_specs(self):
        # Detail columns are typed by detail_specs, main fields by field_specs.
        req = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppRequirementsList"
        )
        self.assertEqual(req.field_specs, {})
        self.assertEqual(req.detail_specs["الكمية"]["type"], "integer")

        samples = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppSendSamplesForAnalysis"
        )
        self.assertEqual(samples.field_specs["تكلفة التحليل"]["type"], "float")
        self.assertEqual(samples.detail_specs["وزن العينة"]["type"], "float")

        foreigner = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppForeignerProcedure"
        )
        self.assertEqual(foreigner.field_specs["الغرض من التصديق"]["type"], "select")
        self.assertEqual(foreigner.detail_specs, {})


class FieldLayoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_app_types")
        call_command("create_roles")
        cls.entry = User.objects.create_user(username="lentry", password="x", is_staff=True)
        assign_role(cls.entry, "technical_data_entry", company_types=["exploration"])
        cls.company = Company.objects.create(
            name_ar="شركة التخطيط", company_type=CompanyType.EXPLORATION
        )
        cls.agreement = Agreement.objects.create(company=cls.company)
        cls.client = Client()

    def _draft(self, model_name):
        at = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name=model_name
        )
        self.client.force_login(self.entry)
        add_url = reverse("admin:applications_application_add")
        self.client.post(
            add_url,
            {
                "agreement": self.agreement.pk,
                "app_type": at.pk,
                "status": ApplicationStatus.DRAFT,
                "notes": "",
                "_save": "حفظ",
            },
        )
        return at, Application.objects.get(app_type=at)

    def test_foreigner_seeded_two_columns(self):
        at = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppForeignerProcedure"
        )
        self.assertEqual(at.field_layout, {"columns": 2, "attachment_columns": 2})

    def test_columns_rendered(self):
        at, app = self._draft("AppForeignerProcedure")
        change_url = reverse("admin:applications_application_change", args=[app.pk])
        response = self.client.get(change_url)
        html = response.content.decode("utf-8")
        self.assertEqual(response.status_code, 200)
        self.assertIn("width:calc((100% - 2%)/2)", html)
        self.assertIn('<select name="f_2"', html)
        # guard against admin CSS fieldset .fieldBox margin-right pushing columns
        self.assertIn(".dyn-field-grid .fieldBox { margin: 0 0 10px 0; }", html)
        self.assertIn('class="form-row dyn-field-grid"', html)
        # attachments grid uses attachment_columns (2 for foreigner)
        self.assertIn("خطاب رسمي من الشركة", html)
        self.assertIn('type="file" name="a_0"', html)

    def test_groups_rendered_in_order(self):
        at, app = self._draft("AppSendSamplesForAnalysis")
        at.field_layout = {
            "columns": 2,
            "groups": [
                {"title": "بيانات الشحن", "fields": ["الدولة", "المدينة"]},
                {"title": "التكلفة", "fields": ["تكلفة التحليل"]},
            ],
        }
        at.save()
        change_url = reverse("admin:applications_application_change", args=[app.pk])
        response = self.client.get(change_url)
        html = response.content.decode("utf-8")
        self.assertIn("بيانات الشحن", html)
        self.assertIn("التكلفة", html)
        # grouped fields render before the ungrouped remainder (العنوان)
        self.assertLess(html.index("بيانات الشحن"), html.index("العنوان"))


FOREIGNER_PURPOSE = [
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


class AppTypeContractFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_app_types")
        cls.company = Company.objects.create(
            name_ar="شركة إنتاج العقود", company_type=CompanyType.PRODUCTION
        )
        cls.mining_agreement = Agreement.objects.create(company=cls.company, contract_type="mining")
        cls.two_minerals_agreement = Agreement.objects.create(
            company=cls.company, contract_type="two_minerals"
        )
        cls.gold = ApplicationType.objects.get(
            company_type=CompanyType.PRODUCTION, model_name="AppGoldProduction"
        )

    def test_contract_types_seeded_from_company_type(self):
        at = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppWorkPlan"
        )
        self.assertEqual(at.contract_types, ["concession"])
        self.assertEqual(self.gold.contract_types, ["mining", "two_minerals"])

    def test_clean_rejects_type_not_allowed_for_contract(self):
        self.gold.contract_types = ["mining"]
        self.gold.save()
        app = Application(agreement=self.two_minerals_agreement, app_type=self.gold)
        with self.assertRaises(ValidationError):
            app.full_clean()
        Application(agreement=self.mining_agreement, app_type=self.gold).full_clean()

    def test_form_filters_app_type_by_contract_type(self):
        Form = build_application_form(None)
        form = Form(data={"agreement": self.mining_agreement.pk})
        self.assertEqual(form.fields["app_type"].queryset.count(), 24)  # production catalog
        self.assertIn(self.gold, form.fields["app_type"].queryset)

    def test_add_page_carries_dependent_dropdown_data(self):
        from django.contrib.auth.models import User
        from django.test import Client

        admin = User.objects.create_superuser("su-apptype", "a@e.com", "x")
        client = Client()
        client.force_login(admin)
        response = client.get(reverse("admin:applications_application_add"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn("data-agreement-contracts", html)
        self.assertIn("data-apptype-contracts", html)
        self.assertIn("application_app_type.js", html)


class RequiredFieldTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_app_types")
        call_command("create_roles")
        cls.entry = User.objects.create_user(username="rentry", password="x", is_staff=True)
        assign_role(cls.entry, "technical_data_entry", company_types=["exploration"])
        cls.company = Company.objects.create(
            name_ar="شركة الإلزام", company_type=CompanyType.EXPLORATION
        )
        cls.agreement = Agreement.objects.create(company=cls.company)
        cls.client = Client()

    def test_required_flag_in_builder(self):
        at = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppAddArea"
        )
        at.field_specs = {
            "المساحة بالكم2": {"type": "float", "required": True},
            "اسباب الاضافة": {"required": True},
        }
        at.save()
        Form = build_application_form(at)
        self.assertTrue(Form.base_fields["f_0"].required)
        self.assertTrue(Form.base_fields["f_1"].required)
        # optional by default elsewhere
        at2 = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppWorkPlan"
        )
        self.assertFalse(build_application_form(at2).base_fields["f_0"].required)

    def test_required_blocks_save_and_valid_value_passes(self):
        at = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppAddArea"
        )
        at.field_specs = {"المساحة بالكم2": {"type": "float", "required": True}}
        at.save()
        self.client.force_login(self.entry)
        add_url = reverse("admin:applications_application_add")
        self.client.post(
            add_url,
            {
                "agreement": self.agreement.pk,
                "app_type": at.pk,
                "status": ApplicationStatus.DRAFT,
                "notes": "",
                "_save": "حفظ",
            },
        )
        app = Application.objects.get(app_type=at)
        change_url = reverse("admin:applications_application_change", args=[app.pk])
        base = {
            "agreement": self.agreement.pk,
            "app_type": at.pk,
            "status": ApplicationStatus.DRAFT,
            "notes": "",
            "f_0": "",
            "f_1": "",
            "_save": "حفظ",
        }
        missing = self.client.post(change_url, base)
        self.assertEqual(missing.status_code, 200)  # required field empty -> form error
        with_value = self.client.post(change_url, {**base, "f_0": "500.5"})
        self.assertEqual(with_value.status_code, 302)
        self.assertEqual(app.fields.get(label="المساحة بالكم2").value, "500.5")

    def test_attachment_and_detail_required(self):
        at = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppWorkPlan"
        )
        at.field_specs = {"خطاب رسمي من الشركة": {"required": True}}
        at.save()
        Form = build_application_form(at)
        self.assertTrue(Form.base_fields["a_0"].required)

        req = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppRequirementsList"
        )
        req.detail_specs = {"الكمية": {"type": "integer", "required": True}}
        req.save()
        Form2 = build_application_form(req)
        self.assertTrue(Form2.base_fields["dt_0_2"].required)


class TransitionAndKpiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_app_types")
        call_command("create_roles")
        cls.entry = User.objects.create_user(username="tentry", password="x")
        assign_role(cls.entry, "technical_data_entry", company_types=["exploration"])
        cls.manager = User.objects.create_user(username="tmanager", password="x")
        cls.manager.groups.add(Group.objects.get(name="manager"))
        cls.company = Company.objects.create(
            name_ar="شركة التحولات", company_type=CompanyType.EXPLORATION
        )
        cls.agreement = Agreement.objects.create(company=cls.company)
        cls.work_plan = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppWorkPlan"
        )

    def _decided(self):
        app = Application.objects.create(agreement=self.agreement, app_type=self.work_plan)
        app.transition_status(ApplicationStatus.SUBMITTED, self.entry)
        app.transition_status(ApplicationStatus.UNDER_PROCESSING, self.manager)
        app.transition_status(ApplicationStatus.APPROVED, self.manager)
        app.save()
        return app

    def test_creation_and_workflow_transitions(self):
        app = self._decided()
        transitions = list(app.transitions.order_by("timestamp"))
        self.assertEqual(
            [(t.from_status, t.to_status) for t in transitions],
            [
                (None, ApplicationStatus.DRAFT),
                (ApplicationStatus.DRAFT, ApplicationStatus.SUBMITTED),
                (ApplicationStatus.SUBMITTED, ApplicationStatus.UNDER_PROCESSING),
                (ApplicationStatus.UNDER_PROCESSING, ApplicationStatus.APPROVED),
            ],
        )
        self.assertEqual(
            app.transitions.get(to_status=ApplicationStatus.SUBMITTED).user, self.entry
        )
        self.assertEqual(
            app.transitions.get(to_status=ApplicationStatus.APPROVED).user, self.manager
        )
        # duration in state: the anchor has none, real transitions record it
        self.assertIsNone(app.transitions.first().duration_seconds)
        self.assertIsNotNone(
            app.transitions.get(to_status=ApplicationStatus.SUBMITTED).duration_seconds
        )

    def test_noop_transition_creates_nothing(self):
        app = Application.objects.create(agreement=self.agreement, app_type=self.work_plan)
        app.transition_status(ApplicationStatus.DRAFT, self.entry)  # no-op
        self.assertEqual(app.transitions.count(), 1)

    def test_kpis_volume_rate_and_stage_durations(self):
        self._decided()  # approved
        app2 = Application.objects.create(agreement=self.agreement, app_type=self.work_plan)
        app2.transition_status(ApplicationStatus.SUBMITTED, self.entry)
        app2.transition_status(ApplicationStatus.UNDER_PROCESSING, self.manager)
        app2.transition_status(ApplicationStatus.REJECTED, self.manager)
        app2.save()

        kpis = compute_kpis()
        self.assertEqual(kpis["total"], 2)
        self.assertEqual(kpis["decided"], 2)
        self.assertEqual(kpis["approved"], 1)
        self.assertEqual(kpis["approval_rate"], 50.0)
        self.assertEqual(kpis["by_status"].get("مجاز"), 1)
        self.assertEqual(kpis["by_status"].get("مرفوض"), 1)
        self.assertEqual(kpis["per_user"]["tmanager"]["decided"], 2)
        # stage durations exist for each worked pair
        self.assertIn(("مسودة", "مؤكد"), kpis["stage_durations"])
        self.assertIn(("مؤكد", "قيد المعالجة"), kpis["stage_durations"])
        self.assertIn(("قيد المعالجة", "مجاز"), kpis["stage_durations"])
        self.assertIn(("قيد المعالجة", "مرفوض"), kpis["stage_durations"])
        # backlog: no applications currently under processing
        self.assertEqual(kpis["backlog_count"], 0)
        self.assertIn(timezone.now().strftime("%Y-%m"), kpis["monthly"])


class WorkingTimeTests(TestCase):
    def _dt(self, y, m, d, hh, mm=0):
        from datetime import datetime

        tz = timezone.get_current_timezone()
        return datetime(y, m, d, hh, mm, tzinfo=tz)

    def test_same_working_day(self):
        from .business_time import working_seconds_between

        start = self._dt(2026, 1, 12, 9, 0)  # Monday
        end = self._dt(2026, 1, 12, 11, 0)
        self.assertEqual(working_seconds_between(start, end), 2 * 3600)

    def test_weekend_excluded(self):
        from .business_time import working_seconds_between

        # Thursday 15:00 -> Sunday 10:00 = 1h (Thu) + 2h (Sun) = 3h (Fri/Sat off)
        start = self._dt(2026, 1, 15, 15, 0)
        end = self._dt(2026, 1, 18, 10, 0)
        self.assertEqual(working_seconds_between(start, end), 3 * 3600)

    def test_overnight_counts_only_working_hours(self):
        from .business_time import working_seconds_between

        # Monday 15:00 -> Tuesday 09:00 = 1h + 1h = 2h
        start = self._dt(2026, 1, 12, 15, 0)
        end = self._dt(2026, 1, 13, 9, 0)
        self.assertEqual(working_seconds_between(start, end), 2 * 3600)

    def test_out_of_hours_and_reverse(self):
        from .business_time import working_seconds_between

        # after work hours on a working day counts nothing for that day
        start = self._dt(2026, 1, 12, 17, 0)
        end = self._dt(2026, 1, 12, 17, 30)
        self.assertEqual(working_seconds_between(start, end), 0)
        # reversed interval returns 0
        start2 = self._dt(2026, 1, 12, 9, 0)
        end2 = self._dt(2026, 1, 12, 8, 0)
        self.assertEqual(working_seconds_between(start2, end2), 0)


class SeasonalScheduleTests(TestCase):
    def test_seasonal_hours_apply_per_date_range(self):
        from datetime import date, datetime, time

        from .business_time import working_seconds_between
        from .models import WorkingHoursSchedule

        WorkingHoursSchedule.objects.create(
            name="صيفي",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 8, 31),
            work_start=time(7, 0),
            work_end=time(15, 0),
            working_weekdays=[0, 1, 2, 3, 6],
        )
        # summer day: 06:00-10:00 counts from 07:00 -> 3h
        summer_start = timezone.make_aware(datetime(2026, 7, 1, 6, 0))
        summer_end = timezone.make_aware(datetime(2026, 7, 1, 10, 0))
        self.assertEqual(working_seconds_between(summer_start, summer_end), 3 * 3600)
        # outside the range the fallback 08:00 applies -> 2h
        other_start = timezone.make_aware(datetime(2026, 4, 1, 6, 0))
        other_end = timezone.make_aware(datetime(2026, 4, 1, 10, 0))
        self.assertEqual(working_seconds_between(other_start, other_end), 2 * 3600)


class EventLabelChoiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_app_types")
        cls.work_plan = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppWorkPlan"
        )

    def test_form_renders_category_choices(self):
        from applications.admin import ApplicationTypeForm

        form = ApplicationTypeForm(instance=self.work_plan)
        field = form.fields["event_label"]
        values = [value for value, _ in field.choices]
        # all categories' options render so the JS can rebuild by category
        self.assertIn("work_plan", values)
        self.assertIn("study", values)
        self.assertIn("extension", values)
        self.assertIn("claim", values)
        labels = dict(field.choices)
        self.assertEqual(labels["work_plan"], "خطة عمل")

    def test_add_form_renders_all_event_options(self):
        from applications.admin import ApplicationTypeForm

        form = ApplicationTypeForm()  # add page, no category yet
        values = [value for value, _ in form.fields["event_label"].choices]
        self.assertIn("work_plan", values)
        self.assertIn("extension", values)
        self.assertIn("claim", values)

    def test_model_clean_rejects_mismatch(self):
        from django.core.exceptions import ValidationError

        bad = ApplicationType(
            company_type=CompanyType.EXPLORATION,
            model_name="m",
            verbose_name="v",
            arabic_name="أ",
            event_category="technical",
            event_label="extension",
        )
        with self.assertRaises(ValidationError):
            bad.full_clean()
        good = ApplicationType(
            company_type=CompanyType.EXPLORATION,
            model_name="m",
            verbose_name="v",
            arabic_name="ب",
            event_category="technical",
            event_label="work_plan",
        )
        good.full_clean()

    def test_admin_page_carries_dropdown_data(self):
        from django.contrib.auth.models import User
        from django.test import Client

        admin = User.objects.create_superuser("su-evlabel", "e@e.com", "x")
        client = Client()
        client.force_login(admin)
        response = client.get(
            reverse("admin:applications_applicationtype_change", args=[self.work_plan.pk])
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn('name="event_label"', html)
        self.assertIn("data-event-labels", html)
        self.assertIn("application_type_event.js", html)


class ForeignerPurposeFieldTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_app_types")
        call_command("create_roles")
        cls.entry = User.objects.create_user(username="fentry", password="x", is_staff=True)
        assign_role(cls.entry, "technical_data_entry", company_types=["exploration"])
        cls.company = Company.objects.create(
            name_ar="شركة الأجانب", company_type=CompanyType.EXPLORATION
        )
        cls.agreement = Agreement.objects.create(company=cls.company)
        cls.foreigner = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppForeignerProcedure"
        )
        cls.client = Client()

    def test_spec_seeded_on_all_catalogs(self):
        for ct_value in (
            CompanyType.EXPLORATION,
            CompanyType.PRODUCTION,
            CompanyType.TAILINGS,
            CompanyType.SMALL,
        ):
            at = ApplicationType.objects.get(
                company_type=ct_value, model_name="AppForeignerProcedure"
            )
            spec = at.field_specs.get("الغرض من التصديق", {})
            self.assertEqual(spec.get("type"), "select")
            self.assertEqual(spec.get("choices"), FOREIGNER_PURPOSE)

    def test_form_field_is_dropdown_with_the_nine_options(self):
        Form = build_application_form(self.foreigner)
        field = Form.base_fields["f_2"]
        self.assertIsInstance(field, django_forms.ChoiceField)
        values = [v for v, _ in field.choices]
        self.assertEqual(values[0], "")  # blank option
        self.assertEqual(values[1:], FOREIGNER_PURPOSE)

    def test_save_round_trip_persists_selected_value(self):
        self.client.force_login(self.entry)
        add_url = reverse("admin:applications_application_add")
        self.client.post(
            add_url,
            {
                "agreement": self.agreement.pk,
                "app_type": self.foreigner.pk,
                "status": ApplicationStatus.DRAFT,
                "notes": "",
                "_save": "حفظ",
            },
        )
        app = Application.objects.get(app_type=self.foreigner)
        change_url = reverse("admin:applications_application_change", args=[app.pk])
        response = self.client.post(
            change_url,
            {
                "agreement": self.agreement.pk,
                "app_type": self.foreigner.pk,
                "status": ApplicationStatus.DRAFT,
                "notes": "",
                "f_0": "2025-03-01",
                "f_1": "2025-06-01",
                "f_2": "كرت عمل",
                "_save": "حفظ",
            },
        )
        self.assertEqual(response.status_code, 302)
        field = app.fields.get(label="الغرض من التصديق")
        self.assertEqual(field.value, "كرت عمل")

    def test_change_page_renders_select_for_purpose(self):
        self.client.force_login(self.entry)
        add_url = reverse("admin:applications_application_add")
        self.client.post(
            add_url,
            {
                "agreement": self.agreement.pk,
                "app_type": self.foreigner.pk,
                "status": ApplicationStatus.DRAFT,
                "notes": "",
                "_save": "حفظ",
            },
        )
        app = Application.objects.get(app_type=self.foreigner)
        change_url = reverse("admin:applications_application_change", args=[app.pk])
        response = self.client.get(change_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn('<select name="f_2"', html)
        self.assertIn('<option value="كرت عمل">كرت عمل</option>', html)
