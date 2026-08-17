from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models
from django.utils import timezone

from companies.models import (
    Agreement,
    CompanyType,
    FinancialEvent,
    FinancialEventType,
    LegalEvent,
    LegalEventType,
    TechnicalEvent,
    TechnicalEventType,
)


class ApplicationStatus(models.TextChoices):
    DRAFT = "draft", "مسودة"
    SUBMITTED = "submitted", "مؤكد"
    UNDER_PROCESSING = "under_processing", "قيد المعالجة"
    APPROVED = "approved", "مجاز"
    REJECTED = "rejected", "مرفوض"


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

    Status workflow (enforced by transition_status):
      DRAFT -> SUBMITTED                       (data entry / manager)
      SUBMITTED -> UNDER_PROCESSING            (manager)
      UNDER_PROCESSING -> APPROVED | REJECTED  (manager)
      REJECTED -> DRAFT                        (resubmit)
    """

    # Allowed transitions: current -> {next}
    TRANSITIONS = {
        ApplicationStatus.DRAFT: {ApplicationStatus.SUBMITTED},
        ApplicationStatus.SUBMITTED: {ApplicationStatus.UNDER_PROCESSING},
        ApplicationStatus.UNDER_PROCESSING: {
            ApplicationStatus.APPROVED,
            ApplicationStatus.REJECTED,
        },
        ApplicationStatus.REJECTED: {ApplicationStatus.DRAFT},
    }

    class Meta:
        verbose_name = "طلب"
        verbose_name_plural = "الطلبات"
        ordering = ["-created_at"]
        permissions = [
            ("can_submit", "يمكنه تأكيد الطلبات"),
            ("can_review", "يمكنه مراجعة الطلبات (قيد المعالجة)"),
            ("can_approve", "يمكنه اعتماد الطلبات"),
            ("can_reject", "يمكنه رفض الطلبات"),
        ]

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
        max_length=20,
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
    created_at = models.DateTimeField("تاريخ الإنشاء", auto_now_add=True)
    updated_at = models.DateTimeField("آخر تحديث", auto_now=True)

    def __str__(self):
        return f"{self.agreement.company.name_ar} — {self.app_type.arabic_name} ({self.get_status_display()})"

    def save(self, *args, **kwargs):
        creating = self.pk is None
        super().save(*args, **kwargs)
        if creating:
            # Anchor the timeline: the first entry (None -> draft) at creation.
            ApplicationTransition.objects.create(
                application=self, from_status=None, to_status=self.status
            )

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

    # ------------------------------------------------------------------ workflow

    def _perm_for(self, new_status):
        return {
            ApplicationStatus.SUBMITTED: "applications.can_submit",
            ApplicationStatus.UNDER_PROCESSING: "applications.can_review",
            ApplicationStatus.APPROVED: "applications.can_approve",
            ApplicationStatus.REJECTED: "applications.can_reject",
        }.get(new_status)

    def allowed_next_statuses(self, user):
        """Statuses this user may move the application to (excluding the current one)."""
        allowed = self.TRANSITIONS.get(self.status, set())
        result = set()
        for target in allowed:
            if user is not None and user.is_authenticated and user.is_superuser:
                result.add(target)
                continue
            if user is not None and user.is_authenticated:
                perm = self._perm_for(target)
                if perm is None or user.has_perm(perm):
                    result.add(target)
            elif target == ApplicationStatus.DRAFT:
                result.add(target)
        return result

    def can_transition_to(self, new_status, user):
        return new_status in self.allowed_next_statuses(user)

    def transition_status(self, new_status, user):
        """Move the application to new_status, enforcing role-based workflow.

        Raises PermissionDenied on an invalid transition or missing permission.
        Logs a historical event when the application is approved.
        """
        if new_status == self.status:
            return
        if new_status not in self.TRANSITIONS.get(self.status, set()):
            raise PermissionDenied(
                f"لا يمكن الانتقال من «{self.get_status_display()}» إلى "
                f"«{dict(ApplicationStatus.choices).get(new_status, new_status)}»."
            )
        if user is not None and not user.is_superuser:
            perm = self._perm_for(new_status)
            if perm and not user.has_perm(perm):
                raise PermissionDenied(f"ليست لديك صلاحية تنفيذ هذا الإجراء ({perm}).")
        old_status = self.status
        self.status = new_status
        now = timezone.now()
        if new_status == ApplicationStatus.SUBMITTED:
            self.submitted_at = now
            self.submitted_by = user
        if new_status in (ApplicationStatus.APPROVED, ApplicationStatus.REJECTED):
            self.reviewed_at = now
            self.reviewed_by = user
        if new_status == ApplicationStatus.APPROVED:
            self.log_approved_event()
        from .business_time import working_seconds_between

        previous = self.transitions.order_by("-timestamp", "-id").first()
        duration_seconds = None
        if previous is not None and previous.timestamp is not None:
            duration_seconds = working_seconds_between(previous.timestamp, timezone.now())
        ApplicationTransition.objects.create(
            application=self,
            from_status=old_status,
            to_status=new_status,
            user=user,
            duration_seconds=duration_seconds,
        )

    # ---------------------------------------------------------- approval -> event

    EVENT_MODEL_MAP = {
        EventCategory.LEGAL: LegalEvent,
        EventCategory.FINANCIAL: FinancialEvent,
        EventCategory.TECHNICAL: TechnicalEvent,
    }

    def log_approved_event(self):
        """Record one historical event (legal/financial/technical) for an approved
        application. Idempotent: skips when an event is already linked to it."""
        at = self.app_type
        if not at.event_category or not at.event_label:
            return
        model_cls = self.EVENT_MODEL_MAP.get(at.event_category)
        if model_cls is None:
            return
        if model_cls.objects.filter(application=self).exists():
            return
        label = at.event_label
        # Fall back to "other" when the label is not a valid choice value.
        valid_values = [v for v, _ in model_cls._meta.get_field("event_type").choices]
        if label not in valid_values:
            label = "other"
        description = f"طلب: {at.arabic_name}"
        if self.notes:
            description += f" — {self.notes}"
        model_cls.objects.create(
            agreement=self.agreement,
            event_type=label,
            date=timezone.localdate(),
            description=description,
            application=self,
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
        max_length=20,
        choices=ApplicationStatus.choices,
        null=True,
        blank=True,
    )
    to_status = models.CharField("إلى الحالة", max_length=20, choices=ApplicationStatus.choices)
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
