"""Handle scaling """
import base64
import json
import logging

import numpy as np

from model import settings
from monitoring import dataproc_monitoring, metrics

TIME_SERIES_HISTORY_IN_MINUTES = 60
NO_MORE_MEMORY_STEP = 4


class ScalingException(Exception):
    """
    Exception class for DataProc functions
    """

    def __init__(self, value):
        self.parameter = value

    def __str__(self):
        return repr(self.parameter)


class Scale:
    """
    Class for all scaling operations
    """

    def __init__(self, payload):
        data = json.loads(base64.b64decode(payload))
        s = settings.get_cluster_settings(data['cluster'])
        if s.count(1) == 1:
            for st in s:
                self.cluster_settings = st
        else:
            raise ScalingException("Cluster not found!")
        self.total = 0
        self.dp = dataproc_monitoring.DataProc(data['cluster'])
        self.scale_to = data['scale_to']
        self.scaling_direction = data['scaling_direction']
        self.containerpendingratio = data['containerpendingratio']
        self.cluster_name = self.cluster_settings.Cluster
        self.preemptible_pct = self.cluster_settings.PreemptiblePct
        self.MinInstances = self.cluster_settings.MinInstances
        self.MaxInstances = self.cluster_settings.MaxInstances
        self.UpContainerPendingRatio = self.cluster_settings.UpContainerPendingRatio
        if self.preemptible_pct != 100:
            self.preemptibles_to_workers_ratio = self.preemptible_pct / (
                100 - self.preemptible_pct)
        else:
            self.preemptibles_to_workers_ratio = -1

        try:
            self.cluster_status = self.dp.get_cluster_status()
            self.current_nodes = int(self.dp.get_number_of_nodes())
        except dataproc_monitoring.DataProcException as e:
            logging.error(e)
            raise e

    def calc_how_many(self):
        """
        calculate how  many new nodes of each type we need
        :return:
        """
        # No allocated memory so we don't need any workers above the
        # bare minimum
        if self.scale_to != -1:
            self.total = self.MinInstances
            logging.debug("No allocated memory lets go down! New workers {}"
                          " New preemptibel".format(self.total))
            return

        # no more memory lets get some lets at 4 nodes
        if self.dp.get_yarn_memory_available_percentage() == 0:
            add_more = NO_MORE_MEMORY_STEP
            logging.debug("no more memory lets get {}  nodes".format(add_more))

            self.total = self.current_nodes + add_more
            logging.debug("New workers {}  prev {} ".format(
                self.total, self.current_nodes))
            return

        self.calc_scale()

    def do_scale(self):
        """
        calculate and actually scale the cluster
        :return:
        """
        logging.debug("Starting do_scale  {}".format(self.total))
        self.calc_how_many()
        self.total = min(self.total, self.MaxInstances)
        logging.info("Scaling to workers {} ".format(self.total))

        if self.total == self.current_nodes:
            logging.debug("Not Modified")
            return 'Not Modified', 200

        # make sure that we have the correct ratio between 2 type of workers
        new_workers, new_preemptible = self.preserve_ratio()

        # do the scaling

        try:
            self.dp.patch_cluster(new_workers, new_preemptible)
        except dataproc_monitoring.DataProcException as e:
            logging.error(e)
            return 'Error', 500
        return 'ok', 204

    def calc_slope(self, minuets):
        """
        calculate the slope of available memory change
        :param: minuets how long to go back in time
        """

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
            logging.debug("Slope is {}".format(slope))
        except np.RankWarning:
            # not enough data so add remove by 2
            if self.scaling_direction == 'up':
                slope = 1
            else:
                slope = -1
            logging.debug("No Data slope is {}".format(slope))

        return slope

    def calc_scale(self):
        """
        How many nodes to add
        :param
        :return:
        """

        # pending containers are waiting....
        if self.containerpendingratio != -1:
            self.total = int(self.current_nodes + self.current_nodes *
                             (1 / self.preemptible_pct))
            logging.debug("New workers {}  prev {} ".format(
                self.total, self.current_nodes))
        else:
            sl = self.calc_slope(TIME_SERIES_HISTORY_IN_MINUTES)
            if sl != 0:
                slope = (1 / sl)
                logging.debug('Slope is {}'.format(slope))
                self.total = self.total + slope
                logging.debug("New workers {}  prev {} ".format(
                    self.total, self.current_nodes))
        logging.info("New workers {}  prev {} ".format(self.total,
                                                       self.current_nodes))

    def preserve_ratio(self):
        """
        Make sure that we have the correct ratio between the 2 types of workers
        """

        scale_ratio = (float(self.cluster_settings.PreemptiblePct) / 100.0)
        new_preemptible = int(scale_ratio * self.total)
        new_workers = int((1 - scale_ratio) * self.total)
        logging.debug("new_workers {} new_preemptible {}".format(
            new_workers, new_preemptible))
        # Make sure that we have the minimum normal workers
        if new_workers < self.MinInstances:
            logging.debug("Adjusting minimum as well {}".format(new_workers))
            diff = self.MinInstances - new_workers
            new_workers = self.MinInstances
            new_preemptible = new_preemptible - diff
        """ Make sure that we didn't fuck up and we have the requested number of 
            preemptible workers"""
        if self.total > new_workers + new_preemptible:
            logging.debug(
                "Adjusting number of preemptible workers to {}".format(
                    new_preemptible))
            diff = self.total - (new_workers + new_preemptible)
            new_preemptible = new_preemptible + diff

        logging.debug("After adjustment {} {} ".format(new_workers,
                                                       new_preemptible))
        return new_workers, new_preemptible
