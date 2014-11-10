import json
import re

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator, validate_slug
from django.core.urlresolvers import reverse
from django.utils.translation import ugettext as _
from django import forms

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Fieldset, Div, HTML

from dimagi.utils.decorators.memoized import memoized

from .models import (CustomDataFieldsDefinition, CustomDataField,
                     CUSTOM_DATA_FIELD_PREFIX)


class CustomDataFieldsForm(forms.Form):
    """
    The main form for editing a custom data definition
    """
    data_fields = forms.CharField(widget=forms.HiddenInput)

    def verify_no_duplicates(self, data_fields):
        errors = set()
        slugs = [field['slug'].lower()
                 for field in data_fields if 'slug' in field]
        for slug in slugs:
            if slugs.count(slug) > 1:
                errors.add(_("Key '{}' was duplicated, key names must be "
                             "unique.").format(slug))
        return errors

    def clean_data_fields(self):
        raw_data_fields = json.loads(self.cleaned_data['data_fields'])
        errors = set()
        data_fields = []
        for raw_data_field in raw_data_fields:
            data_field_form = CustomDataFieldForm(raw_data_field)
            data_field_form.is_valid()
            data_fields.append(data_field_form.cleaned_data)
            if data_field_form.errors:
                errors.update([error[0]
                               for error in data_field_form.errors.values()])

        errors.update(self.verify_no_duplicates(data_fields))

        if errors:
            raise ValidationError('<br/>'.join(sorted(errors)))

        return data_fields


class XmlSlugField(forms.SlugField):
    default_validators = [
        validate_slug,
        RegexValidator(
            re.compile(r'^(?!xml)', flags=re.IGNORECASE),
            _('Properties cannot begin with "xml"'), 'invalid_xml'
        )
    ]


class CustomDataFieldForm(forms.Form):
    """
    Sub-form for editing an individual field's definition.
    """
    label = forms.CharField(
        required=True,
        error_messages={'required': _('All fields are required')}
    )
    slug = XmlSlugField(
        required=True,
        error_messages={
            'required': _("All fields are required"),
            'invalid': _("Key fields must consist only of letters, numbers, "
                         "underscores or hyphens.")
        }
    )
    is_required = forms.BooleanField(required=False)
    choices = forms.CharField(widget=forms.HiddenInput, required=False)

    def __init__(self, raw, *args, **kwargs):
        # Pull the raw_choices out here, because Django incorrectly
        # serializes the list and you can't get it
        self._raw_choices = filter(None, raw.get('choices', []))
        super(CustomDataFieldForm, self).__init__(raw, *args, **kwargs)

    def clean_choices(self):
        return self._raw_choices


class CustomDataFieldsMixin(object):
    """
    Provides the interface for editing the ``CustomDataFieldsDefinition``
    for each entity type.
    Each entity type must provide a subclass of this mixin.
    """
    urlname = None
    template_name = "custom_data_fields/custom_data_fields.html"
    field_type = None
    entity_string = None  # User, Group, Location, Product...

    @classmethod
    def get_validator(cls, domain):
        data_model = CustomDataFieldsDefinition.get_or_create(domain, cls.field_type)
        return data_model.get_validator(cls)

    @classmethod
    def page_name(cls):
        return _("Edit {} Fields").format(cls.entity_string)

    def get_definition(self):
        return CustomDataFieldsDefinition.get_or_create(self.domain,
                                                        self.field_type)

    def get_custom_fields(self):
        definition = self.get_definition()
        if definition:
            return definition.fields
        else:
            return []

    def save_custom_fields(self):
        definition = self.get_definition() or CustomDataFieldsDefinition()
        definition.field_type = self.field_type
        definition.domain = self.domain
        definition.fields = [
            self.get_field(field)
            for field in self.form.cleaned_data['data_fields']
        ]
        definition.save()

    def get_field(self, field):
        return CustomDataField(
            slug=field.get('slug'),
            is_required=field.get('is_required'),
            label=field.get('label'),
            choices=field.get('choices'),
        )

    @property
    def page_context(self):
        return {
            "custom_fields": json.loads(self.form.data['data_fields']),
            "custom_fields_form": self.form,
        }

    @property
    @memoized
    def form(self):
        if self.request.method == "POST":
            return CustomDataFieldsForm(self.request.POST)
        else:
            serialized = json.dumps([field.to_json()
                                     for field in self.get_custom_fields()])
            return CustomDataFieldsForm({'data_fields': serialized})

    def post(self, request, *args, **kwargs):
        if self.form.is_valid():
            self.save_custom_fields()
            return self.get(request, success=True, *args, **kwargs)
        else:
            return self.get(request, *args, **kwargs)


def add_prefix(field_dict):
    """
    Prefix all keys in the dict with the defined
    custom data prefix (such as data-field-whatevs).
    """
    return {
        "{}-{}".format(CUSTOM_DATA_FIELD_PREFIX, k): v
        for k, v in field_dict.iteritems()
    }


def _make_field(field):
    if field.choices:
        return forms.ChoiceField(
            label=field.label,
            required=field.is_required,
            choices=[('', _('Select one'))] + [(c, c) for c in field.choices],
        )
    return forms.CharField(label=field.label, required=field.is_required)


class CustomDataEditor(object):
    """
    Tool to edit the data for a particular entity, like for an individual user.
    """
    def __init__(self, field_view, domain, existing_custom_data=None,
                 post_dict=None, required_only=False):
        self.field_view = field_view
        self.domain = domain
        self.existing_custom_data = existing_custom_data
        self.required_only = required_only
        self.form = self.init_form(post_dict)

    @property
    @memoized
    def model(self):
        definition = CustomDataFieldsDefinition.get_or_create(
            self.domain,
            self.field_view.field_type,
        )
        return definition or CustomDataFieldsDefinition()

    def is_valid(self):
        return self.form.is_valid()

    def get_data_to_save(self):
        cleaned_data = self.form.cleaned_data
        self.existing_custom_data = None
        self.form = self.init_form(add_prefix(cleaned_data))
        self.form.is_valid()
        return cleaned_data

    def init_form(self, post_dict=None):
        fields = {
            field.slug: _make_field(field) for field in self.model.fields
            if not self.required_only or field.is_required
        }
        field_names = fields.keys()

        CustomDataForm = type('CustomDataForm', (forms.Form,), fields)
        CustomDataForm.helper = FormHelper()
        CustomDataForm.helper.form_tag = False
        CustomDataForm.helper.layout = Layout(
            Fieldset(
                _("Additional Information"),
                *field_names
            ) if self.model.fields else '',
            self.get_uncategorized_form(field_names),
        )
        CustomDataForm._has_uncategorized = bool(
            self.get_uncategorized_form(field_names)
        )

        if post_dict:
            fields = post_dict
        elif self.existing_custom_data is not None:
            fields = add_prefix(self.existing_custom_data)
        else:
            fields = None

        self.form = CustomDataForm(fields, prefix=CUSTOM_DATA_FIELD_PREFIX)
        return self.form

    def get_uncategorized_form(self, field_names):

        def FakeInput(val):
            return HTML('<span class="input-xlarge uneditable-input">{}</span>'
                        .format(val))

        def Label(val):
            return HTML('<label class="control-label">{}</label>'.format(val))

        def _make_field_div(slug, val):
            return Div(
                Label(slug),
                Div(
                    FakeInput(val),
                    css_class="controls",
                ),
                css_class="control-group",
            )

        help_div = [
            _make_field_div(slug, val)
            for slug, val in self.existing_custom_data.items()
            if slug not in field_names
        ] if self.existing_custom_data is not None else []

        msg = """
        <strong>Warning!</strong>
        This data is not part of the specified user fields and will be
        deleted if you save.
        You can add them <a href="{}">here</a> to prevent this.
        """.format(reverse(
            self.field_view.urlname, args=[self.domain]
        ))

        return Fieldset(
            _("Unrecognized Information"),
            Div(
                HTML(msg),
                css_class="alert alert-error",
            ),
            *help_div
        ) if len(help_div) else HTML('')
