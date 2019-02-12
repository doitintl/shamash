"""Entry point for Shamash."""
import logging

import flask_admin
from flask import Flask, redirect, request
from google.appengine.api import taskqueue

from model import settings
from monitoring import dataproc_monitoring, metrics
from scaling import scaling, scaling_decisions
from util import pubsub, utils
from view.AdminCustomView import AdminCustomView


app = Flask(__name__)

# Create dummy secret key so we can use sessions
app.config['SECRET_KEY'] = '123456790'
app.config['FLASK_ADMIN_SWATCH'] = 'slate'

sh = logging.StreamHandler() # Log to stderr
sh.setLevel(logging.DEBUG)
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
sh.setFormatter(formatter)
logger = logging.getLogger(__name__)
logger.addHandler(sh)


def create_app():
    """
    Do initialization
    """

    hostname = utils.get_host_name()
    admin = flask_admin.Admin(
        app, 'Admin', base_template='layout.html', template_mode='bootstrap3')

    admin.add_view(AdminCustomView(settings.Settings))
    logger.info("Starting Shamash on %s", hostname)
    clusters = settings.get_all_clusters_settings()
    for cluster in clusters.iter():
        met = metrics.Metrics(cluster.Cluster)
        met.init_metrics()

    client = pubsub.get_pubsub_client()
    pubsub.create_topic(client, 'shamash-monitoring')
    pubsub.create_topic(client, 'shamash-scaling')
    pubsub.create_subscriptions(client, 'monitoring', 'shamash-monitoring')
    pubsub.create_subscriptions(client, 'scaling', 'shamash-scaling')
    pubsub.pull(client, 'monitoring',
                'https://shamash-dot-{}/get_monitoring_data'.format(hostname))
    pubsub.pull(client, 'scaling', "https://shamash-dot-{}/scale".format(hostname))


create_app()


@app.route('/')
def index():
    """
    Main Page
    :return:
    """
    return redirect('/admin', 302)


@app.route('/get_monitoring_data', methods=['POST'])
def get_monitoring_data():
    """
    After data is gathered from cluster into pub sub this function is invoked
    :return:
    """
    return scaling_decisions.should_scale(request.json['message']['data'])


@app.route('/scale', methods=['POST'])
def scale():
    """
    Called when decide  to scale is made
    :return:
    """
    try:
        scaler = scaling.Scale(request.json['message']['data'])
    except scaling.ScalingException as e:
        logger.info(e)
        return 'error', 500
    return scaler.do_scale()


@app.route('/tasks/check-load')
def check_load():
    """Entry point for cron task that launches a task for each cluster
    check cluster stats"""
    logger.debug("Retrieve all cluster settings.")
    clusters = settings.get_all_clusters_settings()
    for cluster in clusters.iter():
        logger.debug("Task queue for cluster: %s", cluster.Cluster)
        if cluster.Enabled:
            task = taskqueue.add(queue_name='vr-shamash',
                                 url="/monitors",
                                 method='GET',
                                 params={'cluster_name': cluster.Cluster})
            logger.debug('Task %s enqueued, ETA %s for cluster %s.',
                         task.name, task.eta, cluster.Cluster)
        else:
            logger.debug("Cluster %s is disabled.", cluster.Cluster)

    return 'ok', 200


@app.route('/monitors', methods=['GET'])
def monitors():
    """
    called by task to do the actual check
    :return:
    """
    dp = dataproc_monitoring.DataProc(request.args.get('cluster_name'))
    return dp.check_load()


@app.route('/patch', methods=['GET'])
def patch():
    """
    called by task to do the actual cluster update
    :return:
    """
    new_workers = int(request.args.get('new_workers'))
    new_preemptible = int(request.args.get('new_preemptible'))
    cluster_name = request.args.get('cluster_name')
    logger.debug('Task Starting for  %s', cluster_name)
    dp = dataproc_monitoring.DataProc(cluster_name)
    logger.debug('Patching new_workers %s new_preemptible %s', new_workers,
                 new_preemptible)
    try:
        logger.debug('Start Patching %s', cluster_name)
        dp.patch_cluster(new_workers, new_preemptible)
        logger.debug('Done Patching %s', cluster_name)
    except dataproc_monitoring.DataProcException as e:
        logger.error(e)
        return 'error', 500
    return 'ok', 200


@app.route('/favicon.ico')
def favicon():
    """
    Get the amazing favicon
    :return:
    """
    return redirect('/static/favicon.ico', 302)


@app.route('/version')
def version():
    """
    Get the amazing favicon
    :return:
    """
    return redirect('/static/version.html', 302)


@app.errorhandler(500)
def server_error(e):
    """Log the error and stacktrace."""
    logger.exception('An error occurred during a request. %s', e)
    return 'An internal error occurred.', 500


if __name__ == "__main__":
    app.run(debug=True)
