from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from companies.models import CompanyType


class StaffProfile(models.Model):
    """Per-user staff settings used by scoped roles.

    company_types restricts the technical_data_entry role: its users can only
    work with companies/agreements/technical data/applications of the listed
    company types. Empty list = nothing visible (never "everything").
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name="المستخدم",
        on_delete=models.CASCADE,
        related_name="staff_profile",
    )
    company_types = models.JSONField(
        "أنواع الشركات",
        default=list,
        blank=True,
        help_text=(
            "قيم من: استكشاف / إنتاج / مخلفات / صغير — تستخدم لتقييد صلاحيات دور "
            "مدخل البيانات الفني على أنواع محددة من الشركات."
        ),
    )

    class Meta:
        verbose_name = "ملف المستخدم"
        verbose_name_plural = "ملفات المستخدمين"

    def clean(self):
        valid = {value for value, _label in CompanyType.choices}
        for value in self.company_types or []:
            if value not in valid:
                raise ValidationError(
                    {"company_types": f"نوع الشركة غير معروف: {value}"}
                )

    def __str__(self):
        return f"ملف {self.user.username}"
