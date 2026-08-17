import json

from django import forms
from django.contrib import admin
from django.utils.html import format_html

from applications.models import Application

from auditlog.models import LogEntry

from .models import (
    Agreement,
    Block,
    Company,
    CompanyType,
    ContractType,
    FinancialEvent,
    FinancialPosition,
    LegalEvent,
    Locality,
    Mineral,
    Nationality,
    State,
    TechnicalEvent,
    TechnicalPosition,
    contract_type_options,
)


class AuditLogProxy(LogEntry):
    """Arabic-labelled read-only view of the audit trail."""

    class Meta:
        proxy = True
        verbose_name = "سجل التدقيق"
        verbose_name_plural = "سجل التدقيق"


@admin.register(AuditLogProxy)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "content_type", "object_repr", "action", "actor", "remote_addr")
    list_filter = ("content_type", "action", "actor")
    search_fields = ("object_repr", "object_pk")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


from .audit_setup import audit_history_html


class AgreementAdminForm(forms.ModelForm):
    """Filters contract_type to the company type's allowed options."""

    class Meta:
        model = Agreement
        fields = "__all__"

    class Media:
        js = ("js/agreement_contract_type.js",)

    def __init__(self, *args, **kwargs):
        # Inline formsets pass the parent company via form_kwargs.
        parent_company = kwargs.pop("parent_company", None)
        super().__init__(*args, **kwargs)
        company = None
        if self.instance and self.instance.company_id:
            company = self.instance.company
        elif parent_company is not None:
            company = parent_company
        elif self.data and self.data.get("company"):
            try:
                company = Company.objects.get(pk=self.data.get("company"))
            except (Company.DoesNotExist, ValueError, TypeError):
                pass
        if company is not None:
            allowed = contract_type_options(company.company_type)
            if allowed:
                self.fields["contract_type"].choices = [
                    (value, label)
                    for value, label in ContractType.choices
                    if value in allowed
                ]
        # Data for the dependent-dropdown JS (company -> type -> allowed contracts).
        contract_map = {
            ct_value: [
                value for value, _ in ContractType.choices
                if value in contract_type_options(ct_value)
            ]
            for ct_value, _ in CompanyType.choices
        }
        self.fields["contract_type"].widget.attrs["data-contract-map"] = json.dumps(contract_map)
        # The RelatedFieldWidgetWrapper passes the raw BoundField attrs to the
        # inner widget, so data attrs must live on BOTH the wrapper and the inner
        # widget to survive rendering. The company field is absent on inline
        # forms (the parent FK is handled by the inline formset), so guard it.
        companies_json = json.dumps({str(c.pk): c.company_type for c in Company.objects.all()})
        company_field = self.fields.get("company")
        if company_field is not None:
            company_widget = company_field.widget
            company_widget.attrs["data-companies"] = companies_json
            if hasattr(company_widget, "widget"):
                company_widget.widget.attrs["data-companies"] = companies_json
        # Locality is a lookup keyed by state: filter options to the selected state.
        state = None
        if self.instance and self.instance.state_id:
            state = self.instance.state
        elif self.data and self.data.get("state"):
            try:
                state = State.objects.get(pk=self.data.get("state"))
            except (State.DoesNotExist, ValueError, TypeError):
                pass
        if state is not None:
            self.fields["locality"].queryset = Locality.objects.filter(state=state)
        locality_map_json = json.dumps({loc.pk: loc.state_id for loc in Locality.objects.all()})
        locality_field = self.fields.get("locality")
        if locality_field is not None:
            locality_widget = locality_field.widget
            locality_widget.attrs["data-locality-map"] = locality_map_json
            if hasattr(locality_widget, "widget"):
                locality_widget.widget.attrs["data-locality-map"] = locality_map_json


@admin.register(State)
class StateAdmin(admin.ModelAdmin):
    list_display = ("name_ar", "name_en")
    search_fields = ("name_ar", "name_en")


@admin.register(Mineral)
class MineralAdmin(admin.ModelAdmin):
    list_display = ("name_ar",)
    search_fields = ("name_ar",)


@admin.register(Block)
class BlockAdmin(admin.ModelAdmin):
    list_display = ("code", "name")
    search_fields = ("code", "name")


@admin.register(Locality)
class LocalityAdmin(admin.ModelAdmin):
    list_display = ("name_ar", "state")
    list_filter = ("state",)
    search_fields = ("name_ar", "state__name_ar")


@admin.register(Nationality)
class NationalityAdmin(admin.ModelAdmin):
    list_display = ("code", "name_ar")
    search_fields = ("code", "name_ar")


class AgreementInline(admin.TabularInline):
    model = Agreement
    form = AgreementAdminForm
    extra = 1
    fields = (
        "agreement_no",
        "contract_type",
        "block",
        "state",
        "locality",
        "start_date",
        "end_date",
        "signing_date",
        "current_area_km2",
        "validity_status",
    )

    def get_formset(self, request, obj=None, **kwargs):
        FormSet = super().get_formset(request, obj, **kwargs)

        class CompanyAwareFormSet(FormSet):
            def __init__(self, *args, **kwargs):
                kwargs.setdefault("form_kwargs", {})
                kwargs["form_kwargs"].setdefault("parent_company", obj)
                super().__init__(*args, **kwargs)

        return CompanyAwareFormSet


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name_ar", "company_type", "general_status", "registration_no", "updated_at")
    list_filter = ("company_type", "general_status")
    search_fields = ("name_ar", "name_en", "registration_no", "phone")
    inlines = [AgreementInline]
    filter_horizontal = ("nationalities",)
    readonly_fields = ("audit_history",)

    def audit_history(self, obj):
        return audit_history_html(obj)

    audit_history.short_description = "سجل التعديلات"
    fieldsets = (
        ("بيانات الشركة", {"fields": ("name_ar", "name_en", "company_type", "registration_no")}),
        ("التواصل", {"fields": ("address", "phone", "email", "website", "nationalities")}),
        (
            "الإدارة والتمثيل",
            {"fields": ("manager_name", "manager_phone", "rep_name", "rep_phone")},
        ),
        ("حالة الشركة العامة (رابعا)", {"fields": ("general_status", "notes")}),
        ("سجل التعديلات", {"fields": ("audit_history",)}),
    )


class FinancialPositionInline(admin.StackedInline):
    model = FinancialPosition
    max_num = 1
    can_delete = False


class TechnicalPositionInline(admin.StackedInline):
    model = TechnicalPosition
    max_num = 1
    can_delete = False


class LegalEventInline(admin.TabularInline):
    model = LegalEvent
    extra = 0
    fields = ("event_type", "date", "description", "application")
    readonly_fields = ("application",)


class FinancialEventInline(admin.TabularInline):
    model = FinancialEvent
    extra = 0
    fields = ("event_type", "date", "amount", "description", "application")
    readonly_fields = ("application",)


class TechnicalEventInline(admin.TabularInline):
    model = TechnicalEvent
    extra = 0
    fields = ("event_type", "category", "date", "description", "application")
    readonly_fields = ("application",)


class ApplicationInline(admin.TabularInline):
    model = Application
    extra = 0
    can_delete = False
    fields = ("app_type", "status", "submitted_at", "reviewed_at")
    readonly_fields = ("app_type", "status", "submitted_at", "reviewed_at")


@admin.register(Agreement)
class AgreementAdmin(admin.ModelAdmin):
    form = AgreementAdminForm
    filter_horizontal = ("minerals",)
    readonly_fields = ("audit_history",)

    def audit_history(self, obj):
        return audit_history_html(obj)

    audit_history.short_description = "سجل التعديلات"
    list_display = (
        "company",
        "agreement_no",
        "contract_type",
        "block",
        "state",
        "start_date",
        "end_date",
        "validity_status",
    )
    list_filter = ("company__company_type", "contract_type", "validity_status", "state")
    search_fields = ("company__name_ar", "company__name_en", "block__code", "area_name")
    inlines = [
        FinancialPositionInline,
        TechnicalPositionInline,
        LegalEventInline,
        FinancialEventInline,
        TechnicalEventInline,
        ApplicationInline,
    ]


@admin.register(FinancialPosition)
class FinancialPositionAdmin(admin.ModelAdmin):
    list_display = ("agreement", "debt", "current_claim", "balance", "satisfied")
    search_fields = ("agreement__company__name_ar", "agreement__company__name_en")


@admin.register(TechnicalPosition)
class TechnicalPositionAdmin(admin.ModelAdmin):
    list_display = ("agreement", "work_program", "processing_method")
    search_fields = ("agreement__company__name_ar", "agreement__company__name_en")


@admin.register(LegalEvent)
class LegalEventAdmin(admin.ModelAdmin):
    list_display = ("agreement", "event_type", "date", "application")
    list_filter = ("event_type",)
    search_fields = ("agreement__company__name_ar", "description")


@admin.register(FinancialEvent)
class FinancialEventAdmin(admin.ModelAdmin):
    list_display = ("agreement", "event_type", "date", "amount", "application")
    list_filter = ("event_type",)
    search_fields = ("agreement__company__name_ar", "description")


@admin.register(TechnicalEvent)
class TechnicalEventAdmin(admin.ModelAdmin):
    list_display = ("agreement", "event_type", "category", "date", "application")
    list_filter = ("event_type",)
    search_fields = ("agreement__company__name_ar", "description", "category")
