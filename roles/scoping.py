"""Admin mixin enforcing company-type scoping for the technical_data_entry role."""

from .roles import company_scope_q


class CompanyTypeScopedAdminMixin:
    """Filters admin querysets and related-field choices to the user's scope.

    Apply to CompanyAdmin / AgreementAdmin / TechnicalPositionAdmin /
    TechnicalEventAdmin / ApplicationAdmin. Out-of-scope objects 404 via
    get_object() -> get_queryset().
    """

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        q = company_scope_q(request.user, self.model)
        return queryset.filter(q) if q is not None else queryset

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        q = company_scope_q(request.user, db_field.remote_field.model)
        if q is not None and "queryset" not in kwargs:
            kwargs["queryset"] = db_field.remote_field.model._default_manager.all().filter(q)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)
