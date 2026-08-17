from django import template

register = template.Library()


@register.filter
def field_by_name(form, name):
    """Return the BoundField for a form field by name."""
    return form[name]


@register.filter
def dict_get(mapping, key):
    """Lookup helper for dictionaries in templates."""
    if mapping is None:
        return None
    return mapping.get(key)
