from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group, User
from django.shortcuts import render
from django.urls import path, reverse

from companies.models import CompanyType

from .models import StaffProfile
from .roles import (
    MANAGED_ROLES,
    ROLE_DEFINITIONS,
    apply_role_to_user,
    role_group_permissions,
)


# ------------------------------------------------------------------ user profile


class StaffProfileForm(forms.ModelForm):
    company_types = forms.MultipleChoiceField(
        choices=CompanyType.choices,
        required=False,
        label="أنواع الشركات",
        help_text="تقييد دور مدخل البيانات الفني على هذه الأنواع فقط.",
    )

    class Meta:
        model = StaffProfile
        fields = ("company_types",)


class StaffProfileInline(admin.StackedInline):
    model = StaffProfile
    form = StaffProfileForm
    extra = 0
    max_num = 1
    can_delete = False


admin.site.unregister(Group)
admin.site.unregister(User)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    inlines = [StaffProfileInline]


# ------------------------------------------------------------------ roles admin


class Role(Group):
    class Meta:
        proxy = True
        verbose_name = "دور"
        verbose_name_plural = "الأدوار"


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = (
        "arabic_name",
        "name",
        "scope_label",
        "member_count",
        "permissions_summary",
    )
    # name stays read-only so the group keeps its link to ROLE_DEFINITIONS
    # (labels, scope, and the sync action key off the group name); the
    # permissions M2M is editable per role.
    readonly_fields = ("name",)
    filter_horizontal = ("permissions",)
    search_fields = ("name",)
    actions = ["sync_permissions"]

    def get_queryset(self, request):
        return super().get_queryset(request).filter(name__in=MANAGED_ROLES)

    def arabic_name(self, obj):
        return ROLE_DEFINITIONS.get(obj.name, {}).get("arabic_name", obj.name)

    arabic_name.short_description = "الدور"

    def scope_label(self, obj):
        if ROLE_DEFINITIONS.get(obj.name, {}).get("company_scoped"):
            return "حسب أنواع الشركات"
        return "جميع الشركات"

    scope_label.short_description = "النطاق"

    def member_count(self, obj):
        return obj.user_set.count()

    member_count.short_description = "عدد المستخدمين"

    def permissions_summary(self, obj):
        codenames = sorted(p.codename for p in obj.permissions.all())
        if not codenames:
            return "—"
        shown = "، ".join(codenames[:10])
        return shown + (" …" if len(codenames) > 10 else "")

    permissions_summary.short_description = "الصلاحيات"

    def sync_permissions(self, request, queryset):
        count = 0
        for group in queryset:
            if group.name in ROLE_DEFINITIONS:
                group.permissions.set(role_group_permissions(group.name))
                count += 1
        self.message_user(
            request, f"تمت مزامنة صلاحيات {count} دور مع التعريف المركزي.", messages.SUCCESS
        )

    sync_permissions.short_description = "مزامنة الصلاحيات مع التعريف المركزي"

    def has_add_permission(self, request):
        return False  # roles are created by manage.py create_roles

    def has_delete_permission(self, request, obj=None):
        return False  # role lifecycle is managed centrally

    # ---------------------------------------------------------- assignment page

    def get_urls(self):
        urls = super().get_urls()
        info = self.opts.app_label, self.opts.model_name
        custom = [
            path(
                "assignment/",
                self.admin_site.admin_view(self.assignment_view),
                name="%s_%s_assignment" % info,
            ),
        ]
        return custom + urls

    def assignment_view(self, request):
        if request.method == "POST":
            updated = 0
            for key, raw_value in request.POST.items():
                if not key.startswith("role_"):
                    continue
                try:
                    user = User.objects.get(pk=int(key[len("role_") :]))
                except (User.DoesNotExist, ValueError):
                    continue
                role_name = raw_value or None
                types = request.POST.getlist(f"company_types_{user.pk}")
                apply_role_to_user(user, role_name, company_types=types)
                updated += 1
            self.message_user(
                request, f"تم تحديث أدوار {updated} مستخدم.", messages.SUCCESS
            )
            from django.http import HttpResponseRedirect

            return HttpResponseRedirect(
                reverse("admin:roles_role_assignment")
            )

        rows = []
        for user in User.objects.filter(is_staff=True).order_by("username"):
            role = next(
                (name for name in MANAGED_ROLES if user.groups.filter(name=name).exists()),
                "",
            )
            try:
                types = list(user.staff_profile.company_types or [])
            except StaffProfile.DoesNotExist:
                types = []
            rows.append({"user": user, "role": role, "types": types})
        context = {
            **self.admin_site.each_context(request),
            "title": "توزيع الأدوار",
            "rows": rows,
            "roles": ROLE_DEFINITIONS,
            "company_types": CompanyType.choices,
        }
        return render(request, "admin/roles/assignment.html", context)
