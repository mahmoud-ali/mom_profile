# نظام إدارة شركات التعدين — ملف الشركة والطلبات

Django 5.2 LTS application for managing mining companies of all four types
(استكشاف / إنتاج / مخلفات / صغير). Each company holds one or more agreements
(الاتفاقيات/العقود); the profile sections (financial, technical, history) and
all applications attach to the Agreement, not the company.

## Company types and their data sources

| Company type      | Profile source                 | Procedures catalog     | # types |
|-------------------|--------------------------------|------------------------|---------|
| استكشاف (Exploration) | ملف الشركة - استكشاف.docx | apps_emtiaz.csv        | 23      |
| إنتاج (Production)    | ملف الشركة - منتجة.docx    | apps_entaj.csv         | 24      |
| مخلفات (Tailings)     | shared profile skeleton    | apps_mokhalfat.csv     | 22      |
| صغير (Small)          | shared profile skeleton    | apps_sageer.csv        | 29      |

The profile mirrors the docx sections: agreement info (أولا), financial position
(ثانيا), technical position (ثالثا), general status (رابعا — on the Company),
plus the historical narratives (السرد التاريخي) as LegalEvent / FinancialEvent /
TechnicalEvent — all under the Agreement.

## Roles (workflow)

Roles are defined centrally in roles/roles.py (ROLE_DEFINITIONS — the single
source of truth) and built into Django groups by `manage.py create_roles`.
The old `data_entry` group is retired; the command warns about users still in
it so they can be reassigned. A role's permissions can also be adjusted
directly on its Admin > الأدوار change page; the "مزامنة الصلاحيات" action
(or re-running create_roles) restores the canonical ROLE_DEFINITIONS set.
Role names are read-only (they key the ROLE_DEFINITIONS mapping) and roles are
created/deleted only via create_roles.

- technical_data_entry — scoped by the user's assigned company types
  (StaffProfile.company_types, set on the user page or via Admin > الأدوار):
  adds/edits TechnicalPosition, TechnicalEvent and Applications for those
  company types only, confirms/submits drafts (مسودة -> مؤكد), and may delete
  DRAFT applications of its own types. Has no access to the ApplicationType
  catalog (manager-only) or financial data.
- financial_data_entry — adds/edits FinancialPosition and FinancialEvent for
  ALL companies; no application or catalog access.
- manager — advances to under-processing and approves or rejects
  (مؤكد -> قيد المعالجة -> مجاز / مرفوض); full company-file editing; the
  ApplicationType (إجراءات) catalog is manager-only.
- When a manager approves an application, one historical event is created
  automatically on the application's agreement (idempotent, no duplicates).
- superuser bypasses all checks and scoping.

## Type-driven application forms

Every application is entered through a form generated from its ApplicationType
(the CSV definition):

- the Application add page creates a draft header (agreement + type),
- the change page then renders the type's form: one field per form_fields label
  (date inputs for date-like labels), one file input per attachments label, and
  detail rows whose columns come from detail_fields (with a category dropdown
  when detail_models has categories, e.g. requirements-list),
- a "تأكيد الطلب" button confirms the draft when the user has can_submit.

Labels are locked to the type; generic free-typed label rows are not used in the
entry flow. Approved applications still log one event on the agreement.

### Field types (field_specs / detail_specs)

ApplicationType declares typed fields per type, keyed by field label —
field_specs (مواصفات الحقول) for main form fields and detail_specs
(مواصفات حقول التفاصيل) for detail columns:

- {"type": "select", "choices": [...]} -> dropdown, e.g. «الغرض من التصديق» in
  طلب اجراءات اجنبي: اذن تحرك، كرت عمل، تجديد كرت عمل، تأشيرة دخول، إقامة،
  تجديد إقامة، طلب تعيين اجنبي، تأشيرة متعددة، إذن عمل مبدئي.
- {"type": "integer"} -> whole number input, e.g. الكمية in طلب كشف احتياجات.
- {"type": "float"} -> decimal number input, e.g. المساحة بالكم2، نسبة التنازل،
  تكلفة التحليل، وزن العينة، كمية الوقود، weights/costs in استمارة انتاج الذهب
  and طلب صادر ذهب، and study coordinates.
- {"type": "decimal"} -> high-precision decimal (supported; not seeded).
- {"required": true} on any spec makes the field (or detail column) mandatory
  (default: optional). Example: {"type": "float", "required": true} on
  المساحة بالكم2 makes it a required number input. Attachments can be required
  too by keying field_specs by the attachment label.

Field layout (field_layout, تخطيط الحقول) arranges the form:
- {"columns": 2} renders the main fields in a two-column grid (default single
  column); e.g. طلب اجراءات اجنبي is seeded with columns: 2.
- {"attachment_columns": 2} renders the attachments (file inputs) in a
  two-column grid too (default single column); foreigner uses 2 for its six
  attachments.
- {"groups": [{"title": "...", "fields": ["label1", "label2"]}]} renders titled
  sections in the given order; per-group columns overrides the default. Fields
  not listed in any group render after the groups.

Detail examples: الكمية (detail column of طلب كشف احتياجات) is integer;
وزن العينة، كمية الوقود، the gold-production weights and the study coordinates
are float detail columns.

The seed data lives in DEFAULT_FIELD_SPECS and DEFAULT_DETAIL_SPECS in
applications/management/commands/load_app_types.py (keyed by model_name, applied
to every company type that carries the procedure) and can be edited per type in
the admin (field_specs / detail_specs JSON). Numeric input is validated
(Arabic error messages); values are stored as plain strings in ApplicationField.

## Setup

    python3 -m venv .venv
    .venv/bin/python -m ensurepip --upgrade
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python manage.py migrate
    .venv/bin/python manage.py load_app_types      # seed the 98 procedure types from the 4 CSVs
    .venv/bin/python manage.py create_roles        # create the roles defined in roles/roles.py
    .venv/bin/python manage.py seed_reference      # Sudan states + minerals
    .venv/bin/python manage.py import_profile_data  # import data/companies.csv + data/agreements.csv
    .venv/bin/python manage.py createsuperuser
    .venv/bin/python manage.py runserver           # admin at http://127.0.0.1:8000/admin/

Create ordinary users, mark them staff, and assign each a role under
Admin > الأدوار > توزيع الأدوار (role + company types for the scoped
technical_data_entry role; the same types are also editable on the user page).

## Useful commands

    .venv/bin/python manage.py load_app_types --company-type exploration   # load one catalog
    .venv/bin/python manage.py import_profile_data --dry-run               # preview without writing
    .venv/bin/python manage.py import_profile_data --company-type small    # import one type only
    .venv/bin/python manage.py test                                        # run tests
    .venv/bin/python manage.py check

The import maps: company_type (emtiaz/entaj/mokhalfat/sageer -> exploration/
production/tailings/small); agreement contract_type is derived from the
company's type (authoritative); الولاية/المحلية/المربع/المعدن are created as
lookups; حالة الاتفاقية/العقد -> validity (سارية/غير سارية/ملغية/مجمدة/تنازل);
رقم الاتفاقية/العقد -> Agreement.agreement_no; unmatched companies in the
agreements file get placeholder companies. Company nationality codes
(comma-separated ids) become rows of the Nationality lookup (Company.nationalities
M2M); Company.status was removed. Agreement minerals are a M2M: combined
المعدن cells (e.g. "نحاس، ذهب") split into individual Mineral rows and spelling
variants normalize (جبص->جبس، الذهب->ذهب). Idempotent (keyed on company name
and agreement number); --dry-run validates inside a rolled-back transaction.

## Project layout

- config/ — Django project (settings, urls, wsgi/asgi).
- companies/ — Company + Agreement + FinancialPosition/TechnicalPosition + the
  three historical event models + the Locality lookup (each locality belongs to
  a State), with admin (Agreement page = full profile).
  Agreement.locality is a FK to Locality; the admin filters the locality
  dropdown by the selected state (dependent JS), and Agreement.clean() rejects
  a locality whose state does not match the agreement's state.
- applications/ — ApplicationType catalog, Application with the role-based
  status machine, dynamic type-driven forms (forms.py + custom change_form
  template), attachments/fields/details, and the load_app_types command.
- roles/ — central role management: ROLE_DEFINITIONS (single source of truth),
  the create_roles command, StaffProfile (per-user company-type scope), the
  company-type scoping mixin, and the admin "الأدوار" section (role list +
  assignment page).
- config/sidebar.py — admin sidebar ordering: override of admin.site.get_app_list
  with explicit app/model order maps (APP_ORDER / MODEL_ORDER); edit the maps
  to change the sidebar, then refresh.

## Event mapping

ApplicationType.event_category + event_label decide where an approved
application lands (legal / financial / technical). The default mapping is
defined in applications/management/commands/load_app_types.py
(DEFAULT_EVENT_MAPPING) and can be re-mapped per type in the admin. In the
admin, «نوع الحدث التاريخي» is a dropdown of the event types valid for the
chosen «تصنيف الحدث التاريخي» (legal/financial/technical) — values are stored
as choice values; ApplicationType.clean() rejects mismatched pairs.

## Audit trail (سجل التدقيق)

Uses django-auditlog: every create/update/delete on Company, Agreement, the
financial/technical positions, the history events, Application and
ApplicationType is logged with the acting user (via AuditlogMiddleware),
timestamp, and old→new field changes. View it under Admin → سجل التدقيق
(read-only, filters by model/action/user) or per record via the "سجل التعديلات"
panel on Company/Agreement/Application pages. M2M changes (nationalities,
minerals) are logged too. The bulk import runs with row-level audit suppressed
and writes one summary entry.

## Application transition KPIs (مؤشرات أداء الطلبات)

Every status change is recorded (ApplicationTransition: from → to, user,
timestamp; creation anchors the timeline). The KPI page at
Admin → الطلبات → مؤشرات الأداء (and the application_kpis command) report
stage durations (avg/median/max per transition pair), per-user activity and
average decision time, monthly transition counts, and the in-review backlog
with its oldest item. Use ?days=30 / 90 on the page or --days N on the command.
Durations are computed on working hours only — weekends and out-of-hours
time are excluded. Hours/days are dynamic per date range via the
WorkingHoursSchedule lookup (Admin → جداول ساعات العمل), so seasonal
schedules are supported: each day uses the schedule covering it (name,
start/end date, work_start/work_end, working_weekdays); dates without a
schedule fall back to Sun–Thu 08:00–16:00 (Africa/Khartoum). seed_reference
creates the default schedule.

## Notes

- SQLite is used for local development; swap DATABASES for Postgres in production.
- The models are ready for a future public UI/API; the admin is the current interface.
- contract_type is constrained by company type (COMPANY_TYPE_CONTRACT_TYPES in
  companies/models.py): exploration -> concession; entaj -> mining,
  two_minerals; mokhalfat -> tailings, preprocessed_tailings; sageer -> small.
  Enforced by Agreement.clean() and by a filtered dropdown in the admin (a small
  JS refreshes the contract list when the company changes).
- ApplicationType.contract_types (أنواع العقود المسموحة) restricts which
  application types an agreement may submit — seeded from the company-type rule
  (each catalog's types allow that company type's contract types), editable per
  type in the admin. Application.clean() rejects mismatches, the add form
  filters app_type by the agreement's contract type, and a JS filters the
  app_type dropdown live when the agreement changes.
