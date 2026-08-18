import io
import re

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

from applications.models import Application, ApplicationStatus, ApplicationType
from companies.models import (
    Agreement,
    Company,
    CompanyType,
    FinancialPosition,
    Locality,
    State,
    TechnicalEvent,
    TechnicalPosition,
)

from .models import StaffProfile
from .roles import (
    MANAGED_ROLES,
    ROLE_DEFINITIONS,
    apply_role_to_user,
    company_scope_q,
    scoped_company_types,
)


def _make_user(username, **kwargs):
    kwargs.setdefault("password", "x")
    return User.objects.create_user(username=username, **kwargs)


def _codenames(group):
    return set(group.permissions.values_list("codename", flat=True))


class SidebarOrderTests(TestCase):
    """config.sidebar orders the admin sidebar apps and models explicitly."""

    @classmethod
    def setUpTestData(cls):
        from django.test import RequestFactory

        cls.admin_user = User.objects.create_superuser("su_sidebar", "s@s.com", "x")
        request = RequestFactory().get("/admin/")
        request.user = cls.admin_user
        cls.request = request

    def test_app_order(self):
        import config.sidebar  # noqa: F401  (installs the override on import)

        from django.contrib import admin

        app_list = admin.site.get_app_list(self.request)
        self.assertEqual(
            [app["app_label"] for app in app_list],
            ["companies", "applications", "roles", "auth", "auditlog"],
        )

    def test_model_order_within_companies(self):
        from django.contrib import admin

        app_list = admin.site.get_app_list(self.request)
        companies = next(app for app in app_list if app["app_label"] == "companies")
        self.assertEqual(
            [m["model"]._meta.model_name for m in companies["models"]],
            [
                "company",
                "agreement",
                "financialposition",
                "technicalposition",
                "financialevent",
                "technicalevent",
                "legalevent",
                "state",
                "locality",
                "mineral",
                "block",
                "nationality",
                "auditlogproxy",
            ],
        )

    def test_model_order_within_applications(self):
        from django.contrib import admin

        app_list = admin.site.get_app_list(self.request)
        applications = next(app for app in app_list if app["app_label"] == "applications")
        self.assertEqual(
            [m["model"]._meta.model_name for m in applications["models"]],
            [
                "application",
                "applicationtype",
                "workinghoursschedule",
            ],
        )


class CreateRolesTests(TestCase):
    def test_idempotent_with_correct_permission_sets(self):
        call_command("create_roles")
        call_command("create_roles")  # idempotent re-run

        tech = Group.objects.get(name="technical_data_entry")
        fin = Group.objects.get(name="financial_data_entry")
        mgr = Group.objects.get(name="manager")

        tech_perms = _codenames(tech)
        self.assertTrue(
            {
                "add_application",
                "change_application",
                "delete_application",
                "view_application",
                "can_submit",
                "add_technicalposition",
                "change_technicalposition",
                "add_technicalevent",
                "change_technicalevent",
            }
            <= tech_perms
        )
        self.assertFalse(tech_perms & {"can_review", "can_approve", "can_reject"})
        # ApplicationType (procedure catalog) is manager-only
        self.assertFalse(any("applicationtype" in p for p in tech_perms))

        fin_perms = _codenames(fin)
        self.assertTrue(
            {
                "add_financialposition",
                "change_financialposition",
                "view_financialposition",
                "add_financialevent",
                "change_financialevent",
                "view_financialevent",
            }
            <= fin_perms
        )
        self.assertFalse(
            any("application" in p or "applicationtype" in p for p in fin_perms)
        )

        mgr_perms = _codenames(mgr)
        self.assertTrue(
            {"can_review", "can_approve", "can_reject", "add_applicationtype", "view_applicationtype"}
            <= mgr_perms
        )

        # old data_entry group is no longer created
        self.assertFalse(Group.objects.filter(name="data_entry").exists())

    def test_retired_group_warns_about_members(self):
        call_command("create_roles")
        stale = Group.objects.create(name="data_entry")
        legacy = _make_user("legacy_user")
        legacy.groups.add(stale)
        buf = io.StringIO()
        call_command("create_roles", stdout=buf)
        output = buf.getvalue()
        self.assertIn("data_entry", output)
        self.assertIn("legacy_user", output)

    def test_role_permissions_editable_via_admin(self):
        """The Role change page lets a superuser edit permissions; the sync
        action / create_roles restores the canonical ROLE_DEFINITIONS set."""
        call_command("create_roles")
        admin = User.objects.create_superuser("su_edit", "e@e.com", "x")
        client = Client()
        client.force_login(admin)
        tech = Group.objects.get(name="technical_data_entry")
        url = reverse("admin:roles_role_change", args=[tech.pk])
        self.assertEqual(client.get(url).status_code, 200)

        keep = [
            p.pk
            for p in tech.permissions.exclude(
                codename__in=["can_submit", "delete_application"]
            )
        ]
        resp = client.post(url, {"name": tech.name, "permissions": keep})
        self.assertEqual(resp.status_code, 302)
        tech.refresh_from_db()
        codenames = set(tech.permissions.values_list("codename", flat=True))
        self.assertNotIn("can_submit", codenames)
        self.assertNotIn("delete_application", codenames)

        # create_roles re-applies the canonical set
        call_command("create_roles")
        codenames = set(
            Group.objects.get(name="technical_data_entry").permissions.values_list(
                "codename", flat=True
            )
        )
        self.assertIn("can_submit", codenames)
        self.assertIn("delete_application", codenames)


class HelperTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("create_roles")

    def test_apply_role_to_user_syncs_group_and_profile(self):
        user = _make_user("helper1")
        apply_role_to_user(user, "technical_data_entry", company_types=["exploration"])
        self.assertTrue(user.groups.filter(name="technical_data_entry").exists())
        self.assertEqual(list(user.staff_profile.company_types), ["exploration"])
        # switching roles removes the previous managed role and clears scope
        apply_role_to_user(user, "financial_data_entry", company_types=["production"])
        user.refresh_from_db()
        user.staff_profile.refresh_from_db()
        self.assertFalse(user.groups.filter(name="technical_data_entry").exists())
        self.assertTrue(user.groups.filter(name="financial_data_entry").exists())
        self.assertEqual(list(user.staff_profile.company_types), ["production"])
        # unassign clears the managed group but leaves unrelated groups alone
        other = Group.objects.create(name="unrelated_group")
        user.groups.add(other)
        apply_role_to_user(user, None, company_types=[])
        user.staff_profile.refresh_from_db()
        self.assertFalse(user.groups.filter(name="financial_data_entry").exists())
        self.assertTrue(user.groups.filter(name="unrelated_group").exists())
        self.assertEqual(list(user.staff_profile.company_types), [])

    def test_scoping_precedence(self):
        scoped = _make_user("helper2")
        apply_role_to_user(scoped, "technical_data_entry", company_types=["exploration"])
        self.assertEqual(scoped_company_types(scoped), ["exploration"])
        # manager + scoped -> never scoped
        apply_role_to_user(scoped, "manager", company_types=[])
        self.assertIsNone(scoped_company_types(scoped))
        # financial role -> never scoped
        fin = _make_user("helper3")
        apply_role_to_user(fin, "financial_data_entry")
        self.assertIsNone(scoped_company_types(fin))
        # superuser -> never scoped
        su = _make_user("helper4", is_superuser=True)
        apply_role_to_user(su, "technical_data_entry", company_types=["exploration"])
        self.assertIsNone(scoped_company_types(su))
        # scoped user with no profile -> sees nothing (empty scope), not everything
        bare = _make_user("helper5")
        apply_role_to_user(bare, "technical_data_entry")
        bare.staff_profile.delete()
        self.assertEqual(scoped_company_types(bare), [])

    def test_company_scope_q_mappings(self):
        scoped = _make_user("helper6")
        apply_role_to_user(scoped, "technical_data_entry", company_types=["exploration"])
        from django.db.models import Q

        from applications.models import Application as App
        from companies.models import Agreement as Agr
        from companies.models import Company as Co

        self.assertEqual(
            company_scope_q(scoped, Co),
            Q(company_type__in=["exploration"]),
        )
        self.assertEqual(
            company_scope_q(scoped, Agr),
            Q(company__company_type__in=["exploration"]),
        )
        self.assertEqual(
            company_scope_q(scoped, TechnicalPosition),
            Q(agreement__company__company_type__in=["exploration"]),
        )
        self.assertEqual(
            company_scope_q(scoped, App),
            Q(agreement__company__company_type__in=["exploration"]),
        )


class ScopedAccessTests(TestCase):
    """technical_data_entry sees/edits only its own company types."""

    @classmethod
    def setUpTestData(cls):
        call_command("load_app_types")
        call_command("create_roles")
        cls.exp = Company.objects.create(
            name_ar="شركة الاستكشاف", company_type=CompanyType.EXPLORATION
        )
        cls.prod = Company.objects.create(
            name_ar="شركة الإنتاج", company_type=CompanyType.PRODUCTION
        )
        cls.exp_agr = Agreement.objects.create(company=cls.exp)
        cls.prod_agr = Agreement.objects.create(company=cls.prod)
        # Give the exploration agreement a state+locality so the read-only
        # agreement page exercises the locality branch of AgreementAdminForm.
        state = State.objects.create(name_ar="ولاية الاختبار", name_en="Test State")
        locality = Locality.objects.create(name_ar="محلية الاختبار", state=state)
        cls.exp_agr.state = state
        cls.exp_agr.locality = locality
        cls.exp_agr.save()
        TechnicalPosition.objects.create(agreement=cls.exp_agr, work_program="برنامج أ")
        TechnicalPosition.objects.create(agreement=cls.prod_agr, work_program="برنامج ب")
        cls.exp_event = TechnicalEvent.objects.create(
            agreement=cls.exp_agr, event_type="study", description="حدث أ"
        )
        cls.prod_event = TechnicalEvent.objects.create(
            agreement=cls.prod_agr, event_type="study", description="حدث ب"
        )
        cls.work_plan = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppWorkPlan"
        )
        cls.gold = ApplicationType.objects.get(
            company_type=CompanyType.PRODUCTION, model_name="AppGoldProduction"
        )
        cls.exp_app = Application.objects.create(agreement=cls.exp_agr, app_type=cls.work_plan)
        cls.prod_app = Application.objects.create(agreement=cls.prod_agr, app_type=cls.gold)

    def _scoped_client(self, types=("exploration",)):
        user = _make_user("scoped_admin", is_staff=True)
        apply_role_to_user(user, "technical_data_entry", company_types=list(types))
        client = Client()
        client.force_login(user)
        return client, user

    def test_changelists_filtered_to_scope(self):
        client, _ = self._scoped_client()
        resp = client.get(reverse("admin:companies_technicalevent_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "شركة الاستكشاف")
        self.assertNotContains(resp, "شركة الإنتاج")

        resp = client.get(reverse("admin:companies_technicalposition_changelist"))
        self.assertContains(resp, "برنامج أ")
        self.assertNotContains(resp, "برنامج ب")

        resp = client.get(reverse("admin:applications_application_changelist"))
        self.assertContains(resp, "شركة الاستكشاف")
        self.assertNotContains(resp, "شركة الإنتاج")

    def test_out_of_scope_objects_redirected_away(self):
        # Django redirects (302 -> admin index) when get_queryset hides the
        # object, so out-of-scope rows are never revealed.
        client, _ = self._scoped_client()
        self.assertEqual(
            client.get(
                reverse("admin:companies_technicalevent_change", args=[self.prod_event.pk])
            ).status_code,
            302,
        )
        self.assertEqual(
            client.get(
                reverse("admin:applications_application_change", args=[self.prod_app.pk])
            ).status_code,
            302,
        )

    def test_agreement_dropdown_scoped_on_add_pages(self):
        client, _ = self._scoped_client()
        resp = client.get(reverse("admin:applications_application_add"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "شركة الاستكشاف")
        self.assertNotContains(resp, "شركة الإنتاج")
        resp = client.get(reverse("admin:companies_technicalevent_add"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "شركة الاستكشاف")
        self.assertNotContains(resp, "شركة الإنتاج")

    def test_empty_scope_sees_nothing(self):
        client, _ = self._scoped_client(types=[])
        resp = client.get(reverse("admin:applications_application_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["cl"].result_count, 0)

    def test_agreement_autocomplete_in_technical_forms(self):
        client, _ = self._scoped_client()
        for url_name in (
            "admin:companies_technicalposition_add",
            "admin:companies_technicalevent_add",
        ):
            resp = client.get(reverse(url_name))
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, "admin-autocomplete")
            self.assertContains(resp, "/admin/autocomplete/")

    def test_view_only_agreement_page_renders(self):
        # Regression: view-only Agreement access excludes all form fields, and
        # AgreementAdminForm must not KeyError on contract_type.
        client, _ = self._scoped_client()
        resp = client.get(
            reverse("admin:companies_agreement_change", args=[self.exp_agr.pk])
        )
        self.assertEqual(resp.status_code, 200)

    def _app_type_filter_options(self, response):
        html = response.content.decode("utf-8")
        match = re.search(
            r'<details[^>]*data-filter-title="نوع الطلب".*?</details>', html, re.S
        )
        self.assertIsNotNone(match, "app_type filter block not found")
        return set(re.findall(r"app_type__id__exact=(\d+)", match.group(0)))

    def test_app_type_filter_scoped_shows_only_in_scope_types_with_apps(self):
        client, _ = self._scoped_client()
        resp = client.get(reverse("admin:applications_application_changelist"))
        self.assertEqual(resp.status_code, 200)
        options = self._app_type_filter_options(resp)
        # work_plan has an application in scope; gold's application is
        # out of scope; AppTamdeed has no application at all.
        self.assertEqual(options, {str(self.work_plan.pk)})

    def test_app_type_filter_superuser_shows_types_with_apps(self):
        admin = User.objects.create_superuser("su_typefilter", "t@t.com", "x")
        client = Client()
        client.force_login(admin)
        resp = client.get(reverse("admin:applications_application_changelist"))
        self.assertEqual(resp.status_code, 200)
        options = self._app_type_filter_options(resp)
        tamdeed = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppTamdeed"
        )
        self.assertEqual(options, {str(self.work_plan.pk), str(self.gold.pk)})
        self.assertNotIn(str(tamdeed.pk), options)

    def test_agreement_autocomplete_scoped(self):
        client, _ = self._scoped_client()
        url = reverse("admin:autocomplete")
        params = {
            "app_label": "companies",
            "model_name": "technicalposition",  # source model owning the FK
            "field_name": "agreement",
            "term": "استكشاف",
        }
        resp = client.get(url, params)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            [int(r["id"]) for r in resp.json()["results"]], [self.exp_agr.pk]
        )
        resp = client.get(url, {**params, "term": "إنتاج"})
        self.assertEqual([r["id"] for r in resp.json()["results"]], [])

    def test_manager_and_superuser_not_scoped(self):
        manager = _make_user("mgr_admin", is_staff=True)
        apply_role_to_user(manager, "manager")
        client = Client()
        client.force_login(manager)
        resp = client.get(reverse("admin:companies_technicalevent_changelist"))
        self.assertContains(resp, "شركة الاستكشاف")
        self.assertContains(resp, "شركة الإنتاج")

    def test_applicationtype_is_manager_only(self):
        client, _ = self._scoped_client()
        self.assertEqual(
            client.get(reverse("admin:applications_applicationtype_changelist")).status_code,
            403,
        )
        manager = _make_user("mgr_admin2", is_staff=True)
        apply_role_to_user(manager, "manager")
        mclient = Client()
        mclient.force_login(manager)
        self.assertEqual(
            mclient.get(reverse("admin:applications_applicationtype_changelist")).status_code,
            200,
        )

    def test_kpis_view_manager_only(self):
        client, _ = self._scoped_client()
        self.assertEqual(
            client.get(reverse("admin:applications_application_kpis")).status_code,
            403,
        )
        manager = _make_user("mgr_admin3", is_staff=True)
        apply_role_to_user(manager, "manager")
        mclient = Client()
        mclient.force_login(manager)
        self.assertEqual(
            mclient.get(reverse("admin:applications_application_kpis")).status_code,
            200,
        )


class DraftDeleteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_app_types")
        call_command("create_roles")
        cls.exp = Company.objects.create(
            name_ar="شركة الاستكشاف", company_type=CompanyType.EXPLORATION
        )
        cls.exp_agr = Agreement.objects.create(company=cls.exp)
        cls.work_plan = ApplicationType.objects.get(
            company_type=CompanyType.EXPLORATION, model_name="AppWorkPlan"
        )

    def _scoped_client(self):
        user = _make_user("del_admin", is_staff=True)
        apply_role_to_user(user, "technical_data_entry", company_types=["exploration"])
        client = Client()
        client.force_login(user)
        return client, user

    def test_can_delete_own_draft(self):
        client, _ = self._scoped_client()
        app = Application.objects.create(agreement=self.exp_agr, app_type=self.work_plan)
        url = reverse("admin:applications_application_delete", args=[app.pk])
        self.assertEqual(client.get(url).status_code, 200)
        self.assertEqual(client.post(url, {"post": "yes"}).status_code, 302)
        self.assertFalse(Application.objects.filter(pk=app.pk).exists())

    def test_cannot_delete_submitted(self):
        client, user = self._scoped_client()
        app = Application.objects.create(agreement=self.exp_agr, app_type=self.work_plan)
        app.transition_status(ApplicationStatus.SUBMITTED, user)
        app.save()  # transition_status mutates in memory; persist for the admin
        url = reverse("admin:applications_application_delete", args=[app.pk])
        self.assertEqual(client.get(url).status_code, 403)
        self.assertTrue(Application.objects.filter(pk=app.pk).exists())

    def test_bulk_delete_disabled_for_non_superuser(self):
        client, _ = self._scoped_client()
        resp = client.get(reverse("admin:applications_application_changelist"))
        self.assertNotContains(resp, "delete_selected")


class FinancialDataEntryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("create_roles")
        cls.exp = Company.objects.create(
            name_ar="شركة الاستكشاف", company_type=CompanyType.EXPLORATION
        )
        cls.prod = Company.objects.create(
            name_ar="شركة الإنتاج", company_type=CompanyType.PRODUCTION
        )
        cls.exp_agr = Agreement.objects.create(company=cls.exp)
        cls.prod_agr = Agreement.objects.create(company=cls.prod)
        # state+locality so the read-only agreement page exercises the locality
        # branch of AgreementAdminForm.
        state = State.objects.create(name_ar="ولاية مالية", name_en="Fin State")
        locality = Locality.objects.create(name_ar="محلية مالية", state=state)
        cls.exp_agr.state = state
        cls.exp_agr.locality = locality
        cls.exp_agr.save()
        FinancialPosition.objects.create(agreement=cls.exp_agr, debt="100")
        FinancialPosition.objects.create(agreement=cls.prod_agr, debt="200")

    def _fin_client(self):
        user = _make_user("fin_admin", is_staff=True)
        apply_role_to_user(user, "financial_data_entry")
        client = Client()
        client.force_login(user)
        return client, user

    def test_sees_and_edits_financial_data_for_all_companies(self):
        client, _ = self._fin_client()
        resp = client.get(reverse("admin:companies_financialposition_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "شركة الاستكشاف")
        self.assertContains(resp, "شركة الإنتاج")

        # fresh agreement without an existing OneToOne position
        third = Company.objects.create(
            name_ar="شركة ثالثة", company_type=CompanyType.SMALL
        )
        third_agr = Agreement.objects.create(company=third)
        resp = client.post(
            reverse("admin:companies_financialposition_add"),
            {"agreement": third_agr.pk, "debt": "500", "_save": "حفظ"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            FinancialPosition.objects.filter(agreement=third_agr, debt="500").exists()
        )

    def test_no_application_or_catalog_access(self):
        client, _ = self._fin_client()
        self.assertEqual(
            client.get(reverse("admin:applications_application_changelist")).status_code,
            403,
        )
        self.assertEqual(
            client.get(reverse("admin:applications_applicationtype_changelist")).status_code,
            403,
        )

    def test_view_only_agreement_page_renders(self):
        # financial_data_entry has view-only Agreement access.
        client, _ = self._fin_client()
        resp = client.get(
            reverse("admin:companies_agreement_change", args=[self.exp_agr.pk])
        )
        self.assertEqual(resp.status_code, 200)

    def test_agreement_autocomplete_in_financial_forms(self):
        client, _ = self._fin_client()
        for url_name in (
            "admin:companies_financialposition_add",
            "admin:companies_financialevent_add",
        ):
            resp = client.get(reverse(url_name))
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, "admin-autocomplete")
            self.assertContains(resp, "/admin/autocomplete/")


class AssignmentPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("create_roles")

    def _admin_client(self):
        admin = User.objects.create_superuser("su_roles", "r@r.com", "x")
        client = Client()
        client.force_login(admin)
        return client

    def test_assignment_page_assigns_and_clears(self):
        client = self._admin_client()
        user = _make_user("assignee")
        url = reverse("admin:roles_role_assignment")

        resp = client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "توزيع الأدوار")

        resp = client.post(
            url,
            {
                f"role_{user.pk}": "technical_data_entry",
                f"company_types_{user.pk}": ["exploration", "production"],
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(user.groups.filter(name="technical_data_entry").exists())
        self.assertEqual(
            list(StaffProfile.objects.get(user=user).company_types),
            ["exploration", "production"],
        )

        # unassign clears role + types
        client.post(url, {f"role_{user.pk}": ""})
        self.assertFalse(user.groups.filter(name__in=MANAGED_ROLES).exists())
        self.assertEqual(list(StaffProfile.objects.get(user=user).company_types), [])
