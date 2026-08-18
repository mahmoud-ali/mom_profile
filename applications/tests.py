from django import forms as django_forms
from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

from companies.models import (
    Agreement,
    Company,
    CompanyType,
    FinancialPosition,
    LegalEvent,
    TechnicalEvent,
    TechnicalPosition,
)

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
    Recommendation,
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
        # the final decision must pass through the two recommendation stages
        with self.assertRaises(PermissionDenied):
            app.transition_status(ApplicationStatus.APPROVED, self.manager)

        app.transition_status(
            ApplicationStatus.COMMITTEE_RECOMMENDATION,
            self.manager,
            recommendation=Recommendation.RECOMMENDED,
            notes="توافق اللجنة",
        )
        self.assertEqual(app.status, ApplicationStatus.COMMITTEE_RECOMMENDATION)
        self.assertEqual(app.committee_recommendation, Recommendation.RECOMMENDED)
        self.assertEqual(app.committee_recommendation_notes, "توافق اللجنة")
        self.assertEqual(app.committee_recommended_by, self.manager)
        self.assertIsNotNone(app.committee_recommended_at)

        app.transition_status(
            ApplicationStatus.UNDERSECRETARY_RECOMMENDATION,
            self.manager,
            recommendation=Recommendation.RECOMMENDED,
        )
        self.assertEqual(app.status, ApplicationStatus.UNDERSECRETARY_RECOMMENDATION)
        self.assertEqual(app.undersecretary_recommendation, Recommendation.RECOMMENDED)
        self.assertEqual(app.undersecretary_recommended_by, self.manager)
        self.assertIsNotNone(app.undersecretary_recommended_at)
        # the committee decision is carried through, not wiped
        self.assertEqual(app.committee_recommendation, Recommendation.RECOMMENDED)

        app.transition_status(ApplicationStatus.APPROVED, self.manager, minister_decision="قرار بالموافقة")
        self.assertEqual(app.status, ApplicationStatus.APPROVED)
        self.assertEqual(app.minister_decision, "قرار بالموافقة")
        events = TechnicalEvent.objects.filter(application=app)
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().event_type, "work_plan")
        self.assertEqual(events.first().agreement, self.agreement)

        app.transition_status(ApplicationStatus.APPROVED, self.manager)  # idempotent
        self.assertEqual(TechnicalEvent.objects.filter(application=app).count(), 1)

    def test_technical_data_entry_cannot_approve(self):
        app = self._make_app()
        app.transition_status(ApplicationStatus.SUBMITTED, self.entry)
        app.transition_status(ApplicationStatus.UNDER_PROCESSING, self.manager)
        with self.assertRaises(PermissionDenied):
            app.transition_status(ApplicationStatus.APPROVED, self.entry)
        with self.assertRaises(PermissionDenied):
            app.transition_status(
                ApplicationStatus.COMMITTEE_RECOMMENDATION,
                self.entry,
                recommendation=Recommendation.RECOMMENDED,
            )
        with self.assertRaises(PermissionDenied):
            app.transition_status(
                ApplicationStatus.UNDERSECRETARY_RECOMMENDATION,
                self.entry,
                recommendation=Recommendation.RECOMMENDED,
            )
        self.assertEqual(TechnicalEvent.objects.filter(application=app).count(), 0)

    def test_rejection_logs_no_event_and_allows_resubmit(self):
        app = self._make_app()
        app.transition_status(ApplicationStatus.SUBMITTED, self.entry)
        app.transition_status(ApplicationStatus.UNDER_PROCESSING, self.manager)
        app.transition_status(
            ApplicationStatus.COMMITTEE_RECOMMENDATION,
            self.manager,
            recommendation=Recommendation.RECOMMENDED,
        )
        app.transition_status(
            ApplicationStatus.UNDERSECRETARY_RECOMMENDATION,
            self.manager,
            recommendation=Recommendation.NOT_RECOMMENDED,
        )
        app.transition_status(
            ApplicationStatus.REJECTED, self.manager, minister_decision="الرفض لعدم اكتمال المستندات"
        )
        self.assertEqual(app.status, ApplicationStatus.REJECTED)
        self.assertEqual(TechnicalEvent.objects.filter(application=app).count(), 0)
        self.assertIsNotNone(app.committee_recommendation)
        self.assertIsNotNone(app.undersecretary_recommendation)
        self.assertEqual(app.minister_decision, "الرفض لعدم اكتمال المستندات")

        # resubmit: back to DRAFT clears the recommendation cycle
        app.transition_status(ApplicationStatus.DRAFT, self.entry)
        self.assertEqual(app.status, ApplicationStatus.DRAFT)
        for field in (
            "committee_recommendation",
            "committee_recommended_by",
            "committee_recommended_at",
            "undersecretary_recommendation",
            "undersecretary_recommended_by",
            "undersecretary_recommended_at",
        ):
            self.assertIsNone(getattr(app, field), field)
        for field in ("committee_recommendation_notes", "undersecretary_recommendation_notes"):
            self.assertEqual(getattr(app, field), "", field)
        self.assertEqual(app.minister_decision, "")

    def test_invalid_transition_blocked(self):
        app = self._make_app()
        with self.assertRaises(PermissionDenied):
            app.transition_status(ApplicationStatus.UNDER_PROCESSING, self.manager)

    def test_recommendation_decision_is_mandatory(self):
        app = self._make_app()
        app.transition_status(ApplicationStatus.SUBMITTED, self.entry)
        app.transition_status(ApplicationStatus.UNDER_PROCESSING, self.manager)
        with self.assertRaises(PermissionDenied):
            app.transition_status(
                ApplicationStatus.COMMITTEE_RECOMMENDATION, self.manager
            )
        app.transition_status(
            ApplicationStatus.COMMITTEE_RECOMMENDATION,
            self.manager,
            recommendation=Recommendation.RECOMMENDED,
        )
        with self.assertRaises(PermissionDenied):
            app.transition_status(
                ApplicationStatus.UNDERSECRETARY_RECOMMENDATION, self.manager
            )

    def test_minister_decision_is_mandatory(self):
        app = self._make_app()
        app.transition_status(ApplicationStatus.SUBMITTED, self.entry)
        app.transition_status(ApplicationStatus.UNDER_PROCESSING, self.manager)
        app.transition_status(
            ApplicationStatus.COMMITTEE_RECOMMENDATION,
            self.manager,
            recommendation=Recommendation.RECOMMENDED,
        )
        app.transition_status(
            ApplicationStatus.UNDERSECRETARY_RECOMMENDATION,
            self.manager,
            recommendation=Recommendation.RECOMMENDED,
        )
        with self.assertRaises(PermissionDenied):
            app.transition_status(ApplicationStatus.APPROVED, self.manager)
        app.transition_status(
            ApplicationStatus.APPROVED, self.manager, minister_decision="موافقة الوزير"
        )
        self.assertEqual(app.minister_decision, "موافقة الوزير")
        self.assertIsNotNone(app.reviewed_at)

    def test_legal_application_logs_legal_event(self):
        tamdeed = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppTamdeed"
        )
        app = self._make_app(tamdeed)
        app.transition_status(ApplicationStatus.SUBMITTED, self.entry)
        app.transition_status(ApplicationStatus.UNDER_PROCESSING, self.manager)
        app.transition_status(
            ApplicationStatus.COMMITTEE_RECOMMENDATION,
            self.manager,
            recommendation=Recommendation.RECOMMENDED,
        )
        app.transition_status(
            ApplicationStatus.UNDERSECRETARY_RECOMMENDATION,
            self.manager,
            recommendation=Recommendation.RECOMMENDED,
        )
        app.transition_status(
            ApplicationStatus.APPROVED, self.manager, minister_decision="تمديد مقبول"
        )
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
        # manager processes, then records the two recommendations, then approves
        client = Client()
        client.force_login(manager)
        client.post(change_url, {**base, "status": ApplicationStatus.UNDER_PROCESSING, "_save": "حفظ"})
        app.refresh_from_db()
        self.assertEqual(app.status, ApplicationStatus.UNDER_PROCESSING)
        client.post(
            change_url,
            {
                **base,
                "status": ApplicationStatus.COMMITTEE_RECOMMENDATION,
                "committee_recommendation": "recommended",
                "committee_recommendation_notes": "توافق اللجنة",
                "_save": "حفظ",
            },
        )
        app.refresh_from_db()
        self.assertEqual(app.status, ApplicationStatus.COMMITTEE_RECOMMENDATION)
        self.assertEqual(app.committee_recommendation, "recommended")
        self.assertEqual(app.committee_recommendation_notes, "توافق اللجنة")
        self.assertEqual(app.committee_recommended_by, manager)
        client.post(
            change_url,
            {
                **base,
                "status": ApplicationStatus.UNDERSECRETARY_RECOMMENDATION,
                "undersecretary_recommendation": "recommended",
                "undersecretary_recommendation_notes": "موافقة وكيل الوزارة",
                "_save": "حفظ",
            },
        )
        app.refresh_from_db()
        self.assertEqual(app.status, ApplicationStatus.UNDERSECRETARY_RECOMMENDATION)
        self.assertEqual(app.undersecretary_recommendation, "recommended")
        self.assertEqual(app.undersecretary_recommended_by, manager)
        response = client.post(
            change_url,
            {
                **base,
                "status": ApplicationStatus.UNDERSECRETARY_RECOMMENDATION,
                "minister_decision": "نص قرار الوزير",
                "_transition_to": ApplicationStatus.APPROVED,
                "_save": "حفظ",
            },
        )
        self.assertEqual(response.status_code, 302)
        app.refresh_from_db()
        self.assertEqual(app.status, ApplicationStatus.APPROVED)
        self.assertEqual(app.minister_decision, "نص قرار الوزير")
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

    def test_agreement_autocomplete_params_point_at_application_field(self):
        """The agreement autocomplete widget must query the Application field
        (source model + field name), not the related name."""
        import re

        from django.contrib.auth.models import User
        from django.test import Client

        admin = User.objects.create_superuser("su-autocmp", "a@e.com", "x")
        client = Client()
        client.force_login(admin)
        html = client.get(reverse("admin:applications_application_add")).content.decode(
            "utf-8"
        )
        select = re.search(r'<select name="agreement"[^>]*>', html).group(0)
        self.assertIn("admin-autocomplete", select)
        self.assertIn('data-app-label="applications"', select)
        self.assertIn('data-model-name="application"', select)
        self.assertIn('data-field-name="agreement"', select)

    def test_app_type_autocomplete_params_point_at_application_field(self):
        """The app_type autocomplete widget must query the Application field."""
        import re

        from django.contrib.auth.models import User
        from django.test import Client

        admin = User.objects.create_superuser("su-autocmp2", "a@e.com", "x")
        client = Client()
        client.force_login(admin)
        html = client.get(reverse("admin:applications_application_add")).content.decode(
            "utf-8"
        )
        select = re.search(r'<select name="app_type"[^>]*>', html).group(0)
        self.assertIn("admin-autocomplete", select)
        self.assertIn('data-app-label="applications"', select)
        self.assertIn('data-model-name="application"', select)
        self.assertIn('data-field-name="app_type"', select)


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
        app.transition_status(
            ApplicationStatus.COMMITTEE_RECOMMENDATION,
            self.manager,
            recommendation=Recommendation.RECOMMENDED,
        )
        app.transition_status(
            ApplicationStatus.UNDERSECRETARY_RECOMMENDATION,
            self.manager,
            recommendation=Recommendation.RECOMMENDED,
        )
        app.transition_status(
            ApplicationStatus.APPROVED, self.manager, minister_decision="موافقة"
        )
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
                (
                    ApplicationStatus.UNDER_PROCESSING,
                    ApplicationStatus.COMMITTEE_RECOMMENDATION,
                ),
                (
                    ApplicationStatus.COMMITTEE_RECOMMENDATION,
                    ApplicationStatus.UNDERSECRETARY_RECOMMENDATION,
                ),
                (ApplicationStatus.UNDERSECRETARY_RECOMMENDATION, ApplicationStatus.APPROVED),
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
        app2.transition_status(
            ApplicationStatus.COMMITTEE_RECOMMENDATION,
            self.manager,
            recommendation=Recommendation.RECOMMENDED,
        )
        app2.transition_status(
            ApplicationStatus.UNDERSECRETARY_RECOMMENDATION,
            self.manager,
            recommendation=Recommendation.NOT_RECOMMENDED,
        )
        app2.transition_status(
            ApplicationStatus.REJECTED, self.manager, minister_decision="رفض"
        )
        app2.save()

        kpis = compute_kpis()
        self.assertEqual(kpis["total"], 2)
        self.assertEqual(kpis["decided"], 2)
        self.assertEqual(kpis["approved"], 1)
        self.assertEqual(kpis["approval_rate"], 50.0)
        self.assertEqual(kpis["by_status"].get("معتمد"), 1)
        self.assertEqual(kpis["by_status"].get("مرفوض"), 1)
        self.assertEqual(kpis["per_user"]["tmanager"]["decided"], 2)
        # stage durations exist for each worked pair
        self.assertIn(("مسودة", "مؤكد"), kpis["stage_durations"])
        self.assertIn(("مؤكد", "قيد المعالجة"), kpis["stage_durations"])
        self.assertIn(("قيد المعالجة", "توصية اللجنة"), kpis["stage_durations"])
        self.assertIn(("توصية اللجنة", "توصية وكيل الوزارة"), kpis["stage_durations"])
        self.assertIn(("توصية وكيل الوزارة", "معتمد"), kpis["stage_durations"])
        self.assertIn(("توصية وكيل الوزارة", "مرفوض"), kpis["stage_durations"])
        # backlog: no applications currently in the review pipeline
        self.assertEqual(kpis["backlog_count"], 0)
        self.assertIn(timezone.now().strftime("%Y-%m"), kpis["monthly"])

    def test_kpis_backlog_includes_recommendation_stages(self):
        app = Application.objects.create(agreement=self.agreement, app_type=self.work_plan)
        app.transition_status(ApplicationStatus.SUBMITTED, self.entry)
        app.transition_status(ApplicationStatus.UNDER_PROCESSING, self.manager)
        app.transition_status(
            ApplicationStatus.COMMITTEE_RECOMMENDATION,
            self.manager,
            recommendation=Recommendation.RECOMMENDED,
        )
        app.save()
        kpis = compute_kpis()
        self.assertEqual(kpis["backlog_count"], 1)
        self.assertEqual(kpis["by_status"].get("توصية اللجنة"), 1)


class ReadonlyWorkflowTests(TestCase):
    """Once an application is submitted it is frozen: only the status moves."""

    @classmethod
    def setUpTestData(cls):
        call_command("load_app_types")
        call_command("create_roles")
        cls.entry = User.objects.create_user(username="ro_entry", password="x", is_staff=True)
        assign_role(cls.entry, "technical_data_entry", company_types=["exploration"])
        cls.manager = User.objects.create_user(username="ro_manager", password="x", is_staff=True)
        cls.manager.groups.add(Group.objects.get(name="manager"))
        cls.company = Company.objects.create(
            name_ar="شركة القراءة فقط", company_type=CompanyType.EXPLORATION
        )
        cls.agreement = Agreement.objects.create(company=cls.company)
        cls.work_plan = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppWorkPlan"
        )
        cls.client = Client()

    def _draft(self):
        self.client.force_login(self.entry)
        add_url = reverse("admin:applications_application_add")
        self.client.post(
            add_url,
            {
                "agreement": self.agreement.pk,
                "app_type": self.work_plan.pk,
                "status": ApplicationStatus.DRAFT,
                "notes": "",
                "_save": "حفظ",
            },
        )
        return Application.objects.get(app_type=self.work_plan)

    def _submit_draft(self, app):
        change_url = reverse("admin:applications_application_change", args=[app.pk])
        self.client.post(
            change_url,
            {
                "agreement": self.agreement.pk,
                "app_type": self.work_plan.pk,
                "status": ApplicationStatus.DRAFT,
                "notes": "ملاحظة أصلية",
                "f_0": "2025-01-01",
                "f_1": "2025-12-31",
                "f_2": "تعليق أصلي",
                "_submit_app": "تأكيد",
            },
        )
        app.refresh_from_db()
        self.assertEqual(app.status, ApplicationStatus.SUBMITTED)
        return change_url

    def test_submitted_app_form_is_readonly(self):
        import re

        app = self._draft()
        change_url = self._submit_draft(app)
        response = self.client.get(change_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        for field in ("f_0", "a_0", "notes", "agreement"):
            self.assertTrue(
                re.search(rf'name="{field}"[^>]*disabled', html),
                f"{field} should be disabled",
            )
        self.assertIn('name="status"', html)
        self.assertFalse(re.search(r'name="status"[^>]*disabled', html))

    def test_post_edits_on_submitted_app_are_discarded(self):
        app = self._draft()
        change_url = self._submit_draft(app)
        response = self.client.post(
            change_url,
            {
                "agreement": self.agreement.pk,
                "app_type": self.work_plan.pk,
                "status": ApplicationStatus.SUBMITTED,
                "notes": "ملاحظة معدلة",
                "f_0": "2099-01-01",
                "f_1": "2099-12-31",
                "f_2": "تعليق معدل",
                "_save": "حفظ",
            },
        )
        self.assertEqual(response.status_code, 302)
        app.refresh_from_db()
        values = {f.label: f.value for f in app.fields.all()}
        self.assertEqual(values["بداية الخطة"], "2025-01-01")
        self.assertEqual(values["نهاية الخطة"], "2025-12-31")
        self.assertEqual(values["تعليق على الخطة"], "تعليق أصلي")
        self.assertEqual(app.notes, "ملاحظة أصلية")
        self.assertEqual(app.status, ApplicationStatus.SUBMITTED)

    def test_entry_cannot_advance_status_of_submitted_app(self):
        app = self._draft()
        change_url = self._submit_draft(app)
        response = self.client.post(
            change_url,
            {
                "agreement": self.agreement.pk,
                "app_type": self.work_plan.pk,
                "status": ApplicationStatus.APPROVED,  # tampered: not in the dropdown
                "notes": "",
                "_save": "حفظ",
            },
        )
        self.assertEqual(response.status_code, 200)  # form rejects the choice
        app.refresh_from_db()
        self.assertEqual(app.status, ApplicationStatus.SUBMITTED)

    def test_committee_decision_required_via_admin(self):
        app = self._draft()
        change_url = self._submit_draft(app)
        client = Client()
        client.force_login(self.manager)
        client.post(
            change_url,
            {
                "agreement": self.agreement.pk,
                "app_type": self.work_plan.pk,
                "status": ApplicationStatus.UNDER_PROCESSING,
                "notes": "",
                "_save": "حفظ",
            },
        )
        app.refresh_from_db()
        self.assertEqual(app.status, ApplicationStatus.UNDER_PROCESSING)
        response = client.post(
            change_url,
            {
                "agreement": self.agreement.pk,
                "app_type": self.work_plan.pk,
                "status": ApplicationStatus.COMMITTEE_RECOMMENDATION,
                "notes": "",
                "_save": "حفظ",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "هذا الحقل مطلوب")
        app.refresh_from_db()
        self.assertEqual(app.status, ApplicationStatus.UNDER_PROCESSING)

    def test_recommendations_recorded_through_admin(self):
        app = self._draft()
        change_url = self._submit_draft(app)
        client = Client()
        client.force_login(self.manager)
        client.post(
            change_url,
            {
                "agreement": self.agreement.pk,
                "app_type": self.work_plan.pk,
                "status": ApplicationStatus.UNDER_PROCESSING,
                "notes": "",
                "_save": "حفظ",
            },
        )
        client.post(
            change_url,
            {
                "agreement": self.agreement.pk,
                "app_type": self.work_plan.pk,
                "status": ApplicationStatus.COMMITTEE_RECOMMENDATION,
                "committee_recommendation": "recommended",
                "committee_recommendation_notes": "توافق اللجنة",
                "notes": "",
                "_save": "حفظ",
            },
        )
        app.refresh_from_db()
        self.assertEqual(app.status, ApplicationStatus.COMMITTEE_RECOMMENDATION)
        self.assertEqual(app.committee_recommendation, "recommended")
        self.assertEqual(app.committee_recommendation_notes, "توافق اللجنة")
        self.assertEqual(app.committee_recommended_by, self.manager)
        self.assertIsNotNone(app.committee_recommended_at)
        self.assertIsNone(app.undersecretary_recommendation)

        client.post(
            change_url,
            {
                "agreement": self.agreement.pk,
                "app_type": self.work_plan.pk,
                "status": ApplicationStatus.UNDERSECRETARY_RECOMMENDATION,
                "undersecretary_recommendation": "not_recommended",
                "undersecretary_recommendation_notes": "متحفظ",
                "notes": "",
                "_save": "حفظ",
            },
        )
        app.refresh_from_db()
        self.assertEqual(app.status, ApplicationStatus.UNDERSECRETARY_RECOMMENDATION)
        self.assertEqual(app.undersecretary_recommendation, "not_recommended")
        self.assertEqual(app.undersecretary_recommendation_notes, "متحفظ")
        self.assertEqual(app.undersecretary_recommended_by, self.manager)
        self.assertIsNotNone(app.undersecretary_recommended_at)
        # the committee decision is carried through, not wiped
        self.assertEqual(app.committee_recommendation, "recommended")

    def test_save_buttons_hidden_after_draft(self):
        """After the draft the generic admin save buttons are hidden — the
        transition buttons are the only way to move the application."""
        app = self._draft()
        change_url = reverse("admin:applications_application_change", args=[app.pk])
        # draft: generic save buttons + the تأكيد الطلب transition button
        html = self.client.get(change_url).content.decode("utf-8")
        self.assertIn('name="_save"', html)
        self.assertIn('name="_transition_to" value="submitted"', html)
        self.assertIn("تأكيد الطلب", html)
        # submitted: no generic save buttons at all
        self._submit_draft(app)
        html = self.client.get(change_url).content.decode("utf-8")
        self.assertNotIn('name="_save"', html)
        self.assertNotIn('name="_addanother"', html)
        self.assertNotIn('name="_continue"', html)
        # the manager also sees only the transition button
        client = Client()
        client.force_login(self.manager)
        html = client.get(change_url).content.decode("utf-8")
        self.assertNotIn('name="_save"', html)
        self.assertIn('name="_transition_to" value="under_processing"', html)

    def test_add_page_still_has_save_buttons(self):
        """Regression: the add page (obj is None) must keep the generic admin
        save buttons — show_save_buttons has to be set there too."""
        self.client.force_login(self.entry)
        response = self.client.get(reverse("admin:applications_application_add"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn('name="_save"', html)
        self.assertIn('name="_addanother"', html)
        self.assertIn('name="_continue"', html)

    def test_status_rendered_hidden_with_transition_buttons(self):
        app = self._draft()
        change_url = self._submit_draft(app)
        # the data-entry user: no review permissions -> no transition buttons
        response = self.client.get(change_url)
        html = response.content.decode("utf-8")
        self.assertIn('<input type="hidden" name="status"', html)
        self.assertNotIn('<select name="status"', html)
        self.assertNotIn('name="_transition_to"', html)
        # the manager sees a button per allowed next status
        client = Client()
        client.force_login(self.manager)
        response = client.get(change_url)
        html = response.content.decode("utf-8")
        self.assertIn('name="_transition_to" value="under_processing"', html)
        self.assertNotIn('name="_transition_to" value="approved"', html)

    def test_transition_via_button_posts_target(self):
        app = self._draft()
        change_url = self._submit_draft(app)
        client = Client()
        client.force_login(self.manager)
        response = client.post(
            change_url,
            {
                "agreement": self.agreement.pk,
                "app_type": self.work_plan.pk,
                "status": ApplicationStatus.SUBMITTED,  # hidden field value
                "notes": "",
                "_transition_to": ApplicationStatus.UNDER_PROCESSING,
                "_save": "حفظ",
            },
        )
        self.assertEqual(response.status_code, 302)
        app.refresh_from_db()
        self.assertEqual(app.status, ApplicationStatus.UNDER_PROCESSING)

    def test_entry_fields_visible_only_at_entry_stage(self):
        """The recommendation/minister fields render only at their entry stage."""
        import re

        app = self._draft()
        change_url = self._submit_draft(app)
        client = Client()
        client.force_login(self.manager)
        # SUBMITTED: no entry fields at all
        html = client.get(change_url).content.decode("utf-8")
        for name in (
            "committee_recommendation",
            "committee_recommendation_notes",
            "undersecretary_recommendation",
            "undersecretary_recommendation_notes",
            "minister_decision",
        ):
            self.assertNotIn(f'name="{name}"', html, name)
        # UNDER_PROCESSING: only the committee fields
        client.post(change_url, {**self._base_post(), "status": ApplicationStatus.SUBMITTED, "_transition_to": ApplicationStatus.UNDER_PROCESSING, "_save": "حفظ"})
        html = client.get(change_url).content.decode("utf-8")
        self.assertIn('name="committee_recommendation"', html)
        self.assertIn('name="committee_recommendation_notes"', html)
        self.assertNotIn('name="undersecretary_recommendation"', html)
        self.assertNotIn('name="minister_decision"', html)
        # COMMITTEE_RECOMMENDATION: undersecretary fields are entered; the
        # recorded committee recommendation stays visible (read-only)
        client.post(change_url, {**self._base_post(), "status": ApplicationStatus.UNDER_PROCESSING, "committee_recommendation": "recommended", "committee_recommendation_notes": "توافق اللجنة", "_transition_to": ApplicationStatus.COMMITTEE_RECOMMENDATION, "_save": "حفظ"})
        html = client.get(change_url).content.decode("utf-8")
        self.assertIn('name="undersecretary_recommendation"', html)
        self.assertIn('name="undersecretary_recommendation_notes"', html)
        # recorded committee values are still shown, disabled
        self.assertTrue(re.search(r'name="committee_recommendation"[^>]*disabled', html))
        self.assertTrue(re.search(r'name="committee_recommendation_notes"[^>]*disabled', html))
        self.assertNotIn('name="minister_decision"', html)
        # UNDERSECRETARY_RECOMMENDATION: only the minister's decision text box
        # is entered; both recorded recommendations stay visible
        client.post(change_url, {**self._base_post(), "status": ApplicationStatus.COMMITTEE_RECOMMENDATION, "undersecretary_recommendation": "recommended", "undersecretary_recommendation_notes": "موافقة وكيل الوزارة", "_transition_to": ApplicationStatus.UNDERSECRETARY_RECOMMENDATION, "_save": "حفظ"})
        html = client.get(change_url).content.decode("utf-8")
        self.assertIn('name="minister_decision"', html)
        self.assertTrue(re.search(r'name="committee_recommendation"[^>]*disabled', html))
        self.assertTrue(re.search(r'name="undersecretary_recommendation"[^>]*disabled', html))
        # the final decision buttons carry plain labels
        self.assertIn('name="_transition_to" value="approved"', html)
        self.assertNotIn("قرار الوزير: معتمد", html)

    def test_minister_decision_text_box_required_and_stored(self):
        import re

        app = self._draft()
        change_url = self._submit_draft(app)
        client = Client()
        client.force_login(self.manager)
        client.post(change_url, {**self._base_post(), "status": ApplicationStatus.SUBMITTED, "_transition_to": ApplicationStatus.UNDER_PROCESSING, "_save": "حفظ"})
        client.post(change_url, {**self._base_post(), "status": ApplicationStatus.UNDER_PROCESSING, "committee_recommendation": "recommended", "committee_recommendation_notes": "توافق اللجنة", "_transition_to": ApplicationStatus.COMMITTEE_RECOMMENDATION, "_save": "حفظ"})
        client.post(change_url, {**self._base_post(), "status": ApplicationStatus.COMMITTEE_RECOMMENDATION, "undersecretary_recommendation": "recommended", "undersecretary_recommendation_notes": "موافقة وكيل الوزارة", "_transition_to": ApplicationStatus.UNDERSECRETARY_RECOMMENDATION, "_save": "حفظ"})
        app.refresh_from_db()
        self.assertEqual(app.status, ApplicationStatus.UNDERSECRETARY_RECOMMENDATION)
        # approving without the minister's decision text is rejected
        response = client.post(
            change_url,
            {**self._base_post(), "status": ApplicationStatus.UNDERSECRETARY_RECOMMENDATION, "_transition_to": ApplicationStatus.APPROVED, "_save": "حفظ"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "هذا الحقل مطلوب")
        app.refresh_from_db()
        self.assertEqual(app.status, ApplicationStatus.UNDERSECRETARY_RECOMMENDATION)
        # with the text, the decision is stored
        response = client.post(
            change_url,
            {**self._base_post(), "status": ApplicationStatus.UNDERSECRETARY_RECOMMENDATION, "minister_decision": "موافقة على الخطة", "_transition_to": ApplicationStatus.APPROVED, "_save": "حفظ"},
        )
        self.assertEqual(response.status_code, 302)
        app.refresh_from_db()
        self.assertEqual(app.status, ApplicationStatus.APPROVED)
        self.assertEqual(app.minister_decision, "موافقة على الخطة")
        # at the final state the recorded decision stays visible, read-only
        html = client.get(change_url).content.decode("utf-8")
        self.assertTrue(re.search(r'name="minister_decision"[^>]*disabled', html))
        self.assertIn("موافقة على الخطة", html)

    def _base_post(self):
        return {
            "agreement": self.agreement.pk,
            "app_type": self.work_plan.pk,
            "notes": "",
        }


class WorkflowModuleTests(TestCase):
    """The workflow lives in applications/workflow.py; the model delegates."""

    @classmethod
    def setUpTestData(cls):
        call_command("load_app_types")
        call_command("create_roles")
        cls.entry = User.objects.create_user(username="wm_entry", password="x")
        assign_role(cls.entry, "technical_data_entry", company_types=["exploration"])
        cls.manager = User.objects.create_user(username="wm_manager", password="x")
        cls.manager.groups.add(Group.objects.get(name="manager"))
        cls.company = Company.objects.create(
            name_ar="شركة الوحدة", company_type=CompanyType.EXPLORATION
        )
        cls.agreement = Agreement.objects.create(company=cls.company)
        cls.work_plan = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppWorkPlan"
        )

    def test_module_does_not_import_models_at_module_level(self):
        import ast
        import inspect

        from applications import workflow

        tree = ast.parse(inspect.getsource(workflow))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module, (".models", "applications.models"))
                self.assertNotEqual(node.module, "models")

    def test_pure_functions_match_model_wrappers(self):
        from applications.workflow import (
            REVIEW_STATUSES,
            SUBMITTABLE_STATUSES,
            allowed_next_statuses_for,
            can_transition,
            is_editable,
            is_readonly,
        )

        app = Application.objects.create(agreement=self.agreement, app_type=self.work_plan)
        self.assertEqual(
            allowed_next_statuses_for(app.status, self.entry),
            app.allowed_next_statuses(self.entry),
        )
        self.assertEqual(
            allowed_next_statuses_for(app.status, self.manager),
            app.allowed_next_statuses(self.manager),
        )
        self.assertTrue(can_transition(app.status, ApplicationStatus.SUBMITTED, self.entry))
        self.assertFalse(can_transition(app.status, ApplicationStatus.APPROVED, self.manager))
        self.assertTrue(is_editable(app.status))
        self.assertFalse(is_readonly(app.status))
        self.assertFalse(is_editable(ApplicationStatus.SUBMITTED))
        self.assertTrue(is_readonly(ApplicationStatus.SUBMITTED))
        self.assertIn(ApplicationStatus.DRAFT, SUBMITTABLE_STATUSES)
        self.assertIn(ApplicationStatus.REJECTED, SUBMITTABLE_STATUSES)
        self.assertIn(ApplicationStatus.UNDERSECRETARY_RECOMMENDATION, REVIEW_STATUSES)

    def test_transition_application_function_directly(self):
        from applications.workflow import transition_application

        app = Application.objects.create(agreement=self.agreement, app_type=self.work_plan)
        transition_application(app, ApplicationStatus.SUBMITTED, self.entry)
        self.assertEqual(app.status, ApplicationStatus.SUBMITTED)
        self.assertEqual(app.submitted_by, self.entry)
        transition_application(app, ApplicationStatus.UNDER_PROCESSING, self.manager)
        with self.assertRaises(PermissionDenied):
            transition_application(
                app, ApplicationStatus.COMMITTEE_RECOMMENDATION, self.manager
            )
        transition_application(
            app,
            ApplicationStatus.COMMITTEE_RECOMMENDATION,
            self.manager,
            recommendation=Recommendation.RECOMMENDED,
        )
        self.assertEqual(app.committee_recommendation, Recommendation.RECOMMENDED)
        self.assertEqual(app.committee_recommended_by, self.manager)


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


class CommitteeSlideshowTests(TestCase):
    """The committee slideshow shows «قيد المعالجة» applications one by one."""

    @classmethod
    def setUpTestData(cls):
        call_command("load_app_types")
        call_command("create_roles")
        cls.manager = User.objects.create_user(
            username="cs_manager", password="x", is_staff=True
        )
        cls.manager.groups.add(Group.objects.get(name="manager"))
        cls.entry = User.objects.create_user(
            username="cs_entry", password="x", is_staff=True
        )
        assign_role(cls.entry, "technical_data_entry", company_types=["exploration"])
        cls.company = Company.objects.create(
            name_ar="شركة العرض", company_type=CompanyType.EXPLORATION
        )
        cls.agreement = Agreement.objects.create(company=cls.company)
        FinancialPosition.objects.create(agreement=cls.agreement, debt="100.00")
        TechnicalPosition.objects.create(agreement=cls.agreement, processing_method="CIL")
        cls.work_plan = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppWorkPlan"
        )

    def _under_processing(self):
        app = Application.objects.create(
            agreement=self.agreement, app_type=self.work_plan, notes="ملاحظة"
        )
        app.transition_status(ApplicationStatus.SUBMITTED, self.entry)
        app.transition_status(ApplicationStatus.UNDER_PROCESSING, self.manager)
        # transition_status mutates the instance in memory; persist it so the
        # slideshow's DB query (status=under_processing) sees it.
        app.save()
        return app

    def _slideshow_url(self, index=None):
        url = reverse("admin:applications_application_committee_slideshow")
        return f"{url}?index={index}" if index is not None else url

    def test_permission_denied_for_non_committee(self):
        client = Client()
        client.force_login(self.entry)
        response = client.get(self._slideshow_url())
        self.assertEqual(response.status_code, 403)

    def test_manager_sees_application_and_full_agreement(self):
        app = self._under_processing()
        ApplicationField.objects.create(
            application=app, label="تعليق على الخطة", value="تنفيذ"
        )
        client = Client()
        client.force_login(self.manager)
        response = client.get(self._slideshow_url())
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn("شركة العرض", html)  # company
        self.assertIn("CIL", html)  # technical position
        self.assertIn("الموقف المالي", html)  # financial section header
        self.assertIn(self.work_plan.arabic_name, html)  # app type
        self.assertIn("تنفيذ", html)  # dynamic field value
        self.assertIn("توصية اللجنة", html)  # decision form

    def test_empty_state(self):
        client = Client()
        client.force_login(self.manager)
        response = client.get(self._slideshow_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "لا توجد طلبات قيد المعالجة حالياً")

    def test_post_records_recommendation_and_advances(self):
        app1 = self._under_processing()
        app2 = self._under_processing()
        client = Client()
        client.force_login(self.manager)
        response = client.post(
            self._slideshow_url(index=0),
            {
                "app_pk": app1.pk,
                "index": "0",
                "committee_recommendation": "recommended",
                "committee_recommendation_notes": "توافق اللجنة",
            },
        )
        self.assertEqual(response.status_code, 302)
        app1.refresh_from_db()
        self.assertEqual(app1.status, ApplicationStatus.COMMITTEE_RECOMMENDATION)
        self.assertEqual(app1.committee_recommendation, "recommended")
        self.assertEqual(app1.committee_recommendation_notes, "توافق اللجنة")
        self.assertEqual(app1.committee_recommended_by, self.manager)
        self.assertIsNotNone(app1.committee_recommended_at)
        # auto-advance: index 0 now shows the remaining application (app2)
        response = client.get(response.url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn(f'name="app_pk" value="{app2.pk}"', html)
        self.assertNotIn(f'name="app_pk" value="{app1.pk}"', html)

    def test_post_without_recommendation_rejected(self):
        app = self._under_processing()
        client = Client()
        client.force_login(self.manager)
        response = client.post(
            self._slideshow_url(index=0),
            {
                "app_pk": app.pk,
                "index": "0",
                "committee_recommendation": "",
                "committee_recommendation_notes": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "هذا الحقل مطلوب")
        app.refresh_from_db()
        self.assertEqual(app.status, ApplicationStatus.UNDER_PROCESSING)

    def test_index_clamped(self):
        self._under_processing()
        client = Client()
        client.force_login(self.manager)
        response = client.get(self._slideshow_url(index=999))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "الطلب 1 من 1")
