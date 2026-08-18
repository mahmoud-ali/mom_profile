import re

from django import forms

from companies.models import Agreement

from .models import (
    Application,
    ApplicationType,
)
from .workflow import (
    ApplicationStatus,
    ENTRY_FIELD_NAMES,
    FINAL_DECISION_STATUSES,
    entry_fields_for,
)

DETAIL_MAX_ROWS = 30

DATE_HINT = re.compile(r"(بداية|نهاية|تاريخ|التاريخ|مدة|مواعيد)")
DATE_COL_HINT = re.compile(r"(تاريخ|التاريخ|الزمن)")


def _make_field(label, date_hint=None, spec=None):
    """Build a field for a label. A spec of {"required": true} makes it required;
    everything stays optional by default."""
    required = bool((spec or {}).get("required"))
    if spec:
        spec_type = spec.get("type")
        if spec_type == "select":
            choices = [("", "—")] + [(c, c) for c in spec.get("choices", [])]
            return forms.ChoiceField(label=label, required=required, choices=choices)
        if spec_type == "integer":
            return forms.IntegerField(label=label, required=required)
        if spec_type == "float":
            return forms.FloatField(label=label, required=required)
        if spec_type == "decimal":
            return forms.DecimalField(
                label=label, required=required, max_digits=20, decimal_places=6
            )
    if bool((date_hint or DATE_HINT).search(label)):
        return forms.DateField(
            label=label, required=required, widget=forms.DateInput(attrs={"type": "date"})
        )
    if len(label) > 25:
        return forms.CharField(
            label=label, required=required, widget=forms.Textarea(attrs={"rows": 2})
        )
    return forms.CharField(label=label, required=required)


def _append_existing_select_value(field, value):
    """Keep a stored select value renderable even if it is no longer in the choices."""
    if value and isinstance(field, forms.ChoiceField) and value not in dict(field.choices):
        field.choices = field.choices + [(value, value)]


def _filter_status_choices(field, obj, user):
    """Limit the status dropdown to the transitions the current user may perform."""
    allowed = {obj.status} if obj and obj.pk else set()
    if obj and obj.pk and user is not None:
        allowed |= obj.allowed_next_statuses(user)
    if not allowed:
        allowed = {ApplicationStatus.DRAFT}
    field.choices = [(value, label) for value, label in ApplicationStatus.choices if value in allowed]


def build_application_form(app_type, instance=None):
    """Return a dynamic ModelForm for Application.

    - app_type is None -> header-only form (add page / no type chosen yet).
    - app_type given   -> header plus:
        * one field per ApplicationType.form_fields label (f_<i>),
        * one FileField per ApplicationType.attachments label (a_<i>),
        * detail rows (dt_<row>_cat + dt_<row>_<col>) with columns from
          ApplicationType.detail_fields and categories from detail_models.
    """
    form_fields = app_type.form_fields if app_type else []
    attachments = app_type.attachments if app_type else []
    detail_fields = app_type.detail_fields if app_type else []
    detail_models = app_type.detail_models if app_type else []
    field_specs = app_type.field_specs or {} if app_type else {}
    detail_specs = app_type.detail_specs or {} if app_type else {}

    existing_count = 0
    if instance is not None and instance.pk and instance.app_type_id:
        existing_count = instance.details.count()
    visible_rows = 0
    if detail_fields:
        visible_rows = min(existing_count + 1, DETAIL_MAX_ROWS)
        if visible_rows == 0:
            visible_rows = 1

    attrs = {
        "Meta": type(
            "Meta",
            (),
            {
                "model": Application,
                "fields": [
                    "agreement",
                    "app_type",
                    "status",
                    "notes",
                    "committee_recommendation",
                    "committee_recommendation_notes",
                    "undersecretary_recommendation",
                    "undersecretary_recommendation_notes",
                    "minister_decision",
                ],
            },
        )
    }

    for i, label in enumerate(form_fields):
        attrs[f"f_{i}"] = _make_field(label, spec=field_specs.get(label))

    for i, label in enumerate(attachments):
        attachment_required = bool((field_specs.get(label) or {}).get("required"))
        attrs[f"a_{i}"] = forms.FileField(label=label, required=attachment_required)

    for row in range(visible_rows):
        if detail_models:
            choices = [("", "—")] + [(c, c) for c in detail_models]
            attrs[f"dt_{row}_cat"] = forms.ChoiceField(label="التصنيف", choices=choices, required=False)
        for c, col_label in enumerate(detail_fields):
            attrs[f"dt_{row}_{c}"] = _make_field(
                col_label, date_hint=DATE_COL_HINT, spec=detail_specs.get(col_label)
            )

    def __init__(self, *args, **kwargs):
        forms.ModelForm.__init__(self, *args, **kwargs)
        user = getattr(self, "current_user", None)
        # Status is moved via dedicated transition buttons (the template posts
        # `_transition_to`), so the field itself renders as a hidden input that
        # carries the current status. The choices stay restricted to the
        # current status + the user's allowed moves (defense in depth).
        _filter_status_choices(
            self.fields["status"], self.instance, getattr(self, "current_user", None)
        )
        self.fields["status"].widget = forms.HiddenInput()
        # Once an application is submitted (any status past the draft) its data
        # is frozen: only the status field may move it through the workflow.
        readonly = (
            self.instance is not None
            and self.instance.pk
            and self.instance.status != ApplicationStatus.DRAFT
        )
        if readonly:
            for name in ("agreement", "notes"):
                if name in self.fields:
                    self.fields[name].disabled = True
            for key in list(self.fields):
                if key.startswith(("f_", "a_", "dt_")):
                    self.fields[key].disabled = True
        if self.instance and self.instance.pk:
            self.fields["app_type"].disabled = True
            values = {f.label: f.value for f in self.instance.fields.all()}
            for i, label in enumerate(form_fields):
                key = f"f_{i}"
                if key in self.fields:
                    current = values.get(label, "")
                    self.fields[key].initial = current
                    _append_existing_select_value(self.fields[key], current)
            if detail_fields:
                rows = list(self.instance.details.all().order_by("order", "id"))
                for row, detail in enumerate(rows[:visible_rows]):
                    cells = {f.label: f.value for f in detail.fields.all()}
                    cat_key = f"dt_{row}_cat"
                    if cat_key in self.fields:
                        self.fields[cat_key].initial = detail.category
                    for c, col_label in enumerate(detail_fields):
                        key = f"dt_{row}_{c}"
                        if key in self.fields:
                            current = cells.get(col_label, "")
                            self.fields[key].initial = current
                            _append_existing_select_value(self.fields[key], current)
        # Workflow data fields (committee/undersecretary recommendations and
        # the minister's decision) are shown ONLY at the stage where they must
        # be entered, and are mandatory there. Elsewhere they stay hidden and
        # disabled so their stored values are never wiped on save.
        status = self.instance.status if (self.instance and self.instance.pk) else None
        entry_fields = set(entry_fields_for(status, user))
        for name in ENTRY_FIELD_NAMES:
            visible = name in entry_fields
            self.fields[name].disabled = not visible
            self.fields[name].required = visible
        # Restrict the type list to the agreement's company type AND contract type.
        agreement = None
        if self.instance and self.instance.agreement_id:
            agreement = self.instance.agreement
        elif self.data and self.data.get("agreement"):
            try:
                agreement = Agreement.objects.get(pk=self.data.get("agreement"))
            except (Agreement.DoesNotExist, ValueError, TypeError):
                agreement = None
        if agreement is not None:
            types = ApplicationType.objects.filter(company_type=agreement.company.company_type)
            if agreement.contract_type:
                matching = [
                    t.pk
                    for t in types
                    if not t.contract_types or agreement.contract_type in t.contract_types
                ]
                types = types.filter(pk__in=matching)
            self.fields["app_type"].queryset = types
        # Dependent-dropdown data for the JS: agreement -> contract_type, and
        # app_type -> allowed contract types.
        import json

        agreement_field = self.fields.get("agreement")
        if agreement_field is not None:
            agreement_map = json.dumps(
                {str(a.pk): a.contract_type or "" for a in Agreement.objects.all()}
            )
            agreement_widget = agreement_field.widget
            agreement_widget.attrs["data-agreement-contracts"] = agreement_map
            if hasattr(agreement_widget, "widget"):
                agreement_widget.widget.attrs["data-agreement-contracts"] = agreement_map
        app_type_field = self.fields.get("app_type")
        if app_type_field is not None:
            app_type_map = json.dumps(
                {str(t.pk): list(t.contract_types or []) for t in ApplicationType.objects.all()}
            )
            app_type_widget = app_type_field.widget
            app_type_widget.attrs["data-apptype-contracts"] = app_type_map
            if hasattr(app_type_widget, "widget"):
                app_type_widget.widget.attrs["data-apptype-contracts"] = app_type_map

    def clean(self):
        # Explicit superclass call: `super()` has no __class__ cell in this
        # dynamically built class closure.
        cleaned = forms.ModelForm.clean(self)
        # The status field carries the current status; the intended move comes
        # from the transition button (`_transition_to`) when one was clicked.
        target = self.data.get("_transition_to") or cleaned.get("status")
        if (
            target == ApplicationStatus.COMMITTEE_RECOMMENDATION
            and "committee_recommendation" not in self.errors
            and not cleaned.get("committee_recommendation")
        ):
            self.add_error(
                "committee_recommendation",
                "يجب تحديد توصية اللجنة (موصى به أو غير موصى به).",
            )
        if (
            target == ApplicationStatus.UNDERSECRETARY_RECOMMENDATION
            and "undersecretary_recommendation" not in self.errors
            and not cleaned.get("undersecretary_recommendation")
        ):
            self.add_error(
                "undersecretary_recommendation",
                "يجب تحديد توصية وكيل الوزارة (موصى به أو غير موصى به).",
            )
        if (
            target in FINAL_DECISION_STATUSES
            and "minister_decision" not in self.errors
            and not cleaned.get("minister_decision")
        ):
            self.add_error("minister_decision", "يجب كتابة نص قرار الوزير.")
        return cleaned

    attrs["__init__"] = __init__
    attrs["clean"] = clean
    return type("DynamicApplicationForm", (forms.ModelForm,), attrs)
