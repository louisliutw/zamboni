from django import http

import commonware
import requests
import waffle
from rest_framework.exceptions import ParseError
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from lib.metrics import get_monolith_client

import amo
from stats.models import Contribution

from mkt.api.authentication import (RestOAuthAuthentication,
                                    RestSharedSecretAuthentication)
from mkt.api.authorization import AllowAppOwner, AnyOf, GroupPermission
from mkt.api.base import CORSMixin, SlugOrIdMixin
from mkt.api.exceptions import NotImplemented, ServiceUnavailable
from mkt.webapps.models import Webapp

from .forms import StatsForm


log = commonware.log.getLogger('z.stats')


# Map of URL metric name to monolith metric name.
#
# The 'dimensions' key is optional query string arguments with defaults that is
# passed to the monolith client and used in the facet filters. If the default
# is `None`, the dimension is excluded unless specified via the API.
#
# The 'lines' key is optional and used for multi-line charts. The format is:
#     {'<name>': {'<dimension-key>': '<dimension-value>'}}
# where <name> is what's returned in the JSON output and the dimension
# key/value is what's sent to Monolith similar to the 'dimensions' above.
#
# The 'coerce' key is optional and used to coerce data types returned from
# monolith to other types. Provide the name of the key in the data you want to
# coerce with a callback for how you want the data coerced. E.g.:
#   {'count': str}
lines = lambda name, vals: dict((val, {name: val}) for val in vals)
STATS = {
    'apps_added_by_package': {
        'metric': 'apps_added_package_count',
        'dimensions': {'region': 'us'},
        'lines': lines('package_type', amo.ADDON_WEBAPP_TYPES.values()),
    },
    'apps_added_by_premium': {
        'metric': 'apps_added_premium_count',
        'dimensions': {'region': 'us'},
        'lines': lines('premium_type', amo.ADDON_PREMIUM_API.values()),
    },
    'apps_available_by_package': {
        'metric': 'apps_available_package_count',
        'dimensions': {'region': 'us'},
        'lines': lines('package_type', amo.ADDON_WEBAPP_TYPES.values()),
    },
    'apps_available_by_premium': {
        'metric': 'apps_available_premium_count',
        'dimensions': {'region': 'us'},
        'lines': lines('premium_type', amo.ADDON_PREMIUM_API.values()),
    },
    'apps_installed': {
        'metric': 'app_installs',
        'dimensions': {'region': None},
    },
    'total_developers': {
        'metric': 'total_dev_count'
    },
    'total_visits': {
        'metric': 'visits'
    },
    'revenue': {
        'metric': 'gross_revenue',
        # Counts are floats. Let's convert them to strings with 2 decimals.
        'coerce': {'count': lambda d: '{0:.2f}'.format(d)},
    },
}
APP_STATS = {
    'installs': {
        'metric': 'app_installs',
        'dimensions': {'region': None},
    },
    'visits': {
        'metric': 'app_visits',
    },
    'revenue': {
        'metric': 'gross_revenue',
        # Counts are floats. Let's convert them to strings with 2 decimals.
        'coerce': {'count': lambda d: '{0:.2f}'.format(d)},
    },
}


def _get_monolith_data(stat, start, end, interval, dimensions):
    # If stat has a 'lines' attribute, it's a multi-line graph. Do a
    # request for each item in 'lines' and compose them in a single
    # response.
    try:
        client = get_monolith_client()
    except requests.ConnectionError as e:
        log.info('Monolith connection error: {0}'.format(e))
        raise ServiceUnavailable

    def _coerce(data):
        for key, coerce in stat.get('coerce', {}).items():
            if data.get(key):
                data[key] = coerce(data[key])

        return data

    try:
        data = {}
        if 'lines' in stat:
            for line_name, line_dimension in stat['lines'].items():
                dimensions.update(line_dimension)
                data[line_name] = map(_coerce,
                                      client(stat['metric'], start, end,
                                             interval, **dimensions))

        else:
            data['objects'] = map(_coerce,
                                  client(stat['metric'], start, end, interval,
                                         **dimensions))

    except ValueError as e:
        # This occurs if monolith doesn't have our metric and we get an
        # elasticsearch SearchPhaseExecutionException error.
        log.info('Monolith ValueError for metric {0}: {1}'.format(
            stat['metric'], e))
        raise ParseError('Invalid metric at this time. Try again later.')

    return data


class GlobalStats(CORSMixin, APIView):
    authentication_classes = (RestOAuthAuthentication,
                              RestSharedSecretAuthentication)
    cors_allowed_methods = ['get']
    permission_classes = [GroupPermission('Stats', 'View')]

    def get(self, request, metric):
        if metric not in STATS:
            raise http.Http404('No metric by that name.')

        if not waffle.switch_is_active('stats-api'):
            raise NotImplemented('Stats not enabled for this host.')

        stat = STATS[metric]

        # Perform form validation.
        form = StatsForm(request.GET)
        if not form.is_valid():
            raise ParseError(dict(form.errors.items()))

        qs = form.cleaned_data

        dimensions = {}
        if 'dimensions' in stat:
            for key, default in stat['dimensions'].items():
                val = request.GET.get(key, default)
                if val is not None:
                    # Avoid passing kwargs to the monolith client when the
                    # dimension is None to avoid facet filters being applied.
                    dimensions[key] = request.GET.get(key, default)

        return Response(_get_monolith_data(stat, qs.get('start'),
                                           qs.get('end'), qs.get('interval'),
                                           dimensions))


class AppStats(CORSMixin, SlugOrIdMixin, ListAPIView):
    authentication_classes = (RestOAuthAuthentication,
                              RestSharedSecretAuthentication)
    cors_allowed_methods = ['get']
    permission_classes = [AnyOf(AllowAppOwner,
                                GroupPermission('Stats', 'View'))]
    queryset = Webapp.objects.all()
    slug_field = 'app_slug'

    def get(self, request, pk, metric):
        if metric not in APP_STATS:
            raise http.Http404('No metric by that name.')

        if not waffle.switch_is_active('stats-api'):
            raise NotImplemented('Stats not enabled for this host.')

        app = self.get_object()

        stat = APP_STATS[metric]

        # Perform form validation.
        form = StatsForm(request.GET)
        if not form.is_valid():
            raise ParseError(dict(form.errors.items()))

        qs = form.cleaned_data

        dimensions = {'app-id': app.id}

        if 'dimensions' in stat:
            for key, default in stat['dimensions'].items():
                val = request.GET.get(key, default)
                if val is not None:
                    # Avoid passing kwargs to the monolith client when the
                    # dimension is None to avoid facet filters being applied.
                    dimensions[key] = request.GET.get(key, default)

        return Response(_get_monolith_data(stat, qs.get('start'),
                                           qs.get('end'), qs.get('interval'),
                                           dimensions))


class TransactionAPI(CORSMixin, APIView):
    """
    API to query by transaction ID.

    Note: This is intended for Monolith to be able to associate a Solitude
    transaction with an app and price tier amount in USD.

    """
    authentication_classes = (RestOAuthAuthentication,
                              RestSharedSecretAuthentication)
    cors_allowed_methods = ['get']
    permission_classes = [GroupPermission('RevenueStats', 'View')]

    def get(self, request, transaction_id):
        try:
            contrib = (Contribution.objects.select_related('price_tier').
                       get(transaction_id=transaction_id))
        except Contribution.DoesNotExist:
            raise http.Http404('No transaction by that ID.')

        data = {
            'id': transaction_id,
            'app_id': contrib.addon_id,
            'amount_USD': contrib.price_tier.price,
            'type': amo.CONTRIB_TYPES[contrib.type],
        }

        return Response(data)
