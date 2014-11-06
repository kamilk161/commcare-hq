from django.http import HttpResponse, HttpResponseRedirect, Http404, HttpResponseNotFound
from django.shortcuts import render
from django.utils.decorators import method_decorator
from django.utils.translation import ugettext as _, ugettext_noop
from django.views.decorators.http import require_POST
from corehq.apps.commtrack.util import get_or_create_default_program, all_sms_codes

from corehq.apps.domain.decorators import domain_admin_required, login_and_domain_required, \
    cls_require_superuser_or_developer
from corehq.apps.domain.models import Domain
from corehq.apps.commtrack.models import Product, Program
from corehq.apps.commtrack.forms import ProductForm, ProgramForm, ConsumptionForm
from corehq.apps.commtrack.util import encode_if_needed
from corehq.apps.custom_data_fields.models import CustomDataFieldsDefinition
from corehq.apps.domain.views import BaseDomainView
from corehq.apps.hqwebapp.utils import get_bulk_upload_form
from corehq.apps.locations.models import Location
from dimagi.utils.decorators.memoized import memoized
from corehq import toggles
from soil.util import expose_download, get_download_context
from django.core.urlresolvers import reverse
from django.contrib import messages
from corehq.apps.commtrack.tasks import import_products_async, recalculate_domain_consumption_task
import json
from couchdbkit import ResourceNotFound
from dimagi.utils.couch.database import iter_docs
import itertools
import copy
from couchexport.writers import Excel2007ExportWriter
from StringIO import StringIO
from couchexport.models import Format
from corehq.apps.custom_data_fields.views import CustomDataFieldsMixin, CustomDataEditor
from dimagi.utils.web import json_response


@domain_admin_required
def default(request, domain):
    if not (request.project and request.project.commtrack_enabled):
        raise Http404()
    return HttpResponseRedirect(reverse(ProductListView.urlname,
                                        args=[domain]))


class BaseCommTrackManageView(BaseDomainView):
    section_name = ugettext_noop("Setup")

    @property
    def section_url(self):
        return reverse('default_commtrack_setup', args=[self.domain])

    @method_decorator(domain_admin_required)  # TODO: will probably want less restrictive permission?
    def dispatch(self, request, *args, **kwargs):
        return super(BaseCommTrackManageView, self).dispatch(request, *args, **kwargs)


class CommTrackSettingsView(BaseCommTrackManageView):
    urlname = 'commtrack_settings'
    page_title = ugettext_noop("Advanced Settings")
    template_name = 'domain/admin/commtrack_settings.html'

    @property
    @memoized
    def commtrack_settings(self):
        return self.domain_object.commtrack_settings

    @property
    def page_context(self):
        return {
            'form': self.commtrack_settings_form
        }

    @property
    @memoized
    def commtrack_settings_form(self):
        from corehq.apps.commtrack.forms import CommTrackSettingsForm
        initial = self.commtrack_settings.to_json()
        initial.update(dict(('consumption_' + k, v) for k, v in
            self.commtrack_settings.consumption_config.to_json().items()))
        initial.update(dict(('stock_' + k, v) for k, v in
            self.commtrack_settings.stock_levels_config.to_json().items()))

        if self.request.method == 'POST':
            return CommTrackSettingsForm(self.request.POST, initial=initial, domain=self.domain)
        return CommTrackSettingsForm(initial=initial, domain=self.domain)

    def set_ota_restore_config(self):
        """
        If the checkbox for syncing consumption fixtures is
        checked, then we build the restore config with appropriate
        special properties, otherwise just clear the object.

        If there becomes a way to tweak these on the UI, this should
        be done differently.
        """

        from corehq.apps.commtrack.models import StockRestoreConfig
        if self.commtrack_settings.sync_consumption_fixtures:
            self.domain_object.commtrack_settings.ota_restore_config = StockRestoreConfig(
                section_to_consumption_types={
                    'stock': 'consumption'
                },
                force_consumption_case_types=[
                    'supply-point'
                ],
                use_dynamic_product_list=True,
            )
        else:
            self.domain_object.commtrack_settings.ota_restore_config = StockRestoreConfig()

    def post(self, request, *args, **kwargs):
        if self.commtrack_settings_form.is_valid():
            data = self.commtrack_settings_form.cleaned_data
            previous_config = copy.copy(self.commtrack_settings)
            self.commtrack_settings.use_auto_consumption = bool(data.get('use_auto_consumption'))
            self.commtrack_settings.sync_location_fixtures = bool(data.get('sync_location_fixtures'))
            self.commtrack_settings.sync_consumption_fixtures = bool(data.get('sync_consumption_fixtures'))
            self.commtrack_settings.individual_consumption_defaults = bool(data.get('individual_consumption_defaults'))

            self.set_ota_restore_config()

            fields = ('emergency_level', 'understock_threshold', 'overstock_threshold')
            for field in fields:
                if data.get('stock_' + field):
                    setattr(self.commtrack_settings.stock_levels_config, field,
                            data['stock_' + field])

            consumption_fields = ('min_transactions', 'min_window', 'optimal_window')
            for field in consumption_fields:
                if data.get('consumption_' + field):
                    setattr(self.commtrack_settings.consumption_config, field,
                            data['consumption_' + field])

            self.commtrack_settings.save()


            if (previous_config.use_auto_consumption != self.commtrack_settings.use_auto_consumption
                or previous_config.consumption_config.to_json() != self.commtrack_settings.consumption_config.to_json()
            ):
                # kick off delayed consumption rebuild
                recalculate_domain_consumption_task.delay(self.domain)
                messages.success(request, _("Settings updated! Your updated consumption settings may take a "
                                            "few minutes to show up in reports and on phones."))
            else:
                messages.success(request, _("Settings updated!"))
            return HttpResponseRedirect(self.page_url)
        return self.get(request, *args, **kwargs)


class DefaultConsumptionView(BaseCommTrackManageView):
    urlname = 'update_default_consumption'
    template_name = 'commtrack/manage/default_consumption.html'
    page_title = ugettext_noop("Consumption")

    @property
    @memoized
    def consumption_form(self):
        if self.request.method == 'POST':
            return ConsumptionForm(self.domain, self.request.POST)
        return ConsumptionForm(self.domain)

    @property
    def page_context(self):
        return {
            'form': self.consumption_form,
        }

    def post(self, request, *args, **kwargs):
        if self.consumption_form.is_valid():
            self.consumption_form.save()
            messages.success(request, _("Default consumption values updated"))
            return HttpResponseRedirect(
                reverse(DefaultConsumptionView.urlname, args=[self.domain])
            )
        return self.get(request, *args, **kwargs)


class ProductFieldsView(CustomDataFieldsMixin, BaseCommTrackManageView):
    urlname = 'product_fields_view'
    field_type = 'ProductFields'
    entity_string = _("Product")


@require_POST
@domain_admin_required
def delete_program(request, domain, prog_id):
    program = Program.get(prog_id)
    program.delete()
    return json_response({
        'success': True,
        'message': _("Program '{program_name}' has successfully been deleted.").format(
            program_name=program.name,
        )
    })


@require_POST
@domain_admin_required
def archive_product(request, domain, prod_id, archive=True):
    """
    Archive product
    """
    product = Product.get(prod_id)
    product.archive()
    return json_response({
        'success': True,
        'message': _("Product '{product_name}' has successfully been {action}.").format(
            product_name=product.name,
            action="archived",
        )
    })


@require_POST
@domain_admin_required
def unarchive_product(request, domain, prod_id, archive=True):
    """
    Unarchive product
    """
    product = Product.get(prod_id)
    product.unarchive()
    return json_response({
        'success': True,
        'message': _("Product '{product_name}' has successfully been {action}.").format(
            product_name=product.name,
            action="unarchived",
        )
    })


class ProductListView(BaseCommTrackManageView):
    # todo mobile workers shares this type of view too---maybe there should be a class for this?
    urlname = 'commtrack_product_list'
    template_name = 'commtrack/manage/products.html'
    page_title = ugettext_noop("Products")

    DEFAULT_LIMIT = 10

    @property
    def page(self):
        return self.request.GET.get('page', 1)

    @property
    def limit(self):
        return self.request.GET.get('limit', self.DEFAULT_LIMIT)

    @property
    def show_inactive(self):
        return json.loads(self.request.GET.get('show_inactive', 'false'))

    @property
    @memoized
    def total(self):
        return Product.count_by_domain(self.domain)

    @property
    def page_context(self):
        return {
            'data_list': {
                'page': self.page,
                'limit': self.limit,
                'total': self.total
            },
            'archive_help_text': _(
                "Archive a product to stop showing data for it in \
                reports and on mobile applications. Archiving is \
                completely reversible, so you can always reactivate \
                it later."
            ),
            'show_inactive': self.show_inactive,
            'pagination_limit_options': range(self.DEFAULT_LIMIT, 51, self.DEFAULT_LIMIT)
        }


class FetchProductListView(ProductListView):
    urlname = 'commtrack_product_fetch'

    def skip(self):
        return (int(self.page) - 1) * int(self.limit)

    def get_archive_text(self, is_archived):
        if is_archived:
            return _("This will re-activate the product, and the product will show up in reports again.")
        return _("As a result of archiving, this product will no longer appear in reports. "
                 "This action is reversable; you can reactivate this product by viewing "
                 "Show Archived Products and clicking 'Unarchive'.")

    @property
    def product_data(self):
        data = []
        if self.show_inactive:
            products = Product.archived_by_domain(
                domain=self.domain,
                limit=self.limit,
                skip=self.skip(),
            )
        else:
            products = Product.by_domain(
                domain=self.domain,
                limit=self.limit,
                skip=self.skip(),
            )

        for p in products:
            if p.program_id:
                program = Program.get(p.program_id)
            else:
                program = get_or_create_default_program(self.domain)
                p.program_id = program.get_id
                p.save()

            info = p._doc
            info['program'] = program.name
            info['edit_url'] = reverse('commtrack_product_edit', kwargs={'domain': self.domain, 'prod_id': p._id})
            info['archive_action_desc'] = self.get_archive_text(self.show_inactive)
            info['archive_action_text'] = _("Un-Archive") if self.show_inactive else _("Archive")
            info['archive_url'] = reverse(
                'unarchive_product' if self.show_inactive else 'archive_product',
                kwargs={'domain': self.domain, 'prod_id': p._id}
            )
            data.append(info)
        return data

    def get(self, request, *args, **kwargs):
        return HttpResponse(json.dumps({
            'success': True,
            'current_page': self.page,
            'data_list': self.product_data,
        }), 'text/json')


class NewProductView(BaseCommTrackManageView):
    urlname = 'commtrack_product_new'
    page_title = ugettext_noop("New Product")
    template_name = 'commtrack/manage/product.html'

    @property
    @memoized
    def product(self):
        return Product(domain=self.domain)

    @property
    def parent_pages(self):
        return [{
            'title': ProductListView.page_title,
            'url': reverse(ProductListView.urlname, args=[self.domain]),
        }]

    @property
    @memoized
    def new_product_form(self):
        if self.request.method == 'POST':
            return ProductForm(self.product, self.request.POST)
        return ProductForm(self.product)

    @property
    def page_context(self):
        return {
            'product': self.product,
            'form': self.new_product_form,
            'data_fields_form': self.custom_data.form,
        }

    @property
    @memoized
    def custom_data(self):
        return CustomDataEditor(
            field_view=ProductFieldsView,
            domain=self.domain,
            required_only=True,
            post_dict=self.request.POST if self.request.method == "POST" else None,
        )

    def post(self, request, *args, **kwargs):
        if all([self.new_product_form.is_valid(),
                self.custom_data.is_valid()]):
            self.product.product_data = self.custom_data.get_data_to_save()
            self.new_product_form.save(self.product)
            messages.success(request, _("Product saved!"))
            return HttpResponseRedirect(reverse(ProductListView.urlname, args=[self.domain]))
        return self.get(request, *args, **kwargs)


class UploadProductView(BaseCommTrackManageView):
    urlname = 'commtrack_upload_products'
    page_title = ugettext_noop("Import Products")
    template_name = 'commtrack/manage/upload_products.html'

    @property
    def page_context(self):
        context = {
            'bulk_upload': {
                "download_url": reverse("product_export", args=(self.domain,)),
                "adjective": _("product"),
                "plural_noun": _("products"),
            },
        }
        context.update({
            'bulk_upload_form': get_bulk_upload_form(context),
        })
        return context

    @property
    def parent_pages(self):
        return [{
            'title': ProductListView.page_title,
            'url': reverse(ProductListView.urlname, args=[self.domain]),
        }]

    def post(self, request, *args, **kwargs):
        upload = request.FILES.get('bulk_upload_file')
        if not upload:
            messages.error(request, _('no file uploaded'))
            return self.get(request, *args, **kwargs)
        elif not upload.name.endswith('.xlsx'):
            messages.error(request, _('please use xlsx format only'))
            return self.get(request, *args, **kwargs)

        domain = args[0]
        # stash this in soil to make it easier to pass to celery
        file_ref = expose_download(upload.read(),
                                   expiry=1*60*60)
        task = import_products_async.delay(
            domain,
            file_ref.download_id,
        )
        file_ref.set_task(task)
        return HttpResponseRedirect(
            reverse(
                ProductImportStatusView.urlname,
                args=[domain, file_ref.download_id]
            )
        )

class ProductImportStatusView(BaseCommTrackManageView):
    urlname = 'product_import_status'
    page_title = ugettext_noop('Product Import Status')

    def get(self, request, *args, **kwargs):
        context = super(ProductImportStatusView, self).main_context
        context.update({
            'domain': self.domain,
            'download_id': kwargs['download_id'],
            'poll_url': reverse('product_importer_job_poll', args=[self.domain, kwargs['download_id']]),
            'title': _("Product Import Status"),
            'progress_text': _("Importing your data. This may take some time..."),
            'error_text': _("Problem importing data! Please try again or report an issue."),
        })
        return render(request, 'hqwebapp/soil_status_full.html', context)

    def page_url(self):
        return reverse(self.urlname, args=self.args, kwargs=self.kwargs)

@login_and_domain_required
def product_importer_job_poll(request, domain, download_id,
        template="commtrack/manage/partials/product_upload_status.html"):
    context = get_download_context(download_id, check_state=True)
    context.update({
        'on_complete_short': _('Import complete.'),
        'on_complete_long': _('Product importing has finished'),

    })
    return render(request, template, context)


def download_products(request, domain):
    def _parse_custom_properties(product):
        product_data_model = CustomDataFieldsDefinition.get_or_create(
            domain,
            ProductFieldsView.field_type
        )
        product_data_fields = [f.slug for f in product_data_model.fields]

        model_data = {}
        uncategorized_data = {}

        for prop, val in product.product_data.iteritems():
            if prop in product_data_fields:
                model_data['data: ' + prop] = encode_if_needed(val)
            else:
                uncategorized_data['uncategorized_data: ' + prop] = encode_if_needed(val)

        return model_data, uncategorized_data

    def _get_products(domain):
        for p_doc in iter_docs(Product.get_db(), Product.ids_by_domain(domain)):
            # filter out archived products from export
            if not ('is_archived' in p_doc and p_doc['is_archived']):
                yield Product.wrap(p_doc)

    def _build_row(keys, product):
        row = []
        for key in keys:
            row.append(product.get(key, '') or '')

        return row

    file = StringIO()
    writer = Excel2007ExportWriter()

    product_keys = [
        'id',
        'name',
        'unit',
        'product_id',
        'description',
        'category',
        'program_id',
        'cost',
    ]

    model_data = set()
    uncategorized_data = set()
    products = []

    for product in _get_products(domain):
        product_dict = product.to_dict()

        product_model, product_uncategorized = _parse_custom_properties(product)

        model_data.update(product_model.keys())
        uncategorized_data.update(product_uncategorized.keys())

        product_dict.update(product_model)
        product_dict.update(product_uncategorized)

        products.append(product_dict)

    keys = product_keys + list(model_data) + list(uncategorized_data)

    writer.open(
        header_table=[
            ('products', [keys])
        ],
        file=file,
    )

    for product in products:
        writer.write([('products', [_build_row(keys, product)])])

    writer.close()

    response = HttpResponse(mimetype=Format.from_format('xlsx').mimetype)
    response['Content-Disposition'] = 'attachment; filename="products.xlsx"'
    response.write(file.getvalue())
    return response


class EditProductView(NewProductView):
    urlname = 'commtrack_product_edit'
    page_title = ugettext_noop("Edit Product")

    @property
    def product_id(self):
        try:
            return self.kwargs['prod_id']
        except KeyError:
            raise Http404()

    @property
    @memoized
    def product(self):
        try:
            return Product.get(self.product_id)
        except ResourceNotFound:
            raise Http404()

    @property
    def page_name(self):
        return _("Edit %s") % self.product.name

    @property
    def page_url(self):
        return reverse(self.urlname, args=[self.domain, self.product_id])

    @property
    @memoized
    def custom_data(self):
        return CustomDataEditor(
            field_view=ProductFieldsView,
            domain=self.domain,
            existing_custom_data=self.product.product_data,
            post_dict=self.request.POST if self.request.method == "POST" else None,
        )


@login_and_domain_required
def api_query_supply_point(request, domain):
    id = request.GET.get('id')
    query = request.GET.get('name', '')
    
    def loc_to_payload(loc):
        return {'id': loc._id, 'name': loc.name}

    if id:
        try:
            loc = Location.get(id)
            return HttpResponse(json.dumps(loc_to_payload(loc)), 'text/json')

        except ResourceNotFound:
            return HttpResponseNotFound(json.dumps({'message': 'no location with is %s found' % id}, 'text/json'))

    else:
        LIMIT = 100
        loc_types = [loc_type.name for loc_type in Domain.get_by_name(domain).commtrack_settings.location_types if not loc_type.administrative]

        def get_locs(type):
            # TODO use ES instead?
            q = query.lower()
            startkey = [domain, type, q]
            endkey = [domain, type, q + 'zzzzzz']
            return [loc for loc in Location.view(
                'locations/by_name',
                startkey=startkey,
                endkey=endkey,
                limit=LIMIT,
                reduce=False,
                include_docs=True,
            ) if not loc.is_archived]

        locs = sorted(
            itertools.chain(*(get_locs(loc_type) for loc_type in loc_types)),
            key=lambda e: e.name
        )[:LIMIT]
        return HttpResponse(json.dumps(map(loc_to_payload, locs)), 'text/json')


class ProgramListView(BaseCommTrackManageView):
    urlname = 'commtrack_program_list'
    template_name = 'commtrack/manage/programs.html'
    page_title = ugettext_noop("Programs")


class FetchProgramListView(ProgramListView):
    urlname = 'commtrack_program_fetch'

    @property
    def program_data(self):
        data = []
        programs = Program.by_domain(self.domain)
        for p in programs:
            info = p._doc
            info['is_default'] = info.pop('default')
            info['edit_url'] = reverse('commtrack_program_edit', kwargs={'domain': self.domain, 'prog_id': p._id})
            info['delete_url'] = reverse('delete_program', kwargs={'domain': self.domain, 'prog_id': p._id})
            data.append(info)
        return data

    def get(self, request, *args, **kwargs):
        return HttpResponse(json.dumps({
            'success': True,
            'data_list': self.program_data,
        }), 'text/json')


class NewProgramView(BaseCommTrackManageView):
    urlname = 'commtrack_program_new'
    page_title = ugettext_noop("New Program")
    template_name = 'commtrack/manage/program.html'

    @property
    @memoized
    def program(self):
        return Program(domain=self.domain)

    @property
    def parent_pages(self):
        return [{
            'title': ProgramListView.page_title,
            'url': reverse(ProgramListView.urlname, args=[self.domain]),
        }]

    @property
    @memoized
    def new_program_form(self):
        if self.request.method == 'POST':
            return ProgramForm(self.program, self.request.POST)
        return ProgramForm(self.program)

    @property
    def page_context(self):
        return {
            'program': self.program,
            'form': self.new_program_form,
        }

    def post(self, request, *args, **kwargs):
        if self.new_program_form.is_valid():
            self.new_program_form.save()
            messages.success(request, _("Program saved!"))
            return HttpResponseRedirect(reverse(ProgramListView.urlname, args=[self.domain]))
        return self.get(request, *args, **kwargs)


class EditProgramView(NewProgramView):
    urlname = 'commtrack_program_edit'
    page_title = ugettext_noop("Edit Program")

    DEFAULT_LIMIT = 10

    @property
    def page(self):
        return self.request.GET.get('page', 1)

    @property
    def limit(self):
        return self.request.GET.get('limit', self.DEFAULT_LIMIT)

    @property
    def total(self):
        return len(Product.by_program_id(self.domain, self.program_id))

    @property
    def page_context(self):
        return {
            'program': self.program,
            'data_list': {
                'page': self.page,
                'limit': self.limit,
                'total': self.total
            },
            'pagination_limit_options': range(self.DEFAULT_LIMIT, 51, self.DEFAULT_LIMIT),
            'form': self.new_program_form,
        }

    @property
    def program_id(self):
        try:
            return self.kwargs['prog_id']
        except KeyError:
            raise Http404()

    @property
    @memoized
    def program(self):
        try:
            return Program.get(self.program_id)
        except ResourceNotFound:
            raise Http404()

    @property
    def page_name(self):
        return _("Edit %s") % self.program.name

    @property
    def page_url(self):
        return reverse(self.urlname, args=[self.domain, self.program_id])


class FetchProductForProgramListView(EditProgramView):
    urlname = 'commtrack_product_for_program_fetch'

    def skip(self):
        return (int(self.page) - 1) * int(self.limit)

    @property
    def product_data(self):
        def _scrub(product_doc):
            product_doc['code'] = product_doc.pop('code_')
            return product_doc

        data = []
        products = Product.by_program_id(domain=self.domain, prog_id=self.program_id, skip=self.skip(),
                limit=self.limit)
        for p in products:
            data.append(_scrub(p._doc))
        return data

    def get(self, request, *args, **kwargs):
        return HttpResponse(json.dumps({
            'success': True,
            'current_page': self.page,
            'data_list': self.product_data,
        }), 'text/json')


class SMSSettingsView(BaseCommTrackManageView):
    urlname = 'commtrack_sms_settings'
    page_title = ugettext_noop("SMS")
    template_name = 'domain/admin/sms_settings.html'

    @property
    def page_context(self):
        return {
            'other_sms_codes': dict(self.get_other_sms_codes()),
            'settings': self.settings_context,
        }

    @property
    def settings_context(self):
        return {
            'keyword': self.domain_object.commtrack_settings.multiaction_keyword,
            'actions': [self._get_action_info(a) for a in self.domain_object.commtrack_settings.actions],
            'requisition_config': {
                'enabled': self.domain_object.commtrack_settings.requisition_config.enabled,
                'actions': [self._get_action_info(a) for a in self.domain_object.commtrack_settings.requisition_config.actions],
            },
        }

    # FIXME
    def _get_action_info(self, action):
        return {
            'type': action.action,
            'keyword': action.keyword,
            'name': action.subaction,
            'caption': action.caption,
        }

    def get_other_sms_codes(self):
        for k, v in all_sms_codes(self.domain).iteritems():
            if v[0] == 'product':
                yield (k, (v[0], v[1].name))

    def post(self, request, *args, **kwargs):
        from corehq.apps.commtrack.models import CommtrackActionConfig

        payload = json.loads(request.POST.get('json'))

        self.domain_object.commtrack_settings.multiaction_keyword = payload['keyword']

        def mk_action(action):
            return CommtrackActionConfig(**{
                    'action': action['type'],
                    'subaction': action['caption'],
                    'keyword': action['keyword'],
                    'caption': action['caption'],
                })

        #TODO add server-side input validation here (currently validated on client)

        self.domain_object.commtrack_settings.actions = [mk_action(a) for a in payload['actions']]
        self.domain_object.commtrack_settings.requisition_config.enabled = payload['requisition_config']['enabled']
        self.domain_object.commtrack_settings.requisition_config.actions = [mk_action(a) for a in payload['requisition_config']['actions']]

        self.domain_object.commtrack_settings.save()

        return self.get(request, *args, **kwargs)
