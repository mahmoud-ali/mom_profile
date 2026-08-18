"""Admin sidebar ordering.

Django renders the admin sidebar from AdminSite.get_app_list() (apps in
registration order, models within each app in registration order). This module
overrides that method on the default admin site with explicit app/model order
maps, leaving anything not listed at the end in its default relative order.

Imported from config/urls.py so the override is installed when the admin loads.
"""

from django.contrib import admin

# App labels in sidebar order; apps not listed follow after, in default order.
APP_ORDER = [
    "companies",
    "applications",
    "roles",
    "auth",
    "auditlog",
]

# Model order per app (keyed by model_name); unlisted models go after.
MODEL_ORDER = {
    "companies": [
        "company",
        "agreement",
        "financialposition",
        "technicalposition",
        "financialevent",
        "technicalevent",
        "legalevent",
        "state",
        "locality",
        "mineral",
        "block",
        "nationality",
        "auditlogproxy",
    ],
    "applications": [
        "application",
        "applicationtype",
        "applicationdetail",
        "workinghoursschedule",
        "applicationtransition",
    ],
    "roles": [
        "role",
    ],
    "auth": [
        "user",
    ],
}

_original_get_app_list = admin.site.get_app_list


def _get_app_list(request):
    app_list = _original_get_app_list(request)

    def app_key(app):
        try:
            return APP_ORDER.index(app["app_label"])
        except ValueError:
            return len(APP_ORDER)

    app_list.sort(key=app_key)

    for app in app_list:
        order = MODEL_ORDER.get(app["app_label"], [])

        def model_key(model):
            model_name = model["model"]._meta.model_name
            try:
                return order.index(model_name)
            except ValueError:
                return len(order)

        app["models"].sort(key=model_key)

    return app_list


admin.site.get_app_list = _get_app_list
