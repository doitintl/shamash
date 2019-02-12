"""Handling metrics."""
import datetime
import logging

import backoff
import googleapiclient.discovery
from google.auth import app_engine
from googleapiclient.errors import HttpError

from util import utils


SCOPES = ('https://www.googleapis.com/auth/monitoring',
          'https://www.googleapis.com/auth/cloud-platform')
CREDENTIALS = app_engine.Credentials(scopes=SCOPES)

sh = logging.StreamHandler() # Log to stderr
sh.setLevel(logging.DEBUG)
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
sh.setFormatter(formatter)
logger = logging.getLogger(__name__)
logger.addHandler(sh)


def format_rfc3339(datetime_instance=None):
    """Format a datetime per RFC 3339.

    :param datetime_instance: Datetime instance to format, defaults to utcnow
    """
    return datetime_instance.isoformat("T") + "Z"


def get_now_rfc3339():
    """
    Return formatted time.

    :return:
    """
    # Return now
    return format_rfc3339(datetime.datetime.utcnow())


def get_start_time(minutes):
    """
    Create start time for minuets from now.

    :param minutes:
    :return:
    """
    # Return now- 5 minutes
    start_time = datetime.datetime.utcnow() - datetime.timedelta(
        minutes=minutes)
    return format_rfc3339(start_time)


class Metrics(object):
    """Writing and reading metrics."""

    def __init__(self, cluster_name):
        self.monitorservice = googleapiclient.discovery. \
            build('monitoring',
                  'v3',
                  credentials=CREDENTIALS)
        self.project_id = utils.get_project_id()
        self.project_resource = "projects/{0}".format(self.project_id)
        self.metric_domain = 'custom.googleapis.com'
        self.cluster_name = cluster_name
        self.metrics = [
            'ContainerPendingRatio', 'YARNMemoryAvailablePercentage',
            'YarnNodes', 'Workers', 'PreemptibleWorkers'
        ]

    def init_metrics(self):
        """Make sure that we have all of our custom metrics."""
        for met in self.metrics:
            if not self._custom_metric_exists(met):
                self._create_custom_metric(met)

    def write_timeseries_value(self, custom_metric_type, data_point):
        """Write the custom metric obtained."""
        now = get_now_rfc3339()
        custom_metric = "{}/{}".format(self.metric_domain, custom_metric_type)
        timeseries_data = {
            'metricKind': 'GAUGE',
            'valueType': 'DOUBLE',
            'points': [{
                'interval': {
                    'startTime': now,
                    'endTime': now
                },
                'value': {
                    'doubleValue': data_point
                }
            }],
            'metric': {
                'type': custom_metric,
                "labels": {
                    'cluster_name': self.cluster_name
                }
            },
            'resource': {
                'type': 'global',
                'labels': {
                    'project_id': self.project_id
                }
            }
        }

        @backoff.on_exception(
            backoff.expo, HttpError, max_tries=3, giveup=utils.fatal_code)
        def _do_request():
            self.monitorservice.projects().timeSeries().create(
                name=self.project_resource,
                body={'timeSeries': [timeseries_data]}).execute()

        try:
            _do_request()
            return True
        except HttpError as e:
            logger.error(e)
            return False

    def read_timeseries(self, custom_metric_type, minutes):
        """
        Get the time series from stackdriver.

        :param custom_metric_type:
        :param minutes:
        :return: json object
        """

        out = []
        custom_metric = '{}/{}'.format(self.metric_domain, custom_metric_type)
        default_request_kwargs = dict(
            name=self.project_resource,
            filter='metric.type="{0}" AND metric.labels.cluster_name="{1}"'.
            format(custom_metric, self.cluster_name),
            pageSize=10000,
            interval_startTime=get_start_time(minutes),
            interval_endTime=get_now_rfc3339())

        @backoff.on_exception(
            backoff.expo, HttpError, max_tries=3, giveup=utils.fatal_code)
        def _do_request(next_page_token=None):
            kwargs = default_request_kwargs.copy()
            if next_page_token:
                kwargs['nextPageToken'] = next_page_token
            req = self.monitorservice.projects().timeSeries().list(**kwargs)
            return req.execute()

        try:
            response = _do_request()

            out.extend(response.get('timeSeries', []))

            next_token = response.get('nextPageToken')

            while next_token:
                response = _do_request(next_token)
                out.extend(response.get('timeSeries', []))
                next_token = response.get('nextPageToken')
        except HttpError as e:
            logger.info(e)
        return out

    def _create_custom_metric(self, custom_metric_type):
        """Create custom metric descriptor."""
        self._custom_metric_exists(custom_metric_type)
        custom_metric = "{}/{}".format(self.metric_domain, custom_metric_type)
        metrics_descriptor = {
            'type': custom_metric,
            'metricKind': 'GAUGE',
            'valueType': 'DOUBLE',
            'description': 'Shamash Dataproc scaling',
            'name': "{}/metricDescriptors/{}".format(self.project_resource,
                                                     custom_metric_type),
        }

        @backoff.on_exception(
            backoff.expo, HttpError, max_tries=3, giveup=utils.fatal_code)
        def _do_request():
            self.monitorservice.projects().metricDescriptors().create(
                name=self.project_resource, body=metrics_descriptor).execute()

        try:
            _do_request()
        except HttpError as e:
            logger.error(e)
        return

    def _custom_metric_exists(self, custom_metric_type):
        custom_metric = "{}/{}".format(self.metric_domain, custom_metric_type)

        @backoff.on_exception(
            backoff.expo, HttpError, max_tries=3, giveup=utils.fatal_code)
        def _do_request():
            self.monitorservice.projects().metricDescriptors().list(
                name=self.project_resource,
                filter='metric.type="{0}"'.format(custom_metric),).execute()

        try:
            _do_request()
            return True
        except HttpError as e:
            logger.error(e)
            return False
