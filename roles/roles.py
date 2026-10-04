"""Single source of truth for the role structure.

Every role (Django group) is defined here. The create_roles management
command, the Roles admin section, the assignment page, and the tests all read
from this module, so the role/permission structure lives in exactly one place.

Permission format: {"app_label.model": ["add"|"change"|"delete"|"view"|custom_codename", ...]}
"""

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q

from applications.workflow import WORKFLOW_PERMISSION_CODENAMES

TECHNICAL_DATA_ENTRY = "technical_data_entry"
FINANCIAL_DATA_ENTRY = "financial_data_entry"
LEGAL_DATA_ENTRY = "legal_data_entry"
MANAGER = "manager"

ROLE_DEFINITIONS = {
    TECHNICAL_DATA_ENTRY: {
        "arabic_name": "مدخل بيانات فني",
        "description": (
            "إدخال وتحديث الموقف الفني والأحداث الفنية والطلبات (تأكيد المسودات) "
            "لأنواع الشركات المحددة للمستخدم فقط؛ حذف الطلبات المسودة فقط."
        ),
        "company_scoped": True,
        "permissions": {
            "applications.application": ["add", "change", "delete", "view", "can_submit"],
            "companies.technicalposition": ["add", "change", "view"],
            "companies.technicalevent": ["add", "change", "view"],
            "companies.agreement": ["view"],
            "companies.company": ["view"],
        },
    },
    FINANCIAL_DATA_ENTRY: {
        "arabic_name": "مدخل بيانات مالي",
        "description": (
            "إدخال وتحديث الموقف المالي والأحداث المالية لجميع الشركات؛ "
            "لا صلاحيات على الطلبات أو كتالوج الإجراءات."
        ),
        "company_scoped": False,
        "permissions": {
            "companies.financialposition": ["add", "change", "view"],
            "companies.financialevent": ["add", "change", "view"],
            "companies.agreement": ["view"],
            "companies.company": ["view"],
        },
    },
    LEGAL_DATA_ENTRY: {
        "arabic_name": "مدخل بيانات قانوني",
        "description": (
            "إدخال وتحديث بيانات الشركات والعقود (الاتفاقيات/الرخص) والأحداث القانونية "
            "لجميع الشركات؛ لا صلاحيات على الطلبات أو المواقف الفنية/المالية أو كتالوج الإجراءات."
        ),
        "company_scoped": False,
        "permissions": {
            "companies.company": ["add", "change", "view"],
            "companies.agreement": ["add", "change", "view"],
            "companies.legalevent": ["add", "change", "view"],
            "companies.block": ["view"],
            "companies.state": ["view"],
            "companies.locality": ["view"],
            "companies.mineral": ["view"],
            "companies.nationality": ["view"],
        },
    },
    MANAGER: {
        "arabic_name": "مدير",
        "description": (
            "التحكم الكامل في سير الطلبات (قيد المعالجة / توصية اللجنة / "
            "توصية وكيل الوزارة / معتمد / مرفوض) مع إضافة وتعديل بيانات "
            "الشركات والملفات؛ كتالوج الإجراءات للمدير فقط."
        ),
        "company_scoped": False,
        "permissions": {
            "applications.application": [
                "add", "change", "view", *WORKFLOW_PERMISSION_CODENAMES,
            ],
            "applications.applicationtype": ["add", "change", "view"],
            "companies.company": ["add", "change", "view"],
            "companies.agreement": ["add", "change", "view"],
            "companies.financialposition": ["add", "change", "view"],
            "companies.technicalposition": ["add", "change", "view"],
            "companies.legalevent": ["add", "change", "view"],
            "companies.financialevent": ["add", "change", "view"],
            "companies.technicalevent": ["add", "change", "view"],
            "companies.state": ["add", "change", "view"],
            "companies.mineral": ["add", "change", "view"],
            "companies.block": ["add", "change", "view"],
        },
    },
}

MANAGED_ROLES = tuple(ROLE_DEFINITIONS)

# Group the roles app retires but keeps reporting about.
RETIRED_GROUP = "data_entry"

_MODEL_ACTIONS = ("add", "change", "delete", "view")


def role_group_permissions(role_name):
    """Resolve a role's ROLE_DEFINITIONS entry to Permission objects."""
    perms = []
    for model_key, actions in ROLE_DEFINITIONS[role_name]["permissions"].items():
        app_label, model_name = model_key.split(".")
        ct = ContentType.objects.get(app_label=app_label, model=model_name)
        for action in actions:
            codename = (action + "_" + model_name) if action in _MODEL_ACTIONS else action
            perms.append(Permission.objects.get(content_type=ct, codename=codename))
    return perms


def apply_role_to_user(user, role_name=None, company_types=None):
    """Assign exactly one managed role to a user (+ scope types).

    Removes the user from every OTHER managed role (non-managed groups such as
    the retired data_entry are left untouched), and syncs StaffProfile.
    """
    if role_name and role_name not in ROLE_DEFINITIONS:
        raise ValueError("Unknown role: " + role_name)
    for managed in MANAGED_ROLES:
        group, _ = Group.objects.get_or_create(name=managed)
        if managed == role_name:
            user.groups.add(group)
        else:
            user.groups.remove(group)
    from .models import StaffProfile

    profile, _ = StaffProfile.objects.get_or_create(user=user)
    profile.company_types = list(company_types or [])
    profile.full_clean()
    profile.save()


def scoped_company_types(user):
    """Company types a scoped technical_data_entry user is limited to.

    Returns None when the user is NOT scoped (sees everything), or a list of
    company_type values (possibly empty = sees nothing) when scoped.
    """
    if user is None or not user.is_authenticated or user.is_superuser:
        return None
    if user.groups.filter(name=MANAGER).exists():
        return None
    if not user.groups.filter(name=TECHNICAL_DATA_ENTRY).exists():
        return None
    from .models import StaffProfile

    try:
        return list(user.staff_profile.company_types or [])
    except StaffProfile.DoesNotExist:
        return []


def company_scope_q(user, model):
    """Q filter limiting `model` rows to the user's scoped company types.

    Returns None when the user is not scoped (no filter), otherwise a Q
    (empty-list scope yields an empty queryset, never full access).
    """
    types = scoped_company_types(user)
    if types is None:
        return None
    from applications.models import Application
    from companies.models import Agreement, Company, TechnicalEvent, TechnicalPosition

    if model is Company:
        return Q(company_type__in=types)
    if model is Agreement:
        return Q(company__company_type__in=types)
    if model in (TechnicalPosition, TechnicalEvent, Application):
        return Q(agreement__company__company_type__in=types)
    return None
