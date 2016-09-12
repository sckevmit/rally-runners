# coding=utf-8

# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from __future__ import print_function

import argparse
import collections
import json
import math

import matplotlib.pyplot as plt
import numpy as np
from scipy import stats


MAX_GAP = 6
WINDOW_SIZE = 21


Cluster = collections.namedtuple('Cluster', ['start', 'end'])
ErrorClusterStats = collections.namedtuple(
    'ErrorClusterStats', ['start', 'end', 'duration', 'variance', 'count'])
AnomalyClusterStats = collections.namedtuple(
    'AnomalyClusterStats',
    ['start', 'end', 'duration', 'variance', 'count', 'difference'])
RunResult = collections.namedtuple(
    'RunResult', ['errors', 'anomalies', 'etalon'])


def find_clusters(arr, filter_fn, max_gap=MAX_GAP):
    # filter_fn: y -> [0, 1]
    clusters = []  # [(start, end)]

    start = None
    end = None

    for i, y in enumerate(arr):
        v = filter_fn(y)
        if v:
            if not start:
                start = i
            end = i
        else:
            if end and i - end > max_gap:
                clusters.append(Cluster(start, end))
                start = end = None

    if end:
        clusters.append(Cluster(start, end))

    return clusters


def process_one_run(data):

    table = []
    etalon = []

    results = data['result']
    hooks = data['hooks']

    if not results or not hooks:
        return  # skip empty

    start = results[0]['timestamp']  # start of the run
    hook_start_time = hooks[0]['started_at'] - start  # when the hook started

    # convert Rally data into our table
    for idx, result in enumerate(results):
        timestamp = result['timestamp'] - start
        duration = result['duration']

        row = {
            'idx': idx,
            'timestamp': timestamp,
            'duration': duration,
            'error': bool(result['error']),
        }

        if timestamp + duration < hook_start_time:
            etalon.append(duration)

        table.append(row)

    etalon = np.array(etalon)
    etalon_mean = np.mean(etalon)
    etalon_median = np.median(etalon)
    etalon_mean_sem = stats.sem(etalon)
    etalon_p95 = np.percentile(etalon, 95)
    etalon_var = np.var(etalon)

    print('Hook time: %s' % hook_start_time)
    print('There are %s etalon samples' % len(etalon))
    print('Etalon mean: %s (±%s)' % (etalon_mean, etalon_mean_sem))
    print('Etalon median: %s' % etalon_median)
    print('Etalon 95%% percentile: %s' % etalon_p95)
    print('Variance: %s' % etalon_var)
    print('Normal test: %s' % str(stats.normaltest(etalon)))
    print('Bayes: %s' % str(stats.bayes_mvs(etalon, 0.95)))

    # find errors
    error_clusters = find_clusters(
        (p['error'] for p in table),
        filter_fn=lambda x: 1 if x else 0
    )
    print('Error clusters: %s' % error_clusters)

    error_stats = []

    for cluster in error_clusters:
        start_idx = cluster.start
        end_idx = cluster.end
        d_start = (table[start_idx]['timestamp'] -
                   table[start_idx - 1]['timestamp'])
        d_end = (table[end_idx + 1]['timestamp'] - table[end_idx]['timestamp'])
        start_ts = table[start_idx]['timestamp'] - d_start / 2
        end_ts = table[end_idx]['timestamp'] + d_end / 2
        var = (d_start + d_end) / 2
        dur = (end_ts - start_ts) / 2
        print('Error duration %s, variance: %s' % (dur, var))
        count = sum(1 if p['error'] else 0 for p in table)
        print('Count: %s' % count)

        error_stats.append(ErrorClusterStats(
            start=start_ts, end=end_ts, duration=dur, variance=var,
            count=count))

    # process non-error data
    table_filtered = [p for p in table if not p['error']]  # rm errors

    mean_idx = []
    mean_derivative_y = []
    mean_x = []
    mean_y = []

    for i in range(0, len(table_filtered) - WINDOW_SIZE):
        durations = [p['duration'] for p in table_filtered[i: i + WINDOW_SIZE]]

        mean = np.mean(durations)

        idx = table_filtered[i]['idx']  # current index of window start
        mean_idx.append(idx)
        mean_y.append(mean)
        mean_x.append(np.mean(
            [p['timestamp'] for p in table_filtered[i: i + WINDOW_SIZE]]))

        if len(mean_y) > 1:
            # calculate derivative
            mean_prev = mean_y[-2]
            loc = mean - mean_prev
            mean_derivative_y.append(loc)

    etalon_derivative_mean = np.mean(mean_derivative_y[:len(etalon)])
    etalon_derivative_s = np.std(mean_derivative_y[:len(etalon)])

    # find anomalies
    anomalies = find_clusters(
        mean_derivative_y,
        filter_fn=lambda y: 0 if abs(y) < abs(etalon_derivative_mean +
                                              5 * etalon_derivative_s) else 1
    )

    print('Anomalies: %s' % anomalies)
    anomaly_stats = []

    for cluster in anomalies:
        start_idx = mean_idx[cluster.start]  # back to original indexing
        end_idx = mean_idx[cluster.end]

        # it means that this item impacted the mean value and caused window
        # to be distinguished
        start_idx += WINDOW_SIZE - 1

        d_start = (table[start_idx]['timestamp'] -
                   table[start_idx - 1]['timestamp'])
        d_end = (table[end_idx + 1]['timestamp'] - table[end_idx]['timestamp'])
        start_ts = table[start_idx]['timestamp'] - d_start / 2
        end_ts = table[end_idx]['timestamp'] + d_end / 2
        var = (d_start + d_end) / 2
        dur = (end_ts - start_ts) / 2
        print('Anomaly duration %s, variance: %s' % (dur, var))

        length = end_idx - start_idx + 1
        print('Anomaly length: %s' % length)

        durations = [p['duration'] for p in table[start_idx: end_idx + 1]]
        anomaly_mean = np.mean(durations)
        anomaly_var = np.var(durations)
        se = math.sqrt(anomaly_var / length + etalon_var / len(etalon))
        dof = len(etalon) + length - 2
        mean_diff = anomaly_mean - etalon_mean
        conf_interval = stats.t.interval(0.95, dof, loc=mean_diff, scale=se)

        print('Mean diff: %s' % mean_diff)
        print('Conf int: %s' % str(conf_interval))

        anomaly_stats.append(AnomalyClusterStats(
            start=start_ts, end=end_ts, duration=dur, variance=var,
            difference=mean_diff, count=length
        ))

    # print stats
    print('Error clusters: %s' % error_stats)
    print('Anomaly clusters: %s' % anomaly_stats)

    # draw the plot
    x = [p['timestamp'] for p in table]
    y = [p['duration'] for p in table]

    x2 = [p['timestamp'] for p in table if p['error']]
    y2 = [p['duration'] for p in table if p['error']]

    plt.plot(x, y, 'b.', x2, y2, 'r.')

    # highlight etalon
    plt.axvspan(0, table[len(etalon)]['timestamp'],
                color='lime', alpha=0.1)

    # hook start
    # plt.axvline(hook_start_time, color='grey')

    # highlight errors
    for c in error_stats:
        plt.axvspan(c.start, c.end, color='red', alpha=0.2)

    # highlight anomalies
    for c in anomaly_stats:
        plt.axvspan(c.start, c.end, color='yellow', alpha=0.1)

    # draw mean
    plt.plot(mean_x, mean_y, 'cyan')

    plt.grid(True)
    plt.xlabel('time, s')
    plt.ylabel('duration, s')
    plt.savefig("test.svg")
    plt.show()


def main():
    parser = argparse.ArgumentParser(prog='rally-reliability-report')
    parser.add_argument('-i', '--input', dest='input', required=True,
                        help='Rally raw json output')
    parser.add_argument('-b', '--book', dest='book',
                        help='folder where to write RST book')
    args = parser.parse_args()

    file_name = args.input
    with open(file_name) as fd:
        data = json.loads(fd.read())

    for one_run in data:
        process_one_run(one_run)
        break


if __name__ == '__main__':
    main()
