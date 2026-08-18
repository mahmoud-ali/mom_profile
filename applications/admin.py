import json

from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import path, reverse

from roles.roles import scoped_company_types
from roles.scoping import CompanyTypeScopedAdminMixin

from .forms import build_application_form
from .kpis import compute_kpis, format_duration
from .models import (
    Application,
    ApplicationAttachment,
    ApplicationDetail,
    ApplicationDetailField,
    ApplicationField,
    ApplicationStatus,
    ApplicationTransition,
    ApplicationType,
    EVENT_LABELS_BY_CATEGORY,
    WorkingHoursSchedule,
    valid_event_labels,
)

admin.site.title = "منصة خدمات الشركات"
admin.site.site_title = "منصة خدمات الشركات"
admin.site.site_header = "منصة خدمات الشركات"
admin.site.site_url = None

class ApplicationTypeForm(forms.ModelForm):
    """Renders event_label as a dropdown of the selected category's event types."""

    class Meta:
        model = ApplicationType
        fields = "__all__"

    class Media:
        js = ("js/application_type_event.js",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Always render ALL categories' options so the JS can rebuild the list
        # when the category changes (works on the add page too, where no
        # category is selected yet). "other" appears in every category; dedupe.
        all_choices = [
            (value, label)
            for labels in EVENT_LABELS_BY_CATEGORY.values()
            for value, label in labels
        ]
        choices = [("", "—")] + list(dict.fromkeys(all_choices))
        current = self.instance.event_label if self.instance else ""
        if current and current not in [value for value, _ in choices]:
            choices.append((current, current))  # keep legacy values editable
        self.fields["event_label"] = forms.ChoiceField(
            label="نوع الحدث التاريخي", required=False, choices=choices
        )
        self.fields["event_label"].widget.attrs["data-event-labels"] = json.dumps(
            {category_value: list(labels) for category_value, labels in EVENT_LABELS_BY_CATEGORY.items()}
        )


@admin.register(ApplicationType)
class ApplicationTypeAdmin(admin.ModelAdmin):
    form = ApplicationTypeForm
    list_display = (
        "arabic_name",
        "company_type",
        "model_name",
        "event_category",
        "event_label",
    )
    list_filter = ("company_type", "event_category")
    search_fields = ("arabic_name", "verbose_name", "model_name")
    fieldsets = (
        (None, {"fields": ("company_type", "model_name", "verbose_name", "arabic_name")}),
        ("الاستمارة", {"fields": ("attachments", "form_fields", "field_specs", "field_layout")}),
        ("التفاصيل", {"fields": ("detail_models", "detail_fields", "detail_specs")}),
        ("القيود", {"fields": ("contract_types",)}),
        ("الحدث التاريخي (عند الاعتماد)", {"fields": ("event_category", "event_label")}),
    )


class ApplicationDetailFieldInline(admin.TabularInline):
    model = ApplicationDetailField
    extra = 0


@admin.register(ApplicationDetail)
class ApplicationDetailAdmin(admin.ModelAdmin):
    list_display = ("application", "category", "order")
    search_fields = ("application__agreement__company__name_ar", "category")
    inlines = [ApplicationDetailFieldInline]


@admin.register(WorkingHoursSchedule)
class WorkingHoursScheduleAdmin(admin.ModelAdmin):
    list_display = ("name", "start_date", "end_date", "work_start", "work_end", "working_weekdays")


@admin.register(ApplicationTransition)
class ApplicationTransitionAdmin(admin.ModelAdmin):
    list_display = ("application", "from_status", "to_status", "user", "timestamp", "duration")
    list_filter = ("to_status", "user")
    search_fields = ("application__agreement__company__name_ar",)

    def duration(self, obj):
        return format_duration(obj.duration_seconds)

    duration.short_description = "المدة في الحالة"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Application)
class ApplicationAdmin(CompanyTypeScopedAdminMixin, admin.ModelAdmin):
    """Type-driven application entry.

    The change page renders the header plus the form generated from the
    ApplicationType (form fields, attachments, detail rows). Generic
    label/value editors are replaced by this generated form.
    """

    list_display = ("agreement", "app_type", "status", "submitted_at", "reviewed_by")
    list_filter = ("status", "agreement__company__company_type", "app_type")
    search_fields = ("agreement__company__name_ar", "agreement__company__name_en", "notes")
    readonly_fields = (
        "submitted_at",
        "reviewed_at",
        "submitted_by",
        "reviewed_by",
        "transition_history",
    )
    fields = ("agreement", "app_type", "status", "notes")

    def transition_history(self, obj):
        if obj is None or not obj.pk:
            return "—"
        labels = dict(ApplicationStatus.choices)
        rows = []
        for t in obj.transitions.select_related("user").order_by("timestamp", "id"):
            from_label = "الإنشاء" if t.from_status is None else labels.get(t.from_status, t.from_status)
            to_label = labels.get(t.to_status, t.to_status)
            actor = t.user.username if t.user else "—"
            rows.append(
                f"<tr><td>{t.timestamp:%Y-%m-%d %H:%M}</td>"
                f"<td>{from_label} ← {to_label}</td>"
                f"<td>{actor}</td>"
                f"<td>{format_duration(t.duration_seconds)}</td></tr>"
            )
        if not rows:
            return "—"
        body = "".join(rows)
        from django.utils.html import format_html

        return format_html(
            '<table class="transition-history" style="width:100%;border-collapse:collapse">'
            "<thead><tr><th>الوقت</th><th>التحول</th><th>المستخدم</th><th>المدة في الحالة</th></tr></thead>"
            f"<tbody>{body}</tbody></table>"
        )

    transition_history.short_description = "تحولات الحالة والمدة"

    # ------------------------------------------------ draft-only delete (scoped)

    def has_delete_permission(self, request, obj=None):
        """technical_data_entry may delete DRAFT applications of its own types only.

        obj is None (bulk "delete selected") is disabled for every non-superuser.
        """
        if request.user.is_superuser:
            return True
        if not request.user.has_perm("applications.delete_application"):
            return False
        if obj is None:
            return False  # no bulk delete for non-superusers
        if obj.status != ApplicationStatus.DRAFT:
            return False
        scope = scoped_company_types(request.user)
        return scope is None or obj.agreement.company.company_type in scope

    def delete_queryset(self, request, queryset):
        # Restrict bulk deletion to DRAFT (+ scope) for non-superusers only;
        # superusers keep the full bulk delete.
        if not request.user.is_superuser:
            from roles.roles import company_scope_q

            queryset = queryset.filter(status=ApplicationStatus.DRAFT)
            q = company_scope_q(request.user, Application)
            if q is not None:
                queryset = queryset.filter(q)
        return super().delete_queryset(request, queryset)

    def get_deleted_objects(self, objs, request):
        # Deleting a DRAFT discards the draft's own cascade rows (transitions,
        # attachments, fields, details). Django's default checks delete
        # permission on every related model, which would block the scoped
        # technical_data_entry role; only the Application delete permission
        # (already enforced by has_delete_permission) should matter here.
        deleted_objects, model_count, perms_needed, protected = super().get_deleted_objects(
            objs, request
        )
        if not request.user.is_superuser:
            perms_needed.clear()
        return deleted_objects, model_count, perms_needed, protected

    class Media:
        js = ("js/application_app_type.js",)

    # ------------------------------------------------------------ dynamic form

    def get_form(self, request, obj=None, change=False, **kwargs):
        app_type = obj.app_type if obj is not None else None
        form = build_application_form(app_type, instance=obj)
        form.current_user = request.user
        form.current_obj = obj
        # This custom form bypasses modelform_factory, so ModelAdmin's
        # formfield_for_foreignkey is never called for the agreement field;
        # apply the scoped-company-type filter here explicitly. The class is
        # freshly built per request, so mutating base_fields is safe.
        from companies.models import Agreement
        from roles.roles import company_scope_q

        q = company_scope_q(request.user, Agreement)
        if q is not None and "agreement" in form.base_fields:
            form.base_fields["agreement"].queryset = Agreement.objects.filter(q)
        return form

    def get_fieldsets(self, request, obj=None):
        # Header fields plus the readonly workflow/audit panels; dynamic
        # sections render via the custom template.
        fields = ("agreement", "app_type", "status", "notes")
        fields = (*fields, *self.get_readonly_fields(request, obj))
        return [(None, {"fields": fields})]

    def save_model(self, request, obj, form, change):
        new_status = form.cleaned_data.get("status", obj.status)
        if not change:
            if new_status != ApplicationStatus.DRAFT:
                raise PermissionDenied("يجب إنشاء الطلب كمسودة أولاً ثم تأكيده لاحقاً.")
            obj.status = ApplicationStatus.DRAFT
            obj.save()
        else:
            # The form already applied the posted status to the instance, so the
            # state machine must see the ORIGINAL status to detect the move.
            current_status = Application.objects.get(pk=obj.pk).status
            if request.POST.get("_submit_app") and current_status == ApplicationStatus.DRAFT:
                target = ApplicationStatus.SUBMITTED
            else:
                target = new_status
            if target != current_status:
                obj.status = current_status
                obj.transition_status(target, request.user)
            obj.save()
        self._save_dynamic(obj, form)

    def _save_dynamic(self, obj, form):
        at = obj.app_type
        # Form values -> ApplicationField rows (labels locked to the type).
        for i, label in enumerate(at.form_fields):
            value = form.cleaned_data.get(f"f_{i}") or ""
            ApplicationField.objects.update_or_create(
                application=obj, label=label, defaults={"value": value}
            )
        obj.fields.exclude(label__in=at.form_fields).delete()
        # Attachments -> ApplicationAttachment rows (labels locked to the type).
        for i, label in enumerate(at.attachments):
            uploaded = form.cleaned_data.get(f"a_{i}")
            if uploaded:
                ApplicationAttachment.objects.update_or_create(
                    application=obj, label=label, defaults={"file": uploaded}
                )
        # Detail rows -> ApplicationDetail + ApplicationDetailField (delete + recreate).
        row_keys = sorted({int(k.split("_")[1]) for k in form.fields if k.startswith("dt_")})
        obj.details.all().delete()
        for row in row_keys:
            category = form.cleaned_data.get(f"dt_{row}_cat") or ""
            cells = [
                (col_label, form.cleaned_data.get(f"dt_{row}_{c}") or "")
                for c, col_label in enumerate(at.detail_fields)
            ]
            if not category and not any(value for _, value in cells):
                continue  # empty row
            detail = ApplicationDetail.objects.create(application=obj, category=category, order=row)
            for col_label, value in cells:
                if value:
                    ApplicationDetailField.objects.create(detail=detail, label=col_label, value=value)

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        if obj is not None:
            form = context["adminform"].form
            at = obj.app_type
            context["app_type"] = at
            context["dynamic_fields"] = [(label, f"f_{i}") for i, label in enumerate(at.form_fields)]
            context["attachment_fields"] = [
                (label, f"a_{i}") for i, label in enumerate(at.attachments)
            ]
            # Layout: column count + titled groups (order follows the group lists).
            layout = at.field_layout or {}
            columns = max(int(layout.get("columns", 1) or 1), 1)
            names_by_label = dict(context["dynamic_fields"])
            grouped_labels = set()
            field_groups = []
            for group in layout.get("groups") or []:
                gfields = []
                for label in group.get("fields") or []:
                    name = names_by_label.get(label)
                    if name:
                        gfields.append((label, name))
                        grouped_labels.add(label)
                if gfields:
                    field_groups.append(
                        {
                            "title": group.get("title", ""),
                            "fields": gfields,
                            "columns": max(int(group.get("columns", columns) or columns), 1),
                        }
                    )
            context["field_groups"] = field_groups
            context["ungrouped_fields"] = [
                (label, name) for label, name in context["dynamic_fields"] if label not in grouped_labels
            ]
            context["field_columns"] = columns
            context["attachment_columns"] = max(
                int(layout.get("attachment_columns", 1) or 1), 1
            )
            context["existing_attachments"] = {a.label: a for a in obj.attachments.all()}
            context["detail_rows"] = []
            row_keys = sorted({int(k.split("_")[1]) for k in form.fields if k.startswith("dt_")})
            for row in row_keys:
                cols = []
                for c, col_label in enumerate(at.detail_fields):
                    key = f"dt_{row}_{c}"
                    if key in form.fields:
                        cols.append((col_label, form[key]))
                cat_field = form[f"dt_{row}_cat"] if f"dt_{row}_cat" in form.fields else None
                context["detail_rows"].append({"row": row, "cat": cat_field, "cols": cols})
            context["show_submit"] = (
                change
                and obj.status in (ApplicationStatus.DRAFT, ApplicationStatus.REJECTED)
                and request.user.has_perm("applications.can_submit")
            )
        return super().render_change_form(
            request, context, add=add, change=change, form_url=form_url, obj=obj
        )

    def response_add(self, request, obj, post_url_continue=None):
        # After creating the draft header, go to the change page to fill the form.
        url = reverse("admin:applications_application_change", args=[obj.pk])
        return HttpResponseRedirect(url)

    # ------------------------------------------------------- transition KPIs

    def get_urls(self):
        urls = super().get_urls()
        info = self.opts.app_label, self.opts.model_name
        custom = [
            path(
                "kpis/",
                self.admin_site.admin_view(self.kpis_view),
                name="%s_%s_kpis" % info,
            ),
        ]
        return custom + urls

    def kpis_view(self, request):
        user = request.user
        if not (user.is_superuser or user.has_perm("applications.can_review")):
            raise PermissionDenied("ليست لديك صلاحية الاطلاع على مؤشرات الأداء.")
        raw = request.GET.get("days", "")
        days = int(raw) if raw.isdigit() else None
        context = {
            **self.admin_site.each_context(request),
            "kpis": compute_kpis(days=days),
            "days": days,
            "title": "مؤشرات أداء الطلبات",
        }
        return render(request, "admin/applications/application/kpis.html", context)
