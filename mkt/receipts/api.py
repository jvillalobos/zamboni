import commonware.log

from tastypie import http
from tastypie.authorization import Authorization
from tastypie.exceptions import ImmediateHttpResponse

import amo
from amo.utils import memoize_get

from access.acl import check_ownership
from lib.cef_loggers import receipt_cef
from lib.metrics import record_action
from mkt.api.authentication import OAuthAuthentication
from mkt.api.base import CORSResource, MarketplaceResource
from mkt.api.http import HttpPaymentRequired
from mkt.constants import apps
from mkt.receipts.forms import ReceiptForm
from mkt.receipts.utils import create_receipt
from mkt.webapps.models import Installed

log = commonware.log.getLogger('z.receipt')


class ReceiptResource(CORSResource, MarketplaceResource):

    class Meta(MarketplaceResource.Meta):
        always_return_data = True
        authentication = OAuthAuthentication()
        authorization = Authorization()
        detail_allowed_methods = []
        list_allowed_methods = ['post']
        object_class = dict
        resource_name = 'install'

    def obj_create(self, bundle, request=None, **kwargs):
        bundle.data['receipt'] = self.handle(bundle, request=request, **kwargs)
        amo.log(amo.LOG.INSTALL_ADDON, bundle.obj)
        record_action('install', request, {
            'app-domain': bundle.obj.domain_from_url(bundle.obj.origin),
            'app-id': bundle.obj.pk,
            'anonymous': request.user.is_anonymous(),
        })
        return bundle

    def get_resource_uri(self, bundle_or_obj=None,
                         url_name='api_dispatch_list'):
        # When we fix bug 845856, remove this.
        return ''

    def handle(self, bundle, request, **kwargs):
        form = ReceiptForm(bundle.data)

        if not form.is_valid():
            raise self.form_errors(form)

        bundle.obj = form.cleaned_data['app']

        # Developers get handled quickly.
        if check_ownership(request, bundle.obj, require_owner=False,
                           ignore_disabled=True, admin=False):
            return self.record(bundle, request, apps.INSTALL_TYPE_DEVELOPER)

        # The app must be public and if its a premium app, you
        # must have purchased it.
        if not bundle.obj.is_public():
            log.info('App not public: %s' % bundle.obj.pk)
            raise ImmediateHttpResponse(response=http.HttpForbidden())

        if (bundle.obj.is_premium() and
                not bundle.obj.has_purchased(request.amo_user)):
            log.info('App not purchased: %s' % bundle.obj.pk)
            raise ImmediateHttpResponse(response=HttpPaymentRequired())

        # Anonymous users will fall through, they don't need anything else
        # handling.
        if request.user.is_authenticated():
            return self.record(bundle, request, apps.INSTALL_TYPE_USER)

    def record(self, bundle, request, install_type):
        # Generate or re-use an existing install record.
        installed, created = Installed.objects.safer_get_or_create(
            addon=bundle.obj, user=request.user.get_profile(),
            install_type=install_type)

        # Generate or re-use a recent receipt.
        receipt_cef.log(request, bundle.obj, 'request', 'Receipt requested')
        receipt = memoize_get('create-receipt', installed.pk)
        if receipt:
            return receipt

        receipt_cef.log(request, bundle.obj, 'sign', 'Receipt signing')
        return create_receipt(installed.pk)
