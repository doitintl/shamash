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
        self.new_workers = 0
        self.new_preemptible = 0
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
            self.current_worker_nodes = int(self.dp.get_number_of_workers())
            self.current_preemptible_nodes = int(
                self.dp.get_number_of_preemptible_workers())
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
            self.new_workers = self.MinInstances
            self.new_preemptible = 0
            logging.debug(
                "No allocated memory lets go down! New workers {} New preemptibel {}".
                format(self.new_workers, self.new_preemptible))
            return

        # no more memory lets get some lets at 4 nodes
        if self.dp.get_yarn_memory_available_percentage() == 0:
            add_more = NO_MORE_MEMORY_STEP
            logging.debug("no more memory lets get {}  nodes".format(add_more))
            scale_ratio = (float(self.cluster_settings.PreemptiblePct) / 100.0)
            logging.debug("scale_ratio {}".format(scale_ratio))

            self.new_workers = int(
                round((
                    (1 - scale_ratio) * add_more) + self.current_worker_nodes))
            self.new_preemptible = int(
                round((
                    (scale_ratio * add_more) + self.current_preemptible_nodes)))

            if (self.new_preemptible + self.new_workers) > self.MaxInstances:
                self.calc_max_nodes_combination()
            logging.debug("New workers {} New preemptibel {} old {} {} ".format(
                self.new_workers, self.new_preemptible,
                self.current_worker_nodes, self.current_preemptible_nodes))
            return

        self.calc_scale()

    def do_scale(self):
        """
        calculate and actually scale the cluster
        :return:
        """
        logging.debug("Workers {} Preemptibel {}".format(
            self.current_worker_nodes, self.current_preemptible_nodes))
        self.calc_how_many()
        logging.info("Scaling to workers   {} preemptibel {} ".format(
            self.new_workers, self.new_preemptible))

        # check boundaries

        # make sure the we have the minimum number of workers
        self.new_workers = max(self.new_workers, self.MinInstances)

        # check upper boundary
        if (self.new_preemptible + self.new_workers) > self.MaxInstances:
            self.calc_max_nodes_combination()
        if (self.new_preemptible + self.new_workers) == self.current_nodes:
            logging.debug("Not Modified")
            return 'Not Modified', 200

        # make sure that we have at least on preemptible node
        if self.preemptible_pct != 0:
            if self.current_preemptible_nodes == 0:
                self.new_preemptible = 1
                self.new_workers = max(self.new_workers - 1, self.MinInstances)
                logging.debug("New workers {} New preemptibel {}".format(
                    self.new_workers, self.new_preemptible))
        logging.info("Updating cluster from {} to {} nodes".format(
            self.current_nodes, self.new_preemptible + self.new_workers))

        # make sure that we have the correct ratio between 2 type of workers
        self.preserve_ratio()

        # do the scaling
        try:
            self.dp.patch_cluster(self.new_workers, self.new_preemptible)
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

    def calc_max_nodes_combination(self):
        """

        MaxNodes - workers + preemptable =0
        preemptable-workers*Ratio=0
        preemptable-workers*Ratio =MaxNodes - workers + preemptable
        workers(Ratio+1) = MaxNodes
        workers = MaxNodes/(Ratio+1)
        """
        logging.debug("Calculating max combination")
        if self.preemptibles_to_workers_ratio != -1:
            new_workers = self.MaxInstances / (
                self.preemptibles_to_workers_ratio + 1)
            new_preemptibel = self.MaxInstances - new_workers
            logging.debug("New workers {} New preemptibel {}".format(
                self.new_workers, self.new_preemptible))
        else:
            new_workers = self.MinInstances
            new_preemptibel = self.MaxInstances - self.MinInstances
            logging.debug("New workers {} New preemptibel {}".format(
                self.new_workers, self.new_preemptible))
        self.new_workers = int(round(new_workers))
        self.new_preemptible = int(round(new_preemptibel))

    def calc_scale(self):
        """
        How many nodes to add
        :param
        :return:
        """

        # pending containers are waiting....
        if self.containerpendingratio != -1:
            if self.cluster_settings.PreemptiblePct > 0:
                self.current_preemptible_nodes = max(
                    self.current_preemptible_nodes, 1)
            self.new_workers = self.current_worker_nodes + (
                1 - self.preemptible_pct) * self.current_worker_nodes * (
                    1 / self.preemptible_pct)
            self.new_preemptible = self.current_preemptible_nodes + (
                self.preemptible_pct / 100) * self.current_preemptible_nodes * (
                    1 / self.containerpendingratio)
            logging.debug("New workers {} New preemptibel {}".format(
                self.new_workers, self.new_preemptible))
        else:
            sl = self.calc_slope(TIME_SERIES_HISTORY_IN_MINUTES)
            if sl != 0:
                slope = (1 / sl)
                logging.info('Slope is {}'.format(slope))
                if slope != 0:
                    self.new_workers = self.current_worker_nodes + slope * (
                        1 - (self.preemptible_pct / 100))
                    self.new_preemptible = self.current_preemptible_nodes + slope * (
                        self.preemptible_pct / 100)
                    logging.debug("New workers {} New preemptibel {}".format(
                        self.new_workers, self.new_preemptible))
        logging.info("Scaling to workers   {} preemptibel {} ".format(
            self.new_workers, self.new_preemptible))
        self.new_preemptible = int(self.new_preemptible)
        self.new_workers = int(self.new_workers)

    def preserve_ratio(self):
        """
        Make sure that we have the correct ratio between the 2 types of workers
        """
        logging.debug("Before adjustment {} {} ".format(self.new_workers,
                                                        self.new_preemptible))
        total_new = self.new_workers + self.new_preemptible
        scale_ratio = (float(self.cluster_settings.PreemptiblePct) / 100.0)
        self.new_preemptible = int(scale_ratio * total_new)
        self.new_workers = int((1 - scale_ratio) * total_new)

        # Make sure that we have the minimum normal workers
        if self.new_workers < self.MinInstances:
            logging.debug("Adjusting minimum as well {}".format(
                self.new_workers))
            diff = self.MinInstances - self.new_workers
            self.new_workers = self.MinInstances
            self.new_preemptible = self.new_preemptible - diff
        """ Make sure that we didn't fuck up and we have the requested number of 
            preemptible workers"""
        if total_new > self.new_workers + self.new_preemptible:
            logging.debug("Adjusting number of p workers from {}".format(self.new_preemptible))
            diff = total_new - (self.new_workers + self.new_preemptible)
            self.new_preemptible = self.new_preemptible + diff

        logging.debug("After adjustment {} {} ".format(self.new_workers,
                                                       self.new_preemptible))
