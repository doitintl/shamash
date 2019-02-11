"""Settings Class and utils"""
import googleapiclient.discovery
from google.appengine.ext import ndb
from google.auth import app_engine

from monitoring import metrics
from util import utils


SCOPES = ['https://www.googleapis.com/auth/cloud-platform']
CREDENTIALS = app_engine.Credentials(scopes=SCOPES)


def get_regions():
    """
    Get all available regions.

    :return: all regions
    """
    compute = googleapiclient.discovery.build('compute', 'v1')

    request = compute.regions().list(project=utils.get_project_id())

    response = request.execute()
    rg = []
    for region in response['items']:
        rg.append(region['description'])
    rg.append('global')
    rg.sort()
    return rg


class Settings(ndb.Model):
    """Setting management for Shamash."""
    Enabled = ndb.BooleanProperty(required=True, default=True)
    Cluster = ndb.StringProperty(indexed=True, required=True)
    Region = ndb.StringProperty(
        choices=get_regions(), default='us-east1', required=True)
    AddRemoveUpDelta = ndb.IntegerProperty(default=0, required=False)
    AddRemoveDownDelta = ndb.IntegerProperty(default=0, required=False)
    UseMemoryForScaling = ndb.BooleanProperty(required=True, default=True)
    UpYARNMemAvailPct = ndb.IntegerProperty(default=15, required=True)
    DownYARNMemAvailePct = ndb.IntegerProperty(default=75, required=True)
    UpContainerPendingRatio = ndb.FloatProperty(default=1, required=True)
    DownContainerPendingRatio = ndb.FloatProperty(default=1, required=True)
    PreemptiblePct = ndb.IntegerProperty(default=80, required=True)
    MaxInstances = ndb.IntegerProperty(default=10, required=True)
    MinInstances = ndb.IntegerProperty(default=2, required=True)
    GracefulDecommissionTimeout = ndb.IntegerProperty(
        default=0, required=False)

    def _post_put_hook(self, future):
        met = metrics.Metrics(self.Cluster)
        met.init_metrics()


def get_cluster_settings(cluster_name):
    """

    :param cluster_name:
    :return:
    """
    return Settings.query(Settings.Cluster == cluster_name)


def get_all_clusters_settings():
    """
    Get all entities of setting kind.

    :return:
    """
    return Settings.query()
