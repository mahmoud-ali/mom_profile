import re

from django.core.exceptions import ValidationError
from django.db import models


class CompanyType(models.TextChoices):
    EXPLORATION = "exploration", "استكشاف"
    PRODUCTION = "production", "إنتاج"
    TAILINGS = "tailings", "مخلفات"
    SMALL = "small", "صغير"


class ContractType(models.TextChoices):
    CONCESSION = "concession", "إستكشاف"
    MINING = "mining", "عقد تعدين"
    TAILINGS = "tailings", "مخلفات"
    TWO_MINERALS = "two_minerals", "حجر معدنين"
    PREPROCESSED_TAILINGS = "preprocessed_tailings", "مخلفات معالجة مسبقا"
    SMALL = "small", "صغير"


class ValidityStatus(models.TextChoices):
    ACTIVE = "active", "ساري"
    EXPIRED = "expired", "منتهي"
    FROZEN = "frozen", "مجمد"
    CANCELLED = "cancelled", "ملغي"


class GeneralStatus(models.TextChoices):
    GREEN = "green", "اخضر"
    YELLOW = "yellow", "اصفر"
    RED = "red", "احمر"


# Which contract types (نوع العقد) each company type may hold.
COMPANY_TYPE_CONTRACT_TYPES = {
    CompanyType.EXPLORATION: (ContractType.CONCESSION,),
    CompanyType.PRODUCTION: (ContractType.MINING, ContractType.TWO_MINERALS),
    CompanyType.TAILINGS: (ContractType.TAILINGS, ContractType.PREPROCESSED_TAILINGS),
    CompanyType.SMALL: (ContractType.SMALL,),
}


def contract_type_options(company_type):
    """Allowed contract types for a company type (empty tuple if unknown)."""
    return COMPANY_TYPE_CONTRACT_TYPES.get(company_type, ())


# Mineral name normalization (spelling variants -> canonical data form).
MINERAL_NORMALIZE = {
    "الذهب": "ذهب",
    "النحاس": "نحاس",
    "الكروم": "كروم",
    "الحديد": "حديد",
    "المنجنيز": "منجنيز",
    "الرخام": "رخام",
    "الجبس": "جبس",
    "جبص": "جبس",
    "الملح": "ملح",
    "الفضة": "فضة",
}


def normalize_mineral(name):
    """Trim a mineral name and map spelling variants to the canonical form."""
    name = (name or "").strip()
    return MINERAL_NORMALIZE.get(name, name)


def split_minerals(value):
    """Split a CSV المعدن cell (Arabic-comma separated) into normalized names."""
    return [
        normalize_mineral(part)
        for part in re.split(r"[،,]", value or "")
        if part.strip()
    ]


# Nationality lookup: code -> Arabic name (from the provided map).
NATIONALITY_NAMES = {
    "1": "سودانية",
    "2": "مغربية",
    "4": "قطرية",
    "9": "تركية",
    "10": "روسية",
    "11": "أردنية",
    "12": "ارمينية",
    "13": "صينية",
    "14": "استراليه",
    "18": "سعودية",
    "26": "اماراتية",
    "28": "جنوب أفريقية",
    "37": "شراكة سودانية اجنبية",
    "41": "كندية",
    "42": "يابانية",
    "43": "مصرية",
    "44": "هندية",
    "45": "سورية",
    "46": "غير معروف",
    "47": "سودانية مصرية",
    "48": "سودانية سعودية",
    "49": "قطر سودانية",
}


class State(models.Model):
    name_ar = models.CharField("اسم الولاية (عربي)", max_length=100, unique=True)
    name_en = models.CharField("State name (English)", max_length=100, unique=True)

    class Meta:
        verbose_name = "ولاية"
        verbose_name_plural = "الولايات"
        ordering = ["name_ar"]

    def __str__(self):
        return self.name_ar


class Mineral(models.Model):
    name_ar = models.CharField("اسم المعدن", max_length=100, unique=True)

    class Meta:
        verbose_name = "معدن"
        verbose_name_plural = "المعادن"
        ordering = ["name_ar"]

    def __str__(self):
        return self.name_ar


class Nationality(models.Model):
    code = models.CharField("الرمز", max_length=50, unique=True)
    name_ar = models.CharField("اسم الجنسية (عربي)", max_length=100, blank=True)

    class Meta:
        verbose_name = "جنسية"
        verbose_name_plural = "الجنسيات"
        ordering = ["code"]

    def __str__(self):
        return self.name_ar or self.code


class Locality(models.Model):
    name_ar = models.CharField("اسم المحلية (عربي)", max_length=100)
    state = models.ForeignKey(
        State,
        verbose_name="الولاية",
        on_delete=models.PROTECT,
        related_name="localities",
    )

    class Meta:
        verbose_name = "محلية"
        verbose_name_plural = "المحليات"
        ordering = ["state", "name_ar"]
        constraints = [
            models.UniqueConstraint(fields=["state", "name_ar"], name="unique_locality_per_state"),
        ]

    def __str__(self):
        return f"{self.name_ar} ({self.state.name_ar})"


class Block(models.Model):
    code = models.CharField("رمز المربع", max_length=50, unique=True)
    name = models.CharField("اسم المربع", max_length=200, blank=True)
    coordinates = models.JSONField("الإحداثيات", default=list, blank=True)

    class Meta:
        verbose_name = "مربع"
        verbose_name_plural = "المربعات"
        ordering = ["code"]

    def __str__(self):
        return self.code


class Company(models.Model):
    name_ar = models.CharField("اسم الشركة (عربي)", max_length=200)
    name_en = models.CharField("Company name (English)", max_length=200, blank=True)
    company_type = models.CharField(
        "نوع الشركة",
        max_length=20,
        choices=CompanyType.choices,
        default=CompanyType.EXPLORATION,
    )
    registration_no = models.CharField("رقم التسجيل", max_length=100, blank=True)
    address = models.CharField("العنوان", max_length=300, blank=True)
    phone = models.CharField("الهاتف", max_length=50, blank=True)
    email = models.EmailField("البريد الإلكتروني", blank=True)
    nationalities = models.ManyToManyField(
        "Nationality",
        verbose_name="الجنسيات",
        blank=True,
        related_name="companies",
    )
    website = models.URLField("الموقع الإلكتروني", max_length=300, blank=True)
    manager_name = models.CharField("اسم المدير", max_length=200, blank=True)
    manager_phone = models.CharField("هاتف المدير", max_length=100, blank=True)
    rep_name = models.CharField("اسم الممثل", max_length=200, blank=True)
    rep_phone = models.CharField("هاتف الممثل", max_length=100, blank=True)
    general_status = models.CharField(
        "حالة الشركة العامة",
        max_length=10,
        choices=GeneralStatus.choices,
        default=GeneralStatus.GREEN,
    )
    notes = models.TextField("ملاحظات", blank=True)
    created_at = models.DateTimeField("تاريخ الإنشاء", auto_now_add=True)
    updated_at = models.DateTimeField("آخر تحديث", auto_now=True)

    class Meta:
        verbose_name = "شركة"
        verbose_name_plural = "الشركات"
        ordering = ["name_ar"]

    def __str__(self):
        return f"{self.name_ar} ({self.get_company_type_display()})"


class Agreement(models.Model):
    company = models.ForeignKey(
        Company, verbose_name="الشركة", on_delete=models.CASCADE, related_name="agreements"
    )
    contract_type = models.CharField(
        "نوع العقد",
        max_length=40,
        choices=ContractType.choices,
        default=ContractType.CONCESSION,
    )
    agreement_no = models.CharField("رقم الاتفاقية/العقد", max_length=100, blank=True)
    block = models.ForeignKey(
        Block,
        verbose_name="المربع",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agreements",
    )
    state = models.ForeignKey(
        State,
        verbose_name="الولاية",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agreements",
    )
    locality = models.ForeignKey(
        Locality,
        verbose_name="المحلية",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agreements",
    )
    area_name = models.CharField("المنطقة", max_length=100, blank=True)
    start_date = models.DateField("تاريخ البداية", null=True, blank=True)
    end_date = models.DateField("تاريخ النهاية", null=True, blank=True)
    signing_date = models.DateField("تاريخ التوقيع", null=True, blank=True)
    initial_area_km2 = models.DecimalField(
        "المساحة الأولية (كم2)", max_digits=14, decimal_places=4, null=True, blank=True
    )
    current_area_km2 = models.DecimalField(
        "المساحة الحالية (كم2)", max_digits=14, decimal_places=4, null=True, blank=True
    )
    minerals = models.ManyToManyField(
        Mineral,
        verbose_name="المعادن",
        blank=True,
        related_name="agreements",
    )
    validity_status = models.CharField(
        "سريان الاتفاقية",
        max_length=20,
        choices=ValidityStatus.choices,
        default=ValidityStatus.ACTIVE,
    )
    notes = models.TextField("ملاحظات", blank=True)

    class Meta:
        verbose_name = "اتفاقية / عقد / رخصة"
        verbose_name_plural = "الاتفاقيات / العقود / الرخص"
        ordering = ["company", "-signing_date"]

    def clean(self):
        if self.company_id and self.contract_type:
            allowed = contract_type_options(self.company.company_type)
            if allowed and self.contract_type not in allowed:
                raise ValidationError(
                    {
                        "contract_type": (
                            "نوع العقد لا يتوافق مع نوع الشركة "
                            f"({self.company.get_company_type_display()})."
                        )
                    }
                )
        if self.locality_id and self.state_id and self.locality.state_id != self.state_id:
            raise ValidationError({"locality": "المحلية لا تنتمي إلى الولاية المحددة."})

    def __str__(self):
        label = self.block.code if self.block else (self.area_name or "—")
        return f"{self.company.name_ar} — {self.get_contract_type_display()} ({label})"


class FinancialPosition(models.Model):
    agreement = models.OneToOneField(
        Agreement,
        verbose_name="الاتفاقية / العقد",
        on_delete=models.CASCADE,
        related_name="financial_position",
    )
    debt = models.DecimalField("المديونية", max_digits=14, decimal_places=2, null=True, blank=True)
    current_claim = models.DecimalField(
        "المطالبة الحالية", max_digits=14, decimal_places=2, null=True, blank=True
    )
    contractual_claim = models.DecimalField(
        "المطالبة المالية وفق العقد", max_digits=14, decimal_places=2, null=True, blank=True
    )
    last_payment_date = models.DateField("تاريخ آخر سداد", null=True, blank=True)
    balance = models.DecimalField("الرصيد", max_digits=14, decimal_places=2, null=True, blank=True)
    satisfied = models.BooleanField(
        "مستوفي / غير مستوفي",
        default=False,
        help_text="محدد = مستوفي، غير محدد = غير مستوفي",
    )

    class Meta:
        verbose_name = "الموقف المالي"
        verbose_name_plural = "المواقف المالية"

    def __str__(self):
        return f"الموقف المالي — {self.agreement.company.name_ar}"


class TechnicalPosition(models.Model):
    agreement = models.OneToOneField(
        Agreement,
        verbose_name="الاتفاقية / العقد",
        on_delete=models.CASCADE,
        related_name="technical_position",
    )
    work_program = models.TextField("برنامج العمل", blank=True)
    budget = models.TextField("الموازنة", blank=True)
    work_method = models.TextField("طريقة العمل", blank=True)
    tech_financial_reports = models.TextField("التقارير الفنية والمالية", blank=True)
    field_status = models.TextField("الموقف بالحقل", blank=True)
    production_status = models.TextField("الموقف الانتاجي", blank=True)
    activity_summary = models.TextField("ملخص النشاط الفني", blank=True)
    work_plan_approval_date = models.DateField("تاريخ إجازة خطة العمل", null=True, blank=True)
    processing_method = models.CharField(
        "طريقة المعالجة", max_length=100, blank=True, help_text="VAT / CIL / Heap Leaching"
    )

    class Meta:
        verbose_name = "الموقف الفني"
        verbose_name_plural = "المواقف الفنية"

    def __str__(self):
        return f"الموقف الفني — {self.agreement.company.name_ar}"


class LegalEventType(models.TextChoices):
    EXTENSION = "extension", "تمديد"
    RELINQUISHMENT = "relinquishment", "تنازل"
    CANCELLATION = "cancellation", "إلغاء"
    LEGAL_WARNING = "legal_warning", "إنذار قانوني"
    FREEZE = "freeze", "تجميد"
    NAME_CHANGE = "name_change", "تغيير اسم"
    AREA_CHANGE = "area_change", "تعديل مساحة"
    NO_RENEWAL = "no_renewal", "عدم رغبة في التجديد"
    OTHER = "other", "أخرى"


class FinancialEventType(models.TextChoices):
    CLAIM = "claim", "مطالبة"
    PAYMENT = "payment", "سداد"
    OTHER = "other", "أخرى"


class TechnicalEventType(models.TextChoices):
    REMOTE_SENSING = "remote_sensing", "دراسات استشعار عن بعد"
    SAMPLING = "sampling", "جمع وفحص عينات"
    SURVEY = "survey", "مسح طبوغرافي/جيوفيزيائي"
    TRENCHING = "trenching", "حفر ترنشات"
    WORK_PLAN = "work_plan", "خطة عمل"
    STUDY = "study", "دراسة جدوى"
    REPORT = "report", "تقرير فني ومالي"
    PRODUCTION = "production", "إنتاج"
    EXPORT = "export", "صادر"
    MINING_DESIGN = "mining_design", "تصميم المنجم"
    PLANT_CAPACITY = "plant_capacity", "الطاقة التصميمية للمصنع"
    TECHNICAL_ISSUES = "technical_issues", "المشاكل الفنية/الايجابيات"
    OTHER = "other", "أخرى"


class LegalEvent(models.Model):
    agreement = models.ForeignKey(
        Agreement,
        verbose_name="الاتفاقية / العقد",
        on_delete=models.CASCADE,
        related_name="legal_events",
    )
    event_type = models.CharField(
        "نوع الحدث", max_length=30, choices=LegalEventType.choices, default=LegalEventType.OTHER
    )
    date = models.DateField("التاريخ", null=True, blank=True)
    description = models.TextField("الوصف", blank=True)
    application = models.ForeignKey(
        "applications.Application",
        verbose_name="الطلب المرتبط",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = "حدث قانوني (السرد التاريخي)"
        verbose_name_plural = "الأحداث القانونية (السرد التاريخي)"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.agreement.company.name_ar} — {self.get_event_type_display()} ({self.date or '—'})"


class FinancialEvent(models.Model):
    agreement = models.ForeignKey(
        Agreement,
        verbose_name="الاتفاقية / العقد",
        on_delete=models.CASCADE,
        related_name="financial_events",
    )
    event_type = models.CharField(
        "نوع الحدث", max_length=20, choices=FinancialEventType.choices, default=FinancialEventType.OTHER
    )
    date = models.DateField("التاريخ", null=True, blank=True)
    amount = models.DecimalField("المبلغ", max_digits=14, decimal_places=2, null=True, blank=True)
    description = models.TextField("الوصف", blank=True)
    application = models.ForeignKey(
        "applications.Application",
        verbose_name="الطلب المرتبط",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = "حدث مالي (السرد التاريخي)"
        verbose_name_plural = "الأحداث المالية (السرد التاريخي)"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.agreement.company.name_ar} — {self.get_event_type_display()} ({self.date or '—'})"


class TechnicalEvent(models.Model):
    agreement = models.ForeignKey(
        Agreement,
        verbose_name="الاتفاقية / العقد",
        on_delete=models.CASCADE,
        related_name="technical_events",
    )
    event_type = models.CharField(
        "نوع الحدث",
        max_length=30,
        choices=TechnicalEventType.choices,
        default=TechnicalEventType.OTHER,
    )
    category = models.CharField(
        "التصنيف",
        max_length=100,
        blank=True,
        help_text="مثال: عقد التعدين / عقد المخلقات / عقد حجر المعدنين",
    )
    date = models.DateField("التاريخ", null=True, blank=True)
    description = models.TextField("الوصف", blank=True)
    application = models.ForeignKey(
        "applications.Application",
        verbose_name="الطلب المرتبط",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = "حدث فني (السرد التاريخي للأعمال الفنية)"
        verbose_name_plural = "الأحداث الفنية (السرد التاريخي للأعمال الفنية)"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.agreement.company.name_ar} — {self.get_event_type_display()} ({self.date or '—'})"
