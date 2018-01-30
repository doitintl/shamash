"""Entry point for Shamash"""
import logging
import os

import flask_admin
from flask import Flask, request, redirect, send_from_directory
from google.appengine.api import taskqueue

from model import settings
from monitoring import dataproc_monitoring, metrics
from scaling import scaling, scaling_decisions
from util import utils, pubsub
from view.AdminCustomView import AdminCustomView

app = Flask(__name__)

# Create dummy secret key so we can use sessions
app.config['SECRET_KEY'] = '123456790'
app.config['FLASK_ADMIN_SWATCH'] = 'slate'


def create_app():
    """
    Do initialization
    """

    hostname = utils.get_host_name()
    admin = flask_admin.Admin(
        app, 'Admin', base_template='layout.html', template_mode='bootstrap3')

    admin.add_view(AdminCustomView(settings.Settings))
    logging.info("Starting {} on {}".format("Shamash", hostname))
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
                "https://{}/get_monitoring_data".format(hostname))
    pubsub.pull(client, 'scaling', "https://{}/scale".format(hostname))


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
        logging.info(e)
        return 'error', 500
    return scaler.do_scale()


@app.route('/tasks/check_load')
def check_load():
    """Entry point for cron task that launches a task for each cluster
    check cluster stats"""
    clusters = settings.get_all_clusters_settings()
    for cluster in clusters.iter():
        task = taskqueue.add(
            queue_name="shamash",
            url="/do_monitor",
            method='GET',
            params={
                'cluster_name': cluster.Cluster
            })
        logging.debug('Task {} enqueued, ETA {}.'.format(task.name, task.eta))

    return 'ok', 200


@app.route('/do_monitor', methods=['GET'])
def do_monitor():
    """
    called by task to do the actual check
    :return:
    """
    dp = dataproc_monitoring.DataProc(request.args.get('cluster_name'))
    return dp.check_load()


@app.route('/favicon.ico')
def favicon():
    return redirect('/static/favicon.ico', 302)

@app.errorhandler(500)
def server_error(e):
    """Log the error and stacktrace."""
    logging.exception('An error occurred during a request. {}'.format(e))
    return 'An internal error occurred.', 500


if __name__ == "__main__":
    app.run(debug=True)
