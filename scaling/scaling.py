"""Handle scaling."""
import base64
import json
import logging

import numpy as np
from google.appengine.api import taskqueue

from model import settings
from monitoring import dataproc_monitoring, metrics


TIME_SERIES_HISTORY_IN_MINUTES = 60

sh = logging.StreamHandler() # Log to stderr
sh.setLevel(logging.DEBUG)
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
sh.setFormatter(formatter)
logger = logging.getLogger(__name__)
logger.addHandler(sh)


class ScalingException(Exception):
    """Exception class for DataProc functions."""

    def __init__(self, value):
        self.parameter = value

    def __str__(self):
        return repr(self.parameter)


class Scale(object):
    """Class for all scaling operations."""

    def __init__(self, payload):
        data = json.loads(base64.b64decode(payload))
        s = settings.get_cluster_settings(data['cluster'])
        if s.count(1) == 1:
            for st in s:
                self.cluster_settings = st
        else:
            raise ScalingException('Cluster not found!')

        self.total = 0
        self.dataproc = dataproc_monitoring.DataProc(data['cluster'])
        self.scale_to = data['scale_to']
        self.scaling_direction = data['scaling_direction']
        self.containerpendingratio = data['containerpendingratio']
        self.cluster_name = self.cluster_settings.Cluster
        self.preemptible_pct = self.cluster_settings.PreemptiblePct
        self.min_instances = self.cluster_settings.MinInstances
        self.max_instances = self.cluster_settings.MaxInstances
        self.use_memory = self.cluster_settings.UseMemoryForScaling

        self.up_container_pending_ratio = \
            self.cluster_settings.UpContainerPendingRatio
        self.down_container_pending_ratio = \
            self.cluster_settings.DownContainerPendingRatio
        if self.preemptible_pct != 100:
            self.preemptibles_to_workers_ratio = self.preemptible_pct / (
                100 - self.preemptible_pct)
        else:
            self.preemptibles_to_workers_ratio = -1

        try:
            self.cluster_status = self.dataproc.get_cluster_status()
        except dataproc_monitoring.DataProcException as e:
            logger.error(e)
            raise e

        try:
            self.current_nodes = \
                int(self.dataproc.get_yarn_metric('yarn-nodes-active'))
        except dataproc_monitoring.DataProcException as e:
            logger.error(e)
            raise e

    def calc_how_many(self):
        """
        Calculate how  many new nodes of each type we need.

        :return:
        """
        # Backport fix from https://github.com/doitintl/shamash/commit/e265a2ff4de2597455a6ae2590ec4389aeac7eed#diff-716bf9d077901cb0da96275d9b2b2577
        # If none of the following condition is matched no scale should be happen
        # so set by default total to current nodes
        self.total = self.current_nodes
        logger.debug('Called how main new nodes we need')
        logger.debug('self.scale: %s, self.containerpendingratio:'
                     '%s, self.use_memory: %s',
                     self.scale_to, self.containerpendingratio,
                     self.use_memory)
        # No allocated memory so we don't need any workers above the
        # bare minimum
        if self.scale_to != -1:
            logger.info("self.scale_to != -1")
            if self.cluster_settings.AddRemoveDownDelta != 0:
                self.total = max(self.current_nodes -
                                 self.cluster_settings.AddRemoveDownDelta,
                                 self.cluster_settings.MinInstances)
            else:
                self.total = self.min_instances
            logger.debug('No allocated memory lets go down! New workers %s'
                         ' New preemptiable', self.total)
            return

        # pending containers are waiting....
        if self.containerpendingratio != -1:
            logger.info("self.containerpendingratio != -1")
            if self.scaling_direction == 'up':
                direction = 1
            else:
                direction = -1
            yarn_vcores_total, \
                yarn_vcores_allocated, \
                yarn_vcores_pending, \
                yarn_nodes_active = self.dataproc.get_container_data()
            ratio = int(yarn_vcores_total) / int(yarn_nodes_active)
            if self.cluster_settings.AddRemoveUpDelta != 0:
                self.total = self.current_nodes + (
                    direction * self.cluster_settings.AddRemoveUpDelta)
            else:
                self.total = (int(yarn_vcores_allocated) +
                              int(yarn_vcores_pending)) / ratio
            delta_nodes = abs(self.total - yarn_nodes_active)
            logger.debug(
                'yarn_vcores_total %s yarn_vcores_allocated %s pending %s '
                'ratio %s current %s delta %s total %s', yarn_vcores_total,
                yarn_vcores_allocated, yarn_vcores_pending, ratio,
                yarn_nodes_active, delta_nodes * direction, self.total)
            logger.debug('Need more containers! New workers %s  prev %s',
                         self.total, self.current_nodes)
            return

        # no more memory lets get some  nodes. calculate how many memory each
        # node uses. Then calculate how many nodes we need by memory
        # consumption
        if self.use_memory:
            logger.info("no more mem")
            if self.dataproc.get_yarn_memory_available_percentage() == 0:
                yarn_memory_mb_allocated, yarn_memory_mb_pending = \
                    self.dataproc.get_memory_data()
                ratio = float(
                    int(yarn_memory_mb_allocated) / int(self.current_nodes))
                if ratio == 0:
                    ratio = 1
                factor = float(int(yarn_memory_mb_pending) / ratio)
                if self.cluster_settings.AddRemoveUpDelta != 0:
                    self.total = self.current_nodes + \
                        self.cluster_settings.AddRemoveUpDelta
                else:
                    self.total = int(self.current_nodes * factor)
                logger.debug(
                    'yarn_memory_mb_allocated %s pending %s ratio %s factor %s'
                    ' current %s total %s', yarn_memory_mb_allocated,
                    yarn_memory_mb_pending, ratio, factor, self.current_nodes,
                    self.total)
                logger.debug('No More Mem! New workers %s  prev %s',
                             self.total, self.current_nodes)
                return
            self.calc_scale()
        logger.debug('End of how main new nodes we need')

    def do_scale(self):
        """
        Calculate and actually scale the cluster.

        :return:
        """
        logger.debug('Starting do_scale %s', self.current_nodes)
        self.calc_how_many()
        logger.debug('Minimun between total: %s and max_instances: %s',
                     self.total, self.max_instances)
        self.total = min(self.total, self.max_instances)
        logger.info("Scaling to workers %s", self.total)

        if self.total == self.current_nodes:
            logger.debug('Not Modified')
            return 'Not Modified', 200

        # make sure that we have the correct ratio between 2 type of workers
        new_workers, new_preemptible = self.preserve_ratio()

        # do the scaling
        retry_options = taskqueue.TaskRetryOptions(task_retry_limit=0)
        task = taskqueue.add(queue_name='vr-shamash',
                             url="/patch",
                             method='GET',
                             retry_options=retry_options,
                             params={
                                 'cluster_name': self.cluster_name,
                                 'new_workers': new_workers,
                                 'new_preemptible': new_preemptible
                             })
        logger.debug('Task %s enqueued, ETA %s Cluster %s', task.name,
                     task.eta, self.cluster_name)
        return 'ok', 204

    def calc_slope(self, minuets):
        """
        Calculate the slope of available memory change.

        :param: minuets how long to go back in time
        """
        logger.info("calc slope")
        met = metrics.Metrics(self.cluster_name)
        series = met.read_timeseries('YARNMemoryAvailablePercentage', minuets)
        retlist = []
        x = []
        y = []
        retlist.extend(series[0]['points'])
        i = len(retlist)
        for rl in retlist:
            x.insert(0, rl['value']['doubleValue'])
            y.insert(0, i)
            i = i - 1
        try:
            slope, intercept = np.polyfit(x, y, 1)
            logger.debug('Slope is %s', slope)
        except np.RankWarning:
            # not enough data so add remove by 2
            if self.scaling_direction == 'up':
                slope = 1
            else:
                slope = -1
            logger.debug('No Data slope is %s', slope)
        logger.info("Slope %s", str(slope))
        return slope

    def calc_scale(self):
        """
        How many nodes to add.

        :return:
        """

        sl = self.calc_slope(TIME_SERIES_HISTORY_IN_MINUTES)
        if sl != 0:
            slope = (1 / sl)
            logger.debug('Slope is %s', slope)
            if slope > 0:
                if self.cluster_settings.AddRemoveUpDelta != 0:
                    self.total = self.total + \
                        self.cluster_settings.AddRemoveUpDelta
                else:
                    self.total = self.total + slope
            if slope < 0:
                if self.cluster_settings.AddRemoveDownDelta != 0:
                    self.total = self.total - \
                        self.cluster_settings.AddRemoveDownDelta
                else:
                    self.total = self.total + slope
            logger.debug('New workers %s  prev %s', self.total,
                         self.current_nodes)

        logger.info('New workers %s prev %s', self.total, self.current_nodes)

    def preserve_ratio(self):
        """
        Make sure that we have the correct ratio between the 2 types of
        workers.
        """

        scale_ratio = (float(self.cluster_settings.PreemptiblePct) / 100.0)
        new_preemptible = int(round(scale_ratio * self.total))
        new_workers = int(round((1 - scale_ratio) * self.total))
        logger.debug('new_workers %s new_preemptible %s', new_workers,
                     new_preemptible)

        # Make sure that we have the minimum normal workers
        if new_workers < self.min_instances:
            logger.debug('Adjusting minimum as well %s', new_workers)
            diff = self.min_instances - new_workers
            new_workers = self.min_instances
            new_preemptible = new_preemptible - diff

        # Make sure that we didn't fuck up and we have the requested number of
        # preemptible workers
        if self.total > new_workers + new_preemptible:
            logger.debug('Adjusting number of preemptible workers to %s',
                         new_preemptible)
            diff = self.total - (new_workers + new_preemptible)
            new_preemptible = new_preemptible + diff

        new_preemptible = max(0, new_preemptible)
        logger.debug('After adjustment %s %s ', new_workers, new_preemptible)
        return new_workers, new_preemptible
