from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand

from roles.roles import (
    MANAGED_ROLES,
    RETIRED_GROUP,
    ROLE_DEFINITIONS,
    role_group_permissions,
)


class Command(BaseCommand):
    help = (
        "Creates the workflow groups from ROLE_DEFINITIONS (the single source of "
        "truth) and retires the old data_entry group."
    )

    def handle(self, *args, **options):
        for name in MANAGED_ROLES:
            group, _ = Group.objects.get_or_create(name=name)
            group.permissions.set(role_group_permissions(name))
            label = ROLE_DEFINITIONS[name]["arabic_name"]
            scoped = "مقيد بنوع الشركة" if ROLE_DEFINITIONS[name]["company_scoped"] else "جميع الشركات"
            self.stdout.write(
                self.style.SUCCESS(
                    f"Role ready: {name} ({label}) — {scoped} — "
                    f"{group.user_set.count()} members"
                )
            )
        stale = Group.objects.filter(name=RETIRED_GROUP).first()
        if stale is not None:
            members = list(
                User.objects.filter(groups=stale).values_list("username", flat=True)
            )
            if members:
                self.stdout.write(
                    self.style.WARNING(
                        "The data_entry group is retired and no longer created. "
                        f"Users still in it (reassign them): {', '.join(members)}"
                    )
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        "The data_entry group is retired; it has no members. "
                        "You may delete it manually."
                    )
                )
