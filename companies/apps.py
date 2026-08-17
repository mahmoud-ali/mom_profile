from django.apps import AppConfig


class CompaniesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "companies"
    verbose_name = "الشركات (ملف الشركة)"

    def ready(self):
        from .audit_setup import setup_audit

        setup_audit()
