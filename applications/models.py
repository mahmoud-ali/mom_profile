from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from companies.models import (
    Agreement,
    CompanyType,
    FinancialEventType,
    LegalEventType,
    TechnicalEventType,
)

from .workflow import (
    ApplicationStatus,
    Recommendation,
    WORKFLOW_PERMISSIONS,
    allowed_next_statuses_for,
    anchor_creation,
    can_transition,
    transition_application,
)


class EventCategory(models.TextChoices):
    LEGAL = "legal", "قانوني"
    FINANCIAL = "financial", "مالي"
    TECHNICAL = "technical", "فني"


# Valid event_label values per event_category (choice-based).
EVENT_LABELS_BY_CATEGORY = {
    EventCategory.LEGAL: LegalEventType.choices,
    EventCategory.FINANCIAL: FinancialEventType.choices,
    EventCategory.TECHNICAL: TechnicalEventType.choices,
}


def valid_event_labels(category):
    """Return the (value, label) choices for an event_category."""
    return EVENT_LABELS_BY_CATEGORY.get(category, [])


class WorkingHoursSchedule(models.Model):
    """Working hours per date range — supports seasonal schedules.

    working_weekdays: list of weekday ints (Mon=0 .. Sun=6); empty uses the
    business_time default (Sunday-Thursday).
    """

    name = models.CharField("الاسم", max_length=100)
    start_date = models.DateField("بداية السريان")
    end_date = models.DateField("نهاية السريان", null=True, blank=True)
    work_start = models.TimeField("بداية الدوام")
    work_end = models.TimeField("نهاية الدوام")
    working_weekdays = models.JSONField("أيام العمل", default=list, blank=True)

    class Meta:
        verbose_name = "جدول ساعات العمل"
        verbose_name_plural = "جداول ساعات العمل"
        ordering = ["start_date"]

    def clean(self):
        if self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "نهاية السريان قبل بدايته."})

    def __str__(self):
        return f"{self.name} ({self.start_date} - {self.end_date or '…'})"


def _clean_list(value):
    """Split a CSV cell on Arabic/regular commas, strip junk, drop empties."""
    import re

    if not value:
        return []
    value = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", value)
    parts = re.split(r"[،,]", value)
    out = []
    for p in parts:
        p = p.strip().strip("‏‎").strip()
        if p:
            out.append(p)
    return out


class ApplicationType(models.Model):
    company_type = models.CharField("نوع الشركة", max_length=20, choices=CompanyType.choices)
    model_name = models.CharField("اسم الموديل", max_length=100)
    verbose_name = models.CharField("الاسم الإنجليزي", max_length=200)
    arabic_name = models.CharField("الاسم العربي", max_length=200)
    attachments = models.JSONField("المرفقات", default=list, blank=True)
    form_fields = models.JSONField("حقول الاستمارة", default=list, blank=True)
    field_specs = models.JSONField(
        "مواصفات الحقول",
        default=dict,
        blank=True,
        help_text=(
            'مثال: {"الغرض من التصديق": {"type": "select", "choices": ["خيار1", "خيار2"]}} — '
            "الأنواع المتاحة: select / integer / float / decimal؛ "
            '"required": true يجعل الحقل إلزامياً (افتراضياً اختياري)'
        ),
    )
    detail_models = models.JSONField("نماذج التفاصيل", default=list, blank=True)
    detail_fields = models.JSONField("حقول التفاصيل", default=list, blank=True)
    detail_specs = models.JSONField(
        "مواصفات حقول التفاصيل",
        default=dict,
        blank=True,
        help_text=(
            'مثال: {"وزن العينة": {"type": "float"}} — '
            "الأنواع المتاحة: select / integer / float / decimal؛ "
            '"required": true يجعل العمود إلزامياً'
        ),
    )
    contract_types = models.JSONField(
        "أنواع العقود المسموحة",
        default=list,
        blank=True,
        help_text='أنواع العقد المسموح لهذا الطلب، مثال: ["mining", "two_minerals"] — فارغ = غير مقيد',
    )
    field_layout = models.JSONField(
        "تخطيط الحقول",
        default=dict,
        blank=True,
        help_text=(
            'مثال: {"columns": 2, "attachment_columns": 2, '
            '"groups": [{"title": "بيانات", "fields": ["حقل1", "حقل2"]}]} — '
            "columns: عدد أعمدة الحقول، attachment_columns: عدد أعمدة المرفقات، "
            "groups: مجموعات بعناوين (الترتيب حسب المجموعات)"
        ),
    )
    event_category = models.CharField(
        "تصنيف الحدث التاريخي",
        max_length=20,
        choices=EventCategory.choices,
        null=True,
        blank=True,
    )
    event_label = models.CharField("نوع الحدث التاريخي", max_length=100, blank=True)

    class Meta:
        verbose_name = "نوع الطلب / الإجراء"
        verbose_name_plural = "أنواع الطلبات / الإجراءات"
        ordering = ["company_type", "arabic_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company_type", "model_name"], name="unique_app_type_per_company_type"
            ),
        ]

    def __str__(self):
        return f"{self.arabic_name} ({self.get_company_type_display()})"

    def clean(self):
        if self.event_category and self.event_label:
            allowed = [value for value, _label in valid_event_labels(self.event_category)]
            if allowed and self.event_label not in allowed:
                raise ValidationError(
                    {"event_label": "نوع الحدث التاريخي لا يتوافق مع التصنيف المحدد."}
                )


class Application(models.Model):
    """A submitted procedure request for a company.

    The status machine (statuses, transitions, permissions and side effects)
    lives in applications/workflow.py; this model keeps thin wrappers so the
    workflow is centralized in one module.
    """

    class Meta:
        verbose_name = "طلب"
        verbose_name_plural = "الطلبات"
        ordering = ["-created_at"]
        permissions = WORKFLOW_PERMISSIONS

    agreement = models.ForeignKey(
        Agreement,
        verbose_name="الاتفاقية / العقد",
        on_delete=models.PROTECT,
        related_name="applications",
    )
    app_type = models.ForeignKey(
        ApplicationType,
        verbose_name="نوع الطلب",
        on_delete=models.PROTECT,
        related_name="applications",
    )
    status = models.CharField(
        "الحالة",
        max_length=32,
        choices=ApplicationStatus.choices,
        default=ApplicationStatus.DRAFT,
    )
    submitted_at = models.DateTimeField("تاريخ التأكيد", null=True, blank=True)
    reviewed_at = models.DateTimeField("تاريخ المراجعة", null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="مؤكّد الطلب",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submitted_applications",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="المراجع",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_applications",
    )
    notes = models.TextField("ملاحظات", blank=True)
    committee_recommendation = models.CharField(
        "توصية اللجنة",
        max_length=20,
        choices=Recommendation.choices,
        null=True,
        blank=True,
    )
    committee_recommendation_notes = models.TextField("ملاحظات توصية اللجنة", blank=True)
    committee_recommended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="مقدم توصية اللجنة",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="committee_recommended_applications",
    )
    committee_recommended_at = models.DateTimeField("تاريخ توصية اللجنة", null=True, blank=True)
    undersecretary_recommendation = models.CharField(
        "توصية وكيل الوزارة",
        max_length=20,
        choices=Recommendation.choices,
        null=True,
        blank=True,
    )
    undersecretary_recommendation_notes = models.TextField(
        "ملاحظات توصية وكيل الوزارة", blank=True
    )
    undersecretary_recommended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="مقدم توصية وكيل الوزارة",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="undersecretary_recommended_applications",
    )
    undersecretary_recommended_at = models.DateTimeField(
        "تاريخ توصية وكيل الوزارة", null=True, blank=True
    )
    minister_decision = models.TextField("قرار الوزير", blank=True)
    created_at = models.DateTimeField("تاريخ الإنشاء", auto_now_add=True)
    updated_at = models.DateTimeField("آخر تحديث", auto_now=True)

    def __str__(self):
        return f"{self.agreement.company.name_ar} — {self.app_type.arabic_name} ({self.get_status_display()})"

    def save(self, *args, **kwargs):
        creating = self.pk is None
        super().save(*args, **kwargs)
        if creating:
            # Anchor the timeline: the first entry (None -> draft) at creation.
            anchor_creation(self)

    def clean(self):
        if self.agreement_id and self.app_type_id:
            app_type = self.app_type
            if app_type.company_type != self.agreement.company.company_type:
                raise ValidationError(
                    {"app_type": "نوع الطلب لا يتوافق مع نوع شركة الاتفاقية (يجب أن ينتمي لكتالوج نفس النوع)."}
                )
            allowed_contracts = app_type.contract_types or []
            if allowed_contracts and self.agreement.contract_type not in allowed_contracts:
                raise ValidationError(
                    {"app_type": "نوع الطلب غير مسموح به لنوع عقد الاتفاقية الحالي."}
                )

    # -------------------------------------------------------- workflow wrappers
    # The state machine itself lives in applications/workflow.py; these thin
    # wrappers keep a stable API for the admin, forms and tests.

    def allowed_next_statuses(self, user):
        """Statuses this user may move the application to (excluding the current one)."""
        return allowed_next_statuses_for(self.status, user)

    def can_transition_to(self, new_status, user):
        return can_transition(self.status, new_status, user)

    def transition_status(self, new_status, user, recommendation=None, notes="", minister_decision=""):
        """Move the application to new_status, enforcing the role-based workflow.

        Raises PermissionDenied on an invalid transition, a missing permission,
        or a missing mandatory entry (recommendation decision / minister's
        decision) when entering the corresponding stage. Logs a historical
        event when the application is approved.
        """
        transition_application(
            self,
            new_status,
            user,
            recommendation=recommendation,
            notes=notes,
            minister_decision=minister_decision,
        )


class ApplicationTransition(models.Model):
    """One workflow step: who moved the application between which statuses, when.

    The first entry (None -> draft) is anchored at creation; stage durations are
    computed from consecutive timestamps per application.
    """

    class Meta:
        verbose_name = "تحول حالة الطلب"
        verbose_name_plural = "تحولات حالة الطلبات"
        ordering = ["application", "timestamp", "id"]

    application = models.ForeignKey(
        Application,
        verbose_name="الطلب",
        on_delete=models.CASCADE,
        related_name="transitions",
    )
    from_status = models.CharField(
        "من الحالة",
        max_length=32,
        choices=ApplicationStatus.choices,
        null=True,
        blank=True,
    )
    to_status = models.CharField("إلى الحالة", max_length=32, choices=ApplicationStatus.choices)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="المستخدم",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    timestamp = models.DateTimeField("التاريخ والوقت", auto_now_add=True)
    duration_seconds = models.PositiveIntegerField(
        "المدة في الحالة (ساعات العمل)",
        null=True,
        blank=True,
        help_text=(
            "الوقت (بالثواني) الذي قضاه الطلب في from_status قبل هذا التحول، "
            "محسوباً على أساس ساعات العمل فقط (الأحد-الخميس 08:00-16:00) — فارغ للإنشاء."
        ),
    )

    def __str__(self):
        label = "الإنشاء" if self.from_status is None else self.get_from_status_display()
        return (
            f"{self.application} — {label} ← {self.get_to_status_display()}"
            f" ({self.timestamp:%Y-%m-%d %H:%M})"
        )


class ApplicationAttachment(models.Model):
    application = models.ForeignKey(
        Application,
        verbose_name="الطلب",
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    label = models.CharField("اسم المرفق", max_length=200, blank=True)
    file = models.FileField("الملف", upload_to="attachments/%Y/%m/")

    class Meta:
        verbose_name = "مرفق"
        verbose_name_plural = "المرفقات"
        ordering = ["id"]

    def __str__(self):
        return f"{self.label or self.file.name} — {self.application}"


class ApplicationField(models.Model):
    application = models.ForeignKey(
        Application,
        verbose_name="الطلب",
        on_delete=models.CASCADE,
        related_name="fields",
    )
    label = models.CharField("الحقل", max_length=200)
    value = models.TextField("القيمة", blank=True)

    class Meta:
        verbose_name = "حقل استمارة"
        verbose_name_plural = "حقول الاستمارة"
        ordering = ["id"]

    def __str__(self):
        return f"{self.label}: {self.value[:50]}"


class ApplicationDetail(models.Model):
    application = models.ForeignKey(
        Application,
        verbose_name="الطلب",
        on_delete=models.CASCADE,
        related_name="details",
    )
    category = models.CharField(
        "التصنيف", max_length=200, blank=True, help_text="مثال: معدات المناجم"
    )
    order = models.PositiveIntegerField("الترتيب", default=0)

    class Meta:
        verbose_name = "بند تفصيلي"
        verbose_name_plural = "البنود التفصيلية"
        ordering = ["application", "order", "id"]

    def __str__(self):
        prefix = f"{self.category}: " if self.category else ""
        return f"{prefix}{self.application} (#{self.pk})"


class ApplicationDetailField(models.Model):
    detail = models.ForeignKey(
        ApplicationDetail,
        verbose_name="البند التفصيلي",
        on_delete=models.CASCADE,
        related_name="fields",
    )
    label = models.CharField("الحقل", max_length=200)
    value = models.TextField("القيمة", blank=True)

    class Meta:
        verbose_name = "خلية تفصيلية"
        verbose_name_plural = "الخلايا التفصيلية"
        ordering = ["id"]

    def __str__(self):
        return f"{self.label}: {self.value[:50]}"
