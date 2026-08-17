from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand

from applications.models import Application, ApplicationType
from companies import models as companies_models

# Groups' codenames are English keys; names shown in the Django admin auth section.
DATA_ENTRY_GROUP = "data_entry"
MANAGER_GROUP = "manager"

# Application permissions per group (workflow split).
APP_PERMS = {
    DATA_ENTRY_GROUP: ["add_application", "change_application", "can_submit"],
    MANAGER_GROUP: [
        "add_application",
        "change_application",
        "can_submit",
        "can_review",
        "can_approve",
        "can_reject",
    ],
}


def _crud_permissions(app_models, with_delete=False):
    """Return add/change/view (+ optional delete) permissions for the given models."""
    out = []
    for model in app_models:
        ct = ContentType.objects.get_for_model(model)
        for p in Permission.objects.filter(content_type=ct):
            if p.codename.startswith("delete_") and not with_delete:
                continue
            out.append(p)
    return out


class Command(BaseCommand):
    help = "Creates the two workflow groups (data_entry, manager) and their permissions."

    def handle(self, *args, **options):
        app_ct = ContentType.objects.get_for_model(Application)
        app_perms = {p.codename: p for p in Permission.objects.filter(content_type=app_ct)}

        company_models = [
            companies_models.Company,
            companies_models.Agreement,
            companies_models.FinancialPosition,
            companies_models.TechnicalPosition,
            companies_models.LegalEvent,
            companies_models.FinancialEvent,
            companies_models.TechnicalEvent,
            companies_models.State,
            companies_models.Mineral,
            companies_models.Block,
        ]
        shared = _crud_permissions(company_models, with_delete=False)
        # Both groups also get add/change/view on the procedure catalog.
        type_ct = ContentType.objects.get_for_model(ApplicationType)
        type_perms = [
            p
            for p in Permission.objects.filter(content_type=type_ct)
            if not p.codename.startswith("delete_")
        ]
        shared += type_perms

        data_entry, _ = Group.objects.get_or_create(name=DATA_ENTRY_GROUP)
        manager, _ = Group.objects.get_or_create(name=MANAGER_GROUP)

        data_entry.permissions.set(
            [app_perms[c] for c in APP_PERMS[DATA_ENTRY_GROUP]] + shared
        )
        manager.permissions.set([app_perms[c] for c in APP_PERMS[MANAGER_GROUP]] + shared)

        self.stdout.write(
            self.style.SUCCESS(
                "Roles ready: data_entry (draft/submit) and manager "
                "(under-processing / approve / reject)."
            )
        )
